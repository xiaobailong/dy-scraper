# -*- coding: utf-8 -*-
"""
抖音网页内容抓取工具 (Playwright 版本)
获取: 页面标题、作者、图片下载地址、视频下载地址、文件大小
并下载图片和视频到本地
"""

import asyncio
from pathlib import Path

from config import (
    RESULT_DIR,
)
from data.db_utils import DBUtils, FileLock
from common.logger import log
from core.browser_manager import BrowserManager
from core.url_processor import UrlProcessor
from entity.scrape_stats import ScrapeStats
from common.utils import normalize_url, safe_rename, safe_unlink, scan_existing_md5s
from common.video_dedup import VideoDedupChecker

# 临时下载目录（先下载到这里，全部 URL 处理完再移动到最终目录）
TEMP_DOWNLOAD_BASE = Path("C:/Users/766698/Downloads")
FINAL_BASE_DIR = Path("D:/TMP/douyin")

TEMP_VIDEO_DIR = TEMP_DOWNLOAD_BASE / "douyin_temp_videos"
TEMP_IMAGE_DIR = TEMP_DOWNLOAD_BASE / "douyin_temp_images"

FINAL_VIDEO_DIR = FINAL_BASE_DIR / "videos"
FINAL_IMAGE_DIR = FINAL_BASE_DIR / "images"


# ============================================================
# 工具函数
# ============================================================

def _clean_temp_dir(dir_path: Path) -> None:
    """清空临时目录中的所有文件"""
    if not dir_path.exists():
        return
    for f in dir_path.iterdir():
        if f.is_file():
            safe_unlink(f)


def _move_files_to_final(src_dir: Path, dst_dir: Path) -> tuple[int, int]:
    """将 src_dir 中的所有文件移动到 dst_dir，跳过已存在的文件。
    返回 (移动成功数, 跳过数)"""
    if not src_dir.exists():
        return 0, 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    skipped = 0
    for f in src_dir.iterdir():
        if not f.is_file():
            continue
        dst = dst_dir / f.name
        if dst.exists():
            log(f"  跳过 (目标已存在): {f.name}")
            safe_unlink(f)
            skipped += 1
        else:
            try:
                safe_rename(f, dst)
                moved += 1
            except Exception as e:
                log(f"  移动失败: {f.name} - {e}")
                skipped += 1
    try:
        src_dir.rmdir()
    except Exception:
        pass
    return moved, skipped


# ============================================================
# 步骤函数
# ============================================================

def _fetch_urls() -> tuple[list[str], int, str]:
    """获取并过滤 URL 列表，返回 (url_list, skipped_count, url_source)"""
    import config
    config.reload_config()

    url_source = getattr(config, "URL_SOURCE", "youdao")
    if url_source == "youdao":
        from api.youdao import fetch_urls_from_youdao
        url_list = fetch_urls_from_youdao()
    else:
        from data.local_file import git_pull, fetch_urls_from_local_file
        git_pull()
        default_local_file = Path(__file__).parent / "data" / "tmp.txt"
        if default_local_file.exists():
            url_list = fetch_urls_from_local_file(str(default_local_file))
        else:
            local_file = config.LOCAL_URL_FILE
            url_list = fetch_urls_from_local_file(str(local_file))

    if not url_list:
        return [], 0, url_source

    db = DBUtils()
    new_urls = []
    skipped_count = 0
    for u in url_list:
        u = normalize_url(u)
        info = db.get_info(u)
        if info:
            skipped_count += 1
            log(f"  跳过(已处理): {u}  (处理时间: {info['create_time']}, 标题: {info['album_name']})", "debug")
            continue
        sinfo = db.get_skipped_info(u)
        if sinfo:
            skipped_count += 1
            log(f"  跳过(无内容): {u}  (处理时间: {sinfo['create_time']}, 标题: {sinfo['album_name']})", "debug")
            continue
        new_urls.append(u)

    if skipped_count:
        log(f"  跳过 {skipped_count} 个已处理的 URL（详见日志文件）")

    return new_urls, skipped_count, url_source


def _setup_dirs() -> None:
    """创建并清空临时目录"""
    TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    _clean_temp_dir(TEMP_VIDEO_DIR)
    _clean_temp_dir(TEMP_IMAGE_DIR)


def _scan_dedup_registry() -> tuple[set[str], dict[str, list[str]]]:
    """扫描已有文件 MD5 和视频 pHash，返回 (md5_registry, video_hash_registry)"""
    log("\n[0/6] 扫描已有文件 MD5，用于去重...")
    md5_registry = scan_existing_md5s(FINAL_VIDEO_DIR) | scan_existing_md5s(FINAL_IMAGE_DIR)
    log(f"  已有 {len(md5_registry)} 个文件，将跳过重复下载")

    video_hash_registry = VideoDedupChecker().scan_existing(FINAL_VIDEO_DIR)
    if video_hash_registry:
        log(f"  已扫描 {len(video_hash_registry)} 个视频的 pHash 指纹")
    return md5_registry, video_hash_registry


def _finalize(stats: ScrapeStats, url_source: str, skipped_count: int) -> None:
    """移动文件、打印统计、git 提交"""
    log(f"\n{'=' * 60}")
    log(f"  移动文件到最终目录...")
    log(f"{'=' * 60}")
    log(f"  视频最终目录: {FINAL_VIDEO_DIR}")
    video_moved, video_skipped = _move_files_to_final(TEMP_VIDEO_DIR, FINAL_VIDEO_DIR)
    log(f"  视频: 移动 {video_moved} 个, 跳过 {video_skipped} 个")

    log(f"  图片最终目录: {FINAL_IMAGE_DIR}")
    image_moved, image_skipped = _move_files_to_final(TEMP_IMAGE_DIR, FINAL_IMAGE_DIR)
    log(f"  图片: 移动 {image_moved} 个, 跳过 {image_skipped} 个")

    stats.skipped_url_count = skipped_count
    stats.print_final_summary(url_source)

    if url_source != "youdao" and stats.all_urls_successful:
        log("\n所有 URL 均成功抓取并下载，执行 git 提交...")
        from data.local_file import clear_tmp_and_git_commit_push
        clear_tmp_and_git_commit_push()
    elif url_source == "youdao":
        pass
    else:
        log(f"\n  ⚠️ 未全部成功（成功URL: {stats.urls_with_downloads}/{stats.url_total}"
            f", 失败视频: {stats.video.failed}, 失败图片: {stats.image.failed}），跳过 git 提交")


# ============================================================
# 主入口
# ============================================================

async def main():
    """主函数：协调整个抓取流程。

    流程概览：
    1. 获取 URL 列表（有道云/本地文件）
    2. 过滤已处理的 URL（查数据库去重）
    3. 扫描已有文件 MD5/视频指纹用于下载去重
    4. 启动 Playwright 无头浏览器
    5. 逐个访问 URL，提取页面数据（标题、作者、视频/图片链接）
    6. 并发下载视频和图片文件
    7. 保存结果 JSON 并输出分类统计信息
    """
    log("=" * 60)
    log("  抖音网页内容抓取工具 (Playwright)")
    log("=" * 60)

    # ── 进程锁 ──
    from config import DB_FILE
    _process_lock = FileLock(DB_FILE + ".process", timeout=0.5)
    if not _process_lock.acquire():
        log("  检测到另一个爬虫正在运行，退出（避免重复下载）")
        return

    try:
        # ── 步骤1-2：获取并过滤 URL ──
        url_list, skipped_count, url_source = _fetch_urls()
        if not url_list:
            if url_source != "youdao":
                log("本地文件模式：清空 tmp.txt 并 git 提交...")
                from data.local_file import clear_tmp_and_git_commit_push
                clear_tmp_and_git_commit_push()
            return

        log(f"\n共 {len(url_list)} 个 URL 待处理\n")

        # ── 步骤3：准备目录和去重注册表 ──
        _setup_dirs()
        md5_registry, video_hash_registry = _scan_dedup_registry()

        # ── 步骤4-7：浏览器 + 管线处理 ──
        stats = ScrapeStats(url_total=len(url_list))
        processor = UrlProcessor(
            db=DBUtils(),
            md5_registry=md5_registry,
            video_hash_registry=video_hash_registry,
            temp_video_dir=TEMP_VIDEO_DIR,
            temp_image_dir=TEMP_IMAGE_DIR,
            result_dir=RESULT_DIR,
        )

        async with BrowserManager() as bm:
            last_final_url = None
            for url_idx, target_url in enumerate(url_list, 1):
                ctx, last_final_url = await processor.process(
                    page=bm.page,
                    target_url=target_url,
                    url_idx=url_idx,
                    url_total=len(url_list),
                    collected_requests=bm.collected_requests,
                    detail_responses=bm.detail_responses,
                    stats=stats,
                    last_final_url=last_final_url,
                )
                if ctx is None:
                    continue

        # ── 收尾 ──
        _finalize(stats, url_source, skipped_count)

    finally:
        _process_lock.release()


if __name__ == "__main__":
    asyncio.run(main())