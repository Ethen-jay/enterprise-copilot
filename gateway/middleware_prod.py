# -*- coding: utf-8 -*-
"""
gateway/middleware_prod.py —— 生产级限流示范（Redis 滑动窗口）

【定位】这是**代码示范**，不接入主链路，仅用于面试展示"会写生产版限流"。
正式接入需：
  1. 安装 redis-py：`pip install redis>=5.0.0`
  2. .env 增加 `REDIS_URL=redis://127.0.0.1:6379/0`
  3. gateway/app.py 把 `GatewayMiddleware` 换成 `GatewayMiddlewareProd`
  4. 本地起 Redis 服务：`docker run -d -p 6379:6379 redis:7-alpine`

【相对原版 middleware.py 的差异】
  只替换 `_rate_limited` 一处，把内存字典 deque 换成 Redis ZSET 滑动窗口。
  其他三件事（Trace ID / 审计日志 / 异常兜底）保持原样不变。

【为什么选 ZSET 滑动窗口而不是 INCR 固定窗口】
  固定窗口在窗口切换瞬间可能放过 2 倍流量（边界突刺），
  对写操作（POST/PUT/DELETE）这种限制更该严格的场景不合适。
  滑动窗口算法思路与原版 deque 一脉相承——都是"按时间戳列表计数"，
  把单进程内存换成 Redis 后天然支持多进程 / 多机共享计数。

【降级策略】
  Redis 不可用时放行 + 报警日志，避免限流系统挂了把所有业务卡死。
  生产可进一步加本地二级限流兜底，避免 Redis 抖动期间完全失守。
"""

import hmac
import json
import logging
import os
import time
import uuid

from common.config import GATEWAY_API_KEYS

logger = logging.getLogger("gateway.audit")

# Redis 连接（从 .env 读 REDIS_URL；示范代码不在 import 期建池，
# 真用需在 app 启动期建立连接池：redis.asyncio.from_url(REDIS_URL)）
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
_redis_pool = None  # 延迟初始化，避免没装 redis 包时 import 报错

# 认证白名单：与原版一致
_PUBLIC_PATHS = {"/", "/health", "/api/health", "/docs", "/openapi.json", "/redoc", "/login"}

# 限流配置：60 秒窗口内同一 IP 最多 RATE_LIMIT_MAX 次写操作
RATE_LIMIT_WINDOW_MS = 60_000  # 毫秒，便于 Lua 内部比较
RATE_LIMIT_MAX = 60
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 滑动窗口 Lua 脚本（一次网络往返 + 原子执行）：
#   1) ZREMRANGEBYSCORE 清窗口外旧记录
#   2) ZCARD 看当前窗口内计数
#   3) 满了返回 0；否则 ZADD 记一笔 + EXPIRE 防止冷 key 永驻
#   ※ member 用"时间戳:随机数"避免同毫秒请求互相覆盖导致计数偏少
_RATE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local member = ARGV[2]
local window = tonumber(ARGV[3])
local max = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= max then
  return 0
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window / 1000)
return 1
"""


async def _get_redis():
    """延迟导入 + 连接池单例（没装 redis 包时不影响 import，只在调用时报错）。"""
    global _redis_pool
    if _redis_pool is None:
        import redis.asyncio as aioredis  # 延迟 import，没装包也能加载本模块
        _redis_pool = aioredis.from_url(REDIS_URL, decode_responses=False)
    return _redis_pool


async def _rate_limited_redis(scope: dict) -> bool:
    """Redis 滑动窗口限流：返回 True 表示超限。

    与原版 `_rate_limited` 的对应关系：
      原: q = _hits[ip]; q.popleft() 直到窗口内; len(q) >= MAX ?
      新: ZSET key=rate:{ip}; ZREMRANGEBYSCORE 清窗口; ZCARD >= MAX ?
    """
    if scope.get("method") not in _WRITE_METHODS:
        return False
    ip = _client_ip(scope)
    now_ms = int(time.time() * 1000)
    member = f"{now_ms}:{uuid.uuid4().hex[:8]}"
    key = f"rate:{ip}"
    try:
        r = await _get_redis()
        allowed = await r.eval(
            _RATE_LUA, 1, key, now_ms, member, RATE_LIMIT_WINDOW_MS, RATE_LIMIT_MAX
        )
        return not int(allowed)
    except Exception as e:
        # 降级：Redis 不可用时放行 + 报警，避免限流挂了把所有业务卡死
        logger.warning("Redis 限流不可用，降级放行：%s", e)
        return False


# ----------------------------------------------------------------------
# 以下沿用原版 middleware.py 的实现（保持一致）
# ----------------------------------------------------------------------
def _client_ip(scope: dict) -> str:
    """取来源 IP（直连场景 scope 自带 client；反代下取 X-Forwarded-For 首段）。"""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            return value.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


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
    return not any(hmac.compare_digest(provided, k) for k in GATEWAY_API_KEYS)


class GatewayMiddlewareProd:
    """生产级网关中间件（Redis 限流版）。

    与 GatewayMiddleware 唯一差异：限流走 Redis（多进程/多机共享计数）。
    其他三件事（Trace ID / 审计日志 / 异常兜底）一字不改。
    """

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
        scope.setdefault("state", {})["request_id"] = request_id

        # 认证
        if _unauthorized(scope):
            await self._send_401(send, request_id, scope)
            return

        # 写操作限流（唯一替换为 Redis 版的位置）
        if await _rate_limited_redis(scope):
            await self._send_429(send, request_id, scope)
            return

        start = time.perf_counter()
        status_holder = {"status": 500}
        response_started = {"flag": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_started["flag"] = True
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("未处理异常 request_id=%s %s %s", request_id, method, path)
            if response_started["flag"]:
                raise  # 响应已开始（如 SSE 流中途出错），无法改写状态码
            await self._send_500(send, request_id)
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

    # ------------------------------------------------------------------
    # 错误出口：与原版同构 {code, detail, request_id}
    # ------------------------------------------------------------------
    async def _send_401(self, send, request_id: str, scope: dict):
        body = json.dumps(
            {"code": 401, "detail": "未认证：请携带 X-API-Key 或 Authorization: Bearer", "request_id": request_id},
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

    async def _send_500(self, send, request_id: str):
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
