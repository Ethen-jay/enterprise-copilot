# -*- coding: utf-8 -*-
"""
common/llm_langchain.py —— langchain ChatOpenAI 工厂（给 rag_qa / doc_agent 用）

暴露 create_chat_openai()，返回 langchain_openai.ChatOpenAI 实例。
各技能传入自己的 temperature（rag_qa=0.1, doc_agent=0.3），timeout 默认取 .env。
配置来源统一走 common.config，保证切 key / 切 vLLM 只改一处。
"""

from langchain_openai import ChatOpenAI

from .config import get_llm_settings


def create_chat_openai(temperature: float = 0.1, timeout: float | None = None) -> ChatOpenAI:
    """创建 langchain ChatOpenAI 客户端。

    参数:
        temperature: 采样温度，各技能按自己的行为传值。
        timeout:     超时秒数，None 时取 .env 的 LLM_TIMEOUT。
    """
    s = get_llm_settings()
    if not s.api_key or s.api_key.startswith("sk-your-"):
        raise ValueError("LLM_API_KEY 未配置，请在 .env 中填入有效密钥。")
    return ChatOpenAI(
        base_url=s.base_url,
        api_key=s.api_key,
        model=s.model,
        temperature=temperature,
        timeout=timeout if timeout is not None else s.timeout,
    )
