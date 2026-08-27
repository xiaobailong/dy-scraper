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
    MAX_FILE_SIZE,
    MIN_FILE_SIZE,
    RESULT_DIR,
)
from data.db_utils import DBUtils, FileLock
from core.downloader import deduplicate_videos, download_files, extract_urls_from_network, sort_images_by_quality
from common.logger import log
import core.metadata as metadata
from common.utils import clean_title, format_bytes, is_cover_image_url, is_emoji_sticker_url, is_ui_asset, normalize_url, safe_rename, safe_unlink, scan_existing_md5s, scan_existing_video_hashes
from api.youdao import fetch_urls_from_youdao
from api.douyin_detail import create_detail_response_collector
from data.local_file import fetch_urls_from_local_file

try:
    from playwright.async_api import async_playwright
except ImportError:
    raise SystemExit("请先安装 playwright: pip install playwright")

# 临时下载目录（先下载到这里，全部 URL 处理完再移动到最终目录）
TEMP_DOWNLOAD_BASE = Path("C:/Users/766698/Downloads")
FINAL_BASE_DIR = Path("D:/TMP/douyin")

# 临时下载子目录
TEMP_VIDEO_DIR = TEMP_DOWNLOAD_BASE / "douyin_temp_videos"
TEMP_IMAGE_DIR = TEMP_DOWNLOAD_BASE / "douyin_temp_images"

# 最终目录
FINAL_VIDEO_DIR = FINAL_BASE_DIR / "videos"
FINAL_IMAGE_DIR = FINAL_BASE_DIR / "images"


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


async def main():
    """主函数：协调整个抓取流程。

    流程概览：
    1. 获取 URL 列表（有道云/本地文件）
    2. 过滤已处理的 URL（查数据库去重）
    3. 扫描已有文件 MD5 用于下载去重
    4. 启动 Playwright 无头浏览器
    5. 逐个访问 URL，提取页面数据（标题、作者、视频/图片链接）
    6. 并发下载视频和图片文件
    7. 保存结果 JSON 并输出分类统计信息
    """
    log("=" * 60)
    log("  抖音网页内容抓取工具 (Playwright)")
    log("=" * 60)

    import config
    config.reload_config()

    # ── 进程锁：防止多个爬虫实例同时运行 ──
    # MD5 注册表不共享，多实例会导致重复下载
    from config import DB_FILE
    _process_lock = FileLock(DB_FILE + ".process", timeout=0.5)
    if not _process_lock.acquire():
        log("  检测到另一个爬虫正在运行，退出（避免重复下载）")
        return

    # ── 步骤1：获取 URL 列表 ──
    # 根据配置选择来源：有道云笔记 或 本地文件
    url_source = getattr(config, "URL_SOURCE", "youdao")
    if url_source == "youdao":
        url_list = fetch_urls_from_youdao()
    else:
        # 本地文件模式：先拉取最新代码
        from data.local_file import git_pull
        git_pull()
        default_local_file = Path(__file__).parent / "data" / "tmp.txt"
        if default_local_file.exists():
            url_list = fetch_urls_from_local_file(str(default_local_file))
        else:
            local_file = config.LOCAL_URL_FILE
            url_list = fetch_urls_from_local_file(str(local_file))
    if not url_list:
        log("未获取到任何 URL，退出")
        _process_lock.release()
        return

    # ── 步骤2：过滤已处理的 URL ──
    # 查询数据库，跳过已抓取过或已确认无内容的 URL
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
    url_list = new_urls
    if skipped_count:
        log(f"  跳过 {skipped_count} 个已处理的 URL（详见日志文件）")
    if not url_list:
        log("所有 URL 均已处理过，退出")
        # 本地文件模式：即使全部已处理，也清空 tmp.txt 并 git 提交
        if url_source != "youdao":
            log("本地文件模式：清空 tmp.txt 并 git 提交...")
            from data.local_file import clear_tmp_and_git_commit_push
            clear_tmp_and_git_commit_push()
        _process_lock.release()
        return

    log(f"\n共 {len(url_list)} 个 URL 待处理\n")

    # 确保下载目录存在（临时目录 + 最终目录）
    TEMP_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    FINAL_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    # 清空临时目录中上次可能残留的文件
    _clean_temp_dir(TEMP_VIDEO_DIR)
    _clean_temp_dir(TEMP_IMAGE_DIR)

    # ── 步骤3：扫描已有文件 MD5 注册表 ──
    # 扫描最终目录，用于下载时跳过内容完全相同的文件
    log("\n[0/6] 扫描已有文件 MD5，用于去重...")
    md5_registry = scan_existing_md5s(FINAL_VIDEO_DIR) | scan_existing_md5s(FINAL_IMAGE_DIR)
    log(f"  已有 {len(md5_registry)} 个文件，将跳过重复下载")

    # 扫描已有视频的 pHash 指纹，用于相似视频去重（不同码率/编码的同一视频）
    video_hash_registry = scan_existing_video_hashes(FINAL_VIDEO_DIR)
    if video_hash_registry:
        log(f"  已扫描 {len(video_hash_registry)} 个视频的 pHash 指纹")

    # ── 步骤4：启动 Playwright 无头浏览器 ──
    log("\n[1/6] 启动浏览器...")
    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,  # 无头模式，不显示浏览器窗口
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",  # 隐藏自动化特征
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

        # 注入反检测脚本：隐藏 webdriver 等自动化标记
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        # 收集页面加载期间的所有网络请求
        collected_requests = []
        # 收集抖音详情 API 的响应（用于提取高清视频/图片链接）
        detail_responses = []

        page.on("response", create_detail_response_collector(detail_responses))
        page.on("response", lambda response: collected_requests.append({
            "url": response.url,
            "contentType": response.headers.get("content-type", ""),
            "contentLength": response.headers.get("content-length"),
        }))

        # 全局下载统计（跨所有 URL）
        total_stats = {
            "video": {"success": 0, "failed": 0, "skipped_small": 0, "skipped_large": 0, "skipped_dup": 0, "skipped_video_phash_dup": 0},
            "image": {"success": 0, "failed": 0, "skipped_small": 0, "skipped_large": 0, "skipped_dup": 0},
        }
        urls_with_downloads = 0  # 有成功下载的 URL 数量

        # ── 步骤5：逐个处理 URL ──
        _last_final_url = None  # 记录上一个 URL 的最终跳转地址，防止重复处理
        for url_idx, TARGET_URL in enumerate(url_list, 1):
            # 使用索引切片代替 clear()，避免上一页面 pending 的响应在 goto() 期间混入
            _req_start = len(collected_requests)
            _detail_start = len(detail_responses)
            log(f"\n{'=' * 60}")
            log(f"  [{url_idx}/{len(url_list)}] {TARGET_URL}")
            log(f"{'=' * 60}")

            # 访问目标页面
            log("[2/6] 访问目标页面...")
            goto_ok = True
            try:
                await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  页面加载超时或出错: {e}")
                goto_ok = False

            final_url = page.url
            log(f"  最终跳转地址: {final_url}")

            # 异常检查：页面加载失败
            if not goto_ok:
                log(f"  ⚠️ 页面加载失败（状态不确定，可能混用新旧页面内容），跳过当前 URL")
                continue

            # 长链接去重检查：检查最终跳转地址是否已被其他短链接处理过
            final_info = db.get_by_final_url(final_url)
            if final_info:
                log(f"  ⚠️ 长链接重复（最终地址已被处理过），跳过")
                log(f"     首次处理短链接: {final_info['short_url']}")
                log(f"     首次处理时间: {final_info['create_time']}")
                continue

            # 异常检查：跳转前后地址相同，可能抖音拦截
            if final_url == _last_final_url:
                log(f"  ⚠️ 跳转前后地址相同，可能未成功进入新页面，跳过")
                continue
            _last_final_url = final_url

            # 异常检查：页面不存在
            if any(s in final_url for s in ("/notfound", "/404", "/about:blank", "/error")):
                log(f"  ⚠️ 目标页面不存在（{final_url}），跳过")
                continue

            # 等待页面内容渲染完成
            log("[3/6] 等待页面内容渲染...")
            try:
                await page.wait_for_selector('video', timeout=15000)
                log("  视频元素已加载")
            except Exception:
                log("  未检测到视频元素，继续尝试...")
            await asyncio.sleep(5)  # 额外等待异步加载的内容

            # ── 提取页面元数据 ──
            log("[4/6] 提取页面数据...")

            # 只取 goto() 开始后新增的响应，防止上一页面的 pending 响应混入
            _new_detail = detail_responses[_detail_start:]
            page_data = await metadata.extract_metadata(page, _new_detail)

            _new_requests = collected_requests[_req_start:]
            network_video_urls, network_image_urls = extract_urls_from_network(_new_requests)

            # 提取视频/图片链接（优先级：API > SSR > DOM > 网络请求）
            api_videos = page_data.get("apiVideoUrls") or []
            api_images = page_data.get("apiImageUrls") or []
            dom_videos = page_data.get("videoUrls") or []
            dom_images = page_data.get("imageUrls") or []

            # 视频链接去重和过滤
            if api_videos:
                log(f"  API获取到 {len(api_videos)} 个视频链接（高清），优先使用")
                all_video_urls = deduplicate_videos(list(dict.fromkeys(api_videos)))
            else:
                log(f"  API未获取到视频链接，退到DOM+网络请求")
                all_video_urls = list(dict.fromkeys(dom_videos + network_video_urls))
                all_video_urls = deduplicate_videos(all_video_urls)

            all_video_urls = [u for u in all_video_urls
                              if not u.startswith("blob:") and not is_ui_asset(u)]

            # 图片链接去重和过滤（按质量排序）
            if api_images:
                log(f"  API/SSR获取到 {len(api_images)} 个图片链接，优先使用")
                all_image_urls = list(dict.fromkeys(api_images))
                all_image_urls = sort_images_by_quality(all_image_urls)
            else:
                log(f"  API/SSR均未获取到图片链接，退到DOM+网络请求")
                all_image_urls = list(dict.fromkeys(dom_images + network_image_urls))
                all_image_urls = sort_images_by_quality(all_image_urls)

            all_image_urls = [u for u in all_image_urls
                              if not u.startswith("blob:") and not is_ui_asset(u)
                              and not is_cover_image_url(u)
                              and not is_emoji_sticker_url(u)]

            # 输出提取结果摘要
            author_info = page_data['author'] or '(未获取到)'
            if page_data.get('authorCode'):
                author_info += f"  (@{page_data['authorCode']})"
            log(f"\n【页面标题】{page_data['title'] or '(未获取到)'}")
            log(f"【作者】{author_info}")
            log(f"【视频】{len(all_video_urls)} 个  【图片】{len(all_image_urls)} 个")

            # ── 步骤6：下载文件 ──
            log(f"\n[5/6] 下载文件...")
            log(f"  视频临时目录: {TEMP_VIDEO_DIR}")
            video_download_results = await download_files(
                all_video_urls, TEMP_VIDEO_DIR, final_url, "video",
                title=page_data["title"], author=page_data["author"], md5_registry=md5_registry,
                video_hash_registry=video_hash_registry
            )

            log(f"  图片临时目录: {TEMP_IMAGE_DIR}")
            image_download_results = await download_files(
                all_image_urls, TEMP_IMAGE_DIR, final_url, "image",
                title=page_data["title"], author=page_data["author"], max_workers=8, md5_registry=md5_registry,
                video_hash_registry=video_hash_registry
            )

            # ── 步骤7：保存结果 ──
            log(f"\n[6/6] 保存结果...")

            # 统计下载结果（含去重计数）
            video_count = len([r for r in video_download_results if r["status"] in ("downloaded", "skipped_duplicate")])
            video_dup = len([r for r in video_download_results if r["status"] == "skipped_duplicate"])
            image_count = len([r for r in image_download_results if r["status"] in ("downloaded", "skipped_duplicate")])
            image_dup = len([r for r in image_download_results if r["status"] == "skipped_duplicate"])

            # 因大小被跳过的数量（有 URL 但不符合大小条件）
            video_size_skipped = len([r for r in video_download_results if r["status"] in ("skipped_small", "skipped_large")])
            image_size_skipped = len([r for r in image_download_results if r["status"] in ("skipped_small", "skipped_large")])

            dup_info = ""
            if video_dup or image_dup:
                dup_info = f"  去重: 视频{video_dup}个 图片{image_dup}个"
            log(f"  视频: {video_count}/{len(all_video_urls)}  图片: {image_count}/{len(all_image_urls)}{dup_info}")

            # 将下载状态映射到统计分类
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

            # 记录到数据库：标记该 URL 已处理
            if video_count > 0 or image_count > 0:
                db.insert(normalize_url(TARGET_URL), album_name=page_data["author"] or "", album_code=page_data.get("authorCode") or "", remark=page_data["title"] or "")
                db.insert_final_url(final_url, TARGET_URL)
                log(f"  URL已记录到数据库")
            elif video_size_skipped > 0 or image_size_skipped > 0:
                db.insert_skipped(normalize_url(TARGET_URL), album_name=page_data["author"] or "", album_code=page_data.get("authorCode") or "", remark=page_data["title"] or "")
                log(f"  URL记录到跳过表（有{len(all_video_urls) + len(all_image_urls)}个媒体URL但均因大小被跳过）")
            else:
                log(f"  URL未记录（无媒体URL）")

            # 构建结果 JSON 并保存
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

        # ── 全部 URL 处理完毕，移动文件到最终目录 ──
        log(f"\n{'=' * 60}")
        log(f"  移动文件到最终目录...")
        log(f"{'=' * 60}")
        log(f"  视频最终目录: {FINAL_VIDEO_DIR}")
        video_moved, video_skipped = _move_files_to_final(TEMP_VIDEO_DIR, FINAL_VIDEO_DIR)
        log(f"  视频: 移动 {video_moved} 个, 跳过 {video_skipped} 个")

        log(f"  图片最终目录: {FINAL_IMAGE_DIR}")
        image_moved, image_skipped = _move_files_to_final(TEMP_IMAGE_DIR, FINAL_IMAGE_DIR)
        log(f"  图片: 移动 {image_moved} 个, 跳过 {image_skipped} 个")

        # ── 全部 URL 处理完毕，输出统计 ──
        log(f"\n{'=' * 60}")
        log(f"  全部完成! 共处理 {len(url_list)} 个 URL")
        log(f"{'=' * 60}")

        vs = total_stats["video"]
        is_ = total_stats["image"]
        video_total = sum(vs.values())
        image_total = sum(is_.values())

        # 视频下载统计
        log(f"\n  ┌─ 视频下载统计 ─────────────────────────────")
        log(f"  │  总计: {video_total} 个")
        log(f"  │  成功: {vs['success']} 个")
        log(f"  │  失败: {vs['failed']} 个")
        if vs['skipped_small']:
            log(f"  │  跳过(小于{format_bytes(MIN_FILE_SIZE)}): {vs['skipped_small']} 个")
        if vs['skipped_large']:
            log(f"  │  跳过(超过{format_bytes(MAX_FILE_SIZE)}): {vs['skipped_large']} 个")
        if vs['skipped_dup']:
            log(f"  │  跳过(MD5重复): {vs['skipped_dup']} 个")
        if vs['skipped_video_phash_dup']:
            log(f"  │  跳过(视频pHash重复): {vs['skipped_video_phash_dup']} 个")
        log(f"  └──────────────────────────────────────────")

        # 图片下载统计
        log(f"\n  ┌─ 图片下载统计 ─────────────────────────────")
        log(f"  │  总计: {image_total} 个")
        log(f"  │  成功: {is_['success']} 个")
        log(f"  │  失败: {is_['failed']} 个")
        if is_['skipped_small']:
            log(f"  │  跳过(小于{format_bytes(MIN_FILE_SIZE)}): {is_['skipped_small']} 个")
        if is_['skipped_large']:
            log(f"  │  跳过(超过{format_bytes(MAX_FILE_SIZE)}): {is_['skipped_large']} 个")
        if is_['skipped_dup']:
            log(f"  │  跳过(MD5重复): {is_['skipped_dup']} 个")
        log(f"  └──────────────────────────────────────────")

        total_success = vs['success'] + is_['success']
        total_failed = vs['failed'] + is_['failed']
        total_skipped = vs['skipped_small'] + vs['skipped_large'] + vs['skipped_dup'] + vs['skipped_video_phash_dup'] + \
                        is_['skipped_small'] + is_['skipped_large'] + is_['skipped_dup']

        # 汇总统计
        log(f"\n  ┌─ 汇总 ────────────────────────────────────")
        log(f"  │  URL 总数: {len(url_list) + skipped_count} 个")
        log(f"  │  本次处理: {len(url_list)} 个")
        if skipped_count:
            log(f"  │  跳过(已处理URL): {skipped_count} 个")
        log(f"  │  有成功下载的 URL: {urls_with_downloads} 个")
        log(f"  │  文件总数: {video_total + image_total} 个")
        log(f"  │  下载成功: {total_success} 个")
        log(f"  │  下载失败: {total_failed} 个")
        log(f"  │  跳过(文件): {total_skipped} 个")
        log(f"  └──────────────────────────────────────────")
        log(f"{'=' * 60}")

        # ── 本地文件模式：全部 URL 成功抓取并下载后，清空 tmp.txt 并 git 提交 ──
        all_urls_successful = (urls_with_downloads == len(url_list) and total_failed == 0)
        if url_source != "youdao" and all_urls_successful:
            log("\n所有 URL 均成功抓取并下载，执行 git 提交...")
            from data.local_file import clear_tmp_and_git_commit_push
            clear_tmp_and_git_commit_push()
        elif url_source == "youdao":
            pass  # 有道云模式不执行 git 操作
        else:
            log(f"\n  ⚠️ 未全部成功（成功URL: {urls_with_downloads}/{len(url_list)}, 失败文件: {total_failed}），跳过 git 提交")

        log("\n关闭浏览器...")
        await browser.close()
        log("完成!")

    _process_lock.release()


if __name__ == "__main__":
    asyncio.run(main())