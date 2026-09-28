"""LLM 客户端：通过 OpenAI 兼容 API（DeepSeek）调用生成模型。"""

from typing import List, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from common.llm_langchain import create_chat_openai

# 系统指令：只能依据给定片段回答，不足就明说，不得编造
SYSTEM_PROMPT = """你是一个严谨的知识问答助手。请严格遵守以下规则：

1. 只能依据下方【参考片段】中的内容回答用户问题。
2. 如果参考片段中没有相关信息，或者信息不足以回答，请明确回答"根据现有文档无法回答该问题"，不得编造。
3. 回答要简洁准确，使用中文。
4. 回答末尾用 [来源: 文件名#分块编号] 的格式标注引用来源。"""


def build_context_blocks(docs: list) -> str:
    """将检索到的文档格式化为上下文片段。"""
    blocks = []
    for doc in docs:
        meta = doc.metadata
        source = meta.get("source", "未知")
        chunk_idx = meta.get("chunk_index", "?")
        blocks.append(
            f"【片段 {source}#{chunk_idx}】\n{doc.page_content}"
        )
    return "\n\n".join(blocks)


def _get_llm():
    """创建并返回 LLM 实例（支持 ainvoke 异步调用）。

    配置统一由 common.llm_langchain 工厂提供，temperature 沿用原 0.1。
    """
    return create_chat_openai(temperature=0.1)


async def chat(
    question: str,
    context_docs: list,
    history: Optional[List[dict]] = None,
) -> str:
    """异步调用 LLM 生成回答。

    参数:
        question: 用户问题。
        context_docs: 检索到的相关文档列表。
        history: 对话历史，格式 [{"role": "user"|"assistant"|"system", "content": "..."}]。

    返回:
        模型生成的回答文本。
    """
    llm = _get_llm()

    # 构建消息
    messages: List[BaseMessage] = [SystemMessage(content=SYSTEM_PROMPT)]

    # 加入历史（最近几轮，可能包含 system 摘要）
    if history:
        for msg in history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))

    # 当前问题 + 参考片段
    context = build_context_blocks(context_docs)
    user_content = f"【参考片段】\n{context}\n\n【用户问题】\n{question}"
    messages.append(HumanMessage(content=user_content))

    resp = await llm.ainvoke(messages)
    return resp.content
