"""计划/路由模块：从自然语言指令和文件类型解析转换需求。

不依赖 LLM 也能工作；LLM 仅在指令模糊时辅助（阶段一暂用规则）。
"""

import re
from dataclasses import dataclass, field

# 支持的格式
SUPPORTED_FORMATS = {"pdf", "markdown", "md", "docx", "html", "xlsx", "xls", "txt", "text"}

# 格式别名
FORMAT_ALIASES = {
    "md": "markdown",
    "markdown": "markdown",
    "pdf": "pdf",
    "docx": "docx",
    "word": "docx",
    "doc": "docx",
    "html": "html",
    "htm": "html",
    "xlsx": "xlsx",
    "xls": "xlsx",
    "excel": "xlsx",
    "txt": "txt",
    "text": "txt",
}

# 目标格式关键词
TARGET_KEYWORDS = {
    "markdown": ["markdown", "md", "标记"],
    "pdf": ["pdf"],
    "docx": ["word", "docx", "doc"],
    "html": ["html", "网页"],
    "xlsx": ["excel", "xlsx", "xls", "表格文件"],
    "txt": ["txt", "纯文本", "文本文件"],
}

# 特殊要求关键词
OPTION_KEYWORDS = {
    "keep_table": ["表格", "table"],
    "polish": ["润色", "优化", "改通顺"],
    "summarize": ["摘要", "总结", "概括"],
}


@dataclass
class ConvertPlan:
    """转换计划。"""
    source_format: str
    target_format: str
    options: dict = field(default_factory=dict)
    raw_instruction: str = ""

    def to_dict(self) -> dict:
        return {
            "source_format": self.source_format,
            "target_format": self.target_format,
            "options": self.options,
        }


def _detect_source_format(filename: str) -> str:
    """从文件名扩展名推断源格式。"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return FORMAT_ALIASES.get(ext, ext)


def _detect_target_format(instruction: str, source_format: str) -> str:
    """从指令中推断目标格式。"""
    instr_lower = instruction.lower()
    for fmt, keywords in TARGET_KEYWORDS.items():
        for kw in keywords:
            if kw in instr_lower:
                # 避免把源格式误判为目标格式
                if fmt != source_format or "转成" in instruction or "转为" in instruction or "转换" in instruction:
                    return fmt
    return ""


def _detect_options(instruction: str) -> dict:
    """从指令中提取特殊要求。"""
    options = {}
    for opt, keywords in OPTION_KEYWORDS.items():
        for kw in keywords:
            if kw in instruction:
                options[opt] = True
                break
    return options


def parse(instruction: str, filename: str) -> ConvertPlan:
    """解析转换需求。

    参数:
        instruction: 自然语言指令。
        filename: 上传文件名。

    返回:
        ConvertPlan 转换计划。
    """
    source = _detect_source_format(filename)
    target = _detect_target_format(instruction, source)
    options = _detect_options(instruction)

    # 如果指令中没明确目标格式，给出默认（转 markdown）
    if not target:
        target = "markdown"

    return ConvertPlan(
        source_format=source,
        target_format=target,
        options=options,
        raw_instruction=instruction,
    )
