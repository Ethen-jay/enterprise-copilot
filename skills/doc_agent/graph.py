"""LangGraph 状态机：Planner → Converter → Reflection，支持病因分流重试。"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from skills.doc_agent import llm_client, memory
from skills.doc_agent.config import OUTPUT_DIR
from skills.doc_agent.converters import (
    docx_converter,
    excel_converter,
    html_converter,
    pdf_converter,
    text_converter,
)
from skills.doc_agent.planner import sniff_legacy_binary_format
from skills.doc_agent.planner_v2 import TaskPlan, create_plan
from skills.doc_agent.reflection import reflect_conversion, CAUSE_TO_ADJUSTMENT
from skills.doc_agent.validator import validate

MAX_RETRY = 2  # 最大重试次数

logger = logging.getLogger("agent_graph")


class AgentState(TypedDict):
    """Agent 状态机共享状态。"""
    instruction: str
    filename: str
    file_bytes: bytes
    src_path: str                       # 临时源文件路径
    plan: Optional[TaskPlan]
    output_path: Optional[str]
    validation: Optional[dict]
    error: Optional[str]
    retry_count: int
    status: str                         # "ok" | "error"
    retry_hint: Optional[dict]          # {cause, adjustment}
    used_adjustments: list              # 已用过的调整策略（防空转）
    reflection_log: list                # 反思日志
    memory_recall: Optional[dict]      # 规划阶段召回的记忆（不可信）


def planning_node(state: AgentState) -> AgentState:
    """Planning 节点：召回记忆 → 检测显式触发 → 生成结构化计划 → 保存临时源文件。

    记忆处理：
    1. 读 get_relevant_memory() 作为不可信参考数据（仅用于上下文，不修改 plan）
    2. 若指令含"记住..."，调用 save_preference() 写入偏好（显式触发）
    3. 计划对象贯穿后续每步（converter/reflection/retry_gate/memory_node）
    """
    instruction = state["instruction"]
    filename = state["filename"]
    file_bytes = state["file_bytes"]

    # 1. 召回记忆（不可信数据）
    memory_recall = memory.get_relevant_memory(instruction, filename)
    if memory_recall["preferences"] or memory_recall["recent_tasks"] or memory_recall["insights"]:
        logger.info(
            "==== 记忆召回（不可信参考） ====\n"
            "  %s\n"
            "  偏好: %s\n"
            "  最近任务: %s\n"
            "  洞察: %s",
            memory_recall["warning"],
            [(p["key"], p["value"]) for p in memory_recall["preferences"]],
            [t.get("instruction", "")[:40] for t in memory_recall["recent_tasks"]],
            [(i["content"][:40], i["confidence"]) for i in memory_recall["insights"]],
        )

    # 2. 检测显式记忆触发（"记住..."）
    trigger = memory.detect_memory_trigger(instruction)
    if trigger:
        memory.save_preference(trigger["key"], trigger["value"], instruction)
        logger.info("检测到显式记忆触发，已保存偏好: %s=%s", trigger["key"], trigger["value"])

    # 3. 生成结构化计划（规则路由）
    plan = create_plan(instruction, filename)

    # 打印结构化计划，便于追踪
    logger.info(
        "==== 规划阶段 ====\n"
        "  task_type: %s\n"
        "  %s → %s\n"
        "  tools: %s\n"
        "  steps(%d): %s\n"
        "  expected_output: %s\n"
        "  options: %s",
        plan.task_type,
        plan.source_format,
        plan.target_format,
        plan.tools,
        len(plan.steps),
        [s.get("action") for s in plan.steps],
        plan.expected_output,
        plan.options,
    )

    # 4. 保存临时源文件
    src_ext = Path(filename).suffix
    fd, tmp_src = tempfile.mkstemp(suffix=src_ext)
    with os.fdopen(fd, "wb") as f:
        f.write(file_bytes)

    return {
        **state,
        "plan": plan,
        "src_path": tmp_src,
        "used_adjustments": [],
        "reflection_log": [],
        "retry_hint": None,
        "memory_recall": memory_recall,
    }


def converter_node(state: AgentState) -> AgentState:
    """Converter 节点：根据计划执行格式转换，支持病因调整。"""
    plan = state["plan"]
    src_path = state["src_path"]
    retry_hint = state.get("retry_hint")

    # 根据重试提示调整参数
    adjustment = None
    if retry_hint:
        adjustment = retry_hint.get("adjustment")

    # 内容嗅探守卫（防"改名骗过识别"）：扩展名识别只看后缀，旧版 .doc/.xls 只要改名成
    # .docx/.xlsx 就能骗过；但旧版是 OLE2 复合文档，python-docx/openpyxl 打不开，
    # 若不拦截会抛出含内部临时路径的裸库报错（PackageNotFoundError）。此处按文件头
    # 魔数提前识别，给出可操作的中文提示。
    if plan.source_format in ("docx", "xlsx"):
        try:
            with open(src_path, "rb") as fh:
                head = fh.read(8)
        except OSError:
            head = b""
        if sniff_legacy_binary_format(head):
            return {
                **state,
                "status": "error",
                "error": (
                    "检测到该文件是旧版 Office 二进制格式（.doc/.xls/.ppt，OLE2 复合文档），"
                    "并非真正的新版格式——仅改扩展名不改变文件内容。"
                    "本系统只解析新版 OOXML（.docx/.xlsx），不支持旧版二进制格式；"
                    "请用 Word/Excel 打开后「另存为」新版格式再上传。"
                ),
            }

    try:
        output_path = _execute_conversion(src_path, plan, adjustment)
        if not output_path:
            return {
                **state,
                "status": "error",
                "error": f"不支持的转换：{plan.source_format} → {plan.target_format}",
            }

        # 润色/摘要（仅 Markdown 输出）
        if plan.target_format == "markdown":
            output_path = _apply_text_operations(output_path, plan.options)

        return {
            **state,
            "output_path": output_path,
            "status": "ok",
            "error": None,
        }
    except Exception as e:
        logger.exception("转换失败")
        return {
            **state,
            "status": "error",
            "error": str(e),
        }


def reflection_node(state: AgentState) -> AgentState:
    """反思节点：校验转换结果，诊断病因。

    替代原 reviewer_node，增加内容指标对比和病因诊断。
    """
    # 如果转换已失败，直接进入重试判断
    if state["status"] == "error":
        return state

    plan = state["plan"]
    output_path = state["output_path"]
    src_path = state["src_path"]

    # 1. 基础校验（文件存在/非空/扩展名）
    basic = validate(output_path, plan.source_format, plan.target_format)
    if not basic["passed"]:
        logger.info("基础校验不通过: %s", basic["issues"])
        return {
            **state,
            "validation": basic,
            "status": "error",
            "error": "; ".join(basic["issues"]),
            "retry_hint": {"cause": "format_mismatch", "adjustment": CAUSE_TO_ADJUSTMENT.get("format_mismatch")},
        }

    # 2. 内容反思（共用解析层 + 容差比较）
    reflection = reflect_conversion(
        src_path, output_path, plan.source_format, plan.target_format,
    )

    # 合并校验结果
    validation = {
        "passed": reflection["passed"],
        "issues": basic["issues"] + reflection["issues"],
        "details": {**basic["details"], **reflection["details"]},
    }

    # 记录反思日志
    log_entry = {
        "step": "reflection",
        "passed": reflection["passed"],
        "cause": reflection.get("cause"),
        "adjustment": reflection.get("adjustment"),
        "issues": reflection.get("issues", []),
        "details": reflection.get("details", {}),
    }
    reflection_log = state.get("reflection_log", [])
    reflection_log.append(log_entry)

    logger.info(
        "反思校验: passed=%s, cause=%s, issues=%s",
        reflection["passed"],
        reflection.get("cause"),
        reflection.get("issues"),
    )

    if reflection["passed"]:
        return {
            **state,
            "validation": validation,
            "status": "ok",
            "reflection_log": reflection_log,
        }

    # 不通过，设置重试提示
    return {
        **state,
        "validation": validation,
        "status": "error",
        "error": "; ".join(reflection["issues"]) if reflection["issues"] else "反思校验未通过",
        "retry_hint": {
            "cause": reflection.get("cause"),
            "adjustment": reflection.get("adjustment"),
        },
        "reflection_log": reflection_log,
    }


def should_retry(state: AgentState) -> str:
    """条件边：决定从 reflection 后的走向。

    返回 "retry"（重试）或 "end"（结束）。
    """
    if state["status"] == "ok":
        return "end"

    retry_count = state.get("retry_count", 0)
    if retry_count >= MAX_RETRY:
        logger.info("重试次数已用完 (%d/%d)，放弃重试", retry_count, MAX_RETRY)
        return "end"

    # 检查是否有可用的调整策略
    retry_hint = state.get("retry_hint")
    if not retry_hint or not retry_hint.get("adjustment"):
        logger.info("无可用的调整策略，放弃重试: cause=%s", retry_hint.get("cause") if retry_hint else None)
        return "end"

    # 检查该调整策略是否已用过（防空转）
    adjustment = retry_hint["adjustment"]
    used = state.get("used_adjustments", [])
    if adjustment in used:
        logger.info("调整策略 %s 已用过，不允许原样重试（防空转）", adjustment)
        return "end"

    return "retry"


def retry_gate(state: AgentState) -> AgentState:
    """重试前：递增计数器，记录已用调整策略。"""
    retry_hint = state.get("retry_hint", {})
    adjustment = retry_hint.get("adjustment", "")

    used = state.get("used_adjustments", [])
    used.append(adjustment)

    logger.info(
        "重试 %d: cause=%s, adjustment=%s",
        state.get("retry_count", 0) + 1,
        retry_hint.get("cause"),
        adjustment,
    )

    return {
        **state,
        "retry_count": state.get("retry_count", 0) + 1,
        "status": "ok",  # 重置状态，让 converter 重新执行
        "error": None,
        "used_adjustments": used,
    }


def memory_node(state: AgentState) -> AgentState:
    """记忆节点：任务结束后按条件写入长期记忆。

    写入条件（避免无意义记忆堆积）：
    1. 任务最终成功 + 发生过重试（retry_count > 0）→ 写 task_history + 写 insight
    2. 失败任务不写（避免噪声）

    所有写入文本经 memory._sanitize 净化。
    """
    # 失败任务不写
    if state.get("status") != "ok":
        return state

    retry_count = state.get("retry_count", 0)
    plan = state.get("plan")

    # 无重试且无显式洞察，不写
    if retry_count == 0:
        return state

    # 组装 reflection_summary（压缩反思日志）
    reflection_log = state.get("reflection_log", [])
    reflection_summary = ""
    if reflection_log:
        # 取最后一次反思的结果
        last = reflection_log[-1]
        reflection_summary = (
            f"最终成功，重试 {retry_count} 次；"
            f"病因链：{[l.get('cause') for l in reflection_log if l.get('cause')]};"
            f"已用策略：{state.get('used_adjustments', [])}"
        )

    output_filename = ""
    if state.get("output_path"):
        output_filename = Path(state["output_path"]).name

    # 写 task_history
    task_id = memory.save_task_history(
        instruction=state["instruction"],
        plan=plan.to_dict() if plan else {},
        result={
            "status": "ok",
            "retry_count": retry_count,
            "reflection_summary": reflection_summary,
            "output_filename": output_filename,
        },
    )

    # 写 insight：记录生效的调整策略
    used_adjustments = state.get("used_adjustments", [])
    if used_adjustments and plan and task_id:
        last_adj = used_adjustments[-1]
        insight_text = (
            f"转换 {plan.source_format}→{plan.target_format} 时，"
            f"策略 '{last_adj}' 解决了重试问题"
        )
        memory.save_insight(
            content=insight_text,
            category="conversion_tip",
            confidence=0.6,  # 中低置信=不可信
            source_task_id=task_id,
        )

    return state


def build_graph():
    """构建并编译 LangGraph 状态机。

    状态机：
    START → planning → converter → reflection
                                     ├─ retry_gate → converter (重试)
                                     └─ memory_node → END (成功/放弃)
    """
    workflow = StateGraph(AgentState)

    # 注册节点
    workflow.add_node("planning", planning_node)
    workflow.add_node("converter", converter_node)
    workflow.add_node("reflection", reflection_node)
    workflow.add_node("retry_gate", retry_gate)
    workflow.add_node("memory", memory_node)

    # 边
    workflow.add_edge(START, "planning")
    workflow.add_edge("planning", "converter")
    workflow.add_edge("converter", "reflection")

    # 条件边：reflection → retry_gate（重试）或 memory（结束）
    workflow.add_conditional_edges(
        "reflection",
        should_retry,
        {
            "retry": "retry_gate",
            "end": "memory",
        },
    )
    workflow.add_edge("retry_gate", "converter")
    workflow.add_edge("memory", END)

    return workflow.compile()


# ============================================================
# 转换逻辑（支持 adjustment 参数）
# ============================================================

def _execute_conversion(
    src_path: str,
    plan: TaskPlan,
    adjustment: Optional[str] = None,
) -> Optional[str]:
    """根据计划执行转换，返回输出路径。

    参数:
        src_path: 源文件路径。
        plan: 转换计划。
        adjustment: 重试调整策略（如 "use_text_strategy"）。
    """
    src = plan.source_format
    tgt = plan.target_format
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    out_ext = _format_to_ext(tgt)
    out_name = f"output_{src}_to_{tgt}.{out_ext}"
    out_path = str(OUTPUT_DIR / out_name)

    # 同格式转换：直接复制，保真
    if src == tgt:
        import shutil
        shutil.copy2(src_path, out_path)
        return out_path

    # Hub-Spoke：源 → Markdown（枢纽）→ 目标
    # 任意两种格式只需各自实现与 Markdown 的双向转换即可互通
    md_text = _read_as_markdown(src_path, src, adjustment)
    _write_from_markdown(md_text, tgt, out_path)
    return out_path


def _read_as_markdown(
    src_path: str,
    src: str,
    adjustment: Optional[str] = None,
) -> str:
    """将源文件解析为 Markdown 文本（hub 的入边）。

    参数:
        src_path: 源文件路径。
        src: 源格式名。
        adjustment: 重试调整策略；use_text_strategy 时 PDF 改用 text 表格策略。
    """
    if src == "pdf":
        pdf_table_settings = None
        if adjustment == "use_text_strategy":
            pdf_table_settings = {
                "vertical_strategy": "text",
                "horizontal_strategy": "text",
            }
        return pdf_converter.pdf_to_markdown(src_path, table_settings=pdf_table_settings)

    if src == "docx":
        return docx_converter.docx_to_markdown(src_path)

    if src == "html":
        html = Path(src_path).read_text(encoding="utf-8")
        return html_converter.html_to_markdown(html)

    if src == "xlsx":
        return excel_converter.excel_to_markdown(src_path)

    if src == "txt":
        text = Path(src_path).read_text(encoding="utf-8")
        return text_converter.text_to_markdown(text)

    # markdown 及其它文本格式：直接读取
    return Path(src_path).read_text(encoding="utf-8")


def _write_from_markdown(md_text: str, tgt: str, out_path: str) -> None:
    """将 Markdown 文本写为目标格式（hub 的出边）。

    参数:
        md_text: Markdown 文本。
        tgt: 目标格式名。
        out_path: 输出文件路径。
    """
    if tgt == "markdown":
        Path(out_path).write_text(md_text, encoding="utf-8")
    elif tgt == "docx":
        docx_converter.markdown_to_docx(md_text, output_path=out_path)
    elif tgt == "html":
        html = html_converter.markdown_to_html(md_text)
        Path(out_path).write_text(html, encoding="utf-8")
    elif tgt == "xlsx":
        excel_converter.markdown_to_excel(md_text, output_path=out_path)
    elif tgt == "pdf":
        docx_converter.markdown_to_pdf(md_text, output_path=out_path)
    elif tgt == "txt":
        text = text_converter.markdown_to_text(md_text)
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        raise ValueError(f"不支持的目标格式: {tgt}")


def _apply_text_operations(md_path: str, options: dict) -> str:
    """对 Markdown 文本应用润色/摘要操作。"""
    if not options:
        return md_path
    text = Path(md_path).read_text(encoding="utf-8")
    if options.get("summarize"):
        text = llm_client.summarize_text(text)
    elif options.get("polish"):
        text = llm_client.polish_text(text)
    Path(md_path).write_text(text, encoding="utf-8")
    return md_path


def _format_to_ext(fmt: str) -> str:
    return {
        "markdown": "md",
        "pdf": "pdf",
        "docx": "docx",
        "html": "html",
        "xlsx": "xlsx",
        "txt": "txt",
    }.get(fmt, fmt)
