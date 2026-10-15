"""RAG 引擎：编排 ingest 和 chat 全流程。

链路（chat）：混合检索（向量 + BM25）→ 重排 → 阈值拒答 → Small-to-Big 父块回溯
→ 拼 Prompt（含冲突仲裁标注）→ LLM → 蕴含裁判反思（混合重试：扩检索 / 塞纸条重写）。
"""

import logging
from pathlib import Path
from typing import Callable, List, Optional

from skills.rag_qa.config import (
    RELEVANCE_THRESHOLD,
    RETRIEVE_CANDIDATES,
    TOP_K,
    USE_PARENT_CHILD,
)
from skills.rag_qa import document_loader, history_manager, llm_client, reranker, text_splitter
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

# BM25 检索器缓存：{user: (doc_count, retriever, all_docs)}，文档数变化时重建
_bm25_cache: dict[str, tuple[int, BM25Retriever, list]] = {}

# 反思最大重试次数
RAG_MAX_RETRY = 2

# 引擎快拒答话术（与 LLM 自拒答话术保持一致）
NO_ANSWER_TEXT = "根据现有文档无法回答该问题。"


def get_store(user: str = DEFAULT_USER) -> VectorStore:
    """获取指定用户的向量库（collection 隔离），进程内缓存。"""
    user = normalize_user(user)
    if user not in _stores:
        _stores[user] = VectorStore(user=user)
    return _stores[user]


def _get_bm25(user: str = DEFAULT_USER) -> Optional[tuple[BM25Retriever, list]]:
    """获取指定用户的 BM25 检索器（文档数变化时自动重建索引）。

    父子分块模式下只索引小块（chunk_type=child），与向量检索单元保持一致，
    排除零向量的父块。返回 (retriever, all_docs)；无文档时返回 None。
    """
    store = get_store(user)
    count = store.count()
    if count == 0:
        _bm25_cache.pop(user, None)
        return None

    cached = _bm25_cache.get(user)
    if cached and cached[0] == count:
        return cached[1], cached[2]

    # 重建 BM25 索引（父子分块模式下仅小块参与）
    all_docs = store.get_all_documents(chunk_type="child" if USE_PARENT_CHILD else None)
    if not all_docs and USE_PARENT_CHILD and count > 0:
        # 兼容：旧数据为单层分块（无 chunk_type 标记），回退取全量
        all_docs = store.get_all_documents()
    if not all_docs:
        return None
    texts = [d.page_content for d in all_docs]
    retriever = BM25Retriever(texts)
    _bm25_cache[user] = (count, retriever, all_docs)
    logger.info("BM25 索引重建: user=%s, docs=%d", user, len(all_docs))
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
) -> dict:
    """入库公共管线（ingest 与 ingest_path 在解析出全文后都走这里）。"""
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

    # 2. 章节标题：写入 metadata.heading，便于检索溯源到章节
    headings = document_loader.locate_headings(chunks, text, document_loader.extract_headings(text))

    # 3. 定位每块所在 PDF 页码（非 PDF 无页码概念，pages=None 不写 page 字段）
    pages = None
    if page_starts is not None:
        pages = document_loader.locate_pages_from_positions(positions, page_starts)

    # 4. 存入用户独立向量库（预计算 embedding + 直接写入 collection）
    cb(f"chunks:{len(chunks)}")
    store = get_store(user)
    # 文档级仲裁字段：doc_date > doc_version > ingested_at（自动生成兜底）
    doc_meta = build_doc_meta(doc_date=doc_date, doc_version=doc_version)
    if replace:
        # 同名替换：先删旧块（含父块），避免 Chroma 对重复 ID 静默跳过导致新旧混杂
        removed = store.delete_document(filename)
        if removed:
            cb(f"replacing:{removed}")
            logger.info("替换入库: 用户=%s, %s, 删除旧块 %d 个", user, filename, removed)
    if USE_PARENT_CHILD:
        count = store.add_parent_child_chunks(
            parents, child_records, source=filename, pages=pages,
            headings=headings, progress_callback=cb, doc_meta=doc_meta,
        )
    else:
        count = store.add_chunks_fast(
            chunks, source=filename, pages=pages, headings=headings,
            progress_callback=cb, doc_meta=doc_meta,
        )

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
    cb("parsing")
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        pages = document_loader.extract_pdf_pages_from_bytes(file_bytes, progress_callback=cb)
        text, page_starts = document_loader.build_page_text(pages)
    else:
        text = document_loader.extract_text(file_bytes, filename)
        page_starts = None

    return _ingest_pipeline(
        text, page_starts, filename, user, replace, doc_date, doc_version, cb
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
    cb("parsing")
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        pages = document_loader.extract_pdf_pages_from_path(path, progress_callback=cb)
        text, page_starts = document_loader.build_page_text(pages)
    else:
        text = document_loader.extract_text_from_path(path, filename)
        page_starts = None

    return _ingest_pipeline(
        text, page_starts, filename, user, replace, doc_date, doc_version, cb
    )


def list_documents(user: str = DEFAULT_USER) -> list[dict]:
    """列出指定用户知识库中的文档（文件名 + 小块数 + 父块数 + 最大页码）。"""
    return get_store(user).list_documents()


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
) -> dict:
    """问答流程：混合检索（小块）→ 重排 → 阈值拒答 → 父块回溯 → 拼 Prompt
    → LLM（含 Prompt Injection 标记）→ 蕴含裁判反思（混合重试）。

    Small-to-Big：向量/BM25 检索单元是小块（200 字，命中精准），
    命中后按 parent_id 回溯父块（1000 字）喂给 LLM（上下文完整）。

    参数:
        question: 用户问题。
        history: 对话历史。
        user: 用户名（X-User 请求头，决定检索哪个 collection）。

    返回:
        {
            "answer": str,
            "citations": [{"file", "chunk", "content", "page"(PDF 有), ...}],
            "reflection": dict,  # 正常路径含 passed/log；快拒答含 no_answer/reason/max_score
        }
    """
    store = get_store(user)

    # 历史压缩（超长时压缩早期对话为摘要）
    trimmed_history = await history_manager.compress_history(history or [])

    # 反思重试循环（混合重试：expand_retrieval 重跑检索 / retry_hint 塞纸条重写）
    retrieve_k = RETRIEVE_CANDIDATES
    retry_hint: Optional[str] = None
    used_adjustments: list[str] = []
    reflection_log: list[dict] = []

    for attempt in range(RAG_MAX_RETRY + 1):
        # 1. 混合检索：向量召回语义相关 + BM25 召回关键词精确匹配，取并集去重
        use_parent_backtrack = USE_PARENT_CHILD
        if USE_PARENT_CHILD:
            vector_candidates = store.similarity_search_children(question, k=retrieve_k)
            if not vector_candidates and store.count() > 0:
                # 兼容：旧数据为单层分块（无 chunk_type 标记），小块检索落空时
                # 回退单层全量检索，避免旧知识库被静默清空成"查不到"
                logger.warning(
                    "collection %s 无小块标记（旧单层数据），回退单层检索",
                    store.collection_name,
                )
                vector_candidates = store.similarity_search(question, k=retrieve_k)
                use_parent_backtrack = False
        else:
            vector_candidates = store.similarity_search(question, k=retrieve_k)

        bm25_candidates: List = []
        bm25_result = _get_bm25(user)
        if bm25_result is not None:
            bm25_retriever, all_docs = bm25_result
            bm25_top = bm25_retriever.search(question, k=retrieve_k)
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
        docs = await reranker.rerank(question, candidates, top_k=TOP_K)

        # 3. 阈值拒答：top_k 分数全低于阈值时跳过 LLM 直接拒答（省一次 LLM 调用）
        refusal = _threshold_refusal(docs)
        if refusal:
            logger.info(
                "RAG 引擎快拒答: reason=%s, max_score=%s (阈值=%.2f)",
                refusal["reason"], refusal["max_score"], RELEVANCE_THRESHOLD,
            )
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

        # 4. 父块回溯：Small-to-Big 检索命中小块后，取所属父块喂给 LLM
        #    （小块保检索精度，父块保上下文完整性；引用/拒答判断仍基于小块）
        if use_parent_backtrack:
            parent_ids = list(dict.fromkeys(  # 去重保序：多个小块命中同一父块只取一次
                d.metadata["parent_id"] for d in docs if d.metadata.get("parent_id")
            ))
            context_docs = store.get_parents(parent_ids) or docs  # 回溯失败降级用小块
            logger.info(
                "RAG 父块回溯: %d 个小块 → %d 个父块", len(docs), len(context_docs)
            )
        else:
            context_docs = docs

        # 4.5 Prompt Injection 检测：扫描检索片段是否含试图覆盖系统指令的文本
        for doc in docs:
            if detect_prompt_injection(doc.page_content):
                logger.warning(
                    "检测到疑似 Prompt Injection: source=%s, chunk=%s（依赖 system prompt 防护规则）",
                    doc.metadata.get("source"), doc.metadata.get("chunk_index"),
                )
                break

        # 5. 异步调 LLM（上下文用父块；重试时前置纸条 retry_hint）
        answer = await llm_client.chat(
            question, context_docs, history=trimmed_history, retry_hint=retry_hint
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
        reflection = await llm_entailment_check(question, answer, context_docs)

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
    return {
        "answer": answer,
        "citations": citations,
        "reflection": {"passed": False, "log": reflection_log},
    }
