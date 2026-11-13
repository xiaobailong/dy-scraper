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
    _HASH_IMPORT_ERROR = None
except ImportError as _e:
    _HASH_AVAILABLE = False
    _HASH_IMPORT_ERROR = _e


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

    _availability_warned = False

    def __init__(self):
        self._available = _HASH_AVAILABLE
        if not self._available and not ImageDedupChecker._availability_warned:
            ImageDedupChecker._availability_warned = True
            log("=" * 62, "error")
            log("  ⚠️ 图片 pHash 去重不可用：缺少依赖 imagehash/Pillow", "error")
            log(f"     错误详情：{_HASH_IMPORT_ERROR}", "error")
            log("     影响：相似图片「保留清晰度高/文件较大」的去重逻辑将被跳过，可能重复下载", "error")
            log("     修复：pip install imagehash pillow", "error")
            log("=" * 62, "error")

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
                        hamming_threshold: int = 5,
                        deleted_names: set | None = None) -> bool:
        """检查 file_path 是否与 directory 下已有图片 pHash 相似。

        如果相似，删除较小的文件：
        - 返回 True  → 当前文件被删除
        - 返回 False → 当前文件保留（无重复 或 当前文件更大，已删除旧文件）

        Args:
            file_path: 待检查的文件路径
            directory: 已有文件所在目录
            hamming_threshold: Hamming 距离阈值，≤ 此值视为同一张图
            deleted_names: 可选集合，用于收集本次被删除的文件名（供调用方
                区分"被去重删除"与"下载后丢失"）
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
            try:
                if existing.samefile(file_path):
                    continue
            except (FileNotFoundError, OSError):
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
                try:
                    existing_size = existing.stat().st_size
                except (FileNotFoundError, OSError):
                    continue
                if current_size >= existing_size:
                    from common.utils import safe_unlink
                    safe_unlink(existing)
                    if deleted_names is not None:
                        deleted_names.add(existing.name)
                    return False
                else:
                    from common.utils import safe_unlink
                    safe_unlink(file_path)
                    if deleted_names is not None:
                        deleted_names.add(file_path.name)
                    return True

        return False

    def check_and_dedup_with_registry(
        self,
        file_path: Path,
        registry: dict[str, str],
        hamming_threshold: int = 5,
    ) -> bool:
        """检查 file_path 是否与全局注册表中的图片 pHash 相似。

        与 check_and_dedup 的区别：不扫描目录，而是对比 registry 中的指纹。
        registry 格式: {完整路径字符串: pHash字符串}，新下载的图片应调用
        add_to_registry 加入，已有文件用 scan_existing 预扫描加入。

        返回 True  → 当前文件被删除
        返回 False → 当前文件保留（无重复 或 当前文件更大，已删除旧文件）
        """
        if not self._available or not file_path.exists():
            return False
        if not registry:
            return False

        current_hash = self.compute_phash(file_path)
        if current_hash is None:
            return False

        current_size = file_path.stat().st_size

        is_dup = False
        to_remove = []
        for existing_key, existing_hash in list(registry.items()):
            try:
                dist = int(imagehash.hex_to_hash(current_hash) -
                           imagehash.hex_to_hash(existing_hash))
            except Exception:
                continue

            if dist <= hamming_threshold:
                existing_path = Path(existing_key)
                if not existing_path.exists():
                    to_remove.append(existing_key)
                    continue
                try:
                    if existing_path.samefile(file_path):
                        continue
                except (FileNotFoundError, OSError):
                    to_remove.append(existing_key)
                    continue

                try:
                    existing_size = existing_path.stat().st_size
                except (FileNotFoundError, OSError):
                    to_remove.append(existing_key)
                    continue

                if current_size >= existing_size:
                    from common.utils import safe_unlink
                    safe_unlink(existing_path)
                    to_remove.append(existing_key)
                else:
                    from common.utils import safe_unlink
                    safe_unlink(file_path)
                    is_dup = True
                break

        for key in to_remove:
            registry.pop(key, None)
        return is_dup

    @staticmethod
    def add_to_registry(registry: dict[str, str], file_path: Path) -> None:
        """将成功下载的图片的 pHash 加入全局注册表。

        注册表 key 使用完整路径字符串，避免跨目录时无法定位文件。
        """
        if registry is None:
            return
        if _HASH_AVAILABLE and file_path.exists():
            try:
                img = Image.open(file_path)
                phash = str(imagehash.phash(img))
                registry[str(file_path)] = phash
            except Exception:
                pass

    def scan_existing(self, directory: Path) -> dict[str, str]:
        """扫描目录下已有图片的 pHash 指纹。

        返回 {完整路径字符串: pHash字符串}
        """
        if not self._available:
            return {}
        if not directory.exists():
            return {}

        result = {}
        for f in directory.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue

            phash = self.compute_phash(f)
            if phash:
                result[str(f)] = phash

        if result:
            log(f"  已扫描 {len(result)} 个图片的 pHash 指纹")
        return result

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