"""文档解析：PDF 用 PyMuPDF，MD/TXT/JSON/JSONL 直接读取，统一返回纯文本。

PDF 支持按页抽取并记录每页起始偏移，供下游把分块定位回页码
（chunk metadata 存 page，引用可展示"文件 · 第N页"）。
扫描版 PDF（无文本层的图片页）用 RapidOCR 做 OCR 兜底，按页判断：
有文本层的页直接用文本层（快），仅扫描页走 OCR（支持混合 PDF）。
"""

import json
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import pymupdf  # PyMuPDF

from skills.rag_qa.config import ENABLE_OCR, OCR_DPI, OCR_MIN_CHARS

SUPPORTED_EXT = {".pdf", ".md", ".markdown", ".txt", ".json", ".jsonl"}


def extract_text(file_bytes: bytes, filename: str) -> str:
    """从文档字节内容中抽取纯文本（不写磁盘）。

    参数:
        file_bytes: 文档的字节内容。
        filename: 文件名（用于判断格式）。

    返回:
        纯文本字符串。

    异常:
        ValueError: 不支持的文件格式。
    """
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        return _extract_pdf(file_bytes)
    elif ext in (".md", ".markdown", ".txt"):
        return file_bytes.decode("utf-8")
    elif ext in (".json", ".jsonl"):
        return _extract_json(file_bytes)
    else:
        raise ValueError(f"不支持的文件格式: {ext}，支持: {SUPPORTED_EXT}")


def extract_text_from_path(path: str, filename: str) -> str:
    """从磁盘文件抽取纯文本（大文件落盘模式：PDF 直接从路径打开，不全量进内存）。

    参数:
        path: 服务器上的临时文件路径。
        filename: 原始文件名（用于判断格式）。

    返回:
        纯文本字符串。

    异常:
        ValueError: 不支持的文件格式。
    """
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        return "\n".join(extract_pdf_pages_from_path(path))
    elif ext in (".md", ".markdown", ".txt"):
        return Path(path).read_text(encoding="utf-8")
    elif ext in (".json", ".jsonl"):
        return _extract_json(Path(path).read_bytes())
    else:
        raise ValueError(f"不支持的文件格式: {ext}，支持: {SUPPORTED_EXT}")


# ----------------------------------------------------------------------
# PDF 分页文本：保留页边界，供引用溯源到原文页码
# ----------------------------------------------------------------------
def extract_pdf_pages_from_path(
    path: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """从磁盘 PDF 抽取分页文本（每页一个字符串），扫描页自动 OCR 兜底。"""
    with pymupdf.open(path) as doc:
        return _extract_pages(doc, progress_callback)


def extract_pdf_pages_from_bytes(
    data: bytes,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """从字节内容抽取 PDF 分页文本（每页一个字符串），扫描页自动 OCR 兜底。"""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return _extract_pages(doc, progress_callback)


def _extract_pages(doc, progress_callback: Optional[Callable[[str], None]]) -> List[str]:
    """逐页抽取文本；页字符数低于 OCR_MIN_CHARS 且页面含图像时走 OCR。

    有文本层的页直接用文本层（快），纯空白页（无图）不跑 OCR，
    支持 文本/扫描 混合 PDF；OCR 结果字符数更多才采纳（防丢内容）。
    """
    cb = progress_callback or (lambda _: None)
    pages_text: list[str] = []
    total = len(doc)
    for i, page in enumerate(doc):
        text = page.get_text()
        if (
            ENABLE_OCR
            and len(text.strip()) < OCR_MIN_CHARS
            and page.get_images()  # 纯空白页（无图）不跑 OCR
        ):
            cb(f"ocr:{i + 1}/{total}")
            ocr_text = _ocr_page(page)
            # OCR 识别出更多字符才采纳，避免低文字页误触发 OCR 反而丢内容
            if len(ocr_text.strip()) > len(text.strip()):
                text = ocr_text
        pages_text.append(text)
    return pages_text


# RapidOCR 引擎懒加载单例（模型加载慢，进程内只初始化一次）
_ocr_engine = None


def _get_ocr_engine():
    """懒加载 RapidOCR（PP-OCRv4 det/rec，onnxruntime CPU 推理）。"""
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_page(page) -> str:
    """渲染单页为图片并 OCR，返回识别文本（每行一条）。"""
    pix = page.get_pixmap(dpi=OCR_DPI)
    result, _ = _get_ocr_engine()(pix.tobytes("png"))
    if not result:
        return ""
    return "\n".join(item[1] for item in result)


def build_page_text(pages: list[str]) -> tuple[str, list[int]]:
    """把分页文本拼成全文，并记录每页起始字符偏移。

    返回:
        (全文文本, page_starts)：page_starts[i] 为第 i+1 页在全文中的起始下标
        （页与页之间用单个换行拼接）。
    """
    starts: list[int] = []
    parts: list[str] = []
    pos = 0
    for page in pages:
        starts.append(pos)
        parts.append(page)
        pos += len(page) + 1  # +1 是页间拼接的换行符
    return "\n".join(parts), starts


def locate_pages(chunks: list[str], text: str, page_starts: list[int]) -> list[int]:
    """定位每个分块落在全文的第几页（1 起）。

    分块按顺序在全文中查找（带 overlap 时同一文本会重复出现，
    游标只前进 1 字符即可保证按序推进）；查不到时（空白被裁剪等）沿用上一块页码。
    """
    import bisect

    result: list[int] = []
    cursor = 0
    last_page = 1
    for chunk in chunks:
        target = (chunk or "").strip()
        pos = text.find(target, cursor) if target else -1
        if pos < 0:
            pos = cursor  # 找不到就用游标位置近似定位
        # pos 落在哪个页区间：bisect_right(page_starts, pos) 即 1 起页码
        page = max(1, bisect.bisect_right(page_starts, pos))
        result.append(page)
        last_page = page
        cursor = max(pos + 1, cursor + 1)
    return result


def locate_pages_from_positions(positions: list[int], page_starts: list[int]) -> list[int]:
    """按精确起始字符位置定位页码（1 起），供父子分块的小块定位页。

    分块器已返回每块的 start_index（精确位置），无需再在全文中查找，
    比 locate_pages 的 find 游标推进更快且不受空白裁剪影响。
    """
    import bisect

    return [max(1, bisect.bisect_right(page_starts, pos)) for pos in positions]


def _extract_json(data: bytes) -> str:
    """解析 JSON 并转为可读的纯文本（保留键值对结构）。

    支持单对象 JSON、JSON 数组、以及多行 JSONL 格式。
    """
    text = data.decode("utf-8").strip()
    lines = []
    decoder = json.JSONDecoder()
    pos = 0

    while pos < len(text):
        # 跳过空白字符和换行
        while pos < len(text) and text[pos] in " \t\r\n":
            pos += 1
        if pos >= len(text):
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
            _flatten_json(obj, prefix="", lines=lines)
            pos = end
        except json.JSONDecodeError:
            # 无法解析剩余内容，按行处理
            remaining = text[pos:].strip()
            if remaining:
                lines.append(remaining[:500])
            break

    if not lines:
        # 兜底：直接返回原始文本
        lines.append(text[:2000])

    return "\n".join(lines)


def _flatten_json(obj, prefix: str, lines: list[str]):
    """递归展平 JSON 对象为 key: value 文本行。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            _flatten_json(v, key, lines)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            key = f"{prefix}[{i}]"
            _flatten_json(item, key, lines)
    else:
        lines.append(f"{prefix}: {obj}")


def _extract_pdf(data: bytes) -> str:
    """用 PyMuPDF 从字节内容抽取 PDF 全文。"""
    text_parts: list[str] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            text_parts.append(page.get_text())
    return "\n".join(text_parts)


# ----------------------------------------------------------------------
# 标题/章节提取：把 Markdown # 标题或"第X章/1."等标题行抽取出来，
# 随分块写入 metadata.heading，便于检索时溯源到章节、引用更精准。
# ----------------------------------------------------------------------
import re as _re

# Markdown 标题行：# 到 ###### 开头
_MD_HEADING_RE = _re.compile(r'^(#{1,6})\s+(.+?)\s*#*\s*$')
# 常见中文标题：第X章/第X节/一、二、/1. 2. （短行+无句末标点）
_CN_HEADING_RE = _re.compile(r'^(第[一二三四五六七八九十百零〇\d]+[章节篇部])\s*(.*)$')
_NUM_HEADING_RE = _re.compile(r'^(\d+(\.\d+)*)\s+(.{2,30})$')


def extract_headings(text: str) -> list[tuple[int, str]]:
    """从全文中提取标题列表，返回 [(字符偏移, 标题文本), ...]。

    支持：Markdown # 标题、中文"第X章/节"、数字编号"1. xxx"。
    用于分块时定位每个 chunk 所属章节，写入 metadata.heading。
    """
    headings: list[tuple[int, str]] = []
    pos = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            pos += len(line) + 1
            continue
        # Markdown 标题
        m = _MD_HEADING_RE.match(stripped)
        if m:
            headings.append((pos, m.group(2).strip()))
            pos += len(line) + 1
            continue
        # 中文第X章/节
        m = _CN_HEADING_RE.match(stripped)
        if m and len(stripped) < 40:
            title = (m.group(1) + " " + m.group(2)).strip()
            headings.append((pos, title))
            pos += len(line) + 1
            continue
        # 数字编号标题（短行，无句末标点）
        m = _NUM_HEADING_RE.match(stripped)
        if m and len(stripped) < 50 and not stripped.endswith(("。", "！", "？", ".", "!", "?")):
            headings.append((pos, m.group(3).strip()))
            pos += len(line) + 1
            continue
        pos += len(line) + 1
    return headings


def locate_headings(chunks: list[str], text: str, headings: list[tuple[int, str]]) -> list[str]:
    """定位每个分块所属的最近标题（按字符偏移二分查找）。

    返回与 chunks 等长的标题列表，无标题时为空字符串。
    """
    import bisect

    if not headings:
        return ["" for _ in chunks]

    offsets = [h[0] for h in headings]
    titles = [h[1] for h in headings]
    result: list[str] = []
    cursor = 0
    for chunk in chunks:
        target = (chunk or "").strip()
        pos = text.find(target, cursor) if target else -1
        if pos < 0:
            pos = cursor
        # 找 pos 之前最近的标题
        idx = bisect.bisect_right(offsets, pos) - 1
        result.append(titles[idx] if idx >= 0 else "")
        cursor = max(pos + 1, cursor + 1)
    return result
