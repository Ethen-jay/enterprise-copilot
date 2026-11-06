"""PDF → Markdown 转换（pdfplumber，保留表格与图片）。"""

import logging
from pathlib import Path
from typing import Optional

import pdfplumber

from skills.doc_agent.content_parser import (
    _adaptive_gap_threshold,
    _group_paragraph_lines,
    _select_content_images,
)

logger = logging.getLogger(__name__)

# 图片渲染参数：200dpi 兼顾清晰度与体积；宽度上限 1200px（约 5.9in），
# 保证插入 docx 后不超过 A4 正文宽度，也避免整页大图把输出撑爆。
_IMAGE_DPI = 200
_IMAGE_MAX_WIDTH_PX = 1200


def pdf_to_markdown(
    pdf_path: str | Path,
    table_settings: Optional[dict] = None,
    assets_dir: Optional[str | Path] = None,
) -> str:
    """将 PDF 转换为 Markdown，尽量还原标题、段落、表格和图片。

    参数:
        pdf_path: PDF 文件路径。
        table_settings: pdfplumber extract_tables 的参数。
            None → 默认参数。
            {"vertical_strategy": "text", "horizontal_strategy": "text"} → 备选策略。
        assets_dir: 图片落盘目录；None 时用源文件同级的 assets/ 目录。

    返回:
        Markdown 字符串，图片以 ![图片](assets/xxx.png) 引用。
    """
    pdf_path = Path(pdf_path)
    assets_dir = Path(assets_dir) if assets_dir else pdf_path.parent / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    assets_name = assets_dir.name

    parts: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        page_items: list[tuple[list, list[dict], float | None, list[dict]]] = []

        for page_no, page in enumerate(pdf.pages, 1):
            # 先定位表格（保留 bbox，用于把表格区域从正文里排除）
            if table_settings:
                found = page.find_tables(table_settings=table_settings) or []
            else:
                found = page.find_tables() or []
            table_bboxes = [tuple(t.bbox) for t in found]

            # 正文行：跳过落在表格 bbox 内的行，避免表格内容重复输出
            body_lines = []
            for line in page.extract_text_lines() or []:
                text = (line.get("text") or "").strip()
                if not text:
                    continue
                if any(_in_bbox(line, bbox) for bbox in table_bboxes):
                    continue
                body_lines.append({**line, "text": text})

            page_gaps = [
                body_lines[idx]["top"] - body_lines[idx - 1]["bottom"]
                for idx in range(1, len(body_lines))
            ]
            images = _extract_page_images(page, page_no, assets_dir, assets_name)
            page_items.append(
                (
                    found,
                    body_lines,
                    _adaptive_gap_threshold(page_gaps),
                    images,
                )
            )

        # 与校验器共用同一套段落分组（按页推断行距阈值）：段内折行合并成
        # 一段，段间边界断开。否则每个视觉行都会被写成独立 Markdown 段落，
        # 经 pandoc 后 docx 段落数随折行数膨胀，校验器按源段落数对比就会
        # 报“段落数差异过大”。
        for found, body_lines, threshold, images in page_items:
            for table in found:
                rows = table.extract()
                if not rows:
                    continue
                parts.append(_table_to_markdown(rows))

            # 段落与图片按纵向位置归并，保证图片落在它实际所在的文字之间
            items: list[tuple[float, int, str]] = []
            for group in _group_paragraph_lines(body_lines, threshold):
                text = _join_wrapped_lines([line["text"] for line in group])
                if text:
                    items.append((group[0]["top"], 0, text))
            for image in images:
                items.append((image["top"], 1, f"![图片]({image['ref']})"))

            items.sort(key=lambda item: (item[0], item[1]))
            parts.extend(text for _, _, text in items)

    # 合并并清理
    md = "\n\n".join(p for p in parts if p.strip())
    return _clean_markdown(md)


def _extract_page_images(
    page,
    page_no: int,
    assets_dir: Path,
    assets_name: str,
) -> list[dict]:
    """抽取一页里的内容图片并渲染为 PNG，返回 {top, ref} 列表。

    文件名只由「页码 + 页内序号」决定：不掺源文件名，源文件是临时文件
    （tmpXXXX.pdf）时同一次转换重跑也能得到同样名字，便于复现与比对。
    单张失败只跳过该张，不中断整次转换。
    """
    extracted: list[dict] = []
    for idx, image in enumerate(_select_content_images(page.images or []), 1):
        filename = f"p{page_no}_{idx}.png"
        try:
            _render_image(page, image, assets_dir / filename)
        except Exception as exc:  # noqa: BLE001 - 单图失败不能影响整篇
            logger.warning(
                "PDF 图片抽取失败 page=%s idx=%s bbox=%s: %s",
                page_no, idx, image, exc,
            )
            continue
        extracted.append(
            {"top": image["top"], "ref": f"{assets_name}/{filename}"}
        )
    return extracted


def _render_image(page, image: dict, dest: Path) -> None:
    """按 bbox 裁剪渲染图片，宽度超限时降 dpi 而不是放大。"""
    x0 = max(float(image["x0"]), 0.0)
    top = max(float(image["top"]), 0.0)
    x1 = min(float(image["x1"]), float(page.width))
    bottom = min(float(image["bottom"]), float(page.height))
    if x1 - x0 < 1 or bottom - top < 1:
        raise ValueError("图片 bbox 落在页面之外")

    width_pt = x1 - x0
    dpi = _IMAGE_DPI
    if width_pt / 72 * dpi > _IMAGE_MAX_WIDTH_PX:
        dpi = max(72, int(_IMAGE_MAX_WIDTH_PX * 72 / width_pt))

    rendered = page.crop((x0, top, x1, bottom)).to_image(resolution=dpi)
    pil_image = getattr(rendered, "original", rendered)
    pil_image.save(str(dest), format="PNG", optimize=True, dpi=(dpi, dpi))


def _join_wrapped_lines(texts: list[str]) -> str:
    """把同一段落内的折行拼回一行。

    中文折行处不补空格；两侧都是 ASCII 字母/数字时补一个空格，
    避免英文单词在折行处粘连。
    """
    merged = ""
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if not merged:
            merged = text
            continue
        prev_ch, next_ch = merged[-1], text[0]
        if prev_ch.isascii() and prev_ch.isalnum() and next_ch.isascii() and next_ch.isalnum():
            merged += " " + text
        else:
            merged += text
    return merged


def _in_bbox(item: dict, bbox) -> bool:
    """判断文本行的几何中心是否落在 bbox(x0, top, x1, bottom) 内。"""
    x0, top, x1, bottom = bbox
    cx = (item["x0"] + item["x1"]) / 2
    cy = (item["top"] + item["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


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
