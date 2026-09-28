# -*- coding: utf-8 -*-
"""
common/llm_openai.py —— 原生 openai 客户端工厂（给 text2sql 用）

暴露 create_openai_client()，返回 openai.OpenAI 实例。
配置来源统一走 common.config.get_llm_settings()，保证切 key / 切 vLLM 只改一处。
"""

from openai import OpenAI

from .config import get_llm_settings


def create_openai_client() -> OpenAI:
    """创建原生 openai 客户端（base_url / api_key / timeout 来自 .env）。"""
    s = get_llm_settings()
    return OpenAI(
        base_url=s.base_url,
        api_key=s.api_key,
        timeout=s.timeout,
    )
