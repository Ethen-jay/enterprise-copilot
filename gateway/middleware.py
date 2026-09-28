# -*- coding: utf-8 -*-
"""
gateway/middleware.py —— 网关企业化基础能力（F2）

用**纯 ASGI 中间件**实现（不缓冲响应体，对 SSE 流式响应 /rag/ingest/stream 安全），
一处挂载同时提供四件事：
  1. Trace ID：为每个请求生成/透传 X-Request-ID，响应原样带回，便于串起日志与排查；
  2. 审计日志：请求结束时打一条结构化 JSON（谁 / 何时 / 做了哪个动作 / 耗时 / 状态码 / 来源 IP）；
  3. 认证（可选）：配置 GATEWAY_API_KEY（逗号分隔支持多 key）后，非白名单路径须携带
     X-API-Key 或 Authorization: Bearer（时序安全比较），否则 401；留空则完全跳过认证；
  4. 简单限流：对写操作（POST/PUT/DELETE）按来源 IP 做固定窗口计数，超限返回 429。

说明：这里的限流与认证都是内存级的最小实现，单进程、重启即清空，
仅用于 PoC 演示“网关承担限流/认证”的能力，生产需换 Redis / 网关组件 + 正式鉴权体系。
"""

import hmac
import json
import logging
import time
import uuid
from collections import defaultdict, deque

from common.config import GATEWAY_API_KEYS

logger = logging.getLogger("gateway.audit")

# 认证白名单：这些路径无需 API Key（登录页 / 健康检查 / 接口文档）
_PUBLIC_PATHS = {"/", "/health", "/api/health", "/docs", "/openapi.json", "/redoc", "/login"}

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


def _extract_api_key(scope: dict) -> str:
    """从 X-API-Key 或 Authorization: Bearer 提取密钥；无则返回空串。"""
    for name, value in scope.get("headers", []):
        if name == b"x-api-key":
            return value.decode("latin-1")
        if name == b"authorization":
            v = value.decode("latin-1")
            if v.lower().startswith("bearer "):
                return v[7:].strip()
    return ""


def _unauthorized(scope: dict) -> bool:
    """认证检查：未启用时放行；启用后非白名单路径须携带正确密钥（多 key，时序安全比较）。"""
    if not GATEWAY_API_KEYS:
        return False
    if scope.get("path", "") in _PUBLIC_PATHS:
        return False
    provided = _extract_api_key(scope)
    if not provided:
        return True  # 缺少密钥
    return not any(hmac.compare_digest(provided, valid) for valid in GATEWAY_API_KEYS)


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

        # 认证：未配置 GATEWAY_API_KEY 时恒放行；配置后非白名单路径须带 X-API-Key
        if _unauthorized(scope):
            await self._send_401(send, request_id, scope)
            return

        # 写操作限流：命中直接回 429，不进入下游
        if _rate_limited(scope):
            await self._send_429(send, request_id, scope)
            return

        start = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                # 把 trace id 注入响应头，供前端/客户端拿到
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.info(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "request_id": request_id,
                "client_ip": _client_ip(scope),
                "method": method,
                "path": path,
                "status": status_holder["status"],
                "duration_ms": duration_ms,
            }, ensure_ascii=False))

    async def _send_401(self, send, request_id: str, scope: dict):
        body = json.dumps(
            {"detail": "未认证：请携带 X-API-Key 或 Authorization: Bearer", "request_id": request_id},
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
            {"detail": "请求过于频繁，请稍后再试", "request_id": request_id},
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