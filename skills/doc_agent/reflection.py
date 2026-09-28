"""反思引擎：转换内容校验 + RAG 蕴含裁判 + 重试病因分流。

核心原则：
1. 校验器与转换器共用 content_parser 解析层（防假失败）
2. 容差比较（15%差异内算通过）
3. RAG 用 LLM 蕴含裁判（不只靠关键词）
4. 重试按病因分流，不许原样重试（防空转）
"""

import json
import logging
from pathlib import Path
from typing import List, Optional

from skills.doc_agent.content_parser import parse_metrics

logger = logging.getLogger("reflection")

# 容差
TOLERANCE = 0.15
KEYWORD_KEEP_RATE = 0.75

# 病因 → 调整策略映射
CAUSE_TO_ADJUSTMENT = {
    "table_lost": "use_text_strategy",           # pdfplumber 用 text 策略重新提取表格
    "content_too_short": "use_text_strategy",   # 同样换 text 策略尝试提取更多
    "format_mismatch": "try_alternative_tool",   # 换工具
    "unsupported_answer": "expand_retrieval",    # RAG 扩大检索范围
    "hallucination": "add_no_answer_constraint", # RAG 加强约束
    "no_citation": "add_citation_instruction",   # RAG 要求加引用
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
# RAG LLM 蕴含裁判
# ============================================================

async def llm_entailment_check(
    question: str,
    answer: str,
    context_docs: list,
) -> dict:
    """用 LLM 判断 answer 是否被 context_docs 蕴含。

    不只靠关键词重叠，用 LLM 做语义蕴含判断。

    返回:
        {
            "passed": bool,
            "issues": [str],
            "cause": str | None,
            "adjustment": str | None,
        }
    """
    from skills.rag_qa.llm_client import _get_llm, build_context_blocks
    from langchain_core.messages import HumanMessage, SystemMessage

    if not answer or not answer.strip():
        return {
            "passed": False,
            "issues": ["回答为空"],
            "cause": "unsupported_answer",
            "adjustment": CAUSE_TO_ADJUSTMENT.get("unsupported_answer"),
        }

    # 如果回答明确说无法回答，且确实没有编造，直接通过
    if "无法回答" in answer or "根据现有文档" in answer:
        # 检查是否同时有具体信息（矛盾）
        if any(c.isdigit() for c in answer) and len(answer) > 30:
            # 可能同时说无法回答又给了具体信息
            pass
        else:
            return {
                "passed": True,
                "issues": [],
                "cause": None,
                "adjustment": None,
            }

    context = build_context_blocks(context_docs)

    prompt = f"""请判断以下回答是否完全被参考片段支撑。

【参考片段】
{context}

【用户问题】
{question}

【回答】
{answer}

请严格检查：
1. 回答中的每个事实陈述是否都能在参考片段中找到依据？
2. 回答中是否有参考片段中没有的信息（即编造的内容）？
3. 回答是否与参考片段矛盾？

请只输出 JSON，不要加其他文字：
{{"supported": true/false, "unsupported_claims": ["不被支撑的具体陈述1", ...], "cause": "unsupported_answer/hallucination/no_citation/null"}}

注意：
- 如果回答明确说"无法回答"且没有编造具体信息，supported=true
- 如果回答包含参考片段中没有的具体数字、日期、人名等，supported=false
"""

    try:
        llm = _get_llm()
        messages = [
            SystemMessage(content="你是一个严谨的事实校验裁判。只输出 JSON。"),
            HumanMessage(content=prompt),
        ]
        resp = await llm.ainvoke(messages)
        raw = resp.content.strip()

        # 解析 JSON（处理可能的 markdown 代码块包裹）
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())

        supported = result.get("supported", False)
        cause = result.get("cause")
        if not supported and not cause:
            cause = "hallucination"
        unsupported_claims = result.get("unsupported_claims", [])

        issues = []
        if not supported:
            if unsupported_claims:
                issues.append(f"回答包含不被支撑的内容: {'; '.join(unsupported_claims)}")
            else:
                issues.append("回答内容未被参考片段完全支撑")

        adjustment = CAUSE_TO_ADJUSTMENT.get(cause) if cause else None

        logger.info(
            "RAG 蕴含裁判: supported=%s, cause=%s, issues=%s",
            supported, cause, issues,
        )

        return {
            "passed": supported,
            "issues": issues,
            "cause": cause if not supported else None,
            "adjustment": adjustment,
        }
    except Exception as e:
        logger.warning("LLM 蕴含裁判失败，降级为关键词校验: %s", e)
        # 降级：关键词重叠校验
        return _keyword_fallback_check(question, answer, context_docs)


def _keyword_fallback_check(
    question: str,
    answer: str,
    context_docs: list,
) -> dict:
    """LLM 裁判失败时的降级：关键词重叠校验。"""
    from skills.doc_agent.content_parser import extract_keywords

    context_text = " ".join(doc.page_content for doc in context_docs)
    ctx_kw = set(extract_keywords(context_text, top_n=20))
    ans_kw = set(extract_keywords(answer, top_n=10))

    if not ctx_kw:
        return {
            "passed": False,
            "issues": ["参考片段为空，无法校验"],
            "cause": "unsupported_answer",
            "adjustment": CAUSE_TO_ADJUSTMENT.get("unsupported_answer"),
        }

    overlap = ans_kw & ctx_kw
    if ans_kw and len(overlap) / len(ans_kw) < 0.3:
        return {
            "passed": False,
            "issues": [f"回答关键词与参考片段重叠率过低: {len(overlap)}/{len(ans_kw)}"],
            "cause": "hallucination",
            "adjustment": CAUSE_TO_ADJUSTMENT.get("hallucination"),
        }

    return {
        "passed": True,
        "issues": [],
        "cause": None,
        "adjustment": None,
    }


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
