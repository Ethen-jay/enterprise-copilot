"""HTML ↔ Markdown 转换。"""

import markdown as md_lib
from markdownify import markdownify as md_to_html


def markdown_to_html(markdown_text: str) -> str:
    """Markdown → HTML。"""
    return md_lib.markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "sane_lists"],
    )


def html_to_markdown(html_text: str) -> str:
    """HTML → Markdown。"""
    return md_to_html(html_text, heading_style="ATX")
