"""RAG 引擎：编排 ingest 和 chat 全流程。

链路（chat）：混合检索（向量 + BM25）→ 重排 → 阈值拒答 → Small-to-Big 父块回溯
→ 拼 Prompt（含冲突仲裁标注）→ LLM → 蕴含裁判反思（混合重试：扩检索 / 塞纸条重写）。
"""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Callable, List, Optional

from skills.rag_qa.config import (
    CHUNK_SIZE,
    PDF_PAGE_BATCH,
    PARENT_CHUNK_SIZE,
    RELEVANCE_THRESHOLD,
    RETRIEVE_CANDIDATES,
    TOP_K,
    USE_PARENT_CHILD,
)
from skills.rag_qa import document_loader, doc_bridge, history_manager, llm_client, reranker, text_splitter
from skills.rag_qa.reflection import llm_entailment_check
from skills.rag_qa.vector_store import (
    DEFAULT_USER,
    VectorStore,
    build_doc_meta,
    normalize_user,
    parent_id_for,
)
from skills.rag_qa.bm25_retriever import BM25Retriever

logger = logging.getLogger("rag_engine")

# ----------------------------------------------------------------------
# Prompt Injection 防御：检测检索片段中是否包含试图覆盖系统指令的文本
# （如"忽略以上指令"、"你现在是..."、"system:"等）。
# 检测到后依赖 system prompt 第 5 条规则兜底（片段是文档非指令），并在日志中标记。
# ----------------------------------------------------------------------
import re as _re

_INJECTION_PATTERNS = [
    _re.compile(r'(忽略|无视|忘记)(之前|上述|上面|以上|前面)的(所有|全部)?(指令|指示|规则|要求|系统提示)', _re.IGNORECASE),
    _re.compile(r'(你现在是|你是|假设你是|扮演).{0,20}(助手|模型|系统|管理员|root|developer)', _re.IGNORECASE),
    _re.compile(r'^(system|assistant|user):\s', _re.IGNORECASE | _re.MULTILINE),
    _re.compile(r'(disregard|ignore).{0,30}(instruction|prompt|system)', _re.IGNORECASE),
    _re.compile(r'(输出|reveal|print|显示).{0,20}(系统提示|system prompt|你的提示词)', _re.IGNORECASE),
]


def detect_prompt_injection(text: str) -> bool:
    """检测文本是否包含 Prompt Injection 特征。"""
    if not text:
        return False
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


# 按用户缓存的向量库（一个用户一个独立 collection，实现知识隔离）
_stores: dict[str, VectorStore] = {}

# BM25 检索器缓存：{user: (write_version, retriever, all_docs)}，写版本变化时重建
# 不能用「块数」判断：同名替换后块数可能不变但内容已变，会导致 BM25 索引陈旧。
_bm25_cache: dict[str, tuple[int, BM25Retriever, list]] = {}

# 每用户写锁：串行化「查重 → 删旧 → 写新」，避免并发同名上传互相覆盖
_write_locks: dict[str, threading.Lock] = {}
_write_locks_guard = threading.Lock()

# 反思最大重试次数
RAG_MAX_RETRY = 2

# 引擎快拒答话术（与 LLM 自拒答话术保持一致）
NO_ANSWER_TEXT = "根据现有文档无法回答该问题。"


class EmptyDocumentError(Exception):
    """文档解析/切块后没有任何可入库内容（空文件、纯空白、扫描件无文本层）。"""


class DocumentParseError(Exception):
    """文档无法解析（空文件、损坏、格式与扩展名不符）。属于客户端输入问题，非 500。"""


class DocumentExistsError(Exception):
    """同名文档已存在且未选择替换（并发上传时由引擎在写锁内复核）。"""

    def __init__(self, source: str):
        self.source = source
        super().__init__(f"同名文档已存在: {source}，请使用替换上传(replace=true)")


def _write_lock(user: str) -> threading.Lock:
    """取某用户的写锁（懒创建）。所有入库/替换都在该锁内做查重与写入。"""
    with _write_locks_guard:
        lock = _write_locks.get(user)
        if lock is None:
            lock = threading.Lock()
            _write_locks[user] = lock
        return lock


def get_store(user: str = DEFAULT_USER) -> VectorStore:
    """获取指定用户的向量库（collection 隔离），进程内缓存。"""
    user = normalize_user(user)
    if user not in _stores:
        _stores[user] = VectorStore(user=user)
    return _stores[user]


def _get_bm25(user: str = DEFAULT_USER) -> Optional[tuple[BM25Retriever, list]]:
    """获取指定用户的 BM25 检索器（写版本变化时自动重建索引）。

    BM25 索引取「排除 parent」的全部可检索单元（child + flat），与向量检索
    单元保持一致。返回 (retriever, all_docs)；无文档时返回 None。
    """
    store = get_store(user)
    version = store.write_version
    if store.count() == 0:
        _bm25_cache.pop(user, None)
        return None

    cached = _bm25_cache.get(user)
    if cached and cached[0] == version:
        return cached[1], cached[2]

    # 重建 BM25 索引（排除零向量的父块；child 与 flat 都参与）
    all_docs = store.get_all_documents(exclude_parent=True)
    if not all_docs:
        return None
    texts = [d.page_content for d in all_docs]
    retriever = BM25Retriever(texts)
    _bm25_cache[user] = (version, retriever, all_docs)
    logger.info("BM25 索引重建: user=%s, docs=%d, version=%d", user, len(all_docs), version)
    return retriever, all_docs


# ----------------------------------------------------------------------
# 入库：解析 → 分块（父子/单层）→ 定位页码与章节 → embedding → 存入用户向量库
# ----------------------------------------------------------------------
def _ingest_pipeline(
    text: str,
    page_starts: Optional[list[int]],
    filename: str,
    user: str,
    replace: bool,
    doc_date: Optional[str],
    doc_version: Optional[str],
    cb: Callable[[str], None],
    doc_extra_meta: Optional[dict] = None,
) -> dict:
    """入库公共管线（ingest 与 ingest_path 在解析出全文后都走这里）。

    doc_extra_meta：文档级补充元数据（如格式转换质量指标），随全部块落库。
    """
    # 1. 分块：Small-to-Big 双层（小块 200 检索 + 父块 1000 喂 LLM）或单层 500
    cb("splitting")
    parents: list[tuple[str, int]] = []
    child_records: list[tuple[str, int, str]] = []  # (小块文本, 全局起始位置, parent_id)
    if USE_PARENT_CHILD:
        parents = text_splitter.split_parent_chunks(text)
        for pi, (ptext, pstart) in enumerate(parents):
            pid = parent_id_for(filename, pi)
            for ctext, local_start in text_splitter.split_child_positions(ptext):
                child_records.append((ctext, pstart + local_start, pid))
        chunks = [r[0] for r in child_records]
        positions = [r[1] for r in child_records]
        chunk_mode = "parent_child"
    else:
        chunk_list = text_splitter.split_text_with_positions(text)
        chunks = [c for c, _ in chunk_list]
        positions = [pos for _, pos in chunk_list]
        chunk_mode = "flat"

    # 2. 空内容保护：0 块不是成功，必须在删旧数据之前拦下（否则旧文档被白删）
    if not chunks:
        raise EmptyDocumentError(
            f"未提取到文本：{filename}（疑似扫描件，请开启 OCR 或提供文本层）"
        )

    # 3. 章节标题：写入 metadata.heading，便于检索溯源到章节
    headings = document_loader.locate_headings(chunks, text, document_loader.extract_headings(text))

    # 4. 定位每块所在 PDF 页码（非 PDF 无页码概念，pages=None 不写 page 字段）
    pages = None
    if page_starts is not None:
        pages = document_loader.locate_pages_from_positions(positions, page_starts)

    # 5. 先算 embedding 并组装 payload（可能失败，此时尚未动旧数据）
    cb(f"chunks:{len(chunks)}")
    store = get_store(user)
    # 文档级仲裁字段：doc_date > doc_version > ingested_at（自动生成兜底）
    doc_meta = build_doc_meta(
        doc_date=doc_date, doc_version=doc_version, extra=doc_extra_meta
    )
    if USE_PARENT_CHILD:
        payload = store.build_parent_child_payload(
            parents, child_records, source=filename, pages=pages,
            headings=headings, progress_callback=cb, doc_meta=doc_meta,
        )
    else:
        payload = store.build_flat_payload(
            chunks, source=filename, pages=pages, headings=headings,
            progress_callback=cb, doc_meta=doc_meta,
        )
    count = int(payload.get("chunk_count", 0))
    if count == 0:
        raise EmptyDocumentError(
            f"未提取到文本：{filename}（疑似扫描件，请开启 OCR 或提供文本层）"
        )

    # 6. 写锁内「复核查重 → 删旧 → 写新」：替换失败自动回滚旧块
    with _write_lock(user):
        if not replace and store.has_document(filename):
            raise DocumentExistsError(filename)
        if replace:
            removed = store.replace_with_payload(payload, filename)
            if removed:
                cb(f"replacing:{removed}")
                logger.info("替换入库: 用户=%s, %s, 删除旧块 %d 个", user, filename, removed)
        else:
            store.add_payload(payload)

    cb("done")
    return {
        "filename": filename,
        "chunks": count,
        "user": store.user,
        "paged": pages is not None,
        "chunk_mode": chunk_mode,
        "doc_date": doc_date,
        "doc_version": doc_version,
    }


def ingest(
    file_bytes: bytes,
    filename: str,
    user: str = DEFAULT_USER,
    progress_callback: Optional[Callable[[str], None]] = None,
    replace: bool = False,
    doc_date: Optional[str] = None,
    doc_version: Optional[str] = None,
) -> dict:
    """入库流程（内存模式）：直接从字节解析，不写磁盘。

    参数:
        file_bytes: 文档的字节内容。
        filename: 原始文件名（用于引用来源和判断格式）。
        user: 用户名（X-User 请求头，决定存入哪个 collection）。
        progress_callback: 进度回调函数，接收状态消息。
        replace: 同名替换（先删旧块再写新块）。
        doc_date: 文档自身日期（YYYY-MM-DD，可选，冲突仲裁依据，优先级最高）。
        doc_version: 文档版本号（可选，冲突仲裁依据）。
    """
    cb = progress_callback or (lambda _: None)

    # 1. 解析纯文本（PDF 额外记录每页起始偏移；扫描页自动 OCR 兜底）
    #    docx/xlsx/html 在解析前同进程调用 doc_agent 的 converter 转文本，
    #    用户无需先手动转 md；source 仍是原始文件名。
    cb("parsing")
    ext = Path(filename).suffix.lower()
    conv_meta: Optional[dict] = None
    try:
        if ext == ".pdf":
            pages = document_loader.extract_pdf_pages_from_bytes(file_bytes, progress_callback=cb)
            text, page_starts = document_loader.build_page_text(pages)
        elif ext in doc_bridge.CONVERTIBLE_EXTS:
            text, conv_meta = doc_bridge.convert_bytes_to_markdown(file_bytes, filename, cb)
            page_starts = None
        else:
            text = document_loader.extract_text(file_bytes, filename)
            page_starts = None
    except doc_bridge.ConversionError as e:
        logger.warning("文档转换失败: %s, %s", filename, e)
        raise DocumentParseError(str(e)) from e
    except Exception as e:
        if isinstance(e, ValueError):
            # 解析层的可读错误（如“PDF 已加密”“格式不支持”）直接上抛，由接口映射为 400
            raise
        logger.warning("文档解析失败: %s", filename, exc_info=True)
        raise DocumentParseError(
            f"文档解析失败：{filename}（文件可能为空、已损坏或扩展名与实际格式不符）"
        ) from e

    return _ingest_pipeline(
        text, page_starts, filename, user, replace, doc_date, doc_version, cb,
        doc_extra_meta=conv_meta,
    )


def ingest_path(
    path: str,
    filename: str,
    progress_callback: Optional[Callable[[str], None]] = None,
    user: str = DEFAULT_USER,
    replace: bool = False,
    doc_date: Optional[str] = None,
    doc_version: Optional[str] = None,
) -> dict:
    """入库流程（落盘模式）：从磁盘临时文件解析，请求体与解析均不占大块内存。

    参数同 ingest（path 为服务器上的临时文件路径）。
    """
    cb = progress_callback or (lambda _: None)

    # 1. 从磁盘解析纯文本（PDF 额外记录每页起始偏移；扫描页自动 OCR 兜底）
    #    docx/xlsx/html 同进程走 doc_agent converter，不落中间产物文件
    cb("parsing")
    ext = Path(filename).suffix.lower()
    conv_meta: Optional[dict] = None
    try:
        if ext == ".pdf":
            pages = document_loader.extract_pdf_pages_from_path(path, progress_callback=cb)
            text, page_starts = document_loader.build_page_text(pages)
        elif ext in doc_bridge.CONVERTIBLE_EXTS:
            text, conv_meta = doc_bridge.convert_path_to_markdown(path, filename, cb)
            page_starts = None
        else:
            text = document_loader.extract_text_from_path(path, filename)
            page_starts = None
    except doc_bridge.ConversionError as e:
        logger.warning("文档转换失败: %s, %s", filename, e)
        raise DocumentParseError(str(e)) from e
    except Exception as e:
        if isinstance(e, ValueError):
            # 解析层的可读错误（如“PDF 已加密”“格式不支持”）直接上抛，由接口映射为 400
            raise
        logger.warning("文档解析失败: %s", filename, exc_info=True)
        raise DocumentParseError(
            f"文档解析失败：{filename}（文件可能为空、已损坏或扩展名与实际格式不符）"
        ) from e

    return _ingest_pipeline(
        text, page_starts, filename, user, replace, doc_date, doc_version, cb,
        doc_extra_meta=conv_meta,
    )


def ingest_path_stream(
    path: str,
    filename: str,
    progress_callback: Optional[Callable[[str], None]] = None,
    user: str = DEFAULT_USER,
    replace: bool = False,
    doc_date: Optional[str] = None,
    doc_version: Optional[str] = None,
) -> dict:
    """入库流程（按页流水线，大文件层3）：PDF 每批 PDF_PAGE_BATCH 页
    解析 → 分块 → 嵌入 → 入库，处理完一批即释放，内存峰值从
    "整本文档全部页文本"降到"一批页 + 尾部缓冲"。

    批间保留 2×父块(或块)长度的 carry-over 缓冲：splitter 顺序贪心切分，
    前缀块边界只依赖边界之前的内容，缓冲足够大时安全区（cutoff 之前）切出的
    块与全量切分一致；仅批边界附近可能有细微差别（检索单元是 200 字小块，
    影响可忽略）。页起点/标题偏移全部累计为全局坐标，页码与章节定位不受影响。

    失败回滚：替换前先快照旧块；任一批异常时删除已写入的新块并恢复旧快照，
    保证不留半份文档、也不因替换失败丢掉旧版。0 块视为失败（空/扫描件无文本层）。
    非 PDF 文本文件无页概念，退回全量管线 ingest_path。

    参数同 ingest_path。
    """
    ext = Path(filename).suffix.lower()
    if ext != ".pdf":
        return ingest_path(path, filename, progress_callback, user,
                           replace, doc_date, doc_version)

    cb = progress_callback or (lambda _: None)
    store = get_store(user)
    doc_meta = build_doc_meta(doc_date=doc_date, doc_version=doc_version)

    # 切分粒度：父子模式按父块保留缓冲，单层模式按 CHUNK_SIZE
    unit = PARENT_CHUNK_SIZE if USE_PARENT_CHILD else CHUNK_SIZE
    keep_tail = 2 * unit  # carry-over 缓冲（防批边界切断块）

    buffer = ""                          # 未切分文本（含保留区）
    buffer_start = 0                     # buffer 首字符的全局偏移
    page_starts: list[int] = []          # 累计：每页起点（全局坐标）
    headings_global: list[tuple[int, str]] = []  # 累计：标题 (全局偏移, 标题)
    seen_headings: set[int] = set()
    child_seq = 0                        # 小块全局序号（分批写入不撞 ID）
    parent_seq = 0                       # 父块全局序号
    total_children = 0

    def _absorb(batch: list[str], first_page: int, final: bool) -> None:
        """吸收一批页文本：拼接 → 累计页起点 → 提取标题 → 安全区切块 → 入库 → 消费缓冲。"""
        nonlocal buffer, buffer_start, child_seq, parent_seq, total_children

        # 1. 拼接进缓冲（每页尾部带一个换行，页间恰为单换行——与 build_page_text
        #    规则一致；尾部多出的最后一个换行被 splitter 忽略，无影响），
        #    同步累计页起点（全局坐标）
        if batch:
            cb(f"pages:{first_page}-{first_page + len(batch) - 1}")
            for page in batch:
                page_starts.append(buffer_start + len(buffer))
                buffer += page + "\n"

        # 2. 提取标题（对整个 buffer，保留安全区内且未见过的，偏移转全局）
        cutoff = len(buffer) if final else len(buffer) - keep_tail
        for off, title in document_loader.extract_headings(buffer):
            if off < cutoff:
                goff = buffer_start + off
                if goff not in seen_headings:
                    headings_global.append((goff, title))
                    seen_headings.add(goff)

        # 3. 安全区切块：只保留起点在 cutoff 之前的块（保留区留给下一批重切）
        if USE_PARENT_CHILD:
            all_parents = text_splitter.split_parent_chunks(buffer)
            kept = [(t, s) for t, s in all_parents if s < cutoff] if not final else all_parents
        else:
            all_chunks = text_splitter.split_text_with_positions(buffer)
            kept = [(t, s) for t, s in all_chunks if s < cutoff] if not final else all_chunks
        if not kept:
            return  # buffer 不足一切（内容太短），等下一批

        # 4. 组装本批记录（全局坐标：父块起点 + 小块父内偏移）
        parent_start_batch = parent_seq
        batch_parents: list[tuple[str, int]] = []
        child_records: list[tuple[str, int, str]] = []
        if USE_PARENT_CHILD:
            for ptext, pstart in kept:
                pid = parent_id_for(filename, parent_seq)
                batch_parents.append((ptext, buffer_start + pstart))
                for ctext, cstart in text_splitter.split_child_positions(ptext):
                    child_records.append((ctext, buffer_start + pstart + cstart, pid))
                parent_seq += 1
        else:
            batch_parents = [(t, buffer_start + s) for t, s in kept]
            child_records = [(t, buffer_start + s, "") for t, s in kept]

        # 5. 页码 + 章节定位（page_starts/headings 已累计到本批，bisect 全局坐标）
        positions = [r[1] for r in child_records]
        pages_batch = (
            document_loader.locate_pages_from_positions(positions, page_starts)
            if page_starts else None
        )
        headings_batch = document_loader.locate_headings_from_positions(positions, headings_global)

        # 6. 嵌入并写入（全局序号偏移，分批不撞 ID）
        cb(f"chunks:{child_seq + len(child_records)}")
        if USE_PARENT_CHILD:
            n = store.add_parent_child_chunks(
                batch_parents, child_records, source=filename,
                pages=pages_batch, headings=headings_batch,
                progress_callback=cb, doc_meta=doc_meta,
                child_start=child_seq, parent_start=parent_start_batch,
            )
        else:
            n = store.add_chunks_fast(
                [t for t, _, _ in child_records], source=filename,
                pages=pages_batch, headings=headings_batch,
                progress_callback=cb, doc_meta=doc_meta, child_start=child_seq,
            )
        total_children += n
        child_seq += len(child_records)

        # 7. 消费缓冲：保留最后一个块末尾之后的内容（保留区）
        last_text, last_start = kept[-1]
        consumed = last_start + len(last_text)
        buffer = buffer[consumed:]
        buffer_start += consumed

    with _write_lock(user):
        if not replace and store.has_document(filename):
            raise DocumentExistsError(filename)

        # 同名替换：先快照旧块再删，失败可原样恢复
        snapshot: Optional[dict] = None
        if replace:
            snapshot = store.snapshot_document(filename)
            removed = store.delete_document(filename)
            if removed:
                cb(f"replacing:{removed}")
                logger.info(
                    "替换入库(流水线): 用户=%s, %s, 删除旧块 %d 个", user, filename, removed
                )

        def _page_batches():
            """只包住解析阶段：解析异常属于输入问题（422），不掩盖后续 embedding/写入异常。"""
            try:
                yield from document_loader.iter_pdf_page_batches(path, PDF_PAGE_BATCH, cb)
            except Exception as e:
                logger.warning("PDF 解析失败: %s", filename, exc_info=True)
                raise DocumentParseError(
                    f"文档解析失败：{filename}（文件可能为空、已损坏或扩展名与实际格式不符）"
                ) from e

        try:
            for batch, first_page in _page_batches():
                _absorb(batch, first_page, final=False)
            _absorb([], 0, final=True)  # flush：切完剩余缓冲
            if total_children == 0:
                raise EmptyDocumentError(
                    f"未提取到文本：{filename}（疑似扫描件，请开启 OCR 或提供文本层）"
                )
        except Exception:
            # 失败回滚：删除已写入的新块，再恢复替换前的旧快照
            rolled = 0
            try:
                rolled = store.delete_document(filename)
            except Exception:
                logger.warning("回滚清理新块失败: %s", filename, exc_info=True)
            restored = 0
            if snapshot:
                try:
                    restored = store.restore_snapshot(snapshot)
                except Exception:
                    logger.exception("回滚恢复旧块失败（需人工介入）: %s", filename)
            logger.warning(
                "流水线入库失败已回滚: 用户=%s, %s, 清理新块 %d, 恢复旧块 %d",
                user, filename, rolled, restored,
            )
            raise

    cb("done")
    return {
        "filename": filename,
        "chunks": total_children,
        "user": store.user,
        "paged": True,
        "chunk_mode": "parent_child" if USE_PARENT_CHILD else "flat",
        "pipeline": "stream",
        "doc_date": doc_date,
        "doc_version": doc_version,
    }


def list_documents(user: str = DEFAULT_USER) -> list[dict]:
    """列出指定用户知识库中的文档（文件名 + 小块数 + 父块数 + 最大页码）。"""
    return get_store(user).list_documents()


def list_document_chunks(
    source: str,
    user: str = DEFAULT_USER,
    offset: int = 0,
    limit: int = 20,
) -> dict:
    """分页查看指定用户知识库中某文档的分块（严格限定在该用户 collection 内）。"""
    if not source or not source.strip():
        raise ValueError("文件名不能为空")
    return get_store(user).list_chunks(source.strip(), offset=offset, limit=limit)


def delete_document(source: str, user: str = DEFAULT_USER) -> int:
    """删除指定用户知识库中某个文档的全部向量块，返回删除块数。"""
    if not source or not source.strip():
        raise ValueError("文件名不能为空")
    return get_store(user).delete_document(source.strip())


# ----------------------------------------------------------------------
# 问答：混合检索 → 重排 → 阈值拒答 → 父块回溯 → LLM → 蕴含裁判反思
# ----------------------------------------------------------------------
def _threshold_refusal(docs: List) -> Optional[dict]:
    """引擎快拒答判断：无检索结果，或 top_k 重排分数全低于阈值。

    返回:
        拒答信息 {"reason", "max_score"}；不触发时返回 None。
        无重排分数（rerank 关闭或失败降级）时跳过阈值判断。
    """
    if not docs:
        return {"reason": "no_results", "max_score": 0.0}

    scores = [doc.metadata.get("rerank_score") for doc in docs]
    if not all(isinstance(s, (int, float)) for s in scores):
        return None  # 无分数，无法做阈值判断

    max_score = max(scores)
    if max_score < RELEVANCE_THRESHOLD:
        return {"reason": "low_relevance", "max_score": round(max_score, 4)}
    return None


async def chat(
    question: str,
    history: Optional[List[dict]] = None,
    user: str = DEFAULT_USER,
    progress_cb: Optional[Callable[[dict], None]] = None,
    retrieval_query: Optional[str] = None,
    observer: Optional[dict] = None,
    ablation: Optional[dict] = None,
) -> dict:
    """问答流程：混合检索（小块）→ 重排 → 阈值拒答 → 父块回溯 → 拼 Prompt
    → LLM（含 Prompt Injection 标记）→ 蕴含裁判反思（混合重试）。

    Small-to-Big：向量/BM25 检索单元是小块（200 字，命中精准），
    命中后按 parent_id 回溯父块（1000 字）喂给 LLM（上下文完整）。

    参数:
        question: 用户问题（原文，用于回答与引用）。
        history: 对话历史。
        user: 用户名（由 JWT 派生，决定检索哪个 collection）。
        progress_cb: 可选进度回调，接收 {"stage", "message"}，仅用于 SSE 状态提示，
            不参与任何业务判断（默认 None 时行为与改前完全一致）。
        retrieval_query: 可选的独立问句（指代消解/查询改写结果）。仅用于检索与
            重排；答案事实依据仍只来自检索到的原文，历史内容不作为事实来源。
        observer: 可选观测字典（评测/链路观测用）。仅写入检索与重试信息，
            不参与任何业务判断；为 None 时行为与改前完全一致。
        ablation: 可选消融开关（仅评测用）。支持的键：hybrid / rerank /
            parent_child / threshold / reflection，缺省均为 True（线上语义）。
            与线上默认值一致时行为完全不变。

    返回:
        {
            "answer": str,
            "citations": [{"file", "chunk", "content", "page"(PDF 有), ...}],
            "reflection": dict,  # 正常路径含 passed/log；快拒答含 no_answer/reason/max_score
        }
    """
    store = get_store(user)
    cb = progress_cb or (lambda _: None)
    abl = ablation or {}
    use_hybrid = bool(abl.get("hybrid", True))
    use_rerank = bool(abl.get("rerank", True))
    use_parent_child = bool(abl.get("parent_child", True)) and USE_PARENT_CHILD
    use_threshold = bool(abl.get("threshold", True))
    use_reflection = bool(abl.get("reflection", True))

    # 检索问句：有改写结果就用改写版（指代消解后），否则用原问题。
    # 注意：改写只服务于检索/重排，历史内容不会进入答案事实依据。
    search_query = (retrieval_query or question).strip() or question

    # 历史压缩（超长时压缩早期对话为摘要）
    trimmed_history = await history_manager.compress_history(history or [])

    # 反思重试循环（混合重试：expand_retrieval 重跑检索 / retry_hint 塞纸条重写）
    retrieve_k = RETRIEVE_CANDIDATES
    retry_hint: Optional[str] = None
    used_adjustments: list[str] = []
    reflection_log: list[dict] = []

    for attempt in range(RAG_MAX_RETRY + 1):
        cb({
            "stage": "retrieving",
            "message": "正在检索知识库…" if attempt == 0 else f"正在重新检索（第 {attempt} 次重试）…",
        })
        await asyncio.sleep(0)  # 先让 SSE 刷出状态，再进入阻塞型检索

        # 1. 混合检索：向量召回语义相关 + BM25 召回关键词精确匹配，取并集去重
        #    向量侧用「排除 parent」，child（父子小块）与 flat（单层/历史数据）
        #    都参与检索；parent 是零向量内部结构，永不召回。
        vector_candidates = store.similarity_search_children(search_query, k=retrieve_k)

        bm25_candidates: List = []
        if use_hybrid:
            bm25_result = _get_bm25(user)
            if bm25_result is not None:
                bm25_retriever, all_docs = bm25_result
                bm25_top = bm25_retriever.search(search_query, k=retrieve_k)
                bm25_candidates = [all_docs[idx] for idx, _ in bm25_top]

        # 合并去重：按 (source, chunk_index) 去重
        seen_keys: set = set()
        candidates: List = []
        for doc in vector_candidates + bm25_candidates:
            key = (doc.metadata.get("source"), doc.metadata.get("chunk_index"))
            if key not in seen_keys:
                seen_keys.add(key)
                candidates.append(doc)

        logger.info(
            "混合检索: vector=%d, bm25=%d, 合并去重后=%d",
            len(vector_candidates), len(bm25_candidates), len(candidates),
        )

        # 2. 重排，取 top_k（分数写入 metadata["rerank_score"]）
        cb({
            "stage": "reranking",
            "message": f"已完成检索，命中 {len(candidates)} 片，重排中…",
        })
        if use_rerank:
            docs = await reranker.rerank(search_query, candidates, top_k=TOP_K)
        else:
            docs = candidates[:TOP_K]
        if observer is not None:
            observer["retrieved"] = [
                {
                    "source": d.metadata.get("source"),
                    "chunk": d.metadata.get("chunk_index"),
                    "rerank_score": d.metadata.get("rerank_score"),
                }
                for d in docs
            ]
            observer["retrieve_k"] = retrieve_k
            observer["attempts"] = attempt + 1

        # 3. 阈值拒答：top_k 分数全低于阈值时跳过 LLM 直接拒答（省一次 LLM 调用）
        refusal = _threshold_refusal(docs) if use_threshold else None
        if refusal:
            logger.info(
                "RAG 引擎快拒答: reason=%s, max_score=%s (阈值=%.2f)",
                refusal["reason"], refusal["max_score"], RELEVANCE_THRESHOLD,
            )
            if observer is not None:
                observer["refused"] = True
                observer["refusal_reason"] = refusal["reason"]
            return {
                "answer": NO_ANSWER_TEXT,
                "citations": [],
                "reflection": {
                    "passed": True,
                    "no_answer": True,
                    "reason": refusal["reason"],
                    "max_score": refusal["max_score"],
                    "log": reflection_log,
                },
            }

        # 4. 父块回溯：命中的 child 小块按 parent_id 取父块喂给 LLM（上下文完整）；
        #    flat（单层/历史数据）没有 parent_id，直接用自身作为上下文。
        #    引用与拒答判断仍基于命中的小块/单层块。
        context_docs = docs
        if use_parent_child:
            parent_ids = list(dict.fromkeys(  # 去重保序：多个小块命中同一父块只取一次
                d.metadata["parent_id"] for d in docs if d.metadata.get("parent_id")
            ))
            flat_docs = [d for d in docs if not d.metadata.get("parent_id")]
            if parent_ids:
                parents = store.get_parents(parent_ids)  # 回溯失败时整段退化为小块
                if parents:
                    context_docs = parents + flat_docs
                    logger.info(
                        "RAG 父块回溯: %d 个小块 → %d 个父块（另有 %d 个单层块直用）",
                        len(docs), len(parents), len(flat_docs),
                    )

        # 4.5 Prompt Injection 检测：扫描检索片段是否含试图覆盖系统指令的文本
        for doc in docs:
            if detect_prompt_injection(doc.page_content):
                logger.warning(
                    "检测到疑似 Prompt Injection: source=%s, chunk=%s（依赖 system prompt 防护规则）",
                    doc.metadata.get("source"), doc.metadata.get("chunk_index"),
                )
                break

        # 5. 异步调 LLM（上下文用父块；重试时前置纸条 retry_hint）
        cb({
            "stage": "generating",
            "message": "生成回答中…" if attempt == 0 else f"生成回答中（第 {attempt} 次重试）…",
        })
        answer = await llm_client.chat(
            question, context_docs, history=trimmed_history, retry_hint=retry_hint,
            standalone_question=search_query,
        )

        # 6. 组装引用（基于命中小块，PDF 分块带 page 页码 + 章节 + 仲裁字段）
        citations = []
        for doc in docs:
            meta = doc.metadata
            citation = {
                "file": meta.get("source", "未知"),
                "chunk": meta.get("chunk_index", -1),
                "content": doc.page_content[:100],
            }
            if "page" in meta:
                citation["page"] = meta["page"]
            if meta.get("heading"):
                citation["heading"] = meta["heading"]
            for field in ("doc_date", "doc_version", "ingested_at"):
                if meta.get(field):
                    citation[field] = meta[field]
            citations.append(citation)

        # 7. 反思校验（LLM 蕴含裁判，校验 LLM 实际看到的上下文）
        #    用改写后的独立问句做校验，避免裁判被"它/这些"等指代干扰
        if use_reflection:
            reflection = await llm_entailment_check(search_query, answer, context_docs)
        else:
            reflection = {"passed": True, "skipped": True}

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
            if observer is not None:
                observer["refused"] = False
                observer["reflection_passed"] = True
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

        # 应用调整策略（混合重试：各病因对症下药）
        if adjustment == "expand_retrieval":
            # unsupported_answer（材料不够）：扩检索重跑全流程——纸条无效，病根是材料不足
            retrieve_k = RETRIEVE_CANDIDATES * 2
            retry_hint = None
            logger.info("RAG 重试: 扩大检索范围 k=%d（材料不够，重跑检索）", retrieve_k)
        elif adjustment == "add_no_answer_constraint":
            # hallucination（编造）：塞纸条重写——列出无依据断言，只重新生成不重跑检索
            claims = reflection.get("unsupported_claims") or []
            if claims:
                claims_text = "\n".join(f"- {c}" for c in claims)
            else:
                claims_text = "-（裁判未给出具体断言，请逐句自查与参考片段的对应关系）"
            retry_hint = (
                "你上一轮回答中的以下内容在参考片段中找不到依据：\n"
                f"{claims_text}\n"
                '请仅依据【参考片段】重写回答：删除或修正以上无依据断言；'
                '若片段确实不足以回答，请直接回答"根据现有文档无法回答该问题"。'
            )
            logger.info("RAG 重试: 塞纸条重写（编造，%d 条无依据断言）", len(claims))
        elif adjustment == "add_citation_instruction":
            # no_citation（缺引用）：塞纸条重写——补引用指令，只重新生成
            retry_hint = (
                "你上一轮回答缺少引用标注。请重写回答，并在末尾用 "
                "[来源: 文件名#分块编号] 的格式标注每条信息的出处。"
            )
            logger.info("RAG 重试: 塞纸条重写（缺引用）")
        else:
            logger.info("RAG 重试: 未知调整策略 %s，跳过", adjustment)
            break

    # 重试失败或不可重试，返回最终结果
    if observer is not None:
        observer["refused"] = False
        observer["reflection_passed"] = False
    return {
        "answer": answer,
        "citations": citations,
        "reflection": {"passed": False, "log": reflection_log},
    }
