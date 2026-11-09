"""Excel (xlsx) 转换模块。

Markdown → Excel：解析 Markdown 表格写入 xlsx。
Excel → Markdown：读取 xlsx 转为 Markdown 表格。
DOCX → Excel：提取 DOCX 表格写入 xlsx。
PDF → Excel：提取 PDF 表格写入 xlsx。
"""

import re
import tempfile
import os
from pathlib import Path
from typing import Optional

from openpyxl import Workbook

# 独占一行的图片引用：xlsx 单元格不承载图片，正文 sheet 里不写占位行
_IMAGE_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")


def markdown_to_excel(markdown_text: str, output_path: Optional[str] = None) -> str:
    """Markdown → Excel：提取所有表格写入 xlsx，每个表格一个 sheet。

    参数:
        markdown_text: Markdown 文本。
        output_path: 输出路径，None 时用临时文件。

    返回:
        输出 xlsx 路径。
    """
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
    else:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    # 删除默认 sheet
    wb.remove(wb.active)

    # 提取所有 Markdown 表格
    tables = _extract_markdown_tables(markdown_text)
    if not tables:
        # 没有表格，把全文写入一个 sheet
        ws = wb.create_sheet("内容")
        row_idx = 0
        for line in markdown_text.split("\n"):
            if _IMAGE_LINE.match(line):
                continue
            row_idx += 1
            ws.cell(row=row_idx, column=1, value=line)
    else:
        for idx, table in enumerate(tables):
            ws = wb.create_sheet(f"表格{idx + 1}")
            for row_idx, row in enumerate(table, 1):
                for col_idx, cell in enumerate(row, 1):
                    ws.cell(row=row_idx, column=col_idx, value=cell)

        # 表格之外的正文写入独立 sheet，避免混排文档转换时丢失正文
        body_lines = [
            line for line in markdown_text.split("\n")
            if line.strip()
            and not _IMAGE_LINE.match(line)
            and not (line.strip().startswith("|") and line.strip().endswith("|"))
        ]
        if body_lines:
            ws = wb.create_sheet("正文")
            for row_idx, line in enumerate(body_lines, 1):
                ws.cell(row=row_idx, column=1, value=line)

    wb.save(output_path)
    return output_path


def excel_to_markdown(
    xlsx_path: str | Path,
    include_sheet_titles: bool = False,
) -> str:
    """Excel → Markdown：单列 sheet 还原为正文段落，多列 sheet 转为表格。

    参数:
        xlsx_path: xlsx 文件路径。
        include_sheet_titles: 为每个 sheet 输出 "## sheet 名" 标题。RAG 入库
            用它把每个 sheet 切成独立的来源段落；转换技能保持默认 False，
            产物口径与原来一致。

    返回:
        Markdown 字符串。
    """
    from openpyxl import load_workbook

    wb = load_workbook(xlsx_path, data_only=True)
    parts = []

    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        table = []
        for row in rows:
            row_cells = [str(cell) if cell is not None else "" for cell in row]
            if any(c.strip() for c in row_cells):
                table.append(row_cells)

        if not table:
            continue

        # 与 _parse_xlsx 同一分类规则：单列行是正文，多列行是表格
        max_non_empty = max(len([c for c in r if c.strip()]) for r in table)
        if max_non_empty <= 1:
            if include_sheet_titles and ws.title:
                parts.append(f"## {ws.title}")
                parts.append("")
            for row_cells in table:
                text = next((c.strip() for c in row_cells if c.strip()), "")
                if text:
                    parts.append(text)
                    parts.append("")
        else:
            if include_sheet_titles and ws.title:
                parts.append(f"## {ws.title}")
                parts.append("")
            parts.append(_table_to_markdown(table))
            parts.append("")

    return "\n".join(parts).strip() + "\n"


def _extract_markdown_tables(text: str) -> list[list[list[str]]]:
    """从 Markdown 文本中提取所有表格。

    返回:
        表格列表，每个表格是二维列表（含表头）。
    """
    tables = []
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        # 检测表格起始
        if line.startswith("|") and line.endswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table_lines.append(lines[i].strip())
                i += 1

            # 解析表格行
            table = []
            for tl in table_lines:
                # 跳过分隔行 |---|---|
                if re.match(r"^\|[\s\-:|]+\|$", tl):
                    continue
                cells = [c.strip() for c in tl.strip("|").split("|")]
                table.append(cells)

            if len(table) >= 1:
                tables.append(table)
        else:
            i += 1

    return tables


def _table_to_markdown(table: list[list[str]]) -> str:
    """二维列表转 Markdown 表格。"""
    if not table:
        return ""
    max_cols = max(len(row) for row in table)
    for row in table:
        while len(row) < max_cols:
            row.append("")
    header = table[0]
    separator = ["---"] * max_cols
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in table[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)
