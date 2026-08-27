# -*- coding: utf-8 -*-
"""通用工具函数"""

import hashlib
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from config import UI_ASSET_DOMAINS

try:
    from PIL import Image
    import imagehash
    _HASH_AVAILABLE = True
except ImportError:
    _HASH_AVAILABLE = False

# ffmpeg / ffprobe 路径（从数据库配置读取，未配置则尝试 PATH 中的）
_FFMPEG_EXE = "ffmpeg"
_FFPROBE_EXE = "ffprobe"
_FFMPEG_AVAILABLE = False


def _init_ffmpeg_paths() -> None:
    """从 config 读取 FFMPEG_BIN_DIR，初始化 ffmpeg/ffprobe 路径"""
    global _FFMPEG_EXE, _FFPROBE_EXE, _FFMPEG_AVAILABLE
    try:
        import config
        config.reload_config()
        bin_dir = getattr(config, "FFMPEG_BIN_DIR", None)
        if bin_dir and isinstance(bin_dir, Path) and bin_dir.exists():
            ffmpeg_path = bin_dir / "ffmpeg.exe"
            ffprobe_path = bin_dir / "ffprobe.exe"
            if ffmpeg_path.exists() and ffprobe_path.exists():
                _FFMPEG_EXE = str(ffmpeg_path)
                _FFPROBE_EXE = str(ffprobe_path)
                _FFMPEG_AVAILABLE = True
                return
    except Exception:
        pass

    # 回退：尝试 PATH 中的 ffmpeg
    try:
        _r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if _r.returncode == 0:
            _r2 = subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
            if _r2.returncode == 0:
                _FFMPEG_AVAILABLE = True
    except Exception:
        pass


_init_ffmpeg_paths()


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


# ============================================================
# pHash 图片相似度去重
# ============================================================

def compute_phash(file_path: Path) -> str | None:
    """计算图片的感知哈希（pHash），用于相似度比较。失败返回 None。"""
    if not _HASH_AVAILABLE:
        return None
    try:
        img = Image.open(file_path)
        return str(imagehash.phash(img))
    except Exception:
        return None


def check_and_dedup_phash(file_path: Path, directory: Path) -> bool:
    """检查 file_path 是否与 directory 下已有图片 pHash 相似。
    如果相似，删除文件较小的那个，返回 True 表示当前文件被删除。
    返回 False 表示当前文件保留（无重复或当前文件更大）。
    """
    if not _HASH_AVAILABLE:
        return False
    if not file_path.exists():
        return False

    current_hash = compute_phash(file_path)
    if current_hash is None:
        return False

    current_size = file_path.stat().st_size

    for existing in directory.iterdir():
        if not existing.is_file():
            continue
        if existing.samefile(file_path):
            continue
        if existing.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'):
            continue

        existing_hash = compute_phash(existing)
        if existing_hash is None:
            continue

        # Hamming distance <= 5 视为同一张图
        try:
            dist = imagehash.hex_to_hash(current_hash) - imagehash.hex_to_hash(existing_hash)
        except Exception:
            continue

        if dist <= 5:
            existing_size = existing.stat().st_size
            if current_size >= existing_size:
                safe_unlink(existing)
                return False
            else:
                safe_unlink(file_path)
                return True

    return False


# ============================================================
# 视频相似度去重（基于关键帧 pHash）
# ============================================================

# 视频文件扩展名
_VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.flv', '.avi', '.mkv', '.m4v', '.3gp'}

# 视频指纹缓存文件（放在视频目录下，避免重复计算）
_VIDEO_HASH_CACHE_NAME = ".video_hashes.json"


def _get_video_duration(file_path: Path) -> float | None:
    """通过 ffprobe 获取视频时长（秒）"""
    if not _FFMPEG_AVAILABLE:
        return None
    try:
        result = subprocess.run(
            [_FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _extract_video_frame(file_path: Path, time_sec: float) -> bytes | None:
    """通过 ffmpeg 提取视频指定时间点的帧，返回 PNG 字节数据"""
    if not _FFMPEG_AVAILABLE:
        return None
    try:
        result = subprocess.run(
            [_FFMPEG_EXE, "-ss", str(time_sec), "-i", str(file_path),
             "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=30
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return None


def compute_video_fingerprint(file_path: Path, num_frames: int = 3) -> list[str] | None:
    """计算视频的感知哈希指纹：提取 N 个关键帧，返回每帧的 pHash 列表。
    帧位置均匀分布在视频时长中（如 3 帧则在 10%/50%/90% 处）。

    返回 None 表示无法计算（无 ffmpeg 或视频损坏）。
    """
    if not _FFMPEG_AVAILABLE or not _HASH_AVAILABLE:
        return None
    if not file_path.exists():
        return None

    duration = _get_video_duration(file_path)
    if duration is None or duration <= 0:
        return None

    hashes = []
    for i in range(num_frames):
        time_sec = duration * (0.1 + 0.8 * i / max(num_frames - 1, 1))
        time_sec = min(time_sec, duration - 1.0)
        time_sec = max(time_sec, 0.1)

        frame_data = _extract_video_frame(file_path, time_sec)
        if frame_data is None:
            continue

        try:
            img = Image.open(io.BytesIO(frame_data))
            phash = str(imagehash.phash(img))
            hashes.append(phash)
        except Exception:
            continue

    return hashes if len(hashes) >= 2 else None


def _load_video_hash_cache(cache_path: Path) -> dict[str, list[str]]:
    """加载视频指纹缓存"""
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_video_hash_cache(cache_path: Path, cache: dict[str, list[str]]) -> None:
    """保存视频指纹缓存"""
    try:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _video_hash_distance(h1: str, h2: str) -> int:
    """计算两个 pHash 之间的 Hamming 距离"""
    try:
        return int(imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2))
    except Exception:
        return 999


def scan_existing_video_hashes(directory: Path) -> dict[str, list[str]]:
    """扫描目录下已有视频文件的 pHash 指纹，使用缓存避免重复计算。
    返回 {filename: [phash1, phash2, ...]}"""
    if not _FFMPEG_AVAILABLE or not _HASH_AVAILABLE:
        return {}

    cache_path = directory / _VIDEO_HASH_CACHE_NAME
    cache = _load_video_hash_cache(cache_path)
    updated = False

    result = {}
    if not directory.exists():
        return result

    for f in directory.iterdir():
        if not f.is_file():
            continue
        if f.suffix.lower() not in _VIDEO_EXTENSIONS:
            continue

        fname = f.name
        mtime = f.stat().st_mtime

        cache_key = f"{fname}|{mtime:.6f}"
        if cache_key in cache:
            result[fname] = cache[cache_key]
            continue

        fingerprint = compute_video_fingerprint(f)
        if fingerprint:
            result[fname] = fingerprint
            cache[cache_key] = fingerprint
            updated = True

    if updated:
        valid_keys = {f"{f.name}|{f.stat().st_mtime:.6f}" for f in directory.iterdir()
                      if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS}
        cache = {k: v for k, v in cache.items() if k in valid_keys}
        _save_video_hash_cache(cache_path, cache)

    return result


def check_and_dedup_video_phash(
    file_path: Path,
    existing_hashes: dict[str, list[str]],
    min_match_frames: int = 2,
    hamming_threshold: int = 8,
) -> bool:
    """检查视频是否与已有视频相似（基于关键帧 pHash 比较）。

    对每一帧，与已有视频的对应位置帧比较 Hamming 距离。
    如果匹配帧数 >= min_match_frames，视为重复视频，删除较小的那个。

    返回 True 表示当前文件被删除，False 表示保留。
    """
    if not _FFMPEG_AVAILABLE or not _HASH_AVAILABLE:
        return False
    if not file_path.exists() or not existing_hashes:
        return False

    current_hash = compute_video_fingerprint(file_path)
    if current_hash is None:
        return False

    current_size = file_path.stat().st_size

    for existing_name, existing_fingerprint in existing_hashes.items():
        if not existing_fingerprint:
            continue

        match_count = 0
        total_compared = 0
        for c_hash, e_hash in zip(current_hash, existing_fingerprint):
            dist = _video_hash_distance(c_hash, e_hash)
            total_compared += 1
            if dist <= hamming_threshold:
                match_count += 1

        if total_compared >= min_match_frames and match_count >= min_match_frames:
            existing_path = file_path.parent / existing_name
            if existing_path.exists() and not existing_path.samefile(file_path):
                existing_size = existing_path.stat().st_size
                if current_size >= existing_size:
                    safe_unlink(existing_path)
                    existing_hashes.pop(existing_name, None)
                    return False
                else:
                    safe_unlink(file_path)
                    return True

    return False


# ============================================================
# 图片 URL 过滤（封面图、表情包/贴纸）
# ============================================================

# 封面图 URL 特征
_COVER_URL_PATTERNS = [
    r'[?&]cover=',
    r'/cover/',  # 封面图路径
    r'video_cover',
    r'cover_image',
]

# 表情包/贴纸 URL 特征
_EMOJI_URL_PATTERNS = [
    r'emoticon',
    r'sticker',
    r'emoji',
    r'/obj/tos-cn-i-tsj2vxp0zn/',  # 抖音表情包 CDN
    r'gif\.douyinpic\.com',  # GIF 表情
]

# 封面图尺寸阈值（宽高比接近 9:16 竖屏封面，且边长较小）
_COVER_ASPECT_RATIO_MIN = 0.5   # 9:16 ≈ 0.5625
_COVER_ASPECT_RATIO_MAX = 0.65
_COVER_MAX_WIDTH = 800  # 封面图宽一般不超过 800px


def is_cover_image_url(url: str) -> bool:
    """根据 URL 特征判断是否为封面图"""
    url_lower = url.lower()
    for pat in _COVER_URL_PATTERNS:
        if re.search(pat, url_lower):
            return True
    return False


def is_emoji_sticker_url(url: str) -> bool:
    """根据 URL 特征判断是否为表情包/贴纸"""
    url_lower = url.lower()
    for pat in _EMOJI_URL_PATTERNS:
        if re.search(pat, url_lower):
            return True
    return False


def is_emoji_by_dimensions(file_path: Path) -> bool:
    """下载后检查：GIF 文件宽高比接近正方形且尺寸较小，判定为表情包。
    返回 True 表示应删除。"""
    if file_path.suffix.lower() != '.gif':
        return False
    try:
        img = Image.open(file_path)
        w, h = img.size
        if w <= 0 or h <= 0:
            return False
        ratio = w / h if w < h else h / w
        # 接近正方形 + 边长 <= 400px → 表情包
        if ratio > 0.6 and max(w, h) <= 400:
            return True
    except Exception:
        pass
    return False