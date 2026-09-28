# converters/case_converters.py

from .registry import register_converter

@register_converter("upper")
def to_upper(text: str) -> str:
    """将文本转为大写"""
    return text.upper()

@register_converter("lower")
def to_lower(text: str) -> str:
    """将文本转为小写"""
    return text.lower()

@register_converter("title")
def to_title(text: str) -> str:
    """将每个单词首字母大写"""
    return text.title()