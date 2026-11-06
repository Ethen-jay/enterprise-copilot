"""DOCX ↔ Markdown ↔ PDF 转换。

DOCX → Markdown：用 python-docx 提取段落和表格。
Markdown → DOCX：用 pandoc（通过 pypandoc）。
DOCX/Markdown → PDF：用 fpdf2 生成（支持中文）。
"""

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from docx import Document
from docx.oxml.ns import qn

logger = logging.getLogger(__name__)

# Windows 系统中文字体路径
_CN_FONT = "C:/Windows/Fonts/msyh.ttc"

# 能落成独立图片文件、并被 pandoc / fpdf / 浏览器接受的图片格式。
# EMF/WMF 是矢量图，Pillow 与 fpdf2 都不能渲染，直接跳过并记日志。
_DOCX_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}


def docx_to_markdown(docx_path: str | Path, assets_dir: Optional[str | Path] = None) -> str:
    """将 DOCX 转换为 Markdown（段落 + 表格 + 图片）。

    图片按文档顺序抽取到 `assets_dir`，在 Markdown 里用 `![图片](assets/xxx.png)`
    表示，位置保持与原文阅读顺序一致。

    参数:
        docx_path: DOCX 文件路径。
        assets_dir: 图片落盘目录；None 时抽取的图片会被丢弃（不影响文本）。

    返回:
        Markdown 字符串。
    """
    doc = Document(docx_path)
    parts: list[str] = []

    assets_path: Optional[Path] = None
    if assets_dir is not None:
        assets_path = Path(assets_dir)
        assets_path.mkdir(parents=True, exist_ok=True)

    image_seq = 0

    for block in doc.element.body.iterchildren():
        tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag

        if tag == "p":
            # 按 XML 顺序遍历：文本 run 与图片 run 交替出现时保持阅读顺序
            buffer: list[str] = []
            for node in block.iter():
                if node.tag.endswith("}t") and node.text:
                    buffer.append(node.text)
                    continue
                if not node.tag.endswith("}blip"):
                    continue

                text = "".join(buffer).strip()
                if text:
                    parts.append(text)
                buffer = []

                ref = _save_docx_image(doc, node, assets_path, image_seq + 1)
                if ref:
                    image_seq += 1
                    parts.append(f"![图片]({ref})")

            text = "".join(buffer).strip()
            if text:
                parts.append(text)
        elif tag == "tbl":
            # 表格
            table = _extract_table(block)
            if table:
                parts.append(_table_to_markdown(table))

    return "\n\n".join(parts).strip() + "\n"


def _save_docx_image(doc, blip, assets_dir: Optional[Path], seq: int) -> Optional[str]:
    """把 docx 里一个图片引用存成文件，返回 Markdown 用的相对路径。

    文件名只由文档内序号决定（`docx_p{n}.png`），不掺上传时的临时文件名；
    与 PDF 的 `p{页}_{序}.png` 前缀不同，两类资源落在同一 assets/ 也不会互相覆盖。
    """
    if assets_dir is None:
        return None

    rid = blip.get(qn("r:embed"))
    if not rid:
        logger.warning("docx 图片为外部链接引用（r:link），未抽取")
        return None

    part = doc.part.related_parts.get(rid)
    if part is None:
        logger.warning("docx 图片关系缺失: rId=%s", rid)
        return None

    ext = Path(str(part.partname)).suffix.lower()
    if ext not in _DOCX_IMAGE_EXTS:
        logger.warning("docx 图片格式 %s 无法渲染，已跳过", ext or "未知")
        return None

    filename = f"docx_p{seq}{ext}"
    try:
        (assets_dir / filename).write_bytes(part.blob)
    except OSError as exc:
        logger.warning("docx 图片落盘失败 %s: %s", filename, exc)
        return None
    return f"{assets_dir.name}/{filename}"


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

# 独占一行的图片引用：![alt](path)
_IMAGE_LINE_RE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$")


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


def markdown_to_docx(
    markdown_text: str,
    output_path: Optional[str] = None,
    assets_dir: Optional[str | Path] = None,
) -> str:
    """Markdown → DOCX（pandoc）。

    Markdown 里的图片引用（如 `![图片](assets/p1_1.png)`）由 pandoc 按
    `--resource-path` 解析后嵌入 docx；嵌入后按正文可用宽度等比缩放，
    避免宽图超出页边距被裁掉。

    参数:
        markdown_text: Markdown 文本。
        output_path: 输出路径，None 时用临时文件。
        assets_dir: 图片相对路径的基准目录；None 时用输出文件所在目录。

    返回:
        输出 DOCX 路径。
    """
    import pypandoc

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
    else:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # 图片基准目录由调用方决定：PDF/DOCX 抽取的资源在 OUTPUT_DIR/assets，
    # 而上传来的 .md/.html 只能相对它自己的目录解析（否则会误用残留资源）
    resource_dir = Path(assets_dir).resolve() if assets_dir else Path(output_path).resolve().parent

    pypandoc.convert_text(
        _normalize_lists_before_pandoc(
            _drop_missing_images(markdown_text, resource_dir)
        ),
        to="docx",
        # 关闭 implicit_figures：默认会把"独占一段的图片"转成 figure + 说明文字
        # （alt 文本变成 CaptionedFigure/ImageCaption 两个段落），既多出无意义
        # 的"图片"说明段、又让段落数比源文件多，口径直接漂掉。
        format="markdown-implicit_figures",
        outputfile=output_path,
        extra_args=["--standalone", f"--resource-path={resource_dir}"],
    )
    _fit_docx_images(output_path)
    return output_path


def _drop_missing_images(markdown_text: str, base_dir: Path) -> str:
    """剔除解析不到本地文件的图片引用行。

    pandoc 找不到图片时会把图片替换成 alt 文本，凭空多出一行“图片”正文，
    段落口径随之漂移；这里直接跳过这类引用（打日志），口径保持稳定。
    """
    kept: list[str] = []
    for line in markdown_text.split("\n"):
        match = _IMAGE_LINE_RE.match(line)
        if match:
            target = Path(match.group(2).strip())
            path = target if target.is_absolute() else base_dir / target
            if not path.is_file():
                logger.warning("图片引用无法解析，已跳过: %s", match.group(2))
                continue
        kept.append(line)
    return "\n".join(kept)


def _fit_docx_images(docx_path: str | Path) -> None:
    """把 docx 内联图片等比缩放到正文宽度以内。

    pandoc 默认按图片像素尺寸（96dpi）排版，高分辨率截图会撑出页边距。
    只缩小不放大，保持原始宽高比。
    """
    from docx.shared import Cm

    doc = Document(str(docx_path))
    shapes = list(doc.inline_shapes)
    if not shapes:
        return

    # pandoc 生成的 docx 可能没有显式 sectPr（page_width 为 None），
    # 此时按 Word 默认 A4（21cm）减左右 2.54cm 页边距的保守宽度处理。
    usable_width = Cm(15.9)
    section = doc.sections[0]
    if (
        section.page_width is not None
        and section.left_margin is not None
        and section.right_margin is not None
    ):
        usable_width = (
            section.page_width - section.left_margin - section.right_margin
        )

    changed = False
    for shape in shapes:
        width = shape.width
        if not width or width <= usable_width:
            continue
        ratio = usable_width / width
        shape.width = int(usable_width)
        shape.height = int(shape.height * ratio)
        changed = True

    if changed:
        doc.save(str(docx_path))


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


def markdown_to_pdf(
    markdown_text: str,
    output_path: Optional[str] = None,
    assets_dir: Optional[str | Path] = None,
) -> str:
    """Markdown → PDF：用 fpdf2 生成，支持中文。

    参数:
        markdown_text: Markdown 文本。
        output_path: 输出路径，None 时用临时文件。

    返回:
        输出 PDF 路径。
    """
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    # multi_cell 默认 new_x=RIGHT，会把光标停在右边距；下一行若紧跟着
    # 非空文本（如 html→md 的连续编号列表项），可用宽度就是 0，fpdf 直接
    # 报 "Not enough horizontal space to render a single character"。
    # 每段结束都把光标归位到左边距，行距由 new_y=NEXT 负责。
    _LMARGIN, _NEXT = XPos.LMARGIN, YPos.NEXT

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

    base_dir = Path(assets_dir).resolve() if assets_dir else Path(output_path).resolve().parent

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

        # 图片：按页面可用宽度等比放置
        image_match = _IMAGE_LINE_RE.match(line)
        if image_match:
            _render_markdown_image(pdf, image_match.group(2), base_dir)
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

            # 先按列宽计算每个单元格折行后的行数，整行取最大行数作为行高，
            # 避免长单元格文字横向溢出表格 bbox（会导致 PDF 统计把表格文字算进段落）
            cell_lines = [
                pdf.multi_cell(
                    col_width, 7, cell_text,
                    dry_run=True, output="LINES", wrapmode="CHAR",
                ) or [""]
                for cell_text in cells
            ]
            row_height = max(len(lines) for lines in cell_lines) * 7

            x0, y0 = pdf.get_x(), pdf.get_y()
            if y0 + row_height > pdf.h - pdf.b_margin:
                pdf.add_page()
                x0, y0 = pdf.get_x(), pdf.get_y()

            for idx, text_lines in enumerate(cell_lines):
                cx = x0 + idx * col_width
                pdf.set_xy(cx, y0)
                pdf.cell(col_width, row_height, "", border=1)
                for line_idx, text_line in enumerate(text_lines):
                    pdf.set_xy(cx, y0 + line_idx * 7)
                    pdf.cell(col_width, 7, text_line, border=0, align="L")

            pdf.set_xy(x0, y0 + row_height)
            pdf.set_font_size(11)
            continue

        # 标题
        if line.startswith("###"):
            pdf.set_font("msyh", size=13)
            pdf.multi_cell(0, 8, line.lstrip("#").strip(), new_x=_LMARGIN, new_y=_NEXT)
            pdf.set_font("msyh", size=11)
        elif line.startswith("##"):
            pdf.set_font("msyh", size=15)
            pdf.multi_cell(0, 10, line.lstrip("#").strip(), new_x=_LMARGIN, new_y=_NEXT)
            pdf.set_font("msyh", size=11)
        elif line.startswith("#"):
            pdf.set_font("msyh", size=17)
            pdf.multi_cell(0, 11, line.lstrip("#").strip(), new_x=_LMARGIN, new_y=_NEXT)
            pdf.set_font("msyh", size=11)
        else:
            pdf.multi_cell(0, 7, line, new_x=_LMARGIN, new_y=_NEXT)

    pdf.output(output_path)
    return output_path


def _render_markdown_image(pdf, target: str, base_dir: Path) -> None:
    """在 PDF 中放置一张图片：宽度受页面可用宽度约束，保持原始宽高比。

    图片本身按 96dpi 的原始物理尺寸放置，不放大（避免小图标被拉成大图）；
    超出页面剩余高度时先换页。图片文件缺失时跳过并记日志。
    """
    from PIL import Image as PILImage

    path = Path(target.strip())
    if not path.is_absolute():
        path = base_dir / path

    if not path.exists():
        # 与 docx 路径同口径：解析不到的图片直接跳过，不写占位文字
        # （占位文字会被当成正文，段落数随之虚增）
        logger.warning("图片引用无法解析，已跳过: %s", target)
        return

    with PILImage.open(path) as image:
        px_width, px_height = image.size
        dpi = image.info.get("dpi") or (96, 96)
    dpi = float(dpi[0]) if dpi[0] else 96.0

    max_width = pdf.w - pdf.l_margin - pdf.r_margin
    width = min(px_width / dpi * 25.4, max_width)
    height = width * px_height / px_width

    if pdf.get_y() + height > pdf.h - pdf.b_margin:
        pdf.add_page()

    top = pdf.get_y()
    pdf.image(str(path), x=pdf.l_margin, y=top, w=width, h=height)
    pdf.set_y(top + height + 3)
