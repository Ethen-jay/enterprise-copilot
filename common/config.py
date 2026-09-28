# -*- coding: utf-8 -*-
"""
common/config.py —— 三技能共享配置的唯一来源（F1 纪律 #3）

所有 LLM 凭证 / base_url / 模型名 / 超时只从这里读。
改 key、切本地 vLLM 28080 只改这一处（.env），代码零改动。

设计原则（评审清单拍板）：
  - 不强行统一 SDK：原生 openai 与 langchain ChatOpenAI 并存，由两个工厂分别暴露；
  - 业务模块不直接 os.getenv，统一走 get_llm_settings()；
  - 每个技能可传自己的 temperature / timeout（行为零变更）。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# common/config.py 位于 common/，上一级即 monorepo 根
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class LLMSettings:
    """LLM 连接参数（脱敏后可打日志）。"""
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0
    temperature: float = 0.0

    def masked(self) -> str:
        key = self.api_key
        if len(key) > 8:
            key = key[:4] + "..." + key[-4:]
        else:
            key = "***"
        return "base_url=%s model=%s key=%s" % (self.base_url, self.model, key)


def get_llm_settings() -> LLMSettings:
    """从 .env 读取对话模型配置（text2sql / rag_qa / doc_agent 共用）。"""
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    for name, val in (("LLM_BASE_URL", base_url), ("LLM_API_KEY", api_key), ("LLM_MODEL", model)):
        if not val:
            raise RuntimeError("环境变量 %s 未配置，请检查项目根 .env" % name)
    return LLMSettings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=float(os.getenv("LLM_TIMEOUT", "60")),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
    )


# 网关认证（可选）：留空 = 不启用；支持逗号分隔多个密钥，业务接口需带 X-API-Key 或 Authorization: Bearer
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "").strip()
GATEWAY_API_KEYS = [k.strip() for k in GATEWAY_API_KEY.split(",") if k.strip()]
