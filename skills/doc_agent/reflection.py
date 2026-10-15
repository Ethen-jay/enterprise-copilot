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

    # 3. 关键词保留率
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

    # 纯文本目标不承载结构化表格：源表格在 txt 中以普通文本行保留。
    # 将源表格行并入段落数、表格行清零，避免行数对比假失败。
    # xlsx 例外：其 paragraph_count 本就等于数据行数，不再叠加。
    if target_format == "txt":
        if source_format == "xlsx":
            effective_paras = src_metrics.get("paragraph_count", 0)
        else:
            effective_paras = (
                src_metrics.get("paragraph_count", 0)
                + src_metrics.get("table_row_count", 0)
            )
        src_metrics = {
            **src_metrics,
            "paragraph_count": effective_paras,
            "table_row_count": 0,
        }
    elif source_format == "xlsx":
        # xlsx 的"段落"本就是表格行的近似：转出到结构化格式后，
        # 表格由 table_row_count 承载，段落指标清零避免假失败。
        src_metrics = {
            **src_metrics,
            "paragraph_count": 0,
        }

    # 容差比较
    comparison = compare_metrics(src_metrics, out_metrics)

    if comparison["passed"]:
        return {
            "passed": True,
            "issues": [],
            "details": comparison["details"],
            "cause": None,
            "adjustment": None,
        }

    # 诊断病因
    cause = _diagnose_cause(comparison["issues"], src_metrics, out_metrics)
    adjustment = CAUSE_TO_ADJUSTMENT.get(cause)

    logger.info(
        "反思校验不通过: cause=%s, adjustment=%s, issues=%s",
        cause, adjustment, comparison["issues"],
    )

    return {
        "passed": False,
        "issues": comparison["issues"],
        "details": comparison["details"],
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
    if "段落数差异过大" in issues_text and out_metrics.get("paragraph_count", 0) < src_metrics.get("paragraph_count", 0) * 0.5:
        return "content_too_short"
    if "关键词保留率过低" in issues_text:
        return "content_too_short"
    return "format_mismatch"


# ============================================================
# RAG LLM 蕴含裁判（已迁移至 skills/rag_qa/reflection.py，两模块独立维护）
# ============================================================


# ============================================================
# 指标统计
# ============================================================

def save_metrics(metrics: dict, path: str = "data/output/reflection_metrics.json"):
    """保存反思指标到 JSON 文件。"""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 读取已有指标
    existing = []
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = [existing]
        except Exception:
            existing = []

    existing.append(metrics)
    out_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
