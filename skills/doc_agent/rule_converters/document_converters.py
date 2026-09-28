# converters/document_converters.py
"""文档转换：纯文本 / Markdown -> DOCX / PDF。

对外提供三个函数（接口与 main.py 的 /export 流程对应）：
    - text_to_markdown(text)                纯文本规整为 Markdown
    - markdown_to_docx(markdown_text, ...)  Markdown -> DOCX（pandoc）
    - markdown_to_pdf(markdown_text, ...)   Markdown -> PDF（WeasyPrint）
"""

import os
import tempfile
from typing import Optional

import markdown
from weasyprint import HTML

# Windows 下 WeasyPrint 依赖 GTK 运行时提供 Pango/cairo 等 DLL
_GTK_BIN = r"C:\Program Files\GTK3-Runtime Win64\bin"
_GTK_LOADED = False

# PDF 页面样式（A4、中文字体、代码块与表格）
PDF_CSS = """
@font-face {
    font-family: 'ChineseFont';
    src: url('file:///C:/Windows/Fonts/msyh.ttc');
}
@font-face {
    font-family: 'ChineseFontSerif';
    src: url('file:///C:/Windows/Fonts/simsun.ttc');
}
@page {
    size: A4;
    margin: 2cm;
    @bottom-center {
        content: counter(page) " / " counter(pages);
        font-size: 9pt;
        color: #888;
    }
}
body {
    font-family: 'ChineseFont', 'ChineseFontSerif', sans-serif;
    font-size: 12pt;
    line-height: 1.6;
}
h1, h2, h3, h4 {
    font-family: 'ChineseFont', 'ChineseFontSerif', sans-serif;
    line-height: 1.3;
}
code {
    font-family: 'Consolas', 'ChineseFont', monospace;
    background: #f4f4f4;
    padding: 2px 4px;
    border-radius: 4px;
    font-size: 0.9em;
}
pre {
    background: #f4f4f4;
    padding: 10px;
    border-radius: 5px;
    white-space: pre-wrap;
    overflow-wrap: break-word;
}
pre code {
    background: none;
    padding: 0;
    font-size: 1em;
}
table {
    border-collapse: collapse;
    width: 100%;
}
th, td {
    border: 1px solid #999;
    padding: 6px 10px;
}
"""


def _ensure_gtk_loaded() -> None:
    """Windows 下加载 GTK 运行时 DLL（仅加载一次）。"""
    global _GTK_LOADED
    if _GTK_LOADED or os.name != "nt":
        return
    if os.path.isdir(_GTK_BIN):
        os.add_dll_directory(_GTK_BIN)
    _GTK_LOADED = True


def _new_temp_path(suffix: str) -> str:
    """生成一个已关闭的临时文件路径，调用方负责写入内容。"""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def _ensure_parent_dir(output_path: str) -> None:
    """确保自定义输出路径的父目录存在。"""
    parent = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(parent, exist_ok=True)


def text_to_markdown(text: str) -> str:
    """将纯文本规整为 Markdown 文本。

    纯文本本身就是合法的 Markdown，这里只做轻量规整：
    去除每行行尾空白，并裁掉首尾空行，保证末尾恰有一个换行符。

    参数:
        text: 用户输入的原始文本。

    返回:
        规整后的 Markdown 文本。
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def _render_html(markdown_text: str) -> str:
    """将 Markdown 渲染为带页面样式的完整 HTML 文档。"""
    body = markdown.markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "sane_lists"],
    )
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        f"<style>{PDF_CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def markdown_to_pdf(
    markdown_text: str,
    output_path: Optional[str] = None,
) -> str:
    """将 Markdown 文本转换为 PDF。

    参数:
        markdown_text: Markdown 格式的文本。
        output_path: 输出 PDF 文件路径；为 None 时使用临时文件。

    返回:
        生成的 PDF 文件路径。
    """
    _ensure_gtk_loaded()

    if output_path is None:
        output_path = _new_temp_path(".pdf")
    else:
        _ensure_parent_dir(output_path)

    HTML(string=_render_html(markdown_text)).write_pdf(output_path)
    return output_path


def markdown_to_docx(
    markdown_text: str,
    output_path: Optional[str] = None,
) -> str:
    """将 Markdown 文本转换为 DOCX（基于 pandoc）。

    参数:
        markdown_text: Markdown 格式的文本。
        output_path: 输出 DOCX 文件路径；为 None 时使用临时文件。

    返回:
        生成的 DOCX 文件路径。

    异常:
        RuntimeError: 运行环境中找不到 pandoc 可执行文件。
    """
    try:
        import pypandoc
    except OSError as exc:  # pypandoc 初始化时找不到 pandoc 二进制
        raise RuntimeError(
            "未找到 pandoc，无法转换 DOCX；请安装 pypandoc-binary。"
        ) from exc

    if output_path is None:
        output_path = _new_temp_path(".docx")
    else:
        _ensure_parent_dir(output_path)

    pypandoc.convert_text(
        markdown_text,
        to="docx",
        format="markdown",
        outputfile=output_path,
        extra_args=["--standalone"],
    )
    return output_path
