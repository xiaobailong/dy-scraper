# -*- coding: utf-8 -*-
"""
文件存储管理器

封装临时目录/最终目录的创建、清理、文件移动等生命周期管理。
"""

from pathlib import Path

from common.logger import log
from common.utils import safe_rename, safe_unlink


class FileStorageManager:
    """文件存储管理器。

    管理下载流程中的临时目录和最终目录：
    - 下载时 → 写入临时目录（避免与已有文件冲突）
    - 全部 URL 处理完后 → 批量移动到最终目录

    用法：
        storage = FileStorageManager(temp_base, final_base)
        storage.setup()                    # 创建并清空目录
        processor = UrlProcessor(..., temp_video_dir=storage.temp_video_dir, ...)
        # ... 处理所有 URL ...
        storage.move_all_to_final()        # 批量移动
    """

    def __init__(
        self,
        temp_base: Path = Path("C:/Users/766698/Downloads"),
        final_base: Path = Path("D:/TMP/douyin"),
    ):
        self._temp_base = temp_base
        self._final_base = final_base

        self.temp_video_dir = temp_base / "douyin_temp_videos"
        self.temp_image_dir = temp_base / "douyin_temp_images"
        self.final_video_dir = final_base / "videos"
        self.final_image_dir = final_base / "images"

    # ── 目录创建与清理 ──────────────────────────────

    def setup(self) -> None:
        """创建所有目录并清空临时目录"""
        self.temp_video_dir.mkdir(parents=True, exist_ok=True)
        self.temp_image_dir.mkdir(parents=True, exist_ok=True)
        self.final_video_dir.mkdir(parents=True, exist_ok=True)
        self.final_image_dir.mkdir(parents=True, exist_ok=True)
        self._clean_temp(self.temp_video_dir)
        self._clean_temp(self.temp_image_dir)

    def _clean_temp(self, dir_path: Path) -> None:
        """清空临时目录中的所有文件"""
        if not dir_path.exists():
            return
        for f in dir_path.iterdir():
            if f.is_file():
                safe_unlink(f)

    # ── 文件移动 ────────────────────────────────────

    def move_all_to_final(self) -> dict:
        """将所有临时文件移动到最终目录。

        返回 {
            "video": {"moved": int, "skipped": int},
            "image": {"moved": int, "skipped": int},
        }
        """
        log(f"\n{'=' * 60}")
        log(f"  移动文件到最终目录...")
        log(f"{'=' * 60}")

        video_result = self._move_dir(self.temp_video_dir, self.final_video_dir, "视频")
        image_result = self._move_dir(self.temp_image_dir, self.final_image_dir, "图片")
        return {"video": video_result, "image": image_result}

    def _move_dir(self, src_dir: Path, dst_dir: Path, label: str) -> dict:
        """将 src_dir 中的所有文件移动到 dst_dir，跳过已存在的文件。

        返回 {"moved": int, "skipped": int}
        """
        if not src_dir.exists():
            log(f"  {label}: 临时目录不存在，跳过")
            return {"moved": 0, "skipped": 0}

        dst_dir.mkdir(parents=True, exist_ok=True)
        log(f"  {label}最终目录: {dst_dir}")

        moved = 0
        skipped = 0
        for f in src_dir.iterdir():
            if not f.is_file():
                continue
            dst = dst_dir / f.name
            if dst.exists():
                log(f"    跳过 (目标已存在): {f.name}")
                safe_unlink(f)
                skipped += 1
            else:
                try:
                    safe_rename(f, dst)
                    moved += 1
                except Exception as e:
                    log(f"    移动失败: {f.name} - {e}")
                    skipped += 1

        try:
            src_dir.rmdir()
        except Exception:
            pass

        log(f"  {label}: 移动 {moved} 个, 跳过 {skipped} 个")
        return {"moved": moved, "skipped": skipped}