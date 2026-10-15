"""RAG 反思模块：LLM 蕴含裁判 + 病因分流（独立于文档转换 Agent 的反思逻辑）。

与 skills/doc_agent/reflection.py 按技能拆分，各自维护独立的病因表，
技能间零依赖（跨技能共享只能走 common/）：
- RAG 侧病因（本文件）：检索材料不够 / 回答编造 / 缺引用
- 转换侧病因（doc_agent/reflection.py）：表格丢失 / 内容过短 / 格式错乱

重试策略为混合式（各病因对症下药）：
- unsupported_answer（材料不够）→ expand_retrieval 扩检索重跑全流程（纸条无效，病根是材料不足）
- hallucination（编造）→ add_no_answer_constraint 塞纸条重写（列出无依据断言，只重新生成）
- no_citation（缺引用）→ add_citation_instruction 塞纸条重写（补引用指令，只重新生成）
"""

import json
import logging
from typing import List

import jieba

logger = logging.getLogger("rag_reflection")

# RAG 侧病因 → 调整策略映射（与转换侧 doc_agent/reflection.py 的病因表互相独立）
CAUSE_TO_ADJUSTMENT = {
    "unsupported_answer": "expand_retrieval",     # 材料不够：扩检索重跑
    "hallucination": "add_no_answer_constraint",  # 编造：塞无依据断言清单重写
    "no_citation": "add_citation_instruction",    # 缺引用：塞引用指令重写
}


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
            "unsupported_claims": [str],  # 不被支撑的具体断言（塞纸条重写用）
        }
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from skills.rag_qa.llm_client import _get_llm, build_context_blocks

    if not answer or not answer.strip():
        return {
            "passed": False,
            "issues": ["回答为空"],
            "cause": "unsupported_answer",
            "adjustment": CAUSE_TO_ADJUSTMENT.get("unsupported_answer"),
            "unsupported_claims": [],
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
                "unsupported_claims": [],
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
            "unsupported_claims": unsupported_claims if not supported else [],
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
    context_text = " ".join(doc.page_content for doc in context_docs)
    ctx_kw = set(_extract_keywords(context_text, top_n=20))
    ans_kw = set(_extract_keywords(answer, top_n=10))

    if not ctx_kw:
        return {
            "passed": False,
            "issues": ["参考片段为空，无法校验"],
            "cause": "unsupported_answer",
            "adjustment": CAUSE_TO_ADJUSTMENT.get("unsupported_answer"),
            "unsupported_claims": [],
        }

    overlap = ans_kw & ctx_kw
    if ans_kw and len(overlap) / len(ans_kw) < 0.3:
        return {
            "passed": False,
            "issues": [f"回答关键词与参考片段重叠率过低: {len(overlap)}/{len(ans_kw)}"],
            "cause": "hallucination",
            "adjustment": CAUSE_TO_ADJUSTMENT.get("hallucination"),
            "unsupported_claims": [],
        }

    return {
        "passed": True,
        "issues": [],
        "cause": None,
        "adjustment": None,
        "unsupported_claims": [],
    }


# 轻量停用词表（RAG 降级校验自用，不依赖 doc_agent.content_parser）
_STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "那", "他", "她", "它", "我们", "什么",
}


def _extract_keywords(text: str, top_n: int = 10) -> List[str]:
    """jieba 分词 + 停用词/符号过滤，取 top_n 高频词（降级校验自用）。"""
    if not text or not text.strip():
        return []
    words = jieba.lcut(text)
    filtered = [
        w for w in words
        if len(w) >= 2
        and w not in _STOP_WORDS
        and not w.isspace()
        and not all(c in "#-*|>`~_=" for c in w)
        and not w.replace(".", "").replace("-", "").isdigit()
    ]
    # 按词频取 top_n
    freq: dict[str, int] = {}
    for w in filtered:
        freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:top_n]]
