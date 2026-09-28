"""RAG 引擎：编排 ingest 和 chat 全流程（含反思校验）。"""

import logging
from typing import Callable, List, Optional

from skills.rag_qa.config import RETRIEVE_CANDIDATES, TOP_K
from skills.rag_qa import document_loader, history_manager, llm_client, reranker, text_splitter
from skills.rag_qa.vector_store import VectorStore

logger = logging.getLogger("rag_engine")

# 按用户缓存的向量库（一个用户一个独立 collection，实现知识隔离）
_stores: dict[str, VectorStore] = {}

# 反思最大重试次数
RAG_MAX_RETRY = 2


def get_store(user: str = "default") -> VectorStore:
    """获取指定用户的向量库（首次访问时创建并缓存）。"""
    if user not in _stores:
        _stores[user] = VectorStore(user=user)
    return _stores[user]


def ingest(
    file_bytes: bytes,
    filename: str,
    progress_callback: Optional[Callable[[str], None]] = None,
    user: str = "default",
) -> dict:
    """入库流程：解析 → 分块 → embedding → 存入 Chroma。

    直接从内存字节解析，不写磁盘。

    参数:
        file_bytes: 文档的字节内容。
        filename: 原始文件名（用于引用来源和判断格式）。
        progress_callback: 进度回调函数，接收状态消息。

    返回:
        {"filename": str, "chunks": int}
    """
    cb = progress_callback or (lambda _: None)

    # 1. 解析纯文本
    cb("parsing")
    text = document_loader.extract_text(file_bytes, filename)

    # 2. 分块
    cb("splitting")
    chunks = text_splitter.split_text(text)

    # 3. 存入向量库（预计算 embedding + 直接写入该用户的 collection）
    cb(f"chunks:{len(chunks)}")
    store = get_store(user)
    count = store.add_chunks_fast(chunks, source=filename, progress_callback=cb)

    cb("done")
    return {"filename": filename, "chunks": count}


async def chat(question: str, history: Optional[List[dict]] = None, user: str = "default") -> dict:
    """问答流程：检索 → 重排 → 历史压缩 → 拼 Prompt → 异步调 LLM → 反思校验。

    参数:
        question: 用户问题。
        history: 对话历史。

    返回:
        {"answer": str, "citations": [{"file", "chunk", "content"}], "reflection": dict}
    """
    store = get_store(user)

    # 历史压缩（超长时压缩早期对话为摘要）
    trimmed_history = await history_manager.compress_history(history or [])

    # 反思重试循环
    retrieve_k = RETRIEVE_CANDIDATES
    used_adjustments: list[str] = []
    reflection_log: list[dict] = []

    for attempt in range(RAG_MAX_RETRY + 1):
        # 1. 检索候选
        candidates = store.similarity_search(question, k=retrieve_k)

        # 2. 重排，取 top_k
        docs = await reranker.rerank(question, candidates, top_k=TOP_K)

        # 3. 异步调 LLM
        answer = await llm_client.chat(question, docs, history=trimmed_history)

        # 4. 组装引用
        citations = []
        for doc in docs:
            meta = doc.metadata
            citations.append(
                {
                    "file": meta.get("source", "未知"),
                    "chunk": meta.get("chunk_index", -1),
                    "content": doc.page_content[:100],
                }
            )

        # 5. 反思校验（LLM 蕴含裁判）
        from skills.doc_agent.reflection import llm_entailment_check, CAUSE_TO_ADJUSTMENT

        reflection = await llm_entailment_check(question, answer, docs)

        log_entry = {
            "attempt": attempt,
            "passed": reflection["passed"],
            "cause": reflection.get("cause"),
            "adjustment": reflection.get("adjustment"),
            "issues": reflection.get("issues", []),
        }
        reflection_log.append(log_entry)

        logger.info(
            "RAG 反思 attempt=%d: passed=%s, cause=%s, issues=%s",
            attempt, reflection["passed"], reflection.get("cause"), reflection.get("issues"),
        )

        if reflection["passed"]:
            # 校验通过，返回结果
            return {
                "answer": answer,
                "citations": citations,
                "reflection": {"passed": True, "log": reflection_log},
            }

        # 不通过，检查是否可重试
        if attempt >= RAG_MAX_RETRY:
            logger.info("RAG 反思重试已用完 (%d/%d)", attempt, RAG_MAX_RETRY)
            break

        cause = reflection.get("cause")
        adjustment = reflection.get("adjustment")

        # 防空转：无 adjustment 或已用过 → 直接返回
        if not adjustment or adjustment in used_adjustments:
            logger.info(
                "RAG 反思无可用调整策略: cause=%s, adjustment=%s",
                cause, adjustment,
            )
            break

        used_adjustments.append(adjustment)

        # 应用调整策略
        if adjustment == "expand_retrieval":
            retrieve_k = RETRIEVE_CANDIDATES * 2
            logger.info("RAG 重试: 扩大检索范围 k=%d", retrieve_k)
        elif adjustment == "add_no_answer_constraint":
            # 加强约束：在 system prompt 中加"如果片段中没有就回答无法回答"
            # 已在 rag/llm_client.py 的 SYSTEM_PROMPT 中预设
            logger.info("RAG 重试: 加强 no_answer 约束")
        elif adjustment == "add_citation_instruction":
            logger.info("RAG 重试: 添加引用指令")
        else:
            logger.info("RAG 重试: 未知调整策略 %s，跳过", adjustment)
            break

    # 重试失败或不可重试，返回最终结果
    return {
        "answer": answer,
        "citations": citations,
        "reflection": {"passed": False, "log": reflection_log},
    }
