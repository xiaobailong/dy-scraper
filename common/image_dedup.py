# -*- coding: utf-8 -*-
"""
图片去重工具类

封装图片相似度检测、表情包过滤、封面图识别等逻辑。
"""

import re
from pathlib import Path

from common.logger import log

try:
    from PIL import Image
    import imagehash
    _HASH_AVAILABLE = True
except ImportError:
    _HASH_AVAILABLE = False


# 封面图 URL 特征
_COVER_PATTERNS = [
    r'[?&]cover=',
    r'/cover/',
    r'video_cover',
    r'cover_image',
]

# 表情包/贴纸 URL 特征
_EMOJI_PATTERNS = [
    r'emoticon',
    r'sticker',
    r'emoji',
    r'/obj/tos-cn-i-tsj2vxp0zn/',
    r'gif\.douyinpic\.com',
]

# 图片文件扩展名
_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'}


class ImageDedupChecker:
    """图片去重检查器。

    能力：
    - 计算图片 pHash 指纹
    - 基于 pHash 的相似度去重（保留较大文件）
    - 封面图 URL 识别
    - 表情包/贴纸 URL 识别
    - 表情包尺寸检测（下载后）
    """

    def __init__(self):
        self._available = _HASH_AVAILABLE

    @property
    def available(self) -> bool:
        return self._available

    # ── pHash 指纹 ──────────────────────────────────

    def compute_phash(self, file_path: Path) -> str | None:
        """计算图片的感知哈希（pHash），失败返回 None"""
        if not self._available:
            return None
        try:
            img = Image.open(file_path)
            return str(imagehash.phash(img))
        except Exception:
            return None

    # ── 相似度去重 ──────────────────────────────────

    def check_and_dedup(self, file_path: Path, directory: Path,
                        hamming_threshold: int = 5) -> bool:
        """检查 file_path 是否与 directory 下已有图片 pHash 相似。

        如果相似，删除较小的文件：
        - 返回 True  → 当前文件被删除
        - 返回 False → 当前文件保留（无重复 或 当前文件更大，已删除旧文件）

        Args:
            file_path: 待检查的文件路径
            directory: 已有文件所在目录
            hamming_threshold: Hamming 距离阈值，≤ 此值视为同一张图
        """
        if not self._available:
            return False
        if not file_path.exists():
            return False

        current_hash = self.compute_phash(file_path)
        if current_hash is None:
            return False

        current_size = file_path.stat().st_size

        for existing in directory.iterdir():
            if not existing.is_file():
                continue
            if existing.samefile(file_path):
                continue
            if existing.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue

            existing_hash = self.compute_phash(existing)
            if existing_hash is None:
                continue

            try:
                dist = int(imagehash.hex_to_hash(current_hash) -
                           imagehash.hex_to_hash(existing_hash))
            except Exception:
                continue

            if dist <= hamming_threshold:
                if current_size >= existing.stat().st_size:
                    from common.utils import safe_unlink
                    safe_unlink(existing)
                    return False
                else:
                    from common.utils import safe_unlink
                    safe_unlink(file_path)
                    return True

        return False

    # ── 封面图识别 ──────────────────────────────────

    @staticmethod
    def is_cover_url(url: str) -> bool:
        """根据 URL 特征判断是否为封面图"""
        url_lower = url.lower()
        for pat in _COVER_PATTERNS:
            if re.search(pat, url_lower):
                return True
        return False

    # ── 表情包识别 ──────────────────────────────────

    @staticmethod
    def is_emoji_sticker_url(url: str) -> bool:
        """根据 URL 特征判断是否为表情包/贴纸"""
        url_lower = url.lower()
        for pat in _EMOJI_PATTERNS:
            if re.search(pat, url_lower):
                return True
        return False

    @staticmethod
    def is_emoji_by_dimensions(file_path: Path) -> bool:
        """下载后检查：GIF 文件宽高比接近正方形且尺寸较小，判定为表情包。

        返回 True 表示应删除。
        """
        if file_path.suffix.lower() != '.gif':
            return False
        try:
            img = Image.open(file_path)
            w, h = img.size
            if w <= 0 or h <= 0:
                return False
            ratio = w / h if w < h else h / w
            if ratio > 0.6 and max(w, h) <= 400:
                return True
        except Exception:
            pass
        return False