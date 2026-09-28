# -*- coding: utf-8 -*-
"""
rule_adapter.py —— 纯规则转换器的内部适配口（F1 收编）

来源：原 text-converter-assistant 根目录的 converters/（不走模型、零 token 成本）。
F1 定位（见《F1 评审清单》拍板点 3）：
    作为 doc_agent 内的"确定性快速通道"保留，**不做独立服务、不独立监听端口**；
    将来网关/编排层可按"规则能搞定就先走规则，搞不定再升级 LLM Agent"的顺序调用。

本模块只做两件事：
  1. 导入即保证规则转换器全部注册；
  2. 暴露与 Agent 通道风格一致的三个薄函数（列能力 / 文本转换 / 文档导出）。
"""

from typing import Optional

# 导入子包即触发 @register_converter 注册（case/json 转换器）
from .rule_converters import document_converters  # noqa: F401  (markdown/docx/pdf)
from .rule_converters import json_converters      # noqa: F401
from .rule_converters import case_converters      # noqa: F401
from .rule_converters.registry import CONVERTER_REGISTRY, get_converter


def list_converters() -> list:
    """返回当前可用的规则转换类型名列表。"""
    return sorted(CONVERTER_REGISTRY.keys())


def convert_text(text: str, convert_type: str) -> str:
    """
    走规则注册表做纯文本转换（大小写/json 美化等，不调模型）。

    返回转换后的字符串；convert_type 未知时抛 KeyError，
    由上层决定是否降级到 LLM Agent。
    """
    converter = get_converter(convert_type)
    if converter is None:
        raise KeyError("无此规则转换类型：%s" % convert_type)
    return converter(text)


def export_document(text: str, fmt: str) -> str:
    """
    把文本导出为文件（markdown/docx/pdf），返回产物文件路径。

    与原根目录 main.py 的 /export 逻辑一致，仅改为内部函数；
    docx/pdf 的实际转换依赖 weasyprint / pandoc 等系统组件（见 README）。
    """
    markdown_text = document_converters.text_to_markdown(text)
    fmt = fmt.lower()
    if fmt == "markdown":
        import os
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        file_path = os.path.join(tmp_dir, "output.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)
        return file_path
    if fmt == "docx":
        return document_converters.markdown_to_docx(markdown_text)
    if fmt == "pdf":
        return document_converters.markdown_to_pdf(markdown_text)
    raise ValueError("不支持的导出格式：%s（仅 markdown/docx/pdf）" % fmt)
