# converters/registry.py

# 转换器注册表：存储转换类型名称 -> 转换函数
CONVERTER_REGISTRY = {}

def register_converter(name):
    """装饰器：将函数注册到注册表中"""
    def decorator(func):
        CONVERTER_REGISTRY[name] = func
        return func
    return decorator

def get_converter(name):
    """根据名称获取转换函数"""
    return CONVERTER_REGISTRY.get(name)