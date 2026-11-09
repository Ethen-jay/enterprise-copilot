# -*- coding: utf-8 -*-
"""
gateway/app.py —— 一体化统一入口（F2）

把三个技能合并到**一个 FastAPI 应用、一个端口（8000）**：
  - Text-to-SQL：主技能，挂在根路径（/ask、/health）
  - RAG 知识问答：辅助，挂在 /rag/*（/rag/chat、/rag/ingest、/rag/health）
  - 文档转换 Agent：辅助，挂在 /doc/*（/doc/convert、/doc/download、/doc/health）

统一前端 web/index.html：只保留「数据问答」「知识问答」两个页签；文档转换
能力仍挂在 /doc/*，但仅作为内部链路与评测脚本使用，前端不再暴露单独入口。

启动：
    python -m uvicorn gateway.app:app --host 127.0.0.1 --port 8000
"""

import logging
import os
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from common import users
from common.auth import (
    TOKEN_TTL_SECONDS,
    create_token,
    current_user,
    password_required,
)
from common.upload_classify import classify_file
from common.users import UserError
from skills.text2sql.service import router as sql_router, health as sql_health
from skills.rag_qa.service import router as rag_router
from skills.doc_agent.service import router as doc_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# 账号库初始化 + 首次启动管理员引导（库为空时创建 admin，随机密码只在日志打印一次）
users.bootstrap_admin()

# 中间件模式开关：standard（默认，单进程内存限流）/ production（Redis 滑动窗口限流）
_MIDDLEWARE_MODE = os.getenv("GATEWAY_MIDDLEWARE", "standard").strip().lower()
if _MIDDLEWARE_MODE not in ("standard", "production"):
    logging.getLogger("gateway.audit").warning(
        "GATEWAY_MIDDLEWARE=%s 不是合法取值（standard|production），已回退 standard", _MIDDLEWARE_MODE
    )
    _MIDDLEWARE_MODE = "standard"

if _MIDDLEWARE_MODE == "production":
    from gateway.middleware_prod import GatewayMiddlewareProd as _Middleware
    from gateway.middleware_prod import status as _middleware_status
else:
    from gateway.middleware import GatewayMiddleware as _Middleware

    def _middleware_status() -> dict:
        return {"rate_limit_backend": "memory", "degraded": False}

# 统一前端目录（monorepo 根 / web）
WEB_DIR = Path(__file__).resolve().parents[1] / "web"

app = FastAPI(title="一体化企业智能助手", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 网关企业化中间件：Trace ID + 审计日志 + 认证 + 限流（加在 CORS 之后，位于最外层）
app.add_middleware(_Middleware)
logging.getLogger("gateway.audit").info(
    "middleware=%s", "production(redis)" if _MIDDLEWARE_MODE == "production" else "standard"
)


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
# 三大技能按前缀挂载：数据问答为主（根），RAG / 文档转换为辅助（子路径）
# ----------------------------------------------------------------------
app.include_router(sql_router)                     # /ask /health
app.include_router(rag_router, prefix="/rag")      # /rag/chat /rag/ingest /rag/health
app.include_router(doc_router, prefix="/doc")      # /doc/convert /doc/download /doc/health


# ----------------------------------------------------------------------
# 上传分流：按文件内容判别“结构化数据表 / 叙述性文档 / SQLite 数据库”
# 只返回判定与可解释依据，真正入库仍走各技能既有接口（前端确认后调用）。
# ----------------------------------------------------------------------
MAX_CLASSIFY_BYTES = 50 * 1024 * 1024


@app.post("/ingest/classify")
async def ingest_classify(file: UploadFile = File(...), user: str = Depends(current_user)):
    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()[:16]
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="ingest_", dir=tempfile.gettempdir())
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_CLASSIFY_BYTES:
                    raise HTTPException(status_code=413, detail="文件超过 50MB 上限")
                out.write(chunk)
        result = classify_file(Path(tmp_path), filename)
        result["size_bytes"] = total
        return result
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


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
        # 实际生效的中间件与限流后端（production 降级时会显示 memory + 原因）
        "middleware": {"mode": _MIDDLEWARE_MODE, **_middleware_status()},
        "skills": {
            "text2sql": "/ask",
            "rag_qa": "/rag/chat",
            "doc_agent": "/doc/convert",
        },
    }


# ----------------------------------------------------------------------
# 账号体系：注册 / 登录 / 当前用户 / 改密
# 身份一律由登录签发的 JWT 派生（服务端不信任客户端 X-User）。
# ----------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str
    password: str
    confirm_password: str = ""
    display_name: str = ""


class AuthLoginRequest(BaseModel):
    username: str = ""
    password: str = ""
    account: str = ""  # 兼容旧前端/脚本的 account 字段


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
    confirm_password: str = ""


class LoginRequest(BaseModel):
    """旧 /login 兼容入口（回归脚本仍在用）。"""
    account: str
    password: str = ""
    api_key: str = ""


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _public_user(user: dict) -> dict:
    return {
        "username": user["username"],
        "display_name": user.get("display_name", ""),
        "role": user.get("role", "user"),
        "created_at": user.get("created_at", ""),
    }


def _issue_token(user: dict) -> dict:
    return {
        "ok": True,
        "account": user["username"],
        "token": create_token(user["username"], user["token_version"]),
        "token_type": "Bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "user": _public_user(user),
        "auth_enabled": True,
        "password_required": True,
    }


@app.post("/auth/register", tags=["auth"])
def auth_register(req: RegisterRequest):
    if req.password != req.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    try:
        user = users.create_user(req.username, req.password, req.display_name)
    except UserError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    return {"ok": True, "user": _public_user(user)}


@app.post("/auth/login", tags=["auth"])
def auth_login(req: AuthLoginRequest, request: Request):
    username = (req.username or req.account).strip()
    if not username or not req.password:
        raise HTTPException(status_code=400, detail="请输入用户名和密码")
    try:
        user = users.authenticate(username, req.password, _client_ip(request))
    except UserError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    return _issue_token(user)


@app.post("/login", tags=["auth"])
def login(req: LoginRequest, request: Request):
    """兼容旧脚本的登录入口，语义与 /auth/login 一致（必须真实密码）。"""
    try:
        user = users.authenticate(
            (req.account or "").strip(), req.password or req.api_key, _client_ip(request)
        )
    except UserError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    return _issue_token(user)


@app.get("/auth/me", tags=["auth"])
def auth_me(request: Request, user: str = Depends(current_user)):
    profile = users.get_user(user)
    if not profile:
        raise HTTPException(status_code=401, detail="用户不存在或已删除，请重新登录")
    return {"ok": True, "user": _public_user(profile)}


@app.post("/auth/password", tags=["auth"])
def auth_change_password(req: ChangePasswordRequest, user: str = Depends(current_user)):
    if req.new_password != req.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
    try:
        users.change_password(user, req.old_password, req.new_password)
    except UserError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    # 改密自增 token_version：旧令牌立即失效，前端需重新登录
    return {"ok": True, "relogin_required": True, "detail": "密码已修改，请重新登录"}


# ----------------------------------------------------------------------
# 统一前端首页（数据问答 + 知识问答两个页签）
# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index():
    # 首页内嵌全部前端逻辑：禁止缓存，避免改版后浏览器仍跑旧脚本
    # （旧脚本可能不带 Authorization，表现为"显示已登录但请求 401"）
    return FileResponse(str(WEB_DIR / "index.html"), headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gateway.app:app", host="127.0.0.1", port=8000, reload=False)