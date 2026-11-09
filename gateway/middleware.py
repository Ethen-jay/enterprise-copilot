# -*- coding: utf-8 -*-
"""
gateway/middleware.py —— 网关企业化基础能力（F2）

用**纯 ASGI 中间件**实现（不缓冲响应体，对 SSE 流式响应 /rag/ingest/stream 安全），
一处挂载同时提供四件事：
  1. Trace ID：为每个请求生成/透传 X-Request-ID，响应原样带回，便于串起日志与排查；
  2. 审计日志：请求结束时打一条结构化 JSON（谁 / 何时 / 做了哪个动作 / 耗时 / 状态码 / 来源 IP）；
  3. 认证：非白名单路径必须携带登录签发的 Authorization: Bearer <JWT>，
     校验通过后把 claims.sub 写入 scope["state"]["user"]（业务层只认这个身份）；
     服务间调用可改用 X-API-Key，映射到固定服务账号，不能冒充任意用户；
     未认证一律 401，不存在"缺少身份头回退 default"；
  4. 简单限流：对写操作（POST/PUT/DELETE）按来源 IP 做固定窗口计数，超限返回 429。

说明：限流是内存级的最小实现，单进程、重启即清空；认证的签名密钥
来自 AUTH_JWT_SECRET（见 common/auth.py）。
"""

import hmac
import json
import logging
import time
import uuid
from collections import defaultdict, deque

from common.auth import SERVICE_ACCOUNT, decode_token
from common.config import GATEWAY_API_KEYS

logger = logging.getLogger("gateway.audit")

# 认证白名单：这些路径无需令牌（登录页 / 健康检查 / 接口文档 / 静态资源）
_PUBLIC_PATHS = {
    "/", "/health", "/api/health", "/rag/health", "/doc/health",
    "/docs", "/openapi.json", "/redoc", "/login",
}
_PUBLIC_PREFIXES = ("/static/", "/favicon")

# 限流配置：窗口 60 秒内，同一 IP 最多发起 RATE_LIMIT_MAX 次写操作
RATE_LIMIT_WINDOW = 60.0
RATE_LIMIT_MAX = 60
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_hits = defaultdict(deque)  # ip -> deque[时间戳]


def _client_ip(scope: dict) -> str:
    """取来源 IP（直连场景 scope 自带 client，反向代理下可取 X-Forwarded-For）。"""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            return value.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def _rate_limited(scope: dict) -> bool:
    """判断该请求是否触发限流；只在写操作上做计数。"""
    if scope.get("method") not in _WRITE_METHODS:
        return False
    ip = _client_ip(scope)
    now = time.time()
    q = _hits[ip]
    # 丢弃窗口外的旧记录
    while q and now - q[0] > RATE_LIMIT_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT_MAX:
        return True
    q.append(now)
    return False


def _is_public(scope: dict) -> bool:
    """白名单路径（登录/健康检查/文档/静态资源）与 CORS 预检无需令牌。"""
    if scope.get("method") == "OPTIONS":
        return True
    path = scope.get("path", "")
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _authenticate(scope: dict) -> tuple[str, str]:
    """校验凭据并派生身份，返回 (user, auth_type)。

    - Authorization: Bearer <JWT> → claims.sub（用户令牌）
    - X-API-Key（配置了 GATEWAY_API_KEYS 时）→ 固定服务账号（服务间调用）
    认证失败返回 ("", "")。客户端传来的 X-User 一律忽略。
    """
    token = ""
    api_key = ""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            v = value.decode("latin-1")
            if v.lower().startswith("bearer "):
                token = v[7:].strip()
        elif name == b"x-api-key":
            api_key = value.decode("latin-1")
    # 注意：不再把 Bearer 当作 API Key 兜底，两种凭据通道互相隔离

    if token:
        claims = decode_token(token)
        sub = str((claims or {}).get("sub", ""))
        if sub:
            return sub, "token"

    if api_key and any(hmac.compare_digest(api_key, k) for k in GATEWAY_API_KEYS):
        return SERVICE_ACCOUNT, "api_key"

    return "", ""


class GatewayMiddleware:
    """网关统一中间件：Trace ID + 审计日志 + 简单限流。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        request_id = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                request_id = value.decode("latin-1")
        if not request_id:
            request_id = uuid.uuid4().hex[:12]

        # 请求级状态：把 request_id 放进 scope["state"]，下游（异常处理器等）可读取回显
        scope.setdefault("state", {})["request_id"] = request_id

        # 认证：非白名单路径必须带合法令牌或服务 API Key，否则 401
        state = scope.setdefault("state", {})
        if not _is_public(scope):
            user, auth_type = _authenticate(scope)
            if not user:
                await self._send_401(send, request_id, scope)
                return
            state["user"] = user
            state["auth_type"] = auth_type
        else:
            state["user"] = ""
            state["auth_type"] = "public"

        # 写操作限流：命中直接回 429，不进入下游
        if _rate_limited(scope):
            await self._send_429(send, request_id, scope)
            return

        start = time.perf_counter()
        status_holder = {"status": 500}
        response_started = {"flag": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_started["flag"] = True
                status_holder["status"] = message["status"]
                # 把 trace id 注入响应头，供前端/客户端拿到
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # 兜底：路由内未被处理的异常统一回 JSON 500（带 request_id），
            # 避免把裸 traceback / 纯文本 500 泄漏给调用方
            logger.exception("未处理异常 request_id=%s %s %s", request_id, method, path)
            if response_started["flag"]:
                raise  # 响应已开始（如 SSE 流中途出错），无法改写状态码，交给服务器断开
            await self._send_500(send, request_id)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.info(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "request_id": request_id,
                "client_ip": _client_ip(scope),
                "user": state.get("user", ""),
                "auth_type": state.get("auth_type", ""),
                "method": method,
                "path": path,
                "status": status_holder["status"],
                "duration_ms": duration_ms,
            }, ensure_ascii=False))

    async def _send_401(self, send, request_id: str, scope: dict):
        body = json.dumps(
            {"code": 401, "detail": "未认证：请先登录获取访问令牌", "request_id": request_id},
            ensure_ascii=False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b"Bearer"),
                (b"x-request-id", request_id.encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
        logger.info(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "request_id": request_id,
            "client_ip": _client_ip(scope),
            "method": scope.get("method"),
            "path": scope.get("path"),
            "status": 401,
            "unauthorized": True,
        }, ensure_ascii=False))

    async def _send_429(self, send, request_id: str, scope: dict):
        body = json.dumps(
            {"code": 429, "detail": "请求过于频繁，请稍后再试", "request_id": request_id},
            ensure_ascii=False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-request-id", request_id.encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
        logger.info(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "request_id": request_id,
            "client_ip": _client_ip(scope),
            "method": scope.get("method"),
            "path": scope.get("path"),
            "status": 429,
            "rate_limited": True,
        }, ensure_ascii=False))

    async def _send_500(self, send, request_id: str):
        """兜底 500：与 401/429 同构的 JSON（code + detail + request_id）。"""
        body = json.dumps(
            {"code": 500, "detail": "服务器内部错误，请稍后重试", "request_id": request_id},
            ensure_ascii=False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-request-id", request_id.encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": body})