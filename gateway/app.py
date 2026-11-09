# -*- coding: utf-8 -*-
"""
gateway/app.py —— 一体化统一入口（F2）

把三个技能合并到**一个 FastAPI 应用、一个端口（8000）**：
  - Text-to-SQL：主技能，挂在根路径（/ask、/health）
  - RAG 知识问答：辅助，挂在 /rag/*（/rag/chat、/rag/ingest、/rag/health）
  - 文档转换 Agent：辅助，挂在 /doc/*（/doc/convert、/doc/download、/doc/health）

统一前端 web/index.html：SQL 取数为默认主页，RAG/文档转换为侧边页签。

启动：
    python -m uvicorn gateway.app:app --host 127.0.0.1 --port 8000
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.auth import (
    TOKEN_TTL_SECONDS,
    create_token,
    password_required,
    verify_credentials,
)
from gateway.middleware import GatewayMiddleware
from skills.text2sql.service import router as sql_router, health as sql_health
from skills.rag_qa.service import router as rag_router
from skills.doc_agent.service import router as doc_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 统一前端目录（monorepo 根 / web）
WEB_DIR = Path(__file__).resolve().parents[1] / "web"

app = FastAPI(title="一体化企业智能助手", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 网关企业化中间件：Trace ID + 审计日志 + 简单限流（加在 CORS 之后，位于最外层）
app.add_middleware(GatewayMiddleware)


# ----------------------------------------------------------------------
# 统一异常响应：所有错误一律返回 {code, detail, request_id} 的 JSON，
# 前端拿 request_id 即可去日志里定位当次请求。
# 路由内未捕获的异常由 GatewayMiddleware 兜底为同格式 500。
# ----------------------------------------------------------------------
def _rid(request: Request) -> str:
    """从请求级 state 取 Trace ID（由 GatewayMiddleware 写入）。"""
    return getattr(request.state, "request_id", "") or ""


@app.exception_handler(StarletteHTTPException)
async def http_exc_handler(request: Request, exc: StarletteHTTPException):
    """HTTPException（各技能 raise 的 4xx/5xx）统一格式。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "detail": exc.detail, "request_id": _rid(request)},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exc_handler(request: Request, exc: RequestValidationError):
    """请求体/参数校验失败（422）统一格式，最多列 5 条错误防刷屏。"""
    errors = [
        {"loc": ".".join(str(x) for x in e.get("loc", [])), "msg": e.get("msg", "")}
        for e in exc.errors()[:5]
    ]
    return JSONResponse(
        status_code=422,
        content={"code": 422, "detail": "请求参数校验失败", "errors": errors, "request_id": _rid(request)},
    )


# ----------------------------------------------------------------------
# 三大技能按前缀挂载：SQL 为主（根），RAG / 文档转换为辅助（子路径）
# ----------------------------------------------------------------------
app.include_router(sql_router)                     # /ask /health
app.include_router(rag_router, prefix="/rag")      # /rag/chat /rag/ingest /rag/health
app.include_router(doc_router, prefix="/doc")      # /doc/convert /doc/download /doc/health


# ----------------------------------------------------------------------
# 统一健康检查：聚合三技能子状态
# ----------------------------------------------------------------------
@app.get("/api/health", tags=["gateway"])
def api_health(probe: bool = False):
    sql = sql_health(probe=probe)
    return {
        "status": sql.get("status", "ok"),
        "gateway": "enterprise-copilot",
        "auth_enabled": True,  # 身份一律由登录签发的 JWT 派生
        "password_required": password_required(),
        # probe=1 时附带 LLM 连通性探测结果，避免“配置齐全但地址不可达”的假绿
        "llm": sql.get("llm"),
        "database": sql.get("database"),
        "skills": {
            "text2sql": "/ask",
            "rag_qa": "/rag/chat",
            "doc_agent": "/doc/convert",
        },
    }


# ----------------------------------------------------------------------
# 登录：校验账号 + 密码（AUTH_USERS；未配置时用共享 API Key 当密码），
# 成功后签发 JWT，后续请求一律凭令牌识别身份（服务端不再信任 X-User）。
# ----------------------------------------------------------------------
class LoginRequest(BaseModel):
    account: str
    password: str = ""
    api_key: str = ""  # 兼容旧前端：api_key 字段视同密码


@app.post("/login", tags=["gateway"])
def login(req: LoginRequest):
    account = verify_credentials(req.account, req.password or req.api_key)
    if not account:
        return {"ok": False, "error": "账号或密码错误（账号仅限字母/数字/下划线/连字符，1-32 位）"}
    return {
        "ok": True,
        "account": account,
        "token": create_token(account),
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "auth_enabled": True,
        "password_required": password_required(),
    }


# ----------------------------------------------------------------------
# 统一前端首页（SQL 为主，RAG / 文档转换为侧边页签）
# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    # 首页内嵌全部前端逻辑：禁止缓存，避免改版后浏览器仍跑旧脚本
    # （旧脚本可能不带 Authorization，表现为"显示已登录但请求 401"）
    return FileResponse(str(WEB_DIR / "index.html"), headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gateway.app:app", host="127.0.0.1", port=8000, reload=False)