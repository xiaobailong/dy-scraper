# -*- coding: utf-8 -*-
"""
文件存储管理器

封装临时目录/最终目录的创建、清理、文件移动等生命周期管理。
"""

import hashlib
from pathlib import Path

from common.logger import log
from common.utils import safe_rename, safe_unlink


class FileStorageManager:
    """文件存储管理器。

    管理下载流程中的临时目录和最终目录：
    - 启动时检查临时目录残留文件，与目标目录比对去重
    - 下载时 → 写入临时目录（避免与已有文件冲突）
    - 全部 URL 处理完后 → 批量移动到最终目录

    用法：
        storage = FileStorageManager(temp_base, final_base)
        storage.setup()                    # 创建目录并检查去重
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
        """创建所有目录，检查临时目录残留文件并去重。

        如果临时目录已有文件（上次采集未完成）：
        - 先与目标目录比对，删除重复文件
        - 保留不重复的文件，待本次采集完成后统一移动
        - 不再无条件将临时文件移动到目标目录
        """
        self.temp_video_dir.mkdir(parents=True, exist_ok=True)
        self.temp_image_dir.mkdir(parents=True, exist_ok=True)
        self.final_video_dir.mkdir(parents=True, exist_ok=True)
        self.final_image_dir.mkdir(parents=True, exist_ok=True)

        # 检查临时目录是否有残留文件
        has_temp_video = any(f.is_file() for f in self.temp_video_dir.iterdir()) if self.temp_video_dir.exists() else False
        has_temp_image = any(f.is_file() for f in self.temp_image_dir.iterdir()) if self.temp_image_dir.exists() else False

        if has_temp_video or has_temp_image:
            log("\n检测到临时目录有残留文件（上次采集未完成），检查是否与目标目录重复...")
            self._dedup_temp_with_final(self.temp_video_dir, self.final_video_dir, "视频")
            self._dedup_temp_with_final(self.temp_image_dir, self.final_image_dir, "图片")
            log("临时目录去重校验完成\n")

    def _dedup_temp_with_final(self, temp_dir: Path, final_dir: Path, label: str) -> None:
        """检查临时目录文件是否与目标目录重复（按文件名比对），重复则删除临时副本。

        同时按 MD5 二次校验同名文件，避免不同内容同名文件被误删。
        """
        if not temp_dir.exists():
            log(f"  [{label}] 临时目录不存在，跳过")
            return

        temp_files = [f for f in temp_dir.iterdir() if f.is_file()]
        if not temp_files:
            log(f"  [{label}] 临时目录为空")
            return

        final_files = {}
        if final_dir.exists():
            for f in final_dir.iterdir():
                if f.is_file():
                    final_files[f.name] = f

        removed_by_md5 = 0
        kept = 0

        for temp_f in temp_files:
            if temp_f.name not in final_files:
                kept += 1
                continue

            # 同名文件存在，用 MD5 二次校验确认是否真的重复
            try:
                temp_md5 = hashlib.md5(temp_f.read_bytes()).hexdigest()
                final_md5 = hashlib.md5(final_files[temp_f.name].read_bytes()).hexdigest()
                if temp_md5 == final_md5:
                    safe_unlink(temp_f)
                    removed_by_md5 += 1
                else:
                    # 同名但内容不同，保留（可能是不同版本的文件）
                    log(f"  [{label}] 同名但内容不同，保留: {temp_f.name}")
                    kept += 1
            except Exception:
                # MD5 校验失败时按保守策略保留
                kept += 1

        if removed_by_md5 > 0:
            log(f"  [{label}] 删除 {removed_by_md5} 个重复文件，保留 {kept} 个文件")
        elif kept > 0:
            log(f"  [{label}] 未发现重复文件，保留 {kept} 个已有文件")
        else:
            log(f"  [{label}] 未发现重复文件")

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