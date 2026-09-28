"""LLM 客户端：文本润色/摘要（DeepSeek）。"""

from langchain_core.messages import HumanMessage, SystemMessage

from common.llm_langchain import create_chat_openai


def _get_llm():
    """配置统一由 common.llm_langchain 工厂提供，temperature 沿用原 0.3。"""
    return create_chat_openai(temperature=0.3)


def polish_text(text: str) -> str:
    """润色文本，使其更通顺。"""
    llm = _get_llm()
    messages = [
        SystemMessage(content="你是一个专业的文本润色助手。请对用户提供的文本进行润色，使其更通顺、更符合中文表达习惯，但不要改变原意和结构。直接输出润色后的文本，不要加解释。"),
        HumanMessage(content=text),
    ]
    return llm.invoke(messages).content


def summarize_text(text: str) -> str:
    """生成文本摘要。"""
    llm = _get_llm()
    messages = [
        SystemMessage(content="你是一个专业的摘要助手。请对用户提供的文本生成简洁的摘要，保留关键信息，用中文输出。直接输出摘要，不要加解释。"),
        HumanMessage(content=text),
    ]
    return llm.invoke(messages).content
