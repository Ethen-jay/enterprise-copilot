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

import hmac
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from common.config import GATEWAY_API_KEYS
from gateway.middleware import GatewayMiddleware
from skills.text2sql.service import router as sql_router
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
def api_health():
    return {
        "status": "ok",
        "gateway": "enterprise-copilot",
        "auth_enabled": bool(GATEWAY_API_KEYS),
        "skills": {
            "text2sql": "/ask",
            "rag_qa": "/rag/chat",
            "doc_agent": "/doc/convert",
        },
    }


# ----------------------------------------------------------------------
# 登录：账号 + 密钥（可选）。认证启用时校验密钥，防身份冒用。
# ----------------------------------------------------------------------
class LoginRequest(BaseModel):
    account: str
    api_key: str = ""


@app.post("/login", tags=["gateway"])
def login(req: LoginRequest):
    account = (req.account or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", account):
        return {"ok": False, "error": "账号仅限字母、数字、下划线、连字符，1-32 位"}
    # 认证启用时，登录必须携带正确密钥，否则拒绝（防止他人输入同名冒充）
    if GATEWAY_API_KEYS:
        if not any(hmac.compare_digest(req.api_key or "", k) for k in GATEWAY_API_KEYS):
            return {"ok": False, "error": "密钥错误"}
    return {"ok": True, "account": account, "auth_enabled": bool(GATEWAY_API_KEYS)}


# ----------------------------------------------------------------------
# 统一前端首页（SQL 为主，RAG / 文档转换为侧边页签）
# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gateway.app:app", host="127.0.0.1", port=8000, reload=False)