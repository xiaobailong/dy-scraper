# -*- coding: utf-8 -*-
"""通用工具函数"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from config import UI_ASSET_DOMAINS


def format_bytes(size: int) -> str:
    if not size:
        return "未知"
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024  # type: ignore
    return f"{size:.2f} TB"


def normalize_url(url: str) -> str:
    """规范化 URL 格式，与原有 dy_detail_python 保持一致，确保 DB 去重有效"""
    url = url.strip('/')
    if url.endswith('.com'):
        url += '/'
    return url


def get_file_name_from_url(url: str, fallback: str = "") -> str:
    try:
        path = urlparse(url).path
        name = Path(path).name
        if name:
            return name
    except Exception:
        pass
    return fallback


def get_file_size(url: str) -> int | None:
    try:
        req = Request(url, method="HEAD", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urlopen(req, timeout=10) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length else None
    except Exception:
        return None


def is_ui_asset(url: str) -> bool:
    """判断是否为页面 UI 素材（非内容视频/图片）"""
    url_lower = url.lower()
    domain = urlparse(url).netloc.lower()
    if any(asset_domain in domain for asset_domain in UI_ASSET_DOMAINS):
        return True
    if "twemoji" in url_lower or "emblem" in url_lower:
        return True
    if "100x100" in url_lower or "aweme-avatar" in url_lower:
        return True
    if url_lower.endswith(".avif") and "douyin" not in domain:
        return True
    # 抖音小图标/表情包（obj/tos-cn-i-tsj2vxp0zn 路径通常是 UI 素材）
    if "tsj2vxp0zn" in url_lower and "obj/" in url_lower:
        return True
    return False


def clean_title(title: str) -> str:
    """清理标题，只保留中文汉字、数字、ASCII英文字母、-、_"""
    cleaned = re.sub(r'\s*-\s*抖音$', '', title)
    cleaned = re.sub(r'\d{8}', '', cleaned)
    cleaned = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9_\-]', '', cleaned)
    cleaned = re.sub(r'[_-]{2,}', '_', cleaned)
    cleaned = cleaned.strip('_-')
    return cleaned[:50] if cleaned else "douyin"


def scan_existing_md5s(directory: Path) -> set[str]:
    """扫描目录下已有文件的 MD5 集合，用于去重校验"""
    md5_set = set()
    if not directory.exists():
        return md5_set
    for f in directory.iterdir():
        if f.is_file() and f.suffix.lower() != ".json":
            try:
                md5_set.add(hashlib.md5(f.read_bytes()).hexdigest())
            except Exception:
                pass
    return md5_set


def safe_unlink(path: Path) -> None:
    """安全删除文件，重试3次避免 PermissionError"""
    for attempt in range(3):
        try:
            if path.exists():
                path.unlink()
            return
        except PermissionError:
            if attempt < 2:
                time.sleep(0.3)
        except Exception:
            return


def safe_rename(src: Path, dst: Path) -> bool: # type: ignore
    """安全重命名文件，Windows 下用指数退避重试 8 次（最长约 12.7s），防止杀毒软件等占用"""
    for attempt in range(8):
        try:
            src.rename(dst)
            return True
        except PermissionError:
            if attempt < 7:
                delay = 0.1 * (2 ** attempt)  # 0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.4
                time.sleep(delay)
            else:
                raise