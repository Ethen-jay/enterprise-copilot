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

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from common.auth import current_user
from common import chat_memory
from common.chat_memory import session_header
from . import config
from . import datasource, metric_dict, schema_scan, userdb
from .anchor import get_anchor_date, get_snapshot_date
from .schema_linker import build_context, log_link
from .llm_client import generate_sql, regenerate_sql, rewrite_question, LLMError
from .sql_guard import check_sql
from .executor import execute_select, ReadonlyExecError

logger = logging.getLogger("text2sql")

# 入参上限：问题/澄清补充过长会把 prompt 撑爆（延迟与费用不可控）
MAX_QUESTION_CHARS = 2000
MAX_CLARIFICATION_CHARS = 2000


# ----------------------------------------------------------------------
# SQL 报错脱敏：回喂模型前剥掉文件路径/系统信息，只保留错误类型与问题点
# 模型已有 schema 上下文，表名/列名在报错中出现不是信息泄漏；
# 需防的是 SQLite 内部路径、traceback 等非业务信息泄漏。
# ----------------------------------------------------------------------
_PATH_RE = re.compile(r'(file:|/[A-Za-z]:\\|/[A-Za-z0-9_./-]+/[A-Za-z0-9_.-]+\.(db|sqlite|sqlite3))', re.IGNORECASE)
_TRACEBACK_RE = re.compile(r'Traceback \(most recent call last\):[\s\S]*', re.IGNORECASE)
_LINE_RE = re.compile(r'line \d+,.*$', re.MULTILINE)


def sanitize_sql_error(err: str) -> str:
    """对 SQLite 执行错误做脱敏，保留可用于 SQL 修复的信息，剥掉系统内部信息。"""
    if not err:
        return "SQL 执行失败"
    s = str(err)
    s = _TRACEBACK_RE.sub("", s)       # 删 Python traceback
    s = _PATH_RE.sub("[internal_path]", s)  # 脱敏文件路径
    s = _LINE_RE.sub("", s)            # 删 "line N" 调试信息
    s = re.sub(r'\s+', ' ', s).strip()
    # 兜底：过长截断，防止异常信息过大占满 prompt
    return s[:300] if s else "SQL 执行失败"

# 路由抽象成 APIRouter：独立启动（本文件 app）与 gateway 统一入口（单端口 8000）复用同一份
router = APIRouter()


def _sse(event: str, data) -> str:
    """统一 SSE 帧格式：event 名 + 单行 JSON data + 空行结尾。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ----------------------------------------------------------------------
# 请求/响应模型
# ----------------------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="用户的中文问题")
    clarification: str = Field("", description="上一轮 CLARIFY 后用户补充的信息，首轮为空")


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
    clarify: Optional[str] = None  # 模型要求补充信息时的澄清问题（不执行 SQL）
    # 会话记忆：追问改写后的独立问句、本会话已记住多少轮、是否走记忆回答
    rewritten_question: str = ""
    used_memory: bool = False
    memory_turns: int = 0
    from_memory: bool = False


# ----------------------------------------------------------------------
# 会话记忆（Text2SQL）：记住问句、SQL、命中的表、结果摘要
# ----------------------------------------------------------------------
_SQL_META_RE = re.compile(r"(刚才|上一条|上一轮|上一次|上一个).{0,8}(sql|查询语句|语句)", re.IGNORECASE)
# 带这些动作词时，用户是在"改上一条 SQL"，不是想回显它；必须走改写而非元问题通道
_SQL_ACTION_RE = re.compile(
    r"(加|添加|去掉|删除|改|换|排序|升序|降序|分组|过滤|限制|只看|只要|再按|按|合并|拆分|top\s*\d+|前\s*\d+)",
    re.IGNORECASE,
)


def _is_sql_meta(question: str) -> bool:
    """只有纯粹"问上一条 SQL 是什么"才算元问题；带改动动作的走追问改写。"""
    return bool(_SQL_META_RE.search(question)) and not _SQL_ACTION_RE.search(question)


def _remember_sql_turn(user: str, session_id: Optional[str], resp: AskResponse) -> int:
    """把本轮问答写入会话记忆（元问题答复不再写回，避免污染"上一轮"）。"""
    if not session_id or resp.from_memory:
        return resp.memory_turns
    summary = resp.sql or resp.error or resp.clarify or "（无结果）"
    return chat_memory.append_turn(
        user, session_id, "sql", resp.question, summary,
        {"sql": resp.sql, "tables": resp.picked_tables,
         "row_count": resp.row_count, "error": resp.error or "",
         "clarify": resp.clarify or ""},
    )


def _resolve_sql_memory(question: str, user: str, session_id: Optional[str]):
    """返回 (记忆应答或 None, 改写后的问句, 轮次列表)。

    - "我刚才那条 SQL 是什么" → 直接基于会话记忆回答，不执行；
    - 指代型追问 → 用最近几轮改写成独立问句（失败回退原问题）；
    - 上一轮没有 SQL 又是纯指代 → 明确拒绝，不凭空编表。
    """
    turns = chat_memory.get_turns(user, session_id, "sql")

    # 元问题：问上一轮问了什么（无记忆也给出明确"本会话没有记录"，不交给模型瞎猜）
    if chat_memory.is_meta_question(question):
        text, _ = chat_memory.meta_answer(question, turns)
        return AskResponse(question=question, explanation=text,
                           from_memory=True, memory_turns=len(turns)), question, turns

    # 元问题：问上一条 SQL（带"加排序/换分组"等动作词的除外，那属于改动指令）
    if _is_sql_meta(question):
        last_sql = (turns[-1].get("extra") or {}).get("sql", "") if turns else ""
        if last_sql:
            return AskResponse(
                question=question, sql=last_sql,
                explanation="（来自对话记忆）上一轮生成的 SQL 已回显，未重新执行。",
                from_memory=True, memory_turns=len(turns),
            ), question, turns
        return AskResponse(
            question=question,
            explanation=("（来自对话记忆）当前会话还没有生成过 SQL，无法回显。"
                         if not turns else "（来自对话记忆）上一轮没有生成可用的 SQL，无法回显。"),
            from_memory=True, memory_turns=len(turns),
        ), question, turns

    if not turns:
        return None, question, turns

    if not chat_memory.needs_rewrite(question):
        return None, question, turns

    last_sql = (turns[-1].get("extra") or {}).get("sql", "")
    if not last_sql:
        return AskResponse(
            question=question,
            error="上一轮没有生成可用的 SQL，无法确定你说的「它/刚才那条」指的是什么；"
                  "请把要查询的对象、时间范围和条件说完整。",
            memory_turns=len(turns),
        ), question, turns

    rewritten = rewrite_question(question, turns, last_sql=last_sql)
    return None, rewritten, turns


def _validate_ask(req: AskRequest) -> None:
    """校验入参：问题非空且 ≤2000 字，澄清补充 ≤2000 字。"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    if len(req.question) > MAX_QUESTION_CHARS:
        raise HTTPException(
            status_code=400,
            detail="问题过长（%d 字），上限 %d 字，请精简后重试"
                   % (len(req.question), MAX_QUESTION_CHARS),
        )
    if len(req.clarification or "") > MAX_CLARIFICATION_CHARS:
        raise HTTPException(
            status_code=400,
            detail="补充信息过长（%d 字），上限 %d 字"
                   % (len(req.clarification), MAX_CLARIFICATION_CHARS),
        )


# ----------------------------------------------------------------------
# 健康检查：页面加载前可用来判断配置是否齐全
# ----------------------------------------------------------------------
@router.get("/health")
def health(probe: bool = False):
    """健康检查；probe=1 时额外用 2 秒超时探测 LLM 地址是否可达。

    默认不探测：避免每次健康检查都发外部请求；预检脚本用 ?probe=1，
    可发现“配置齐全但模型地址不可达”这种演示必翻车的情况。
    """
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

    llm_reachable = None
    if probe and llm_ready:
        try:
            import httpx

            resp = httpx.get(
                llm.base_url.rstrip("/") + "/models",
                headers={"Authorization": "Bearer " + llm.api_key},
                timeout=2.0,
            )
            llm_reachable = resp.status_code < 500
        except Exception:
            llm_reachable = False

    anchor = get_anchor_date(db_path=config.DB_PATH)
    healthy = db_ok and llm_ready and llm_reachable is not False
    return {
        "status": "ok" if healthy else "degraded",
        "database": {"path": str(config.DB_PATH), "ok": db_ok, "tables": table_count},
        "llm": {"ready": llm_ready, "reachable": llm_reachable, "info": llm_info},
        "anchor_date": anchor.date,
        "anchor_dynamic": anchor.dynamic,
        "anchor_source": anchor.source,
        "snapshot_date": get_snapshot_date(config.DB_PATH),
    }


# ----------------------------------------------------------------------
# 数据源：接入 / 切换 / 状态（状态条完全由这里驱动，不再写死）
# ----------------------------------------------------------------------
def _datasource_status(user: str, refresh: bool = False) -> dict:
    """返回当前用户数据源的真实状态：连接信息 + schema 扫描结果。"""
    info = userdb.describe(user)
    if not info["connected"]:
        return {
            "connected": False,
            "state": "not_connected",
            "message": "未接入数据库",
            "kind": None,
            "filename": "",
            "connected_at": "",
            "table_count": 0,
            "total_tables": 0,
            "excluded_count": 0,
            "tables": [],
            "excluded": [],
            "snapshot_date": "",
            "snapshot_source": "",
            "scan": {"cached": False, "duration_ms": 0, "scanned_at": ""},
        }
    path = userdb.resolve_path(user)
    if path is None:
        return {
            "connected": False,
            "state": "missing",
            "message": "数据源文件不存在，请重新接入",
            "kind": info.get("kind"),
            "filename": info.get("filename", ""),
            "connected_at": info.get("connected_at", ""),
            "table_count": 0,
            "total_tables": 0,
            "excluded_count": 0,
            "tables": [],
            "excluded": [],
            "snapshot_date": "",
            "snapshot_source": "",
            "scan": {"cached": False, "duration_ms": 0, "scanned_at": ""},
        }
    scan = schema_scan.scan(path, force=refresh)
    snapshot_date = scan["snapshot_date"]
    snapshot_source = scan["snapshot_source"]
    snapshot_note = scan["snapshot_note"]
    if info.get("kind") == userdb.KIND_DEMO:
        # 示例库用业务锚点（事实表的业务日期最大日），避免把交付承诺日 due_date
        # 这类未来日期当成“数据快照”；仍是实时计算，不是写死值。
        anchor = get_anchor_date(db_path=path)
        snapshot_date = get_snapshot_date(path)
        snapshot_source = anchor.source
        snapshot_note = "取业务事实表日期列的最大值（与问数锚点一致）"
    return {
        "connected": True,
        "state": "ok",
        "message": "已接入",
        "kind": info.get("kind"),
        "filename": info.get("filename", ""),
        "connected_at": info.get("connected_at", ""),
        "size_bytes": info.get("size_bytes", 0),
        "table_count": scan["included_count"],
        "total_tables": scan["total_tables"],
        "excluded_count": scan["excluded_count"],
        "tables": scan["tables"],
        "excluded": scan["excluded"],
        "snapshot_date": snapshot_date,
        "snapshot_source": snapshot_source,
        "snapshot_note": snapshot_note,
        "scan": {
            "cached": scan["cached"],
            "duration_ms": scan["duration_ms"],
            "scanned_at": scan["scanned_at"],
            "fingerprint": scan["fingerprint"],
        },
    }


@router.get("/datasource/status")
def datasource_status(refresh: bool = False, user: str = Depends(current_user)):
    """状态条接口：表数量、数据快照日期、纳入/排除表清单都来自真实扫描。"""
    return _datasource_status(user, refresh=refresh)


@router.post("/datasource/connect-demo")
def datasource_connect_demo(user: str = Depends(current_user)):
    """接入内置示例库（只读共享）。"""
    try:
        userdb.connect_demo(user)
    except userdb.DataSourceConnectError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e
    return _datasource_status(user, refresh=True)


@router.post("/datasource/sqlite")
async def datasource_connect_sqlite(
    file: UploadFile = File(...),
    user: str = Depends(current_user),
):
    """上传 SQLite（.db/.sqlite/.sqlite3）作为当前用户私有数据源。"""
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in {".db", ".sqlite", ".sqlite3"}:
        raise HTTPException(
            status_code=400,
            detail="不支持的数据库文件格式: %s，支持 .db/.sqlite/.sqlite3" % (ext or "未知"),
        )

    max_bytes = config.MAX_DATASOURCE_MB * 1024 * 1024
    upload_dir = config.DATA_DIR / "tmp" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="dsdb_", dir=str(upload_dir))
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="文件超过 %dMB 上限" % config.MAX_DATASOURCE_MB,
                    )
                out.write(chunk)
        try:
            userdb.connect_sqlite(user, Path(tmp_path), filename)
        except userdb.DataSourceConnectError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e)) from e
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
    return _datasource_status(user, refresh=True)


@router.delete("/datasource/connection")
def datasource_disconnect(user: str = Depends(current_user)):
    """断开当前数据源（不删除已上传文件）。"""
    userdb.disconnect(user)
    return _datasource_status(user)


# ----------------------------------------------------------------------
# 主接口：自然语言 -> SQL -> 结果
# ----------------------------------------------------------------------
@router.post("/ask", response_model=AskResponse)
def ask(
    req: AskRequest,
    user: str = Depends(current_user),
    session_id: Optional[str] = Depends(session_header),
):
    """自然语言 → SQL → 结果（JSON）。会话记忆只影响追问改写，不改变守卫与执行约束。"""
    _validate_ask(req)
    resp = _ask_pipeline(req, user=user, session_id=session_id)
    resp.memory_turns = _remember_sql_turn(user, session_id, resp)
    return resp


def _ask_pipeline(
    req: AskRequest,
    progress: Optional[Callable[[str, str], None]] = None,
    user: str = "",
    session_id: Optional[str] = None,
) -> AskResponse:
    """问答主链路入口：先解绑/绑定当前用户的数据源，再走实际管线。

    未接入数据库时直接返回明确提示，不打开任何默认库、不抛堆栈。
    """
    db_path = userdb.resolve_path(user)
    if db_path is None:
        return AskResponse(
            question=req.question.strip(),
            error="请先接入数据库：在「数据问答」页接入示例库或上传 SQLite 文件后再提问",
        )
    userdb.set_current(db_path)
    try:
        return _ask_pipeline_bound(req, progress, user, session_id, db_path)
    finally:
        userdb.clear_current()


def _ask_pipeline_bound(
    req: AskRequest,
    progress: Optional[Callable[[str, str], None]] = None,
    user: str = "",
    session_id: Optional[str] = None,
    db_path=None,
) -> AskResponse:
    """问答主链路（/ask 与 /ask/stream 共用）：选表 → 生成 SQL → 守卫 → 执行（失败自动修复 1 次）。

    progress(stage, message) 仅用于 SSE 状态提示，不参与任何业务判断。
    会话记忆：仅把追问改写成独立问句，再走原有链路；守卫/LIMIT/超时一律不变。
    db_path：本次请求绑定的数据源（调用方保证已接入）。
    """
    emit = progress or (lambda stage, message: None)
    question = req.question.strip()
    resp = AskResponse(question=question)

    # 0) 会话记忆：元问题直接回答；指代追问先改写（失败回退原问题）
    mem_resp, effective_question, turns = _resolve_sql_memory(question, user, session_id)
    if mem_resp is not None:
        return mem_resp

    # 对上一轮查询做增量修改（换排序 / 换 TopN / 只要前 N 条等）时，
    # 把上一轮 SQL 作为结构化基准追加到改写结果：不依赖改写模型主动复述，
    # 后续生成一定能拿到原表、筛选、聚合与排序，再叠加本次修改。
    previous_sql = ""
    if turns:
        _last_sql = (turns[-1].get("extra") or {}).get("sql", "")
        if _last_sql and chat_memory.is_sql_modification(question):
            previous_sql = str(_last_sql)
    if previous_sql:
        effective_question = (
            "%s\n（基于上一轮查询，原 SQL：%s；"
            "请在保留原表、筛选条件、聚合口径与排序的前提下，只执行本次修改）"
            % (effective_question, previous_sql)
        )

    if effective_question != question:
        resp.rewritten_question = effective_question
        resp.used_memory = True
        emit("rewriting", "已结合上文把追问改写为：「%s」" % effective_question)

    # 动态锚点：一次请求内取一次，schema 上下文与 system prompt 保持同一锚点
    emit("selecting", "正在选择数据表…")
    anchor_info = get_anchor_date(db_path=db_path)
    print("[Ask] 动态锚点：%s（%s）" % (anchor_info.date, anchor_info.source))

    # 1) Schema Linking（选表异常不致命：仍把全量表清单交给模型试一次）
    try:
        link = build_context(effective_question, anchor_date=anchor_info.date,
                             db_path=db_path)
        log_link(link)
        resp.picked_tables = link.table_names
        resp.few_shots = link.few_shots
        resp.warnings = link.warnings
        schema_context = link.context_text
    except Exception as e:  # 语义层文件缺失等，打日志后继续
        logger.exception("Schema 上下文组装失败")
        resp.warnings.append("Schema 上下文组装失败，已按全量表清单继续尝试（详情见服务日志）")
        schema_context = ""

    # 2) 调大模型生成 SQL（可能是多轮：携带用户对上一轮 CLARIFY 的补充）
    emit("generating", "已命中 %d 张表，正在生成 SQL…" % len(resp.picked_tables))
    try:
        llm_result = generate_sql(effective_question, schema_context,
                                  anchor_date=anchor_info.date,
                                  snapshot_date=get_snapshot_date(db_path),
                                  clarification=req.clarification.strip())
    except LLMError as e:
        emit("error", "生成 SQL 失败：%s" % e)
        resp.error = str(e)
        return resp
    resp.model = llm_result.model
    resp.explanation = llm_result.explanation

    # 模型主动声明无法回答
    if llm_result.refusal:
        emit("no_sql", "模型判断该问题无法用数据回答")
        resp.error = llm_result.refusal
        return resp

    # 模型要求用户补充信息：返回澄清问题，本轮不生成/不执行 SQL
    if llm_result.clarify:
        emit("clarify", "需要你补充信息后才能生成 SQL")
        resp.clarify = llm_result.clarify
        return resp

    resp.sql = llm_result.sql

    # 3) 只读守卫
    emit("guarding", "SQL 已生成，正在做只读守卫校验…")
    guarded = check_sql(llm_result.sql)
    if not guarded.ok:
        emit("guard_rejected", "只读守卫未通过：%s" % guarded.reason)
        resp.error = "SQL 未通过只读守卫，已拒绝执行：%s" % guarded.reason
        return resp
    resp.sql = guarded.sql  # 可能被自动补过 LIMIT，以最终版回显

    # 4) 只读执行；失败时把原 SQL + 报错回喂模型自动修正，再走一遍守卫+执行（最多 1 次）
    emit("executing", "守卫通过，正在执行查询…")
    try:
        result = execute_select(guarded.sql, db_path)
    except ReadonlyExecError as first_err:
        logger.warning("SQL 执行失败，自动修复重试：%s", first_err)
        emit("repairing", "执行失败，正在把报错回喂模型自动修复…")
        first_msg = sanitize_sql_error(str(first_err))
        try:
            fix = regenerate_sql(effective_question, schema_context, guarded.sql,
                                 sanitize_sql_error(str(first_err)),
                                 anchor_date=anchor_info.date,
                                 snapshot_date=get_snapshot_date(db_path))
        except LLMError as e:
            emit("error", "自动修复时调用模型失败：%s" % e)
            resp.error = "%s（自动修复时调用模型失败：%s）" % (first_msg, e)
            return resp
        # 模型判断问题本身答不了
        if fix.refusal:
            emit("no_sql", "模型判断该问题无法用数据回答")
            resp.error = "%s（自动修复：%s）" % (first_msg, fix.refusal)
            return resp
        # 修正版仍须过只读守卫，杜绝修复环节引入写操作
        emit("guarding", "修正版 SQL 已生成，正在重新做只读守卫校验…")
        g2 = check_sql(fix.sql)
        if not g2.ok:
            emit("guard_rejected", "修正版 SQL 未通过只读守卫：%s" % g2.reason)
            resp.error = "%s（修正版 SQL 未通过只读守卫：%s）" % (first_msg, g2.reason)
            return resp
        emit("executing", "修正版 SQL 守卫通过，正在重新执行查询…")
        try:
            result = execute_select(g2.sql, db_path)
        except ReadonlyExecError as e2:
            emit("error", "已自动修正重试仍失败：%s" % e2)
            resp.error = "%s；已自动修正重试仍失败：%s" % (first_msg, sanitize_sql_error(str(e2)))
            return resp
        # 修复成功：回显修正后的 SQL 与口径，并提示用户
        resp.sql = g2.sql
        if fix.explanation:
            resp.explanation = fix.explanation
        resp.warnings.append("首次生成的 SQL 执行失败，已把报错回喂模型自动修正后重试成功")

    resp.columns = result["columns"]
    resp.rows = result["rows"]
    resp.row_count = result["row_count"]
    resp.truncated = result["truncated"]
    emit("executed", "执行完成，返回 %d 行" % resp.row_count)

    print("[Ask] 问题=%s | 表=%s | 返回 %d 行"
          % (question, resp.picked_tables, resp.row_count))
    return resp


@router.post("/ask/stream")
async def ask_stream(
    req: AskRequest,
    user: str = Depends(current_user),
    session_id: Optional[str] = Depends(session_header),
):
    """SSE 状态进度流：选表中 → 生成 SQL → 守卫校验 → 执行中 → 执行完成，最后 final 返回完整 AskResponse。

    结果表格是结构化数据，整体在 final 一次性返回；/ask 的 JSON 行为保持不变。
    """
    _validate_ask(req)
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def progress(stage: str, message: str) -> None:
        # 管线在线程池中执行，入队需回到事件循环线程（客户端已断开时循环可能已关闭）
        try:
            loop.call_soon_threadsafe(queue.put_nowait, {"stage": stage, "message": message})
        except RuntimeError:
            pass

    async def event_generator():
        # 阻塞型管线放线程池，保证状态可实时推送
        task = loop.run_in_executor(
            None, lambda: _ask_pipeline(req, progress, user=user, session_id=session_id)
        )
        getter: Optional[asyncio.Task] = None
        try:
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield _sse("status", getter.result())
                else:
                    getter.cancel()
                if task in done:
                    break

            await asyncio.sleep(0)  # 让线程中已发出的最后一条状态入队
            while not queue.empty():
                yield _sse("status", queue.get_nowait())

            try:
                resp = task.result()
            except Exception:
                logger.exception("Text2SQL 流式问答异常")
                yield _sse("error", {"stage": "error", "error": "问答失败，请稍后重试（已记录日志）"})
            else:
                resp.memory_turns = _remember_sql_turn(user, session_id, resp)
                yield _sse("final", resp.model_dump())
        finally:
            if getter is not None and not getter.done():
                getter.cancel()
            if not task.done():
                task.cancel()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/session")
def sql_session_info(user: str = Depends(current_user),
                     session_id: Optional[str] = Depends(session_header)):
    """查询 Text2SQL 会话记忆概况（前端显示"已记住 N 轮"）。"""
    info = chat_memory.stats(user, session_id, "sql")
    turns = chat_memory.get_turns(user, session_id, "sql")
    if turns:
        info["last_question"] = turns[-1].get("question", "")
        info["last_sql"] = (turns[-1].get("extra") or {}).get("sql", "")
    return info


@router.delete("/session")
def sql_session_clear(user: str = Depends(current_user),
                      session_id: Optional[str] = Depends(session_header)):
    """清空 Text2SQL 会话记忆（清空后追问无法再改写，回到单轮行为）。"""
    cleared = chat_memory.clear(user, session_id, "sql")
    return {"ok": True, "cleared": cleared}


# ----------------------------------------------------------------------
# 上传数据源：xlsx/csv → 推断 schema → 建表 → 立即可问数
# 独立库 user_uploads.db，查询时由 executor 以只读 ATTACH 成 userdb；
# 守卫 / LIMIT / 超时 / 澄清 / 自动修复全部复用 /ask 原链路。
# ----------------------------------------------------------------------
_DATASOURCE_EXTS = {".xlsx", ".xlsm", ".csv"}


def _validate_datasource_ext(filename: str) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == ".xls":
        raise HTTPException(
            status_code=400,
            detail="旧版 .xls 不支持导入；请用 Excel 另存为 .xlsx 或 .csv 后重试",
        )
    if ext not in _DATASOURCE_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的数据文件格式: {ext}，支持 .xlsx/.xlsm/.csv",
        )
    return ext


@router.post("/datasource/upload")
async def datasource_upload(
    file: UploadFile = File(...),
    table_name: str = Form(""),
    replace: bool = Form(False),
    user: str = Depends(current_user),
):
    """上传 Excel/CSV 并建表；返回表名、列类型、行数与推断备注。"""
    filename = file.filename or ""
    ext = _validate_datasource_ext(filename)

    max_bytes = config.MAX_DATASOURCE_MB * 1024 * 1024
    upload_dir = config.DATA_DIR / "tmp" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="ds_", dir=str(upload_dir))
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {config.MAX_DATASOURCE_MB}MB 上限",
                    )
                out.write(chunk)
        try:
            # 用户接入自己的 SQLite 时，表格直接建到该私有副本里；否则进共享
            # 导入库（内置示例库模式下由 executor 以 userdb 只读 ATTACH）
            target_db = None
            try:
                if userdb.describe(user).get("kind") == userdb.KIND_SQLITE:
                    target_db = userdb.resolve_path(user)
            except Exception:  # noqa: BLE001 - 连接信息异常不影响共享库导入
                target_db = None
            result = datasource.import_file(
                Path(tmp_path), filename, table_name=table_name.strip() or None,
                replace=replace, db_path=target_db,
            )
            if target_db:
                schema_scan.invalidate(target_db)
        except datasource.DataSourceError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e)) from e
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass

    result["user"] = user
    return result


@router.get("/datasource")
def datasource_list(user: str = Depends(current_user)):
    """列出已导入的用户数据表（含列定义与行数）。"""
    return {"user": user, "tables": datasource.list_sources(),
            "db": str(config.USER_DB_PATH)}


@router.delete("/datasource/{name}")
def datasource_delete(name: str, user: str = Depends(current_user)):
    try:
        return datasource.delete_source(name)
    except datasource.DataSourceError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e)) from e


# ----------------------------------------------------------------------
# 指标口径字典：查看 + 从已入库文档人工确认后写入
# ----------------------------------------------------------------------
class MetricFromDocRequest(BaseModel):
    source: str = Field(..., min_length=1, description="已入库文档的原始文件名")
    snippet: str = Field(..., min_length=1, description="约定格式的 Markdown 指标片段")
    confirmed_by: str = Field("", description="确认人/评审记录，可空")


@router.get("/metric-dictionary")
def metric_dictionary(user: str = Depends(current_user)):
    """查看当前字典条目与注入状态（解析失败时 metrics 为空，不报错）。"""
    metrics = metric_dict.load_metrics()
    return {
        "path": str(config.METRIC_DICT_PATH),
        "count": len(metrics),
        "injected": bool(metric_dict.load_metric_section()),
        "metrics": metrics,
    }


@router.post("/metric-dictionary/from-doc")
def metric_dictionary_from_doc(
    req: MetricFromDocRequest,
    user: str = Depends(current_user),
):
    """把已入库文档里人工确认的口径写入字典。

    校验顺序：片段格式 → 文档归属（可选，RAG 不可用时自动跳过）→ 写 YAML。
    同名指标视为更新口径，不会重复注入。
    """
    metric = metric_dict.parse_markdown_snippet(
        req.snippet, source=req.source, confirmed_by=req.confirmed_by
    )
    # 归属校验：文档必须在当前用户知识库中；RAG 模块不可用时降级为不校验，
    # 不阻断人工维护 YAML 这条兜底路径。
    try:
        from skills.rag_qa.vector_store import get_store

        if not get_store(user).has_document(req.source):
            raise HTTPException(
                status_code=404,
                detail=f"文档 {req.source} 不在当前用户知识库中，请先入库或检查文件名",
            )
    except HTTPException:
        raise
    except Exception:
        logger.warning("RAG 文档归属校验不可用，跳过（不影响字典写入）", exc_info=True)

    try:
        metric_dict.append_metric(metric)
    except metric_dict.MetricDictError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "ok": True,
        "metric": metric,
        "count": len(metric_dict.load_metrics()),
        "path": str(config.METRIC_DICT_PATH),
    }


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
