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

# 旧版 Office 二进制格式（OLE2 复合文档）文件头魔数：.doc / .xls / .ppt 共用。
# 新版 OOXML（.docx/.xlsx/.pptx）实为 ZIP 容器（魔数 PK\x03\x04），两者可按文件头区分。
LEGACY_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sniff_legacy_binary_format(head: bytes) -> bool:
    """按文件头魔数判断是否为旧版 Office 二进制格式（OLE2 复合文档）。

    扩展名识别只看后缀，用户把旧版 .doc 改名成 .docx 即可骗过；
    故对 OOXML 目标格式（docx/xlsx）再用内容特征复核：旧版是 OLE2 复合文档、
    新版是 ZIP 容器，文件头不同，可据此区分。

    参数:
        head: 文件开头若干字节（≥8 字节即可）。

    返回:
        True = 旧版二进制 Office 格式（.doc/.xls/.ppt）。
    """
    return head[:8] == LEGACY_OLE2_MAGIC


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
    """从指令中推断目标格式。

    规则：目标格式关键词应出现在转换动词（转成/转为/转换/导出为/保存为）之后；
    出现在动词之前的是源格式（如"把这个 Markdown 转成 Word"里的 Markdown），
    不能误判为目标。无动词且只提及源格式时按未指定处理（默认转 markdown）。
    """
    instr_lower = instruction.lower()
    verb_positions = [
        instr_lower.find(v)
        for v in ("转成", "转为", "转换成", "转换", "导出为", "导出", "保存为", "输出为")
        if instr_lower.find(v) >= 0
    ]
    verb_pos = min(verb_positions) if verb_positions else -1

    for fmt, keywords in TARGET_KEYWORDS.items():
        for kw in keywords:
            pos = instr_lower.find(kw)
            if pos < 0:
                continue
            # 关键词与源格式相同且出现在动词之前（或根本没有动词）→ 是源格式，跳过
            if fmt == source_format and (verb_pos < 0 or pos < verb_pos):
                continue
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
