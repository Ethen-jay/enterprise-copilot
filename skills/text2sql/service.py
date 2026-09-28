# -*- coding: utf-8 -*-
"""
main.py —— FastAPI 服务入口（阶段 D）

把阶段 B/C 的四个模块串成一条 HTTP 链路：

    用户问题
      → schema_linker  选表 + 组装 schema 上下文（关键词映射，确定性可解释）
      → llm_client     调大模型生成 1 条 SQL
      → sql_guard      只读守卫：只放行单条 SELECT/WITH，拒绝写操作
      → executor       只读 URI 执行，返回 columns/rows

接口：
  GET  /health  健康检查（模型配置/数据库/快照日）
  POST /ask     入参 {"question": "中文问题"}，返回 SQL + 结果表格
  GET  /        问答网页（app/static/index.html）

启动（在项目根目录）：
    python -m app.main
  或：
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8010
"""

import sqlite3
from typing import List, Optional

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .schema_linker import build_context, log_link
from .llm_client import generate_sql, LLMError
from .sql_guard import check_sql
from .executor import execute_select, ReadonlyExecError

# 路由抽象成 APIRouter：独立启动（本文件 app）与 gateway 统一入口（单端口 8000）复用同一份
router = APIRouter()


# ----------------------------------------------------------------------
# 请求/响应模型
# ----------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="用户的中文问题")


class AskResponse(BaseModel):
    question: str
    sql: str = ""                  # 最终执行（或被守卫拦下）的 SQL
    explanation: str = ""          # 模型给的一句口径说明
    columns: List[str] = []
    rows: List[list] = []
    row_count: int = 0
    truncated: bool = False
    picked_tables: List[str] = []  # Schema Linking 选中的表（网页侧可展示）
    few_shots: List[str] = []
    warnings: List[str] = []
    model: str = ""
    error: Optional[str] = None    # 非致命错误提示（无法回答/守卫拒绝/SQL执行失败）


# ----------------------------------------------------------------------
# 健康检查：页面加载前可用来判断配置是否齐全
# ----------------------------------------------------------------------
@router.get("/health")
def health():
    # 数据库可读性检查（只读 URI 连一下，不跑业务 SQL）
    db_ok = False
    table_count = 0
    try:
        uri = "file:%s?mode=ro" % config.DB_PATH.resolve().as_posix()
        con = sqlite3.connect(uri, uri=True)
        table_count = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        con.close()
        db_ok = table_count > 0
    except sqlite3.Error:
        db_ok = False

    # 模型配置只回传脱敏信息，绝不回传完整 key
    try:
        llm = config.get_llm_settings()
        llm_info = llm.masked()
        llm_ready = True
    except RuntimeError:
        llm_info = "未配置 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL"
        llm_ready = False

    return {
        "status": "ok" if db_ok and llm_ready else "degraded",
        "database": {"path": str(config.DB_PATH), "ok": db_ok, "tables": table_count},
        "llm": {"ready": llm_ready, "info": llm_info},
        "anchor_date": config.ANCHOR_DATE,
        "snapshot_date": config.SNAPSHOT_DATE,
    }


# ----------------------------------------------------------------------
# 主接口：自然语言 -> SQL -> 结果
# ----------------------------------------------------------------------
@router.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    question = req.question.strip()
    resp = AskResponse(question=question)

    # 1) Schema Linking（选表异常不致命：仍把全量表清单交给模型试一次）
    try:
        link = build_context(question)
        log_link(link)
        resp.picked_tables = link.table_names
        resp.few_shots = link.few_shots
        resp.warnings = link.warnings
        schema_context = link.context_text
    except Exception as e:  # 语义层文件缺失等，打日志后继续
        print("[SchemaLinking] 组装上下文失败：%s" % e)
        resp.warnings.append("Schema 上下文组装失败：%s" % e)
        schema_context = ""

    # 2) 调大模型生成 SQL
    try:
        llm_result = generate_sql(question, schema_context,
                                  snapshot_date=config.SNAPSHOT_DATE)
    except LLMError as e:
        resp.error = str(e)
        return resp
    resp.model = llm_result.model
    resp.explanation = llm_result.explanation

    # 模型主动声明无法回答
    if llm_result.refusal:
        resp.error = llm_result.refusal
        return resp

    resp.sql = llm_result.sql

    # 3) 只读守卫
    guarded = check_sql(llm_result.sql)
    if not guarded.ok:
        resp.error = "SQL 未通过只读守卫，已拒绝执行：%s" % guarded.reason
        return resp
    resp.sql = guarded.sql  # 可能被自动补过 LIMIT，以最终版回显

    # 4) 只读执行
    try:
        result = execute_select(guarded.sql)
    except ReadonlyExecError as e:
        resp.error = str(e)
        return resp

    resp.columns = result["columns"]
    resp.rows = result["rows"]
    resp.row_count = result["row_count"]
    resp.truncated = result["truncated"]

    print("[Ask] 问题=%s | 表=%s | 返回 %d 行"
          % (question, resp.picked_tables, resp.row_count))
    return resp


# ----------------------------------------------------------------------
# 独立启动形态（F1 保底，端口 8010）：单文件问答网页 + 本技能 router
# gateway 一体化模式下由 gateway/app.py 直接 include 上面的 router，不走这里
# ----------------------------------------------------------------------
app = FastAPI(title="IC载板 ERP+MES 自然语言数据查询", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
app.include_router(router)


@app.get("/")
def index():
    return FileResponse(str(config.STATIC_DIR / "index.html"))


# 直接 python -m app.main 启动时走这里（参数最小集，host/port 由 config 给）
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("skills.text2sql.service:app",
                host=config.SERVER_HOST, port=config.SERVER_PORT)
