"""检索结果重排：用 bge-reranker-v2-m3 API 对候选片段重新排序。"""

from typing import List

import httpx
from langchain_core.documents import Document

from skills.rag_qa.api_retry import retry_async
from skills.rag_qa.config import RERANK_API_KEY, RERANK_BASE_URL, RERANK_MODEL, USE_RERANK


async def rerank(
    query: str,
    docs: List[Document],
    top_k: int,
) -> List[Document]:
    """异步对检索到的文档按相关性重排，返回 top_k 个。

    重排相关性分数写入每个返回文档的 metadata["rerank_score"]，
    供下游做阈值拒答判断（全低于 RELEVANCE_THRESHOLD 时跳过 LLM 直接拒答）。

    参数:
        query: 用户问题。
        docs: 候选文档列表。
        top_k: 返回前 k 个。

    返回:
        重排后的文档列表（按相关性降序，metadata 带 rerank_score）。
    """
    if not USE_RERANK or not docs:
        return docs[:top_k]

    try:
        url = f"{RERANK_BASE_URL.rstrip('/')}/rerank"
        headers = {
            "Authorization": f"Bearer {RERANK_API_KEY}",
            "Content-Type": "application/json",
        }
        documents = [doc.page_content for doc in docs]
        payload = {
            "model": RERANK_MODEL,
            "query": query,
            "documents": documents,
        }

        async def _do_request() -> dict:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, headers=headers, timeout=60.0)
            resp.raise_for_status()
            return resp.json()

        # 限流/5xx/网络异常先指数退避重试，重试耗尽才走下方降级
        data = await retry_async(_do_request, op="rerank")

        # 按 relevance_score 降序排序
        results = sorted(
            data.get("results", []),
            key=lambda x: x.get("relevance_score", 0),
            reverse=True,
        )

        # 取 top_k，分数写入 metadata
        reranked = []
        for item in results[:top_k]:
            idx = item.get("index")
            score = item.get("relevance_score", 0.0)
            if 0 <= idx < len(docs):
                doc = docs[idx]
                doc.metadata["rerank_score"] = round(float(score), 4)
                reranked.append(doc)
        return reranked if reranked else docs[:top_k]

    except Exception:
        # 重排失败时降级：返回原始前 top_k 个（无分数，跳过阈值拒答）
        return docs[:top_k]
