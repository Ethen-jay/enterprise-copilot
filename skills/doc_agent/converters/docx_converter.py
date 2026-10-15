"""DOCX ↔ Markdown ↔ PDF 转换。

DOCX → Markdown：用 python-docx 提取段落和表格。
Markdown → DOCX：用 pandoc（通过 pypandoc）。
DOCX/Markdown → PDF：用 fpdf2 生成（支持中文）。
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from docx import Document

# Windows 系统中文字体路径
_CN_FONT = "C:/Windows/Fonts/msyh.ttc"


def docx_to_markdown(docx_path: str | Path) -> str:
    """将 DOCX 转换为 Markdown（段落 + 表格）。

    参数:
        docx_path: DOCX 文件路径。

    返回:
        Markdown 字符串。
    """
    doc = Document(docx_path)
    parts: list[str] = []

    for block in doc.element.body.iterchildren():
        tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag

        if tag == "p":
            # 段落：只提取 w:t 文本节点，避免重复
            text = "".join(
                node.text or ""
                for node in block.iter()
                if node.tag.endswith("}t") and node.text
            )
            text = text.strip()
            if text:
                parts.append(text)
        elif tag == "tbl":
            # 表格
            table = _extract_table(block)
            if table:
                parts.append(_table_to_markdown(table))

    return "\n\n".join(parts).strip() + "\n"


def _extract_table(tbl_element) -> list[list[str]]:
    """从 DOCX XML 表格元素提取二维列表。"""
    table = []
    for row in tbl_element.iterchildren():
        row_tag = row.tag.split("}")[-1] if "}" in row.tag else row.tag
        if row_tag != "tr":
            continue
        cells = []
        for cell in row.iterchildren():
            cell_tag = cell.tag.split("}")[-1] if "}" in cell.tag else cell.tag
            if cell_tag != "tc":
                continue
            # 只提取 w:t 文本节点
            text = "".join(
                node.text or ""
                for node in cell.iter()
                if node.tag.endswith("}t") and node.text
            )
            cells.append(text.strip())
        if cells:
            table.append(cells)
    return table


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


# 列表项行：- / * / + / 1. / 1) / 1、 开头
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+")


def _normalize_lists_before_pandoc(text: str) -> str:
    """列表块与前后正文段之间补空行（pandoc 前的规范化）。

    Markdown 里列表紧跟正文段（无空行）时，pandoc 按"懒续行"把列表项
    合并进上一段，Word 里列表会退化成一坨普通文本（段落计数也对不上）。
    补空行让 pandoc 把列表解析为真正的列表结构：每个列表项在 docx 中
    是独立段落（Word 编号/项目符号），与 Markdown"非空行=段落"的
    校验口径一致。
    """
    lines = text.split("\n")
    out: list[str] = []
    prev_is_list = False
    prev_is_blank = True  # 文件开头视为空行
    for line in lines:
        is_blank = not line.strip()
        is_list = bool(_LIST_ITEM_RE.match(line))
        is_indented = bool(line) and line[0] in (" ", "\t")  # 缩进行=列表项内容，不切断
        # 正文段 → 列表：补空行
        if is_list and not prev_is_blank and not prev_is_list and not is_indented:
            out.append("")
        # 列表 → 非缩进正文段：补空行
        if not is_list and not is_blank and not is_indented and prev_is_list:
            out.append("")
        out.append(line)
        prev_is_list = is_list
        prev_is_blank = is_blank
    return "\n".join(out)


def markdown_to_docx(markdown_text: str, output_path: Optional[str] = None) -> str:
    """Markdown → DOCX（pandoc）。

    参数:
        markdown_text: Markdown 文本。
        output_path: 输出路径，None 时用临时文件。

    返回:
        输出 DOCX 路径。
    """
    import pypandoc

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
    else:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    pypandoc.convert_text(
        _normalize_lists_before_pandoc(markdown_text),
        to="docx",
        format="markdown",
        outputfile=output_path,
        extra_args=["--standalone"],
    )
    return output_path


def docx_to_pdf(docx_path: str | Path, output_path: Optional[str] = None) -> str:
    """DOCX → PDF：先提取为 Markdown 文本，再用 fpdf2 生成 PDF。

    参数:
        docx_path: DOCX 文件路径。
        output_path: 输出路径，None 时用临时文件。

    返回:
        输出 PDF 路径。
    """
    md_text = docx_to_markdown(docx_path)
    return markdown_to_pdf(md_text, output_path)


def markdown_to_pdf(markdown_text: str, output_path: Optional[str] = None) -> str:
    """Markdown → PDF：用 fpdf2 生成，支持中文。

    参数:
        markdown_text: Markdown 文本。
        output_path: 输出路径，None 时用临时文件。

    返回:
        输出 PDF 路径。
    """
    from fpdf import FPDF

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
    else:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font("msyh", "", _CN_FONT, uni=True)
    pdf.set_font("msyh", size=11)
    pdf.add_page()

    # 按行解析 Markdown
    lines = markdown_text.split("\n")
    in_table = False

    for line in lines:
        line = line.rstrip()

        # 空行
        if not line:
            if in_table:
                in_table = False
            pdf.ln(4)
            continue

        # Markdown 表格分隔行（|---|---|）
        if re.match(r"^\|[\s\-:|]+\|$", line):
            continue

        # Markdown 表格行
        if line.startswith("|") and line.endswith("|"):
            in_table = True
            cells = [c.strip() for c in line.strip("|").split("|")]
            col_count = len(cells)
            if col_count == 0:
                continue
            page_width = pdf.w - 2 * pdf.l_margin
            col_width = page_width / col_count
            pdf.set_font_size(9)
            for cell_text in cells:
                # 截断过长内容避免溢出
                safe_text = cell_text[:50] if len(cell_text) > 50 else cell_text
                pdf.cell(col_width, 7, safe_text, border=1, align="L")
            pdf.ln(7)
            pdf.set_font_size(11)
            continue

        # 标题
        if line.startswith("###"):
            pdf.set_font("msyh", size=13)
            pdf.multi_cell(0, 8, line.lstrip("#").strip())
            pdf.set_font("msyh", size=11)
        elif line.startswith("##"):
            pdf.set_font("msyh", size=15)
            pdf.multi_cell(0, 10, line.lstrip("#").strip())
            pdf.set_font("msyh", size=11)
        elif line.startswith("#"):
            pdf.set_font("msyh", size=17)
            pdf.multi_cell(0, 11, line.lstrip("#").strip())
            pdf.set_font("msyh", size=11)
        else:
            pdf.multi_cell(0, 7, line)

    pdf.output(output_path)
    return output_path
