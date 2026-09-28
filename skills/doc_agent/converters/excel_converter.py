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
        for i, line in enumerate(markdown_text.split("\n"), 1):
            ws.cell(row=i, column=1, value=line)
    else:
        for idx, table in enumerate(tables):
            ws = wb.create_sheet(f"表格{idx + 1}")
            for row_idx, row in enumerate(table, 1):
                for col_idx, cell in enumerate(row, 1):
                    ws.cell(row=row_idx, column=col_idx, value=cell)

    wb.save(output_path)
    return output_path


def excel_to_markdown(xlsx_path: str | Path) -> str:
    """Excel → Markdown：每个 sheet 转为一个 Markdown 表格。

    参数:
        xlsx_path: xlsx 文件路径。

    返回:
        Markdown 字符串。
    """
    from openpyxl import load_workbook

    wb = load_workbook(xlsx_path, data_only=True)
    parts = []
    # 仅多 sheet 时输出 sheet 标题，避免单 sheet 转出多余段落
    multi_sheet = len(wb.worksheets) > 1

    for ws in wb.worksheets:
        if multi_sheet and ws.title:
            parts.append(f"## {ws.title}\n")

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        # 转为 Markdown 表格
        table = []
        for row in rows:
            row_cells = [str(cell) if cell is not None else "" for cell in row]
            if any(c.strip() for c in row_cells):
                table.append(row_cells)

        if table:
            parts.append(_table_to_markdown(table))
        parts.append("")

    return "\n".join(parts).strip() + "\n"


def docx_tables_to_excel(tables: list[list[list[str]]], output_path: Optional[str] = None) -> str:
    """DOCX 表格数据 → Excel：每个表格写入一个 sheet。

    参数:
        tables: 表格列表，每个表格是二维列表。
        output_path: 输出路径。

    返回:
        输出 xlsx 路径。
    """
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
    else:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    wb.remove(wb.active)

    if not tables:
        ws = wb.create_sheet("空")
        ws.cell(row=1, column=1, value="未检测到表格")
    else:
        for idx, table in enumerate(tables):
            ws = wb.create_sheet(f"表格{idx + 1}")
            for row_idx, row in enumerate(table, 1):
                for col_idx, cell in enumerate(row, 1):
                    ws.cell(row=row_idx, column=col_idx, value=cell)

    wb.save(output_path)
    return output_path


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
