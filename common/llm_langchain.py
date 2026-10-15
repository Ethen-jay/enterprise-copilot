# -*- coding: utf-8 -*-
"""
common/llm_langchain.py —— langchain ChatOpenAI 工厂（给 rag_qa / doc_agent 用）

暴露 create_chat_openai()，返回 langchain_openai.ChatOpenAI 实例（带云模型降级）。
各技能传入自己的 temperature（rag_qa=0.1, doc_agent=0.3），timeout 默认取 .env。
配置来源统一走 common.config，保证切 key / 切 vLLM 只改一处。

降级策略（G23）：本地 vLLM 挂掉时，自动回退到 FALLBACK_LLM_* 配置的公网 API。
降级仅在配置了 FALLBACK_LLM_BASE_URL 时启用，透明生效，调用方无需改动。
"""

import logging
from typing import List, Optional

from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI

from .config import get_llm_settings, get_fallback_llm_settings

logger = logging.getLogger("llm_langchain")


class FallbackChatOpenAI:
    """带降级的 ChatOpenAI 包装：主模型调用失败时自动用降级模型重试。

    用法与 ChatOpenAI 一致：await wrapper.ainvoke(messages)。
    降级模型未配置时退化为直接调用主模型（无降级）。
    """

    def __init__(self, primary: ChatOpenAI, fallback: Optional[ChatOpenAI] = None):
        self._primary = primary
        self._fallback = fallback

    async def ainvoke(self, messages: List[BaseMessage], **kwargs):
        try:
            return await self._primary.ainvoke(messages, **kwargs)
        except Exception as e:
            if self._fallback is None:
                raise
            logger.warning(
                "主模型调用失败，降级到备选模型: %s", str(e)[:200]
            )
            return await self._fallback.ainvoke(messages, **kwargs)

    # 兼容 ChatOpenAI 的同步调用（部分场景可能用到）
    def invoke(self, messages, **kwargs):
        try:
            return self._primary.invoke(messages, **kwargs)
        except Exception as e:
            if self._fallback is None:
                raise
            logger.warning("主模型调用失败，降级到备选模型: %s", str(e)[:200])
            return self._fallback.invoke(messages, **kwargs)

    # 透传属性访问（如 .model_name 等）
    def __getattr__(self, name):
        return getattr(self._primary, name)


def create_chat_openai(
    temperature: float = 0.1,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> "FallbackChatOpenAI | ChatOpenAI":
    """创建 langchain ChatOpenAI 客户端（带云模型降级）。

    参数:
        temperature: 采样温度，各技能按自己的行为传值。
        timeout:     超时秒数，None 时取 .env 的 LLM_TIMEOUT。
        max_retries: openai SDK 内置重试次数；None = SDK 默认（2）。
                     技能自建指数退避重试（如 rag_qa 的 api_retry）时应传 0，
                     禁用 SDK 内置重试避免双层重试叠加放大等待时间。
    """
    s = get_llm_settings()
    if not s.api_key or s.api_key.startswith("sk-your-"):
        raise ValueError("LLM_API_KEY 未配置，请在 .env 中填入有效密钥。")

    primary = ChatOpenAI(
        base_url=s.base_url,
        api_key=s.api_key,
        model=s.model,
        temperature=temperature,
        timeout=timeout if timeout is not None else s.timeout,
        max_retries=max_retries,
    )

    # 降级模型（可选，未配置则不启用降级）
    fallback = None
    fb = get_fallback_llm_settings()
    if fb is not None:
        fallback = ChatOpenAI(
            base_url=fb.base_url,
            api_key=fb.api_key,
            model=fb.model,
            temperature=temperature,
            timeout=timeout if timeout is not None else fb.timeout,
            max_retries=max_retries,
        )
        logger.info("LLM 降级已启用: 主=%s → 备=%s", s.model, fb.model)

    if fallback is not None:
        return FallbackChatOpenAI(primary, fallback)
    return primary
