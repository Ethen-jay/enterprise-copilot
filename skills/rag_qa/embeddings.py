"""Embedding：通过 OpenAI 兼容 API（SiliconFlow）调用 BAAI/bge-base-zh-v1.5。"""

from typing import List

import httpx
from langchain_core.embeddings import Embeddings

from skills.rag_qa.config import EMBED_API_KEY, EMBED_BASE_URL, EMBED_MODEL

# 每批最大文本数，避免请求体过大导致超时
BATCH_SIZE = 64
MAX_WORKERS = 8


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
        """单批 embedding 请求。"""
        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self.model, "input": texts}
        resp = self._client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]

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
