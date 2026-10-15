"""外部 API 指数退避重试：限流(429)/服务端临时错误(5xx)/网络异常自动重试。

覆盖 RAG 链路全部外部调用：embedding（SiliconFlow）、rerank（SiliconFlow）、
LLM 生成（DeepSeek，含历史压缩与反思校验的复用调用）。

策略：
- 可重试：HTTP 429（限流）、500/502/503/504（服务端临时错误）、
  网络异常（超时、连接失败，含 openai SDK 的 APIConnectionError/APITimeoutError）
- 不可重试：其余 4xx（参数/鉴权错误，重试无意义）→ 直接抛出
- 退避：指数退避 + 全抖动（full jitter）：
  delay = random(0, min(API_RETRY_MAX_DELAY, API_RETRY_BASE_DELAY * 2^attempt))
  全抖动可打散并发请求的重试节奏，避免限流下的同步惊群
"""

import asyncio
import logging
import random
import time
from typing import Callable, TypeVar

import httpx
import openai

from skills.rag_qa.config import API_MAX_RETRIES, API_RETRY_BASE_DELAY, API_RETRY_MAX_DELAY

logger = logging.getLogger("api_retry")

T = TypeVar("T")

# 可重试的 HTTP 状态码：429 限流 + 5xx 服务端临时错误
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def is_retryable(exc: Exception) -> bool:
    """判断异常是否可重试。

    httpx.HTTPStatusError 带 response.status_code；openai 的 APIStatusError
    （含 RateLimitError 429 / InternalServerError 5xx）带 status_code 属性；
    网络类异常（httpx.TransportError 含超时/连接失败、openai.APIConnectionError
    含 APITimeoutError）无状态码，按类型判断。
    """
    status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if status is not None:
        return status in RETRYABLE_STATUS
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, openai.APIConnectionError):  # APITimeoutError 是其子类
        return True
    return False


def backoff_delay(attempt: int) -> float:
    """第 attempt 次失败（0-based）后的等待秒数：指数退避 + 全抖动。"""
    cap = min(API_RETRY_MAX_DELAY, API_RETRY_BASE_DELAY * (2 ** attempt))
    return random.uniform(0, cap)


def _log_retry(op: str, attempt: int, delay: float, exc: Exception) -> None:
    logger.warning(
        "[%s] 第 %d/%d 次重试，%.1fs 后重试（原因: %s: %s）",
        op, attempt + 1, API_MAX_RETRIES, delay,
        type(exc).__name__, str(exc)[:120],
    )


def retry_sync(fn: Callable[[], T], op: str = "api") -> T:
    """同步执行 fn，可重试异常按指数退避重试，最多 API_MAX_RETRIES 次。

    参数:
        fn: 无参可调用，执行实际请求（抛出异常表示失败）。
        op: 操作名（用于日志定位，如 "embeddings(64)"）。

    异常:
        重试耗尽或异常不可重试时，抛出最后一次的原始异常。
    """
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= API_MAX_RETRIES or not is_retryable(exc):
                raise
            delay = backoff_delay(attempt)
            _log_retry(op, attempt, delay, exc)
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


async def retry_async(fn: Callable[[], object], op: str = "api"):
    """异步执行 fn，可重试异常按指数退避重试，最多 API_MAX_RETRIES 次。

    参数与异常语义同 retry_sync。
    """
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            return await fn()
        except Exception as exc:
            if attempt >= API_MAX_RETRIES or not is_retryable(exc):
                raise
            delay = backoff_delay(attempt)
            _log_retry(op, attempt, delay, exc)
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover
