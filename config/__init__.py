from config.config import *

import config.config as _config_module


def reload_config():
    """从数据库加载全部配置，并设置为包级别属性"""
    result = _config_module.reload_config()
    import sys

    pkg = sys.modules[__name__]
    for key, value in result.items():
        setattr(pkg, key, value)
    return result


def __getattr__(name: str):
    """首次访问时自动加载，并将值设为包属性"""
    if name.startswith("_"):
        raise AttributeError(f"module 'config' has no attribute '{name}'")
    value = getattr(_config_module, name)
    import sys

    setattr(sys.modules[__name__], name, value)
    return value