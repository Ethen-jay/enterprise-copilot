# -*- coding: utf-8 -*-
"""gateway/middleware_prod.py —— 生产级网关中间件（Redis 滑动窗口限流）

【定位】
  与 gateway/middleware.py 的 GatewayMiddleware 完全同构：Trace ID、认证、审计日志、
  异常兜底、错误出口全部继承自标准版，唯一差异是**限流后端**：
  标准版 = 单进程内存固定窗口；生产版 = Redis ZSET 滑动窗口（Lua 原子脚本）。

【启用方式】
  .env 设置 `GATEWAY_MIDDLEWARE=production`（默认 standard），重启即可切到本类。
  无 Redis 环境保持 standard，行为与原先完全一致，演示不受影响。

【依赖与前置条件】
  1. pip install "redis>=5.0.0"（requirements.txt 中为可选依赖，默认不装）
  2. Redis 服务可达：默认 redis://127.0.0.1:6379/0，可用 REDIS_URL 覆盖
     本地可用 docker run -d -p 6379:6379 redis:7-alpine

【降级策略】
  production 模式下若未安装 redis 包或连接失败：自动降级为内存限流，
  打一条 warning（只打一次，不按请求刷屏），服务照常启动并正常响应。
  实际生效的限流后端可通过 GET /api/health 的 middleware.rate_limit_backend 查看。

【为什么用 ZSET 滑动窗口而不是 INCR 固定窗口】
  固定窗口在窗口切换瞬间可能放过 2 倍流量（边界突刺），对写操作不合适。
  ZSET 按时间戳记录每次请求：ZREMRANGEBYSCORE 清窗口 → ZCARD 计数 → ZADD 记录，
  语义与原内存 deque 一致；Lua 保证原子执行且只需一次网络往返。
  member 用"时间戳:随机后缀"，避免同毫秒并发请求互相覆盖导致计数偏少。
"""

import importlib.util
import logging
import os
import time
import uuid

from gateway.middleware import GatewayMiddleware, _WRITE_METHODS, _client_ip, _rate_limited

logger = logging.getLogger("gateway.audit")

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

# 限流配置：60 秒窗口内同一 IP 最多 RATE_LIMIT_MAX 次写操作
RATE_LIMIT_WINDOW_MS = 60_000  # 毫秒，便于 Lua 内部比较
RATE_LIMIT_MAX = 60

# 滑动窗口 Lua 脚本：
#   1) ZREMRANGEBYSCORE 清窗口外旧记录
#   2) ZCARD 看当前窗口内计数
#   3) 满了返回 0；否则 ZADD 记一笔 + EXPIRE 防止冷 key 永驻
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

_redis_pool = None  # 延迟初始化：没装 redis 包时本模块仍可正常 import

# 运行状态：供 /api/health 展示实际生效的限流后端
_STATUS = {
    "rate_limit_backend": "redis",
    "degraded": False,
    "degrade_reason": "",
}


def status() -> dict:
    """当前限流后端状态（/api/health 用）。"""
    return dict(_STATUS)


def _degrade(reason: str) -> None:
    """降级为内存限流，只记录一次告警，避免每个请求刷日志。"""
    if _STATUS["degraded"]:
        return
    _STATUS.update(rate_limit_backend="memory", degraded=True, degrade_reason=reason)
    logger.warning("Redis 限流不可用，已自动降级为内存限流：%s", reason)


class GatewayMiddlewareProd(GatewayMiddleware):
    """生产版网关中间件：仅把限流后端换成 Redis 滑动窗口，其余行为与标准版一致。"""

    def __init__(self, app):
        super().__init__(app)
        if importlib.util.find_spec("redis") is None:
            _degrade("未安装 redis 包，安装后可启用 Redis 限流：pip install \"redis>=5.0.0\"")
        else:
            logger.info("Redis 限流已启用：%s（窗口 %d 秒，上限 %d 次/IP）",
                        REDIS_URL, RATE_LIMIT_WINDOW_MS // 1000, RATE_LIMIT_MAX)

    async def _check_rate_limit(self, scope: dict) -> bool:
        """限流检查：Redis 可用走滑动窗口；异常或已降级时回落内存限流。"""
        if _STATUS["degraded"]:
            return _rate_limited(scope)
        try:
            return await self._rate_limited_redis(scope)
        except Exception as e:
            _degrade(f"{type(e).__name__}: {e}")
            return _rate_limited(scope)

    async def _get_redis(self):
        """延迟导入 + 连接池单例。"""
        global _redis_pool
        if _redis_pool is None:
            import redis.asyncio as aioredis  # 延迟 import，避免可选依赖变成硬依赖

            _redis_pool = aioredis.from_url(REDIS_URL, decode_responses=False)
        return _redis_pool

    async def _rate_limited_redis(self, scope: dict) -> bool:
        """Redis 滑动窗口限流：返回 True 表示超限。

        与原内存版的对应关系：
          原: q = _hits[ip]; q.popleft() 丢弃窗口外; len(q) >= MAX ?
          新: ZSET key=rate:{ip}; ZREMRANGEBYSCORE 清窗口; ZCARD >= MAX ?
        """
        if scope.get("method") not in _WRITE_METHODS:
            return False
        ip = _client_ip(scope)
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}:{uuid.uuid4().hex[:8]}"
        key = f"rate:{ip}"
        r = await self._get_redis()
        allowed = await r.eval(
            _RATE_LUA, 1, key, now_ms, member, RATE_LIMIT_WINDOW_MS, RATE_LIMIT_MAX
        )
        return not int(allowed)
