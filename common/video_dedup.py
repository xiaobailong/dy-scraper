# -*- coding: utf-8 -*-
"""
视频去重工具类

封装视频指纹计算、缓存管理、基于关键帧 pHash 的相似度检测。
"""

import io
import json
import subprocess
from pathlib import Path

from common.logger import log

try:
    from PIL import Image
    import imagehash
    _HASH_AVAILABLE = True
except ImportError:
    _HASH_AVAILABLE = False

# ffmpeg 路径
_FFMPEG_EXE = "ffmpeg"
_FFPROBE_EXE = "ffprobe"
_FFMPEG_AVAILABLE = False

# 指纹算法版本号
_SCHEMA_VERSION = 2

# 视频文件扩展名
_VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.flv', '.avi', '.mkv', '.m4v', '.3gp'}

# 缓存文件名
_CACHE_FILENAME = ".video_hashes.json"


def _init_ffmpeg() -> None:
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
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            r2 = subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
            if r2.returncode == 0:
                _FFMPEG_AVAILABLE = True
    except Exception:
        pass


_init_ffmpeg()


class VideoDedupChecker:
    """视频去重检查器。

    能力：
    - 计算视频关键帧 pHash 指纹
    - 指纹缓存（避免重复计算）
    - 扫描已有视频的指纹注册表
    - 基于多帧 pHash 匹配的相似度去重（保留较大文件）
    """

    def __init__(self):
        self._available = _HASH_AVAILABLE and _FFMPEG_AVAILABLE

    @property
    def available(self) -> bool:
        return self._available

    # ── 指纹计算 ──────────────────────────────────

    def _get_duration(self, file_path: Path) -> float | None:
        """通过 ffprobe 获取视频时长（秒）"""
        if not _FFMPEG_AVAILABLE:
            return None
        try:
            result = subprocess.run(
                [_FFPROBE_EXE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                return float(result.stdout.strip())
        except Exception:
            pass
        return None

    def _extract_frame(self, file_path: Path, time_sec: float) -> bytes | None:
        """通过 ffmpeg 提取视频指定时间点的帧，返回 PNG 字节数据"""
        if not _FFMPEG_AVAILABLE:
            return None
        try:
            result = subprocess.run(
                [_FFMPEG_EXE, "-ss", str(time_sec), "-i", str(file_path),
                 "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:
            pass
        return None

    def compute_fingerprint(self, file_path: Path, num_frames: int = 5) -> list[str] | None:
        """计算视频的感知哈希指纹。

        抽帧策略：
        1. 固定绝对时间帧（0.5s / 1.5s / 3s）：对齐资源管理器封面缩略图区域
        2. 比例分布帧（10% / 50% / 90%）：覆盖视频中后段

        返回每帧的 pHash 列表，失败返回 None。
        """
        if not self._available:
            return None
        if not file_path.exists():
            return None

        duration = self._get_duration(file_path)
        if duration is None or duration <= 0:
            return None

        anchor_times = [0.5, 1.5, 3.0]
        ratio_count = max(0, num_frames - len(anchor_times))
        ratio_times = []
        for i in range(ratio_count):
            t = (duration * (0.1 + 0.8 * i / max(ratio_count - 1, 1))
                 if ratio_count > 1 else duration * 0.5)
            ratio_times.append(t)

        request_times = [t for t in anchor_times if t < duration] + ratio_times
        final_times = []
        seen = set()
        for t in request_times:
            t = min(max(t, 0.1), duration - 0.5)
            key = round(t, 1)
            if key in seen:
                continue
            seen.add(key)
            final_times.append(t)

        hashes = []
        for time_sec in final_times:
            frame_data = self._extract_frame(file_path, time_sec)
            if frame_data is None:
                continue
            try:
                img = Image.open(io.BytesIO(frame_data))
                hashes.append(str(imagehash.phash(img)))
            except Exception:
                continue

        return hashes if len(hashes) >= 3 else None

    # ── 缓存管理 ──────────────────────────────────

    def _load_cache(self, cache_path: Path) -> dict:
        """加载指纹缓存，版本不匹配时返回空"""
        if not cache_path.exists():
            return {"_version": _SCHEMA_VERSION}
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("_version") == _SCHEMA_VERSION:
                return data
        except Exception:
            pass
        return {"_version": _SCHEMA_VERSION}

    def _save_cache(self, cache_path: Path, cache: dict) -> None:
        """保存指纹缓存"""
        try:
            cache["_version"] = _SCHEMA_VERSION
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    # ── 扫描已有视频 ──────────────────────────────────

    def scan_existing(self, directory: Path) -> dict[str, list[str]]:
        """扫描目录下已有视频的 pHash 指纹（使用缓存）。

        返回 {filename: [phash1, phash2, ...]}
        """
        if not self._available:
            return {}
        if not directory.exists():
            return {}

        cache_path = directory / _CACHE_FILENAME
        cache = self._load_cache(cache_path)
        updated = False
        result = {}

        for f in directory.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in _VIDEO_EXTENSIONS:
                continue

            fname = f.name
            cache_key = f"{fname}|{f.stat().st_mtime:.6f}"
            if cache_key in cache:
                result[fname] = cache[cache_key]
                continue

            fingerprint = self.compute_fingerprint(f)
            if fingerprint:
                result[fname] = fingerprint
                cache[cache_key] = fingerprint
                updated = True

        if updated:
            valid_keys = {"_version"} | {
                f"{f.name}|{f.stat().st_mtime:.6f}"
                for f in directory.iterdir()
                if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS
            }
            cache = {k: v for k, v in cache.items() if k in valid_keys}
            self._save_cache(cache_path, cache)

        return result

    # ── 相似度去重 ──────────────────────────────────

    @staticmethod
    def _hash_distance(h1: str, h2: str) -> int:
        """计算两个 pHash 之间的 Hamming 距离"""
        try:
            return int(imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2))
        except Exception:
            return 999

    def check_and_dedup(
        self,
        file_path: Path,
        existing_hashes: dict[str, list[str]],
        min_match_ratio: float = 0.5,
        hamming_threshold: int = 8,
    ) -> bool:
        """检查视频是否与已有视频相似（基于关键帧 pHash 比较）。

        对 current 的每一帧，查找 existing 的所有帧，只要存在任意一帧
        Hamming 距离 <= threshold 即"匹配成功"。
        当匹配帧占比 >= min_match_ratio 时视为重复，删除较小的文件。

        返回 True  → 当前文件被删除
        返回 False → 当前文件保留（无重复 或 当前文件更大，已删除旧文件）
        """
        if not self._available:
            return False
        if not file_path.exists() or not existing_hashes:
            return False

        current_hash = self.compute_fingerprint(file_path)
        if current_hash is None:
            return False

        current_size = file_path.stat().st_size

        for existing_name, existing_fingerprint in existing_hashes.items():
            if not existing_fingerprint:
                continue

            match_count = 0
            for c_hash in current_hash:
                min_dist = min(
                    (self._hash_distance(c_hash, e_hash)
                     for e_hash in existing_fingerprint),
                    default=999,
                )
                if min_dist <= hamming_threshold:
                    match_count += 1

            denom = min(len(current_hash), len(existing_fingerprint))
            if denom == 0:
                continue
            ratio = match_count / denom

            if ratio >= min_match_ratio and match_count >= 2:
                existing_path = file_path.parent / existing_name
                if not existing_path.exists() or existing_path.samefile(file_path):
                    if not existing_path.exists():
                        continue

                if current_size >= existing_path.stat().st_size:
                    from common.utils import safe_unlink
                    safe_unlink(existing_path)
                    existing_hashes.pop(existing_name, None)
                    return False
                else:
                    from common.utils import safe_unlink
                    safe_unlink(file_path)
                    return True

        return False