"""增强规划模块：结构化计划 + 规则路由 + LLM 辅助。

设计原则：
1. 简单请求直接出最短计划，不过度拆解
2. 计划对象贯穿后续每步，可被反思节点更新
3. 不调用 LLM 也有合理默认路由
4. LLM 仅在指令模糊时辅助解析
"""

import logging
from dataclasses import dataclass, field
from typing import List

from skills.doc_agent.planner import (
    FORMAT_ALIASES,
    OPTION_KEYWORDS,
    SUPPORTED_FORMATS,
    _detect_options,
    _detect_source_format,
    _detect_target_format,
)

logger = logging.getLogger("planner_v2")


@dataclass
class TaskPlan:
    """结构化任务计划。"""
    task_type: str                  # "convert" | "rag_qa" | "other"
    source_format: str = ""
    target_format: str = ""
    tools: List[str] = field(default_factory=list)
    steps: List[dict] = field(default_factory=list)
    expected_output: str = ""      # "markdown_file" | "docx_file" | "answer_with_citations"
    options: dict = field(default_factory=dict)
    raw_instruction: str = ""
    retry_hints: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "source_format": self.source_format,
            "target_format": self.target_format,
            "tools": self.tools,
            "steps": self.steps,
            "expected_output": self.expected_output,
            "options": self.options,
            "retry_hints": self.retry_hints,
        }

    def add_retry_hint(self, hint: str):
        """反思节点更新重试提示。"""
        self.retry_hints.append(hint)


# ============================================================
# 规则路由（不调 LLM）
# ============================================================

def create_plan(instruction: str, filename: str) -> TaskPlan:
    """规则路由：从指令+文件名生成结构化计划。

    简单指令 → 最短计划（2步：parse+convert）
    复杂指令 → 完整计划（parse+convert+polish/summarize+reflect）
    """
    source = _detect_source_format(filename)
    target = _detect_target_format(instruction, source)
    options = _detect_options(instruction)

    # 默认目标格式
    if not target:
        target = "markdown"

    # 确定工具
    tools = _select_tools(source, target, options)

    # 构建步骤列表
    steps = _build_steps(source, target, options)

    # 预期产出
    expected_output = _expected_output(target)

    plan = TaskPlan(
        task_type="convert",
        source_format=source,
        target_format=target,
        tools=tools,
        steps=steps,
        expected_output=expected_output,
        options=options,
        raw_instruction=instruction,
    )

    logger.info(
        "规划完成: task_type=%s, %s→%s, tools=%s, steps=%d",
        plan.task_type, source, target, tools, len(steps),
    )

    return plan


def _select_tools(source: str, target: str, options: dict) -> List[str]:
    """根据格式对和选项选择工具。"""
    tools = []

    # 转换工具
    if source == "pdf":
        tools.append("pdfplumber")
    elif source == "docx":
        tools.append("python-docx")
    elif source == "xlsx":
        tools.append("openpyxl")

    if target == "docx" and source != "docx":
        tools.append("pandoc")
    elif target == "html" and source != "html":
        tools.append("markdown")
    elif target == "xlsx":
        tools.append("openpyxl")
    elif target == "pdf":
        tools.append("fpdf2")

    # 后处理工具
    if options.get("polish"):
        tools.append("llm_polish")
    if options.get("summarize"):
        tools.append("llm_summarize")

    # 反思工具
    tools.append("content_parser")  # 校验用
    tools.append("reflection")

    return list(dict.fromkeys(tools))  # 去重保序


def _build_steps(source: str, target: str, options: dict) -> List[dict]:
    """构建步骤列表。"""
    steps = []

    # Step 1: 解析源文件
    steps.append({
        "action": "parse",
        "tool": _parse_tool(source),
        "description": f"解析 {source} 源文件",
    })

    # Step 2: 格式转换
    steps.append({
        "action": "convert",
        "tool": _convert_tool(source, target),
        "params": {"source": source, "target": target},
        "description": f"{source} → {target} 格式转换",
    })

    # Step 3: 后处理（仅当有特殊要求时才加）
    if options.get("polish"):
        steps.append({
            "action": "polish",
            "tool": "llm_polish",
            "description": "LLM 文本润色",
        })

    if options.get("summarize"):
        steps.append({
            "action": "summarize",
            "tool": "llm_summarize",
            "description": "LLM 生成摘要",
        })

    # Step: 反思校验
    steps.append({
        "action": "reflect",
        "tool": "reflection",
        "description": "反思校验：内容指标对比 + 病因诊断",
    })

    return steps


def _parse_tool(source: str) -> str:
    return {
        "pdf": "pdfplumber",
        "docx": "python-docx",
        "markdown": "direct_read",
        "html": "direct_read",
        "xlsx": "openpyxl",
        "txt": "direct_read",
    }.get(source, "direct_read")


def _convert_tool(source: str, target: str) -> str:
    if source == "pdf" and target == "markdown":
        return "pdf_converter"
    if source == "docx" and target == "markdown":
        return "docx_converter"
    if source == "xlsx" and target == "markdown":
        return "excel_converter"
    if source == "html" and target == "markdown":
        return "html_converter"
    if source == "txt" and target == "markdown":
        return "text_converter"
    if target == "docx":
        return "pandoc"
    if target == "html":
        return "markdown"
    if target == "xlsx":
        return "excel_converter"
    if target == "pdf":
        return "fpdf2"
    if target == "txt":
        return "text_converter"
    return "pandoc"


def _expected_output(target: str) -> str:
    return {
        "markdown": "markdown_file",
        "docx": "docx_file",
        "pdf": "pdf_file",
        "html": "html_file",
        "xlsx": "xlsx_file",
        "txt": "text_file",
    }.get(target, "file")

