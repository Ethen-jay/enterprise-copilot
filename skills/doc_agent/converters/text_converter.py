"""纯文本 (TXT) ↔ Markdown 转换。

TXT → Markdown：每个非空行作为独立段落（空行分隔），
    保证经 pandoc 转 DOCX 后每行对应一个段落。
Markdown → TXT：去除全部 Markdown 语法标记，
    表格转为制表符分隔的纯文本行（内容不丢失）。
"""

import re

# 表格分隔行：|---|:--:|---|
_TABLE_SEPARATOR = re.compile(r"^\|[\s\-:|]+\|$")

# 水平线：--- / *** / ___
_HORIZONTAL_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")

# 行首标记
_HEADING_PREFIX = re.compile(r"^\s{0,3}#{1,6}\s*")
_QUOTE_PREFIX = re.compile(r"^\s{0,3}>+\s?")
_LIST_PREFIX = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+")

# 行内语法
_INLINE_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BOLD_STAR = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_UNDER = re.compile(r"__([^_]+)__")
_ITALIC_STAR = re.compile(r"\*([^*]+)\*")
_ITALIC_UNDER = re.compile(r"(?<![A-Za-z0-9])_([^_]+)_(?![A-Za-z0-9])")
_INLINE_CODE = re.compile(r"`+([^`]+)`+")
_HTML_TAG = re.compile(r"<[^>]+>")
_CODE_FENCE = re.compile(r"^\s*(```+|~~~+)")


def text_to_markdown(text: str) -> str:
    """纯文本 → Markdown：非空行用空行分隔为独立段落。

    参数:
        text: 纯文本字符串。

    返回:
        Markdown 字符串。
    """
    lines = [line.strip() for line in text.splitlines()]
    paragraphs = [line for line in lines if line]
    return "\n\n".join(paragraphs).strip() + ("\n" if paragraphs else "")


def markdown_to_text(markdown_text: str) -> str:
    """Markdown → 纯文本：去除语法标记，保留全部文字内容。

    参数:
        markdown_text: Markdown 字符串。

    返回:
        纯文本字符串。
    """
    out_lines: list[str] = []
    in_code_block = False

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()

        # 代码块围栏：切换状态，围栏本身不输出
        if _CODE_FENCE.match(line):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            # 代码块内部原样保留
            out_lines.append(line)
            continue

        stripped = line.strip()
        if not stripped:
            out_lines.append("")
            continue

        # 水平线：整行删除
        if _HORIZONTAL_RULE.match(line):
            continue

        # 表格分隔行：删除
        if _TABLE_SEPARATOR.match(stripped):
            continue

        # 表格数据行：单元格转为制表符分隔
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            out_lines.append("\t".join(cells))
            continue

        # 去除行首结构标记
        line = _QUOTE_PREFIX.sub("", line)
        line = _HEADING_PREFIX.sub("", line)
        line = _LIST_PREFIX.sub("", line)

        # 去除行内语法（顺序：图片→链接→粗体→斜体→代码→HTML标签）
        line = _INLINE_IMAGE.sub(r"\1", line)
        line = _INLINE_LINK.sub(r"\1", line)
        line = _BOLD_STAR.sub(r"\1", line)
        line = _BOLD_UNDER.sub(r"\1", line)
        line = _ITALIC_STAR.sub(r"\1", line)
        line = _ITALIC_UNDER.sub(r"\1", line)
        line = _INLINE_CODE.sub(r"\1", line)
        line = _HTML_TAG.sub("", line)

        out_lines.append(line.strip())

    # 清理多余空行
    result = "\n".join(out_lines).strip()
    return result + "\n" if result else ""
