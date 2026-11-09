"""源文件 → Markdown 的统一抽取（Hub-Spoke 的 hub 入边）。

doc_agent 的转换状态机与 RAG 入库共用本模块：各格式的解析逻辑只在这里
调用一次 converter，避免两个技能各写一份 docx/xlsx/html 解析规则后漂移。
"""

from pathlib import Path
from typing import Optional

from skills.doc_agent.config import OUTPUT_DIR
from skills.doc_agent.converters import (
    docx_converter,
    excel_converter,
    html_converter,
    pdf_converter,
    text_converter,
)

# 支持从源格式直接抽出 Markdown 的格式（与 planner.FORMAT_ALIASES 对齐）
SOURCE_FORMATS = {"pdf", "docx", "html", "xlsx", "txt", "markdown", "md", "csv"}


def source_to_markdown(
    src_path: str,
    src: str,
    adjustment: Optional[str] = None,
    assets_dir: Optional[str | Path] = None,
    include_sheet_titles: bool = False,
) -> str:
    """把源文件解析为 Markdown 文本。

    参数:
        src_path: 源文件路径。
        src: 源格式名（planner 归一化后的值：pdf/docx/html/xlsx/txt/markdown）。
        adjustment: 重试调整策略；use_text_strategy 时 PDF 改用 text 表格策略。
        assets_dir: 抽取图片的落盘目录；None 时用 OUTPUT_DIR/assets。
        include_sheet_titles: xlsx 每个 sheet 前是否输出 "## sheet 名" 标题
            （RAG 入库按 sheet 分段时需要；doc_agent 默认关闭以保持原产物口径）。
    """
    assets = Path(assets_dir) if assets_dir is not None else OUTPUT_DIR / "assets"

    # 未知格式明确拒绝：历史上会走末尾的文本兜底，把 .bin 等二进制当文本“转换成功”
    if src not in SOURCE_FORMATS:
        raise ValueError(
            "不支持的文件格式：%s；支持 pdf/docx/html/xlsx/csv/txt/md" % (src or "未知")
        )

    if src == "pdf":
        pdf_table_settings = None
        if adjustment == "use_text_strategy":
            pdf_table_settings = {
                "vertical_strategy": "text",
                "horizontal_strategy": "text",
            }
        return pdf_converter.pdf_to_markdown(
            src_path,
            table_settings=pdf_table_settings,
            assets_dir=assets,
        )

    if src == "docx":
        return docx_converter.docx_to_markdown(src_path, assets_dir=assets)

    if src == "html":
        html = Path(src_path).read_text(encoding="utf-8")
        return html_converter.html_to_markdown(html)

    if src == "xlsx":
        return excel_converter.excel_to_markdown(
            src_path, include_sheet_titles=include_sheet_titles
        )

    if src == "txt":
        text = Path(src_path).read_text(encoding="utf-8")
        return text_converter.text_to_markdown(text)

    if src == "csv":
        return _csv_to_markdown(src_path)

    # markdown 及其它文本格式：直接读取
    return Path(src_path).read_text(encoding="utf-8")


def _csv_to_markdown(src_path: str) -> str:
    """CSV 转 Markdown 表格。

    过去 CSV 按纯文本读，转 docx 后多行会被并成一段，反思校验会判
    “段落数差异过大”而失败。按表格解析可保留行列结构，转换与校验口径一致。
    """
    import csv
    import io

    raw = Path(src_path).read_text(encoding="utf-8-sig")
    rows = [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(raw))
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]

    def _cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(_cell(c) for c in rows[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows[1:])
    return "\n".join(lines) + "\n"
