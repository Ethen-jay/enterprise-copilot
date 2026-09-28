# converters/json_converters.py

import json
from .registry import register_converter

@register_converter("json_format")
def json_format(text: str) -> str:
    """美化JSON字符串（添加缩进）"""
    try:
        parsed = json.loads(text)  # 解析JSON字符串
        return json.dumps(parsed, indent=4, ensure_ascii=False)
    except Exception as e:
        return f"错误：无效的JSON - {e}"

@register_converter("json_compact")
def json_compact(text: str) -> str:
    """压缩JSON字符串（去除空格）"""
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)
    except Exception as e:
        return f"错误：无效的JSON - {e}"