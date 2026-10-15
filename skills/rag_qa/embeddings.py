"""Embedding：通过 OpenAI 兼容 API（SiliconFlow）调用 BAAI/bge-large-zh-v1.5。

bge 官方推荐 cosine + 向量归一化：所有向量在出口统一做 L2 归一化（幂等，
即使 API 已返回单位向量也不受影响）。归一化后 L2 距离与 cosine 相似度
排序严格等价（||a-b||² = 2-2·cosθ），与 collection 的 hnsw:space 配置解耦。

外部 API 调用经 api_retry 指数退避重试（429/5xx/网络异常）。
"""

import math
from typing import List

import httpx
from langchain_core.embeddings import Embeddings

from skills.rag_qa.api_retry import retry_sync
from skills.rag_qa.config import EMBED_API_KEY, EMBED_BASE_URL, EMBED_MODEL

# 每批最大文本数，避免请求体过大导致超时
BATCH_SIZE = 64
MAX_WORKERS = 8


def _normalize(vec: List[float]) -> List[float]:
    """L2 归一化为单位向量；零范数向量原样返回（防除零）。"""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm < 1e-12:
        return vec
    return [x / norm for x in vec]


class APIEmbeddings(Embeddings):
    """基于 OpenAI 兼容 /embeddings 接口的 Embedding 实现。"""

    def __init__(
        self,
        base_url: str = EMBED_BASE_URL,
        api_key: str = EMBED_API_KEY,
        model: str = EMBED_MODEL,
    ):
        if not api_key or api_key.startswith("sk-your-"):
            raise ValueError(
                "EMBED_API_KEY 未配置，请在 .env 中填入 SiliconFlow API Key。"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.Client(timeout=120.0)

    def _post_batch(self, texts: List[str]) -> List[List[float]]:
        """单批 embedding 请求（限流/5xx/网络异常自动指数退避重试）。"""
        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "input": texts}

        def _do_request() -> dict:
            resp = self._client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

        data = retry_sync(_do_request, op=f"embeddings({len(texts)})")
        items = sorted(data["data"], key=lambda x: x["index"])
        # 出口统一 L2 归一化（幂等）：覆盖 embed_documents 与 embed_query 两条路径
        return [_normalize(item["embedding"]) for item in items]

    def _post(self, texts: List[str]) -> List[List[float]]:
        """分批并行调用 embedding API。"""
        if len(texts) <= BATCH_SIZE:
            return self._post_batch(texts)

        # 分批
        batches = [texts[i:i + BATCH_SIZE] for i in range(0, len(texts), BATCH_SIZE)]

        # 并行请求（线程池）
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results: dict[int, List[List[float]]] = {}

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(self._post_batch, batch): idx
                for idx, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()

        # 按批次顺序合并
        all_embeddings: List[List[float]] = []
        for idx in range(len(batches)):
            all_embeddings.extend(results[idx])

        return all_embeddings

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """批量向量化文档。"""
        return self._post(texts)

    def embed_query(self, text: str) -> List[float]:
        """向量化查询。"""
        return self._post_batch([text])[0]
