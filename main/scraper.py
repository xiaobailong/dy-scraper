# -*- coding: utf-8 -*-
"""
抖音网页内容抓取工具 (Playwright 版本)
获取: 页面标题、作者、图片下载地址、视频下载地址、文件大小
并下载图片和视频到本地
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from data.db_utils import DBUtils, FileLock
from data.db_sync_manager import SyncManager
from common.logger import log
from core.browser_manager import BrowserManager
from core.url_processor import UrlProcessor
from core.file_storage import FileStorageManager
from entity.scrape_stats import ScrapeStats
from common.utils import normalize_url, scan_existing_md5s
from common.video_dedup import VideoDedupChecker
from common.image_dedup import ImageDedupChecker

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
        default_local_file = Path(__file__).parent.parent / "src" / "data" / "tmp.txt"
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
            reason_info = f" 原因: {sinfo.get('skip_reason', '')}" if sinfo.get('skip_reason') else ""
            log(f"  跳过(无内容): {u}  (处理时间: {sinfo['create_time']}, 标题: {sinfo['album_name']}{reason_info})", "debug")
            continue
        new_urls.append(u)

    if skipped_count:
        log(f"  跳过 {skipped_count} 个已处理的 URL（详见日志文件）")

    return new_urls, skipped_count, url_source


def _unique_dirs(*dirs) -> list[Path]:
    """去重后的目录列表（保持顺序，跳过 None/空值，Windows 路径不区分大小写）"""
    result: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        if not d:
            continue
        p = Path(d)
        key = str(p).rstrip("\\/").lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def _dedup_scan_dirs(storage: FileStorageManager) -> tuple[list[Path], list[Path]]:
    """汇总所有需要参与去重的图片/视频目录。

    除 FileStorageManager 的最终目录、临时目录外，还包括数据库配置的
    DOWNLOAD_IMAGE_DIR / DOWNLOAD_VIDEO_DIR（历史下载目录，可能不在最终目录下）。
    """
    import config

    image_dirs = _unique_dirs(
        storage.final_image_dir,
        storage.temp_image_dir,
        getattr(config, "DOWNLOAD_IMAGE_DIR", None),
    )
    video_dirs = _unique_dirs(
        storage.final_video_dir,
        storage.temp_video_dir,
        getattr(config, "DOWNLOAD_VIDEO_DIR", None),
    )
    return image_dirs, video_dirs


def _scan_dedup_registry(storage: FileStorageManager) -> tuple[set[str], dict[str, list[str]], dict[str, str]]:
    """扫描已有文件 MD5、视频 pHash 和图片 pHash，返回 (md5_registry, video_hash_registry, image_hash_registry)

    扫描范围 = 最终目录 + 临时目录（上次采集残留）+ 配置的历史下载目录，三者都会参与去重。
    """
    log("\n[0/6] 扫描已有文件 MD5，用于去重...")
    image_dirs, video_dirs = _dedup_scan_dirs(storage)

    log(f"  图片扫描源({len(image_dirs)}):")
    for d in image_dirs:
        log(f"    - {d}")
    log(f"  视频扫描源({len(video_dirs)}):")
    for d in video_dirs:
        log(f"    - {d}")

    md5_registry: set[str] = set()
    for d in video_dirs + image_dirs:
        md5_registry |= scan_existing_md5s(d)
    log(f"  已有 {len(md5_registry)} 个文件，将跳过重复下载")

    video_dedup = VideoDedupChecker()
    video_hash_registry: dict[str, list[str]] = {}
    for d in video_dirs:
        video_hash_registry.update(video_dedup.scan_existing(d))
    log(f"  视频 pHash 注册表合计 {len(video_hash_registry)} 条（{len(video_dirs)} 个目录）")

    image_dedup = ImageDedupChecker()
    image_hash_registry: dict[str, str] = {}
    for d in image_dirs:
        image_hash_registry.update(image_dedup.scan_existing(d))
    log(f"  图片 pHash 注册表合计 {len(image_hash_registry)} 条（{len(image_dirs)} 个目录）")
    if not image_dedup.available:
        log("  ⚠️ 图片 pHash 不可用，本次相似图片去重（保留较大/清晰）不会生效！", "error")
    return md5_registry, video_hash_registry, image_hash_registry


def _finalize(stats: ScrapeStats, storage: FileStorageManager, url_source: str, skipped_count: int) -> None:
    """移动文件、打印统计、git 提交"""
    storage.move_all_to_final()

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

    # ── 同步管理器：拷贝 → 本地库 → 爬虫 → 合并 → 源库 ──
    from config import DB_FILE
    _original_db = DB_FILE
    _data_dir = Path(__file__).parent.parent / "data"

    # ── 进程锁（放在本地 data/ 目录，避免触发百度网盘同步）──
    _process_lock = FileLock(str(_data_dir / "scraper.process"), timeout=0.5)
    if not _process_lock.acquire():
        log("  检测到另一个爬虫正在运行，退出（避免重复下载）")
        return
    _sync_mgr = SyncManager(_original_db, _data_dir)

    # 1. 恢复上次可能未完成的合并（崩溃/异常中断等场景）
    _sync_mgr.recover_pending()

    # 2. 拷贝源库到本地（带时间戳版本），避免抓取过程中频繁触发百度网盘同步
    _temp_db = _sync_mgr.copy_to_local()

    # 后续所有数据库操作走本地临时文件，不再触发网盘 IO
    import config
    config.DB_FILE = _temp_db

    try:
        # ── 步骤1-2：获取并过滤 URL ──
        url_list, skipped_count, url_source = _fetch_urls()

        # ── 步骤3：准备目录和去重注册表 ──
        storage = FileStorageManager()
        storage.setup()

        if not url_list:
            log(f"\n  共 0 个 URL 待处理，全部已处理过")
            # 将上次运行遗留的临时文件移动到最终目录
            storage.move_all_to_final()
            if url_source != "youdao":
                log("本地文件模式：清空 tmp.txt 并 git 提交...")
                from data.local_file import clear_tmp_and_git_commit_push
                clear_tmp_and_git_commit_push()
            return

        log(f"\n共 {len(url_list)} 个 URL 待处理\n")
        md5_registry, video_hash_registry, image_hash_registry = _scan_dedup_registry(storage)

        # ── 步骤4-7：浏览器 + 管线处理 ──
        stats = ScrapeStats(url_total=len(url_list))
        processor = UrlProcessor(
            db=DBUtils(),
            md5_registry=md5_registry,
            video_hash_registry=video_hash_registry,
            image_hash_registry=image_hash_registry,
            temp_video_dir=storage.temp_video_dir,
            temp_image_dir=storage.temp_image_dir,
        )

        async with BrowserManager() as bm:
            last_final_url = None
            for url_idx, target_url in enumerate(url_list, 1):
                try:
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
                except ConnectionError:
                    log(f"\n  ⚠️ 浏览器连接断开，停止处理剩余 {len(url_list) - url_idx} 个 URL")
                    break
                if ctx is None:
                    continue

        # ── 收尾 ──
        _finalize(stats, storage, url_source, skipped_count)

    finally:
        # ── 3. 将本地库新增数据合并到百度网盘同步目录（增量追加，不覆盖已有数据）──
        log(f"\n  正在将新增数据合并回百度网盘...")
        try:
            _sync_mgr.merge_to_original(_temp_db)
            log(f"  新增数据已合并至: {_original_db}")
        except Exception as e:
            log(f"  ⚠️ 数据合并失败: {e}", "error")
            log(f"  本地库完整保留在 {_temp_db}，下次启动自动恢复")

        _process_lock.release()


if __name__ == "__main__":
    asyncio.run(main())