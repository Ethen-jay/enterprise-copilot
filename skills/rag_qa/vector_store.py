"""Chroma 向量库封装：按用户隔离 collection、添加分块、相似度检索、持久化。

用户隔离：通过 X-User 请求头区分不同用户，每个用户对应独立 collection
（命名格式 kb_{user}）；default 用户沿用历史 collection rag_knowledge_base
（迁移前已入库的数据无需重灌）。

向量空间：新 collection 建立时指定 hnsw:space（默认 cosine，bge 官方推荐，
配合 embedding 出口的 L2 归一化）。注意 Chroma 的 space 在建 collection 时
固定不可改：已存在的旧 collection（如默认 l2 建的）保持原 space，仅打日志提示，
不影响可用性（向量已归一化时 l2 与 cosine 排序等价）。

Small-to-Big 父子分块：同一 collection 内存两类文档（metadata.chunk_type 区分）：
- child（小块，200 字）：带真实向量，是唯一参与向量检索单元，metadata 存 parent_id
- parent（父块，1000 字）：零向量（不参与检索），仅按 ID 回溯取文本，喂给 LLM

冲突仲裁：build_doc_meta 构造文档级仲裁字段（doc_date > doc_version >
ingested_at），写入该文档全部块（child+parent）的 metadata，供拼 prompt 时
标注新旧、LLM 按优先级采信。
"""

import logging
import re
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from langchain_chroma import Chroma
from langchain_core.documents import Document

from skills.rag_qa.config import CHROMA_DIR, TOP_K, VECTOR_SPACE
from skills.rag_qa.embeddings import APIEmbeddings

logger = logging.getLogger("vector_store")

# 默认用户（请求未携带 X-User 头时使用）
DEFAULT_USER = "default"

# default 用户沿用迁移前的旧 collection（已有数据无需重灌），其余用户用 kb_{user}
DEFAULT_COLLECTION = "rag_knowledge_base"

# 用户名合法格式：字母/数字/下划线/连字符，1-32 位（与登录账号规则一致）
USER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def normalize_user(user: Optional[str]) -> str:
    """规范化用户名：空值回退 default；非法格式抛 ValueError。"""
    user = (user or "").strip()
    if not user:
        return DEFAULT_USER
    if not USER_PATTERN.fullmatch(user):
        raise ValueError(
            "X-User 格式非法：仅允许字母、数字、下划线、连字符，1-32 位"
        )
    return user


def collection_name_for(user: str) -> str:
    """用户名 → collection 名：default 沿用旧集合，其余 kb_{user}。"""
    if user == DEFAULT_USER:
        return DEFAULT_COLLECTION
    return f"kb_{user}"


def parent_id_for(source: str, parent_index: int) -> str:
    """父块文档 ID：{source}#p{N}。入库与检索回溯共用此规则。"""
    return f"{source}#p{parent_index}"


def build_doc_meta(doc_date: Optional[str] = None, doc_version: Optional[str] = None) -> dict:
    """构造文档级仲裁字段，写入该文档全部块（child+parent）的 metadata。

    冲突仲裁依据（优先级从高到低）：
    - doc_date：文档自身日期（上传时显式指定，YYYY-MM-DD）
    - doc_version：文档版本号（上传时显式指定）
    - ingested_at：入库时间（自动生成，无显式信息时的兜底——后上传视为更新）
    """
    meta: dict = {"ingested_at": datetime.now().isoformat(timespec="seconds")}
    if doc_date:
        meta["doc_date"] = doc_date
    if doc_version:
        meta["doc_version"] = doc_version
    return meta


class VectorStore:
    """Chroma 向量库封装（每个用户一个独立 collection）。"""

    def __init__(self, user: str = DEFAULT_USER):
        self.user = user
        self.collection_name = collection_name_for(user)
        self.embeddings = APIEmbeddings()
        self._store = Chroma(
            collection_name=self.collection_name,
            embedding_function=self.embeddings,
            persist_directory=str(CHROMA_DIR),
            collection_metadata={"hnsw:space": VECTOR_SPACE},
        )
        # Chroma 的 space 建 collection 时固定：同名旧 collection 复用时保持原 space，
        # 此处检测不一致仅提示（向量已归一化，l2 与 cosine 排序等价，不影响可用性）
        actual = (self._store._collection.metadata or {}).get("hnsw:space")  # noqa: SLF001
        if actual and actual != VECTOR_SPACE:
            logger.warning(
                "collection %s 的向量空间为 %s（配置为 %s）；Chroma space 建 collection 后不可改，"
                "如需切换请删除该 collection 后重新入库",
                self.collection_name, actual, VECTOR_SPACE,
            )

    def add_chunks_fast(
        self,
        chunks: List[str],
        source: str,
        progress_callback: Optional[Callable[[str], None]] = None,
        pages: Optional[List[int]] = None,
        headings: Optional[List[str]] = None,
        doc_meta: Optional[dict] = None,
        child_start: int = 0,
    ) -> int:
        """高性能入库（单层分块模式）：预计算 embedding → 直接写入 collection。

        跳过 LangChain Chroma 的中间层，直接操作底层 collection。

        参数:
            pages: 与 chunks 等长的页码列表（1 起）；PDF 入库时提供，
                   写入 metadata.page 供引用溯源到原文页码。
            headings: 与 chunks 等长的章节标题列表；提供时写入 metadata.heading，
                      便于检索结果溯源到文档章节。
            doc_meta: 文档级仲裁字段（doc_date/doc_version/ingested_at，见 build_doc_meta）。
            child_start: 本批 chunk 的全局起始序号（按页流水线分批写入时累计偏移，
                         保证 ID/chunk_index 全文档唯一不冲突；单次全量写入保持默认 0）。
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
        ids = [f"{source}#{child_start + i}" for i in range(len(chunks))]
        metadatas = [
            _build_metadata(source, child_start + i, pages, headings, doc_meta, local_index=i)
            for i in range(len(chunks))
        ]

        self._store._collection.add(  # noqa: SLF001
            ids=ids,
            documents=chunks,
            embeddings=vectors,
            metadatas=metadatas,
        )

        return len(chunks)

    def add_parent_child_chunks(
        self,
        parent_chunks: List[Tuple[str, int]],
        child_records: List[Tuple[str, int, str]],
        source: str,
        pages: Optional[List[int]] = None,
        headings: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        doc_meta: Optional[dict] = None,
        child_start: int = 0,
        parent_start: int = 0,
    ) -> int:
        """Small-to-Big 父子双层入库。

        小块（child）带真实向量参与检索，metadata 存 parent_id 指向父块；
        父块（parent）存零向量（检索永远过滤 chunk_type=child，父块不会被搜到），
        仅按 ID 回溯取文本，省一半 embedding 调用。

        参数:
            parent_chunks: [(父块文本, 父块起始位置), ...]（引擎层已切好）。
            child_records: [(小块文本, 小块全局起始位置, parent_id), ...]。
            source: 来源文件名。
            pages: 本批每个小块所在 PDF 页码（可选，与 child_records 等长局部索引）。
            headings: 本批每个小块所属章节标题（可选，与 child_records 等长局部索引）。
            progress_callback: 进度回调函数。
            doc_meta: 文档级仲裁字段（doc_date/doc_version/ingested_at，见 build_doc_meta）。
            child_start: 本批小块的全局起始序号（分批写入累计偏移，保 ID/chunk_index 唯一）。
            parent_start: 本批父块的全局起始序号（parent_id 全文档唯一不冲突）。

        返回:
            入库的小块数。
        """
        if not child_records:
            return 0
        cb = progress_callback or (lambda _: None)
        doc_meta = doc_meta or {}

        # 1. 小块：预计算 embedding → 直接写入
        cb(f"embedding:{len(child_records)}")
        child_texts = [r[0] for r in child_records]
        vectors = self.embeddings.embed_documents(child_texts)
        cb("storing")

        child_metas = []
        for i, (_, _, parent_id) in enumerate(child_records):
            meta = {
                "source": source,
                "chunk_index": child_start + i,  # 全局序号（引用溯源用）
                "chunk_type": "child",
                "parent_id": parent_id,
            }
            # pages/headings 为本批局部索引（与 child_records 等长），不能用全局序号索引
            if pages is not None and i < len(pages) and pages[i]:
                meta["page"] = int(pages[i])
            if headings is not None and i < len(headings) and headings[i]:
                meta["heading"] = headings[i]
            meta.update(doc_meta)  # 仲裁字段（doc_date/doc_version/ingested_at）
            child_metas.append(meta)
        child_ids = [f"{source}#c{child_start + i}" for i in range(len(child_records))]

        # 2. 父块：零向量（维度取自小块向量），不参与向量检索
        dim = len(vectors[0])
        parent_ids = [parent_id_for(source, parent_start + i) for i in range(len(parent_chunks))]
        parent_metas = []
        for i in range(len(parent_chunks)):
            meta = {"source": source, "chunk_type": "parent", "parent_index": parent_start + i}
            meta.update(doc_meta)  # 父块也带仲裁字段：LLM 上下文用父块，仲裁依据必须存在这层
            parent_metas.append(meta)
        parent_texts = [p[0] for p in parent_chunks]
        zero_vecs = [[0.0] * dim for _ in parent_chunks]

        self._store._collection.add(  # noqa: SLF001
            ids=child_ids + parent_ids,
            documents=child_texts + parent_texts,
            embeddings=vectors + zero_vecs,
            metadatas=child_metas + parent_metas,
        )

        return len(child_records)

    def similarity_search_children(
        self,
        query: str,
        k: int = TOP_K,
    ) -> List[Document]:
        """Small-to-Big 检索层：只对小块（chunk_type=child）做向量检索。"""
        return self._store.similarity_search(query, k=k, filter={"chunk_type": "child"})

    def get_parents(self, parent_ids: List[str]) -> List[Document]:
        """按 ID 批量回溯父块，返回顺序与传入一致（缺失的跳过）。"""
        if not parent_ids:
            return []
        res = self._store._collection.get(  # noqa: SLF001
            ids=parent_ids, include=["metadatas", "documents"]
        )
        by_id = {
            id_: Document(page_content=doc, metadata=meta or {})
            for id_, doc, meta in zip(res.get("ids", []), res.get("documents", []), res.get("metadatas", []))
        }
        return [by_id[pid] for pid in parent_ids if pid in by_id]

    def similarity_search(
        self,
        query: str,
        k: int = TOP_K,
    ) -> List[Document]:
        """检索最相关的 k 个片段（单层分块模式）。"""
        return self._store.similarity_search(query, k=k)

    def count(self) -> int:
        """返回当前用户向量库中的文档总数。"""
        try:
            return self._store._collection.count()  # noqa: SLF001
        except Exception:
            return 0

    def count_source(self, source: str) -> int:
        """统计指定来源文件的块数（小块+父块）。"""
        try:
            res = self._store._collection.get(  # noqa: SLF001
                where={"source": source}, include=[]
            )
            return len(res.get("ids", []))
        except Exception:
            return 0

    def has_document(self, source: str) -> bool:
        """判断指定来源文件是否已入库（存在任意一块即视为存在）。"""
        return self.count_source(source) > 0

    def list_documents(self) -> List[dict]:
        """列出当前用户知识库中的所有文档：来源、小块数、父块数、最大页码。

        小块（chunks）为可检索单元；父块（parents）为父子分块模式的内部结构；
        max_page 为 PDF 分块记录的最大页码（非 PDF 为 0）。
        """
        res = self._store._collection.get(include=["metadatas"])  # noqa: SLF001
        docs: dict[str, dict] = {}
        for meta in res.get("metadatas", []):
            meta = meta or {}
            source = meta.get("source", "")
            if not source:
                continue
            item = docs.setdefault(
                source, {"source": source, "chunks": 0, "parents": 0, "max_page": 0}
            )
            if meta.get("chunk_type") == "parent":
                item["parents"] += 1
            else:
                item["chunks"] += 1
                page = meta.get("page")
                if isinstance(page, int) and page > item["max_page"]:
                    item["max_page"] = page
        return sorted(docs.values(), key=lambda d: d["source"])

    def delete_document(self, source: str) -> int:
        """按来源文件名删除其全部块（小块+父块），返回删除的块数。

        Chroma 的 delete 支持 where 过滤：按 metadata.source 精确匹配一次删净。
        """
        removed = self.count_source(source)
        if removed == 0:
            return 0
        self._store._collection.delete(where={"source": source})  # noqa: SLF001
        return removed

    def get_all_documents(self, chunk_type: Optional[str] = None) -> List[Document]:
        """返回知识库中全部文档（用于构建 BM25 索引等需要全量文本的场景）。

        参数:
            chunk_type: 父子分块模式下传 "child" 只取小块（BM25 检索单元
                        与向量检索单元保持一致，排除零向量的父块）；
                        None 取全部（单层分块模式直接用）。
        """
        try:
            if chunk_type:
                data = self._store._collection.get(  # noqa: SLF001
                    where={"chunk_type": chunk_type},
                    include=["documents", "metadatas"],
                )
            else:
                data = self._store._collection.get(  # noqa: SLF001
                    include=["documents", "metadatas"],
                )
        except Exception:
            return []
        docs = data.get("documents", []) or []
        metas = data.get("metadatas", []) or []
        return [
            Document(page_content=doc, metadata=meta or {})
            for doc, meta in zip(docs, metas)
        ]


def _build_metadata(
    source: str,
    chunk_index: int,
    pages: Optional[List[int]],
    headings: Optional[List[str]] = None,
    doc_meta: Optional[dict] = None,
    local_index: Optional[int] = None,
) -> dict:
    """构造 chunk metadata：source + chunk_index，PDF 分块额外存 page 页码，
    headings 存章节标题，doc_meta 为文档级仲裁字段（doc_date/doc_version/ingested_at）。

    local_index: pages/headings 的本批局部索引（分批写入时与全局 chunk_index 不同，
                 单次全量写入不传，退化为 chunk_index）。"""
    idx = local_index if local_index is not None else chunk_index
    meta = {"source": source, "chunk_index": chunk_index}
    if pages is not None and idx < len(pages) and pages[idx]:
        meta["page"] = int(pages[idx])
    if headings is not None and idx < len(headings) and headings[idx]:
        meta["heading"] = headings[idx]
    if doc_meta:
        meta.update(doc_meta)
    return meta
