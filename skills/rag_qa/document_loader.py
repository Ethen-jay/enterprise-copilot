"""文档解析：PDF 用 PyMuPDF，MD/TXT/JSON/JSONL 直接读取，统一返回纯文本。"""

import json
from pathlib import Path

import pymupdf  # PyMuPDF

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
