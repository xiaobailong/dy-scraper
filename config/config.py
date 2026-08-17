# -*- coding: utf-8 -*-
"""全局配置（所有配置值从数据库 config 表读取，代码中不含任何敏感信息）"""

import json
import sys
from pathlib import Path

# 数据库路径
DB_FILE = r"D:\BaiduSyncdisk\douyin.db"

# 类型转换：从数据库读取的字符串 → Python 对象
_TYPE_CONVERTERS = {
    "DOWNLOAD_VIDEO_DIR": Path,
    "DOWNLOAD_IMAGE_DIR": Path,
    "RESULT_DIR": Path,
    "MIN_FILE_SIZE": int,
    "MAX_FILE_SIZE": int,
    "UI_ASSET_DOMAINS": lambda v: set(json.loads(v)),
}

_loaded = False


def reload_config() -> dict:
    """从数据库加载全部配置，并设置为模块属性（后续代码直接通过 config.XXX 访问）"""
    global _loaded

    from data.db_utils import DBUtils

    db = DBUtils()
    stored = db.get_all_config()
    if not stored:
        raise RuntimeError("数据库 config 表为空，请先向 config 表写入配置")

    module = sys.modules[__name__]
    result = {}
    for key, value in stored.items():
        converter = _TYPE_CONVERTERS.get(key)
        try:
            converted = converter(value) if converter else value
        except Exception:
            converted = value
        result[key] = converted
        setattr(module, key, converted)

    _loaded = True
    return result


def __getattr__(name: str):
    """懒加载：首次访问配置项时自动从数据库加载全部配置"""
    if name.startswith("_"):
        raise AttributeError(f"module 'config' has no attribute '{name}'")
    if not _loaded:
        reload_config()
    cfg = {k: v for k, v in sys.modules[__name__].__dict__.items() if not k.startswith("_") and k != "DB_FILE"}
    if name in cfg:
        return cfg[name]
    raise AttributeError(f"module 'config' has no attribute '{name}'")