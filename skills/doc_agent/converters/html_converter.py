"""HTML ↔ Markdown 转换。"""

import base64
import mimetypes
import re
from pathlib import Path
from typing import Optional

import markdown as md_lib
from markdownify import markdownify as md_to_html

_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', re.IGNORECASE)


def markdown_to_html(
    markdown_text: str,
    base_dir: Optional[str | Path] = None,
) -> str:
    """Markdown → HTML。

    参数:
        markdown_text: Markdown 文本。
        base_dir: 图片相对路径的基准目录；给出时把本地图片内嵌为 data URI，
            保证单独下载的 html 文件也能显示图片。None 时保留原始相对引用。
    """
    html = md_lib.markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "sane_lists"],
    )
    if base_dir is None:
        return html
    return _inline_local_images(html, Path(base_dir))


def _inline_local_images(html: str, base_dir: Path) -> str:
    """把 <img src="本地相对路径"> 替换为 data URI；外部链接与缺失文件原样保留。"""

    def replace(match: re.Match) -> str:
        src = match.group(2)
        if src.startswith(("data:", "http://", "https://")):
            return match.group(0)
        path = Path(src)
        if not path.is_absolute():
            path = base_dir / path
        if not path.is_file():
            return match.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"{match.group(1)}data:{mime};base64,{encoded}{match.group(3)}"

    return _IMG_SRC_RE.sub(replace, html)


def html_to_markdown(html_text: str) -> str:
    """HTML → Markdown。"""
    return md_to_html(html_text, heading_style="ATX")
