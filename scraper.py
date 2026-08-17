# -*- coding: utf-8 -*-
"""
抖音网页内容抓取工具 (Playwright 版本)
获取: 页面标题、作者、图片下载地址、视频下载地址、文件大小
并下载图片和视频到本地
"""

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from config import (
    CHROME_PATH,
    DOWNLOAD_IMAGE_DIR,
    DOWNLOAD_VIDEO_DIR,
    RESULT_DIR,
)
from data.db_utils import DBUtils, FileLock
from core.downloader import deduplicate_videos, download_files, extract_urls_from_network, sort_images_by_quality
from common.logger import log
import core.metadata as metadata
from common.utils import clean_title, is_ui_asset, normalize_url, scan_existing_md5s
from data.youdao import fetch_urls_from_youdao

try:
    from playwright.async_api import async_playwright
except ImportError:
    raise SystemExit("请先安装 playwright: pip install playwright")


async def main():
    log("=" * 60)
    log("  抖音网页内容抓取工具 (Playwright)")
    log("=" * 60)

    import config
    config.reload_config()

    # 进程锁：防止多个爬虫实例同时运行（MD5 注册表不共享会导致重复下载）
    from config import DB_FILE
    _process_lock = FileLock(DB_FILE + ".process", timeout=0.5)
    if not _process_lock.acquire():
        log("  检测到另一个爬虫正在运行，退出（避免重复下载）")
        return

    url_list = fetch_urls_from_youdao()
    if not url_list:
        log("未获取到任何 URL，退出")
        _process_lock.release()
        return

    db = DBUtils()
    new_urls = []
    skipped_count = 0
    for u in url_list:
        u = normalize_url(u)
        info = db.get_info(u)
        if info:
            skipped_count += 1
            log(f"  跳过(已处理): {u}  (处理时间: {info['create_time']}, 标题: {info['album_name']})", "debug")
        else:
            new_urls.append(u)
    url_list = new_urls
    if skipped_count:
        log(f"  跳过 {skipped_count} 个已处理的 URL（详见日志文件）")
    if not url_list:
        log("所有 URL 均已处理过，退出")
        _process_lock.release()
        return

    log(f"\n共 {len(url_list)} 个 URL 待处理\n")

    DOWNLOAD_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    log("\n[0/6] 扫描已有文件 MD5，用于去重...")
    md5_registry = scan_existing_md5s(DOWNLOAD_VIDEO_DIR) | scan_existing_md5s(DOWNLOAD_IMAGE_DIR)
    log(f"  已有 {len(md5_registry)} 个文件，将跳过重复下载")

    log("\n[1/6] 启动浏览器...")
    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if CHROME_PATH and Path(CHROME_PATH).exists():
            launch_kwargs["executable_path"] = CHROME_PATH
            log(f"  使用本地浏览器: {CHROME_PATH}")
        else:
            log("  使用 Playwright 自带浏览器")

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        collected_requests = []
        detail_responses = []

        def collect_detail_response(response):
            url = response.url
            if "/aweme/v1/web/aweme/detail/" in url or "/aweme/v1/web/note/" in url:
                detail_responses.append(response)

        page.on("response", collect_detail_response)
        page.on("response", lambda response: collected_requests.append({
            "url": response.url,
            "contentType": response.headers.get("content-type", ""),
            "contentLength": response.headers.get("content-length"),
        }))

        total_stats = {
            "video": {"success": 0, "failed": 0, "skipped_small": 0, "skipped_large": 0, "skipped_dup": 0},
            "image": {"success": 0, "failed": 0, "skipped_small": 0, "skipped_large": 0, "skipped_dup": 0},
        }
        urls_with_downloads = 0

        for url_idx, TARGET_URL in enumerate(url_list, 1):
            collected_requests.clear()
            detail_responses.clear()
            log(f"\n{'=' * 60}")
            log(f"  [{url_idx}/{len(url_list)}] {TARGET_URL}")
            log(f"{'=' * 60}")

            log("[2/6] 访问目标页面...")
            try:
                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  页面加载超时或出错: {e}")
                log("  尝试继续提取已加载的内容...")

            final_url = page.url
            log(f"  最终跳转地址: {final_url}")

            log("[3/6] 等待页面内容渲染...")
            try:
                await page.wait_for_selector('video', timeout=15000)
                log("  视频元素已加载")
            except Exception:
                log("  未检测到视频元素，继续尝试...")
            await asyncio.sleep(5)

            log("[4/6] 提取页面数据...")

            page_data = await metadata.extract_metadata(page, detail_responses)

            network_video_urls, network_image_urls = extract_urls_from_network(collected_requests)

            api_videos = page_data.get("apiVideoUrls") or []
            api_images = page_data.get("apiImageUrls") or []
            dom_videos = page_data.get("videoUrls") or []
            dom_images = page_data.get("imageUrls") or []

            if api_videos:
                log(f"  API获取到 {len(api_videos)} 个视频链接（高清），优先使用")
                all_video_urls = deduplicate_videos(list(dict.fromkeys(api_videos)))
            else:
                log(f"  API未获取到视频链接，退到DOM+网络请求")
                all_video_urls = list(dict.fromkeys(dom_videos + network_video_urls))
                all_video_urls = deduplicate_videos(all_video_urls)

            all_video_urls = [u for u in all_video_urls
                              if not u.startswith("blob:") and not is_ui_asset(u)]

            if api_images:
                log(f"  API/SSR获取到 {len(api_images)} 个图片链接，优先使用")
                all_image_urls = list(dict.fromkeys(api_images))
                all_image_urls = sort_images_by_quality(all_image_urls)
            else:
                log(f"  API/SSR均未获取到图片链接，退到DOM+网络请求")
                all_image_urls = list(dict.fromkeys(dom_images + network_image_urls))
                all_image_urls = sort_images_by_quality(all_image_urls)

            all_image_urls = [u for u in all_image_urls
                              if not u.startswith("blob:") and not is_ui_asset(u)]

            author_info = page_data['author'] or '(未获取到)'
            if page_data.get('authorCode'):
                author_info += f"  (@{page_data['authorCode']})"
            log(f"\n【页面标题】{page_data['title'] or '(未获取到)'}")
            log(f"【作者】{author_info}")
            log(f"【视频】{len(all_video_urls)} 个  【图片】{len(all_image_urls)} 个")

            log(f"\n[5/6] 下载文件...")
            log(f"  视频目录: {DOWNLOAD_VIDEO_DIR}")
            video_download_results = await download_files(
                all_video_urls, DOWNLOAD_VIDEO_DIR, final_url, "video",
                title=page_data["title"], author=page_data["author"], md5_registry=md5_registry
            )

            log(f"  图片目录: {DOWNLOAD_IMAGE_DIR}")
            image_download_results = await download_files(
                all_image_urls, DOWNLOAD_IMAGE_DIR, final_url, "image",
                title=page_data["title"], author=page_data["author"], max_workers=8, md5_registry=md5_registry
            )

            log(f"\n[6/6] 保存结果...")

            video_count = len([r for r in video_download_results if r["status"] in ("downloaded", "skipped_duplicate")])
            video_dup = len([r for r in video_download_results if r["status"] == "skipped_duplicate"])
            image_count = len([r for r in image_download_results if r["status"] in ("downloaded", "skipped_duplicate")])
            image_dup = len([r for r in image_download_results if r["status"] == "skipped_duplicate"])

            dup_info = ""
            if video_dup or image_dup:
                dup_info = f"  去重: 视频{video_dup}个 图片{image_dup}个"
            log(f"  视频: {video_count}/{len(all_video_urls)}  图片: {image_count}/{len(all_image_urls)}{dup_info}")

            STATUS_MAP = {"downloaded": "success", "skipped_duplicate": "success"}

            for r in video_download_results:
                key = STATUS_MAP.get(r["status"], r["status"])
                if key in total_stats["video"]:
                    total_stats["video"][key] += 1
            for r in image_download_results:
                key = STATUS_MAP.get(r["status"], r["status"])
                if key in total_stats["image"]:
                    total_stats["image"][key] += 1

            if video_count > 0 or image_count > 0:
                urls_with_downloads += 1

            if video_count > 0 or image_count > 0:
                db.insert(normalize_url(TARGET_URL), album_name=page_data["author"] or "", album_code=page_data.get("authorCode") or "", remark=page_data["title"] or "")
                log(f"  URL已记录到数据库")
            else:
                log(f"  URL未记录（无成功下载）")

            output_data = {
                "targetUrl": TARGET_URL,
                "finalUrl": page_data["pageUrl"],
                "title": page_data["title"],
                "author": page_data["author"],
                "authorCode": page_data.get("authorCode", ""),
                "description": page_data["description"],
                "coverUrl": page_data.get("coverUrl", ""),
                "scrapeTime": datetime.now().isoformat(),
                "downloadStats": {
                    "videos": {"total": len(all_video_urls), "success": video_count, "duplicate": video_dup},
                    "images": {"total": len(all_image_urls), "success": image_count, "duplicate": image_dup},
                },
                "videos": video_download_results,
                "images": image_download_results,
            }

            RESULT_DIR.mkdir(parents=True, exist_ok=True)
            result_json = json.dumps(output_data, ensure_ascii=False, indent=2)
            result_json_bytes = result_json.encode("utf-8")
            result_md5 = hashlib.md5(result_json_bytes).hexdigest()[:8]
            safe_title = clean_title(page_data["title"])
            safe_author = clean_title(page_data["author"]) if page_data["author"] else ""
            prefix = f"{safe_author}_" if safe_author else ""
            result_time = datetime.now().strftime("%Y%m%d_%H%M%S")
            result_name = f"{prefix}{safe_title}_{result_time}_{result_md5}.json"
            result_path = RESULT_DIR / result_name
            result_path.write_text(result_json, encoding="utf-8")
            log(f"  结果已保存: {result_path}")

        log(f"\n{'=' * 60}")
        log(f"  全部完成! 共处理 {len(url_list)} 个 URL")
        log(f"{'=' * 60}")

        vs = total_stats["video"]
        is_ = total_stats["image"]
        video_total = sum(vs.values())
        image_total = sum(is_.values())

        log(f"\n  ┌─ 视频下载统计 ─────────────────────────────")
        log(f"  │  总计: {video_total} 个")
        log(f"  │  成功: {vs['success']} 个")
        log(f"  │  失败: {vs['failed']} 个")
        if vs['skipped_small']:
            log(f"  │  跳过(小于30KB): {vs['skipped_small']} 个")
        if vs['skipped_large']:
            log(f"  │  跳过(超过10MB): {vs['skipped_large']} 个")
        if vs['skipped_dup']:
            log(f"  │  跳过(MD5重复): {vs['skipped_dup']} 个")
        log(f"  └──────────────────────────────────────────")

        log(f"\n  ┌─ 图片下载统计 ─────────────────────────────")
        log(f"  │  总计: {image_total} 个")
        log(f"  │  成功: {is_['success']} 个")
        log(f"  │  失败: {is_['failed']} 个")
        if is_['skipped_small']:
            log(f"  │  跳过(小于30KB): {is_['skipped_small']} 个")
        if is_['skipped_large']:
            log(f"  │  跳过(超过10MB): {is_['skipped_large']} 个")
        if is_['skipped_dup']:
            log(f"  │  跳过(MD5重复): {is_['skipped_dup']} 个")
        log(f"  └──────────────────────────────────────────")

        total_success = vs['success'] + is_['success']
        total_failed = vs['failed'] + is_['failed']
        total_skipped = vs['skipped_small'] + vs['skipped_large'] + vs['skipped_dup'] + \
                        is_['skipped_small'] + is_['skipped_large'] + is_['skipped_dup']

        log(f"\n  ┌─ 汇总 ────────────────────────────────────")
        log(f"  │  URL 处理数: {len(url_list)} 个")
        log(f"  │  有成功下载的 URL: {urls_with_downloads} 个")
        log(f"  │  文件总数: {video_total + image_total} 个")
        log(f"  │  下载成功: {total_success} 个")
        log(f"  │  下载失败: {total_failed} 个")
        log(f"  │  跳过: {total_skipped} 个")
        log(f"  └──────────────────────────────────────────")
        log(f"{'=' * 60}")

        log("\n关闭浏览器...")
        await browser.close()
        log("完成!")

    _process_lock.release()


if __name__ == "__main__":
    asyncio.run(main())