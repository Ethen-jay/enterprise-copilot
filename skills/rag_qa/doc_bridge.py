"""RAG 入库前的格式桥接：把 .docx/.xlsx/.html 解析成 Markdown 文本。

与 doc_agent 的转换能力同进程复用（不复制解析逻辑、不走 HTTP）：
- 文本抽取统一调用 skills/doc_agent/extract.source_to_markdown，
  即 doc_agent 状态机 Hub 入边使用的同一份 converter；
- 这里只额外负责：旧版 Office 拦截、转换质量指标采样、临时文件管理。

入库时 source 始终是用户原始文件名：本模块只返回文本，不落产物文件，
因此不会把 output_xxx_to_markdown.md 之类中间产物名带进知识库。
"""

import logging
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from skills.doc_agent.config import OUTPUT_DIR
from skills.doc_agent.content_parser import parse_metrics
from skills.doc_agent.extract import source_to_markdown
from skills.doc_agent.planner import FORMAT_ALIASES, sniff_legacy_binary_format

logger = logging.getLogger("rag.doc_bridge")

# 入库前需要先转成文本的格式（.htm 是 .html 的别名）
CONVERTIBLE_EXTS = {".docx", ".xlsx", ".html", ".htm"}
# 旧版 Office 二进制格式：明确拒绝并给出可操作提示
LEGACY_OFFICE_EXTS = {".doc", ".xls", ".ppt"}

LEGACY_FORMAT_HINT = (
    "检测到旧版 Office 二进制格式（.doc/.xls/.ppt，OLE2 复合文档），"
    "本系统只支持新版 OOXML（.docx/.xlsx）；"
    "请用 Word/Excel 打开后「另存为」新版格式再上传"
)


class ConversionError(Exception):
    """文档转文本失败（旧版格式、文件损坏、无文本内容）。message 可直接展示。"""


def _detect_legacy(path: str) -> bool:
    """按文件头魔数判断是否为旧版 Office 二进制格式（防改名骗过扩展名）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    return sniff_legacy_binary_format(head)


def _safe_unlink(path: str) -> None:
    """Windows 上临时文件可能被杀毒/索引短暂占用，删除失败不影响主流程。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        logger.debug("临时文件删除失败（忽略）: %s", path)


def _quality_metrics(src_path: str, fmt: str, metric_md: str) -> dict:
    """采样转换质量指标（文档级，随分块写入 metadata）。

    指标口径与 doc_agent 反思层一致：源文件 vs 转换后 Markdown 的
    段落数 / 表格行数 / 图片数 / 关键词保留率，偏差取三者最大相对差异。

    xlsx 的 "## sheet 名" 是我们为分段新增的结构标题，不参与保真度比较
    （metric_md 传不带 sheet 标题的版本），避免把新增结构算成内容偏差。
    """
    src = parse_metrics(src_path, fmt)
    fd, tmp_md = tempfile.mkstemp(suffix=".md", prefix="rag_conv_")
    try:
        with Path(tmp_md).open("w", encoding="utf-8") as fh:
            fh.write(metric_md)
        out = parse_metrics(tmp_md, "markdown")
    finally:
        _safe_unlink(tmp_md)

    diffs = []

    def _ratio(src_v: int, out_v: int) -> Optional[float]:
        if src_v <= 0:
            return None
        return abs(src_v - out_v) / src_v

    for key, out_key in (
        ("paragraph_count", "paragraph_count"),
        ("table_row_count", "table_row_count"),
        ("image_count", "image_count"),
    ):
        r = _ratio(int(src.get(key, 0)), int(out.get(out_key, 0)))
        if r is not None:
            diffs.append(r)

    src_kw = set(src.get("keywords") or [])
    out_kw = set(out.get("keywords") or [])
    keep_rate = len(src_kw & out_kw) / len(src_kw) if src_kw else 1.0

    return {
        "conv_source_format": fmt,
        "conv_paragraphs": int(src.get("paragraph_count", 0)),
        "conv_table_rows": int(src.get("table_row_count", 0)),
        "conv_images": int(src.get("image_count", 0)),
        "conv_keyword_keep_rate": round(keep_rate, 3),
        "conv_deviation_pct": round(max(diffs) * 100, 2) if diffs else 0.0,
    }


def convert_path_to_markdown(
    src_path: str,
    filename: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[str, dict]:
    """把磁盘上的 docx/xlsx/html 转成 Markdown，并返回质量指标。

    返回:
        (markdown 文本, 文档级质量指标 dict)

    异常:
        ConversionError: 旧版格式 / 解析失败 / 无文本内容。
    """
    cb = progress_callback or (lambda _: None)
    ext = Path(filename).suffix.lower()
    fmt = FORMAT_ALIASES.get(ext.lstrip("."), ext.lstrip("."))

    if fmt in ("docx", "xlsx") and _detect_legacy(src_path):
        raise ConversionError(LEGACY_FORMAT_HINT)

    cb("converting")
    try:
        md_text = source_to_markdown(
            src_path, fmt,
            assets_dir=OUTPUT_DIR / "assets",
            include_sheet_titles=(fmt == "xlsx"),
        )
    except ConversionError:
        raise
    except Exception as e:
        logger.warning("文档转换失败: %s", filename, exc_info=True)
        raise ConversionError(
            f"文档转换失败：{filename}（文件可能为空、已损坏或扩展名与实际格式不符）"
        ) from e

    if not md_text or not md_text.strip():
        raise ConversionError(
            f"未提取到文本：{filename}（文档为空或无文本内容）"
        )

    metric_md = md_text
    if fmt == "xlsx":
        # 叠一层不带 sheet 标题的版本做保真度基准，避免新增标题被算成内容偏差
        try:
            metric_md = source_to_markdown(src_path, fmt, include_sheet_titles=False)
        except Exception:
            metric_md = md_text

    try:
        metrics = _quality_metrics(src_path, fmt, metric_md)
    except Exception:
        # 指标采样失败不影响入库本身，缺字段即可（文档体检报告后续降级）
        logger.warning("转换质量指标采样失败: %s", filename, exc_info=True)
        metrics = {"conv_source_format": fmt}

    return md_text, metrics


def convert_bytes_to_markdown(
    data: bytes,
    filename: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[str, dict]:
    """内存模式入口：先落临时文件，再复用 convert_path_to_markdown。"""
    ext = Path(filename).suffix.lower() or ".bin"
    fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="rag_src_")
    try:
        with Path(tmp_path).open("wb") as fh:
            fh.write(data)
        return convert_path_to_markdown(tmp_path, filename, progress_callback)
    finally:
        _safe_unlink(tmp_path)
