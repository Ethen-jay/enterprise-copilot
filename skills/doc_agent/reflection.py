"""反思引擎（文档转换 Agent 专用）：转换内容校验 + 重试病因分流。

RAG 侧的蕴含裁判与病因表已拆分至 skills/rag_qa/reflection.py，
两模块各自维护独立的病因表与消费方式，技能间零依赖：
- 转换侧（本文件）：病因 → 拼 retry_prompt（纸条重写）
- RAG 侧：病因 → 扩检索重跑 / 塞纸条重写（见 skills/rag_qa/reflection.py）

核心原则：
1. 校验器与转换器共用 content_parser 解析层（防假失败）
2. 容差比较（15%差异内算通过）
3. 重试按病因分流，不许原样重试（防空转）
"""

import json
import logging
from pathlib import Path

from skills.doc_agent.content_parser import parse_metrics

logger = logging.getLogger("reflection")

# 容差
TOLERANCE = 0.15
KEYWORD_KEEP_RATE = 0.75

# 转换侧病因 → 调整策略映射（RAG 侧病因表见 skills/rag_qa/reflection.py）
CAUSE_TO_ADJUSTMENT = {
    "table_lost": "use_text_strategy",           # pdfplumber 用 text 策略重新提取表格
    "content_too_short": "use_text_strategy",   # 同样换 text 策略尝试提取更多
    "format_mismatch": "try_alternative_tool",   # 换工具
}


def compare_metrics(
    src_metrics: dict,
    out_metrics: dict,
    tolerance: float = TOLERANCE,
) -> dict:
    """带容差的指标对比。

    参数:
        src_metrics: 源文件指标。
        out_metrics: 输出文件指标。
        tolerance: 允许的相对差异比例。

    返回:
        {"passed": bool, "issues": [str], "details": dict}
    """
    issues: list[str] = []
    details = {}

    # 1. 段落数对比
    src_paras = src_metrics.get("paragraph_count", 0)
    out_paras = out_metrics.get("paragraph_count", 0)
    details["src_paragraphs"] = src_paras
    details["out_paragraphs"] = out_paras
    if src_paras > 0:
        para_diff = abs(src_paras - out_paras) / src_paras
        details["paragraph_diff_ratio"] = round(para_diff, 3)
        if para_diff > tolerance:
            issues.append(
                f"段落数差异过大: 源 {src_paras} → 输出 {out_paras} (差异 {para_diff:.1%})"
            )

    # 2. 表格行数对比
    src_rows = src_metrics.get("table_row_count", 0)
    out_rows = out_metrics.get("table_row_count", 0)
    details["src_table_rows"] = src_rows
    details["out_table_rows"] = out_rows
    if src_rows > 0:
        row_diff = abs(src_rows - out_rows) / src_rows
        details["table_row_diff_ratio"] = round(row_diff, 3)
        if row_diff > tolerance:
            issues.append(
                f"表格行数差异过大: 源 {src_rows} → 输出 {out_rows} (差异 {row_diff:.1%})"
            )
    elif src_rows == 0 and out_rows > 0:
        # 源没表格但输出了表格，不算问题
        pass

    # 3. 图片数量对比（图片是独立指标，不计入段落口径）
    src_images = src_metrics.get("image_count", 0)
    out_images = out_metrics.get("image_count", 0)
    details["src_images"] = src_images
    details["out_images"] = out_images
    if src_images > 0:
        image_diff = abs(src_images - out_images) / src_images
        details["image_diff_ratio"] = round(image_diff, 3)
        if image_diff > tolerance:
            issues.append(
                f"图片数量差异过大: 源 {src_images} → 输出 {out_images} (差异 {image_diff:.1%})"
            )

    # 4. 关键词保留率
    src_kw = set(src_metrics.get("keywords", []))
    out_kw = set(out_metrics.get("keywords", []))
    if src_kw:
        kept = src_kw & out_kw
        keep_rate = len(kept) / len(src_kw)
        details["keyword_keep_rate"] = round(keep_rate, 3)
        details["lost_keywords"] = list(src_kw - out_kw)
        if keep_rate < KEYWORD_KEEP_RATE:
            issues.append(
                f"关键词保留率过低: {keep_rate:.1%} (要求 >= {KEYWORD_KEEP_RATE:.0%}), "
                f"丢失: {src_kw - out_kw}"
            )

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "details": details,
    }


def reflect_conversion(
    src_path: str,
    output_path: str,
    source_format: str,
    target_format: str,
) -> dict:
    """转换反思：对比源文件和输出文件的内容指标。

    返回:
        {
            "passed": bool,
            "issues": [str],
            "details": dict,
            "cause": str | None,       # 失败病因
            "adjustment": str | None,  # 重试调整策略
        }
    """
    # 用共用解析层提取两端指标
    src_metrics = parse_metrics(src_path, source_format)
    out_metrics = parse_metrics(output_path, target_format)

    # 扫描件判定要在图片口径折算之前取原值：纯图片 PDF 没有可解析的文字，
    # 段落数为 0、图片数 > 0，此时不能因为"段落 0 → 0"就静默判通过。
    scanned_pdf = (
        source_format == "pdf"
        and src_metrics.get("paragraph_count", 0) == 0
        and src_metrics.get("image_count", 0) > 0
    )

    # 图片口径：txt/xlsx 目标不承载图片（txt 是纯文本、xlsx 只在正文 sheet
    # 写文字），这类组合两端同时清零，表示"该链路不校验图片"，避免拿不到
    # 图的一端被判成"图片丢失"。其余组合（docx/pdf/md/html 源与目标）都
    # 参与图片校验。
    if target_format in ("txt", "xlsx"):
        src_metrics = {**src_metrics, "image_count": 0}
        out_metrics = {**out_metrics, "image_count": 0}

    # 纯文本 / PDF 目标不承载可稳定识别的结构化表格：
    # 源表格在输出里以普通文本行（PDF 版式行）保留，没有独立的表格行语义。
    # 将两端表格行并入段落数、表格行清零，避免行数对比假失败；
    # 内容丢失仍会体现为段落数缺口，不会被掩盖。
    if target_format in ("txt", "pdf"):
        # 两端统一折算为“正文行 + 表格行 = 文本行”：txt/pdf 里表格也以文本行呈现，
        # xlsx 源同样适用（_parse_xlsx 已区分单列正文行与多列表格行）。
        src_metrics = {
            **src_metrics,
            "paragraph_count": (
                src_metrics.get("paragraph_count", 0)
                + src_metrics.get("table_row_count", 0)
            ),
            "table_row_count": 0,
        }
        # txt 输出没有表格行，该折算对其无影响；PDF 输出需同样折算，
        # 保证两端都按"正文行 + 表格行 = 文本行"的同一口径比较。
        out_metrics = {
            **out_metrics,
            "paragraph_count": (
                out_metrics.get("paragraph_count", 0)
                + out_metrics.get("table_row_count", 0)
            ),
            "table_row_count": 0,
        }

    # 容差比较
    comparison = compare_metrics(src_metrics, out_metrics)
    issues = list(comparison["issues"])
    details = dict(comparison["details"])

    if scanned_pdf:
        details["scanned_pdf"] = True
        issues.append(
            "源 PDF 未提取到任何文字（疑似扫描件/纯图片 PDF），输出只含图片或为空；"
            "请先做 OCR 文字识别再转换"
        )

    if not issues:
        return {
            "passed": True,
            "issues": [],
            "details": details,
            "cause": None,
            "adjustment": None,
        }

    # 诊断病因
    cause = _diagnose_cause(issues, src_metrics, out_metrics)
    adjustment = CAUSE_TO_ADJUSTMENT.get(cause)

    logger.info(
        "反思校验不通过: cause=%s, adjustment=%s, issues=%s",
        cause, adjustment, issues,
    )

    return {
        "passed": False,
        "issues": issues,
        "details": details,
        "cause": cause,
        "adjustment": adjustment,
    }


def _diagnose_cause(
    issues: list[str],
    src_metrics: dict,
    out_metrics: dict,
) -> str:
    """根据校验失败的具体问题诊断病因。"""
    issues_text = " ".join(issues)

    if "表格行数差异过大" in issues_text:
        return "table_lost"
    if "疑似扫描件" in issues_text:
        # 没有可重试的提取策略：换表格策略也拿不到文字，需要 OCR
        return "scanned_pdf"
    if "图片数量差异过大" in issues_text:
        # 图片刻意丢失无法靠换提取策略补救，交给用户/上层处理
        return "image_lost"
    if "段落数差异过大" in issues_text and out_metrics.get("paragraph_count", 0) < src_metrics.get("paragraph_count", 0) * 0.5:
        return "content_too_short"
    if "关键词保留率过低" in issues_text:
        return "content_too_short"
    return "format_mismatch"


# ============================================================
# RAG LLM 蕴含裁判（已迁移至 skills/rag_qa/reflection.py，两模块独立维护）
# ============================================================



