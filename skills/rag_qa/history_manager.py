"""对话历史管理：超长时用 LLM 压缩为摘要，保留最近几轮原文。"""

from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from skills.rag_qa.config import HISTORY_MAX_MSGS, HISTORY_TURNS
from skills.rag_qa.llm_client import _get_llm

SUMMARY_PROMPT = """请将以下多轮对话历史压缩成一段简洁的摘要，保留关键信息（用户问了什么、助手回答了什么），用中文输出，不超过 200 字。

对话历史：
{history_text}

摘要："""


def _history_to_text(history: List[dict]) -> str:
    """将历史消息列表转为文本。"""
    lines = []
    for msg in history:
        role = "用户" if msg.get("role") == "user" else "助手"
        content = msg.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def compress_history(history: List[dict]) -> List[dict]:
    """异步压缩对话历史。

    当历史消息数超过 HISTORY_MAX_MSGS 时，将除最近 HISTORY_TURNS 轮外的
    早期对话用 LLM 压缩为摘要，作为一条 system 消息前置。

    参数:
        history: 原始历史，格式 [{"role": "user"|"assistant", "content": "..."}]

    返回:
        压缩后的历史列表。
    """
    if not history:
        return []

    if len(history) <= HISTORY_MAX_MSGS:
        # 未超阈值，直接截取最近几轮
        max_msgs = HISTORY_TURNS * 2
        return history[-max_msgs:]

    # 超过阈值：早期压缩 + 最近几轮原文
    recent_msgs = HISTORY_TURNS * 2
    recent = history[-recent_msgs:]
    older = history[:-recent_msgs]

    try:
        llm = _get_llm()
        history_text = _history_to_text(older)
        messages = [
            SystemMessage(content="你是一个对话摘要助手。"),
            HumanMessage(content=SUMMARY_PROMPT.format(history_text=history_text)),
        ]
        resp = await llm.ainvoke(messages)
        summary = resp.content
    except Exception:
        # LLM 调用失败时降级：只保留最近几轮
        return recent

    # 摘要作为 system 消息，放在历史最前面
    compressed = [{"role": "system", "content": f"[历史摘要] {summary}"}]
    compressed.extend(recent)
    return compressed
    