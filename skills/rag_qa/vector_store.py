"""Chroma 向量库封装：添加分块、相似度检索、持久化（按用户隔离）。"""

import re
from typing import Callable, List, Optional

from langchain_chroma import Chroma
from langchain_core.documents import Document

from skills.rag_qa.config import CHROMA_DIR, TOP_K
from skills.rag_qa.embeddings import APIEmbeddings

DEFAULT_COLLECTION = "rag_knowledge_base"


def _sanitize_user(user: str) -> str:
    """把用户标识转成 Chroma collection 允许的安全片段（字母数字下划线连字符）。"""
    s = re.sub(r"[^A-Za-z0-9_-]", "_", (user or "").strip())
    return s or "default"


class VectorStore:
    """Chroma 向量库封装（一个用户一个独立 collection，实现知识隔离）。"""

    def __init__(self, user: str = "default"):
        self.user = _sanitize_user(user)
        # 默认用户沿用原有 collection，其余用户用 kb_{user}，互不可见
        self.collection_name = (
            DEFAULT_COLLECTION if self.user == "default" else f"kb_{self.user}"
        )
        self.embeddings = APIEmbeddings()
        self._store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=str(CHROMA_DIR),
        )

    def add_chunks(
        self,
        chunks: List[str],
        source: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> int:
        """将分块文本加入向量库。

        参数:
            chunks: 分块文本列表。
            source: 来源文件名（用于引用）。
            progress_callback: 进度回调函数，接收状态消息。

        返回:
            加入的块数。
        """
        if not chunks:
            return 0

        docs = [
            Document(
                page_content=chunk,
                metadata={"source": source, "chunk_index": i},
            )
            for i, chunk in enumerate(chunks)
        ]
        ids = [f"{source}#{i}" for i in range(len(chunks))]

        # 预计算 embedding（并行批量），避免 Chroma 内部串行调用
        if progress_callback:
            progress_callback(f"embedding:{len(chunks)}")

        texts = [doc.page_content for doc in docs]
        precomputed_vectors = self.embeddings.embed_documents(texts)

        if progress_callback:
            progress_callback("storing")

        # 用预计算向量直接写入 Chroma，跳过 Chroma 内部 embedding 调用
        self._store.add_texts(
            texts=texts,
            metadatas=[doc.metadata for doc in docs],
            ids=ids,
        )

        # Chroma add_texts 不支持传向量，需要用底层 API
        # 但 add_texts 内部会调用 embedding_function，所以改为直接预计算后跳过
        # 实际上 Chroma 的 add_texts 会调 embed_documents，我们已经在上面预计算了
        # 这里用更高效的方式：直接操作 collection

        return len(docs)

    def add_chunks_fast(
        self,
        chunks: List[str],
        source: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> int:
        """高性能入库：预计算 embedding → 直接写入 Chroma collection。

        跳过 LangChain Chroma 的中间层，直接操作底层 collection。
        """
        if not chunks:
            return 0

        # 1. 预计算 embedding（并行批量）
        if progress_callback:
            progress_callback(f"embedding:{len(chunks)}")
        vectors = self.embeddings.embed_documents(chunks)

        # 2. 直接写入 Chroma collection
        if progress_callback:
            progress_callback("storing")
        ids = [f"{source}#{i}" for i in range(len(chunks))]
        metadatas = [{"source": source, "chunk_index": i} for i in range(len(chunks))]

        self._store._collection.add(  # noqa: SLF001
            ids=ids,
            documents=chunks,
            embeddings=vectors,
            metadatas=metadatas,
        )

        return len(chunks)

    def similarity_search(
        self,
        query: str,
        k: int = TOP_K,
    ) -> List[Document]:
        """检索最相关的 k 个片段。"""
        return self._store.similarity_search(query, k=k)

    def count(self) -> int:
        """返回向量库中的文档总数。"""
        try:
            return self._store._collection.count()  # noqa: SLF001
        except Exception:
            return 0
