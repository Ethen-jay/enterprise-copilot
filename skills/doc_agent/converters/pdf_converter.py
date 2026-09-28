"""PDF → Markdown 转换（pdfplumber，保留表格）。"""

from pathlib import Path
from typing import Optional

import pdfplumber


def pdf_to_markdown(
    pdf_path: str | Path,
    table_settings: Optional[dict] = None,
) -> str:
    """将 PDF 转换为 Markdown，尽量还原标题、段落和表格。

    参数:
        pdf_path: PDF 文件路径。
        table_settings: pdfplumber extract_tables 的参数。
            None → 默认参数。
            {"vertical_strategy": "text", "horizontal_strategy": "text"} → 备选策略。

    返回:
        Markdown 字符串。
    """
    parts: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # 先提取表格（用 table_settings 参数）
            if table_settings:
                tables = page.extract_tables(table_settings=table_settings) or []
            else:
                tables = page.extract_tables() or []
            table_regions = []
            for table in tables:
                if not table:
                    continue
                md_table = _table_to_markdown(table)
                parts.append(md_table)
                # 记录表格所在区域，避免文本重复提取
                # （简化处理：表格和文本分别提取，可能有少量重复，后续可优化）

            # 提取文本：逐行加入并用空行分隔，
            # 保证经枢纽转 HTML/DOCX 时每行对应独立段落
            text = page.extract_text() or ""
            for line in text.splitlines():
                if line.strip():
                    parts.append(line.strip())

    # 合并并清理
    md = "\n\n".join(p for p in parts if p.strip())
    return _clean_markdown(md)


def _table_to_markdown(table: list[list]) -> str:
    """将 pdfplumber 提取的表格转为 Markdown 表格。"""
    if not table or not table[0]:
        return ""

    # 清理单元格：None → 空字符串，去除换行
    cleaned = []
    for row in table:
        cleaned.append(
            [str(cell).replace("\n", " ").strip() if cell is not None else "" for cell in row]
        )

    # 取最大列数
    max_cols = max(len(row) for row in cleaned)
    for row in cleaned:
        while len(row) < max_cols:
            row.append("")

    # 第一行作为表头
    header = cleaned[0]
    separator = ["---"] * max_cols
    body = cleaned[1:]

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _clean_markdown(text: str) -> str:
    """清理 Markdown 文本，去除多余空行。"""
    lines = text.splitlines()
    result = []
    prev_empty = False
    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            if not prev_empty:
                result.append("")
            prev_empty = True
        else:
            result.append(stripped)
            prev_empty = False
    return "\n".join(result).strip() + "\n"
