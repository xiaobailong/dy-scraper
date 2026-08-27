# -*- coding: utf-8 -*-
"""
抖音视频/图片独立下载工具
用法: python download_one.py <抖音链接>
      python download_one.py                       # 交互式输入链接（支持粘贴一大段文字自动提取链接）

支持粘贴一整段文字（如微信/QQ聊天记录），自动从中提取所有有效的抖音分享链接。
直接下载到系统下载目录（无文件大小限制）
"""

import asyncio
import hashlib
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    from playwright.async_api import async_playwright
except ImportError:
    raise SystemExit("请先安装 playwright: pip install playwright")

# ============================================================
# 系统下载目录
# ============================================================
DOWNLOAD_DIR = Path(os.path.join(os.path.expanduser("~"), "Downloads")) / "douyin"
VIDEO_DIR = DOWNLOAD_DIR / "视频"
IMAGE_DIR = DOWNLOAD_DIR / "图片"


def log(msg: str) -> None:
    print(msg)


def clean_title(title: str) -> str:
    cleaned = re.sub(r'\s*-\s*抖音$', '', title)
    cleaned = re.sub(r'\d{8}', '', cleaned)
    cleaned = re.sub(r'[^\u4e00-\u9fffA-Za-z0-9_\-]', '', cleaned)
    cleaned = re.sub(r'[_-]{2,}', '_', cleaned)
    cleaned = cleaned.strip('_-')
    return cleaned[:50] if cleaned else "douyin"


def format_bytes(size: int) -> str:
    if not size:
        return "未知"
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def safe_unlink(path: Path) -> None:
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


# 抖音分享链接的正则模式
_DOUYIN_URL_PATTERN = re.compile(
    r'https?://'
    r'(?:'
    r'v\.douyin\.com/[A-Za-z0-9_-]+/?|'
    r'(?:www\.)?douyin\.com/(?:video|note|user)/[A-Za-z0-9_-]+(?:\?[^\s]*)?|'
    r'(?:www\.)?iesdouyin\.com/share/(?:video|note)/[A-Za-z0-9_-]+/?'
    r')',
    re.IGNORECASE
)

# 从完整链接中提取视频/图集 ID 的正则
_DOUYIN_ID_PATTERN = re.compile(
    r'douyin\.com/(video|note)/(\d+)',
    re.IGNORECASE
)
_DOUYIN_SHORT_PATTERN = re.compile(
    r'v\.douyin\.com/([A-Za-z0-9_-]+)',
    re.IGNORECASE
)


def extract_douyin_urls(text: str) -> List[str]:
    """从任意文本中提取所有有效的抖音分享链接，去重后返回"""
    if not text or not text.strip():
        return []
    urls = _DOUYIN_URL_PATTERN.findall(text)
    seen = set()
    result = []
    for url in urls:
        url = url.rstrip(',.，。;；!！?？)）]】\'\"')
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _normalize_url(url: str) -> str:
    """规范化抖音链接：去除查询参数、统一格式"""
    match = _DOUYIN_ID_PATTERN.search(url)
    if match:
        content_type, content_id = match.group(1), match.group(2)
        return f"https://www.douyin.com/{content_type}/{content_id}"
    short_match = _DOUYIN_SHORT_PATTERN.search(url)
    if short_match:
        return f"https://v.douyin.com/{short_match.group(1)}"
    return url.rstrip('/')


def _resolve_short_link(url: str) -> str:
    """通过 GET 请求解析短链接，获取重定向后的真实 URL（仅获取最终 URL，不下载内容）"""
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        })
        with urlopen(req, timeout=5) as resp:
            final_url = resp.geturl()
            if final_url and final_url != url:
                return final_url
    except Exception:
        pass
    return url


def deduplicate_douyin_urls(urls: List[str]) -> List[str]:
    """对抖音链接进行智能去重：
    - 规范化完整链接（去除查询参数）
    - 解析短链接来判断是否与已有完整链接重复
    - 按视频/图集 ID 去重，优先保留完整链接
    """
    if len(urls) <= 1:
        return urls

    full_urls = {}
    short_urls = []

    for url in urls:
        match = _DOUYIN_ID_PATTERN.search(url)
        if match:
            content_type, content_id = match.group(1), match.group(2)
            key = f"{content_type}:{content_id}"
            normalized = _normalize_url(url)
            if key not in full_urls:
                full_urls[key] = normalized
            continue

        short_match = _DOUYIN_SHORT_PATTERN.search(url)
        if short_match:
            short_urls.append(url)
            continue

        if url not in full_urls:
            full_urls[url] = url

    result = list(full_urls.values())

    for short_url in short_urls:
        resolved = _resolve_short_link(short_url)
        match = _DOUYIN_ID_PATTERN.search(resolved)
        if match:
            content_type, content_id = match.group(1), match.group(2)
            key = f"{content_type}:{content_id}"
            if key in full_urls:
                log(f"  ⚡ 短链接 {short_url} 与已有链接重复，已跳过")
                continue
            full_urls[key] = _normalize_url(resolved)
            result.append(_normalize_url(resolved))
        else:
            result.append(short_url)

    return result


def _locked_write(file_path: str, data: bytes) -> None:
    fd = os.open(file_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_BINARY)
    try:
        try:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, len(data) + 1)
        except (ImportError, OSError):
            pass
        os.write(fd, data)
        try:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, len(data) + 1)
        except (ImportError, OSError):
            pass
    finally:
        os.close(fd)


def download_file_sync(url: str, save_path: Path, referer: str = "") -> tuple:
    """同步下载文件，返回 (成功, 大小描述, md5, 实际下载地址, 页面来源)，无大小限制"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if referer:
            headers["Referer"] = referer

        req = Request(url, headers=headers)
        with urlopen(req, timeout=300) as resp:
            actual_url = resp.geturl()
            chunks = []
            total = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                chunks.append(chunk)

            data = b"".join(chunks)
            md5 = hashlib.md5(data).hexdigest()
            save_path.parent.mkdir(parents=True, exist_ok=True)
            _locked_write(str(save_path), data)
            return True, format_bytes(len(data)), md5, actual_url, referer
    except Exception as e:
        if save_path.exists():
            safe_unlink(save_path)
        return False, str(e), "", "", ""


async def download_files(
    urls: list,
    save_dir: Path,
    referer: str,
    file_type: str,
    title: str = "douyin",
    author: str = "",
    max_workers: int = 5,
) -> list:
    """异步下载多个文件，无大小限制，直接下载到最终路径"""
    if not urls:
        return []

    save_dir.mkdir(parents=True, exist_ok=True)
    results = []
    loop = asyncio.get_running_loop()
    safe_title = clean_title(title)
    safe_author = clean_title(author) if author else ""
    download_time = datetime.now().strftime("%Y%m%d_%H%M%S")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        tasks = []
        for i, url in enumerate(urls):
            ext = Path(urlparse(url).path).suffix
            if not ext or len(ext) > 10:
                ext = ".mp4" if file_type == "video" else ".jpg"

            prefix = f"{safe_author}_" if safe_author else ""
            final_name = f"{prefix}{safe_title}_{download_time}_{i}{ext}"
            final_path = save_dir / final_name

            log(f"  [{i + 1}/{len(urls)}] 下载中: {url[:80]}...")
            task = loop.run_in_executor(pool, download_file_sync, url, final_path, referer)
            tasks.append((i, url, final_name, final_path, task))

        for i, url, final_name, final_path, task in tasks:
            success, info, md5, actual_url, page_url = await task
            if success:
                log(f"  [{i + 1}/{len(urls)}] 完成: {final_name} ({info})  [来源: {actual_url}]  [页面: {page_url}]")
                results.append({
                    "name": final_name, "url": url, "path": str(final_path),
                    "size": info, "md5": md5, "status": "downloaded"
                })
            else:
                if final_path.exists():
                    safe_unlink(final_path)
                log(f"  [{i + 1}/{len(urls)}] 失败: {info}")
                results.append({
                    "name": "", "url": url, "path": "", "size": None,
                    "md5": "", "status": "failed", "error": info
                })

    return results


# ============================================================
# 从 core 模块导入提取逻辑
# ============================================================
import sys as _sys
_sys.path.insert(0, str(Path(__file__).parent))
from core.downloader import deduplicate_videos, extract_urls_from_network, sort_images_by_quality
from core.metadata import extract_metadata
from api.douyin_detail import create_detail_response_collector
from data.db_utils import DBUtils


async def process_url(target_url: str) -> None:
    """处理单个抖音链接：提取并下载所有视频和图片"""
    log("=" * 60)
    log(f"  抖音下载工具")
    log(f"  目标: {target_url}")
    log(f"  下载目录: {DOWNLOAD_DIR}")
    log("=" * 60)

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        from config import config
        config.reload_config()
        chromepath = getattr(config, "CHROME_PATH", "")
        if chromepath and Path(chromepath).exists():
            launch_kwargs["executable_path"] = chromepath
            log(f"  使用浏览器: {chromepath}")
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

        page.on("response", create_detail_response_collector(detail_responses))
        page.on("response", lambda r: collected_requests.append({
            "url": r.url,
            "contentType": r.headers.get("content-type", ""),
            "contentLength": r.headers.get("content-length"),
        }))

        _req_start = len(collected_requests)
        _detail_start = len(detail_responses)

        # 访问页面
        log("\n[1/4] 访问目标页面...")
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log(f"  页面加载超时: {e}")

        final_url = page.url
        log(f"  最终跳转: {final_url}")

        # 长链接去重检查：检查最终跳转地址是否已被处理过
        db = DBUtils()
        final_info = db.get_by_final_url(final_url)
        if final_info:
            log(f"  ⚠️ 长链接重复（最终地址已被处理过），跳过")
            log(f"     首次处理短链接: {final_info['short_url']}")
            log(f"     首次处理时间: {final_info['create_time']}")
            await browser.close()
            return

        # 等待渲染
        log("[2/4] 等待页面渲染...")
        try:
            await page.wait_for_selector('video', timeout=15000)
            log("  视频元素已加载")
        except Exception:
            log("  未检测到视频元素，继续...")
        await asyncio.sleep(5)

        # 提取数据
        log("[3/4] 提取页面数据...")
        _new_detail = detail_responses[_detail_start:]
        page_data = await extract_metadata(page, _new_detail)

        _new_requests = collected_requests[_req_start:]
        network_video_urls, network_image_urls = extract_urls_from_network(_new_requests)

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

        all_video_urls = [u for u in all_video_urls if not u.startswith("blob:")]

        if api_images:
            log(f"  API/SSR获取到 {len(api_images)} 个图片链接，优先使用")
            all_image_urls = list(dict.fromkeys(api_images))
            all_image_urls = sort_images_by_quality(all_image_urls)
        else:
            log(f"  API/SSR均未获取到图片链接，退到DOM+网络请求")
            all_image_urls = list(dict.fromkeys(dom_images + network_image_urls))
            all_image_urls = sort_images_by_quality(all_image_urls)

        all_image_urls = [u for u in all_image_urls if not u.startswith("blob:")]

        author_info = page_data['author'] or '(未获取到)'
        if page_data.get('authorCode'):
            author_info += f"  (@{page_data['authorCode']})"
        log(f"\n【页面标题】{page_data['title'] or '(未获取到)'}")
        log(f"【作者】{author_info}")
        log(f"【视频】{len(all_video_urls)} 个  【图片】{len(all_image_urls)} 个")

        # 下载
        log(f"\n[4/4] 下载文件（无大小限制）...")
        log(f"  视频目录: {VIDEO_DIR}")
        video_results = await download_files(
            all_video_urls, VIDEO_DIR, final_url, "video",
            title=page_data["title"], author=page_data["author"]
        )

        log(f"  图片目录: {IMAGE_DIR}")
        image_results = await download_files(
            all_image_urls, IMAGE_DIR, final_url, "image",
            title=page_data["title"], author=page_data["author"], max_workers=8
        )

        # 统计
        video_ok = len([r for r in video_results if r["status"] == "downloaded"])
        video_fail = len([r for r in video_results if r["status"] == "failed"])
        image_ok = len([r for r in image_results if r["status"] == "downloaded"])
        image_fail = len([r for r in image_results if r["status"] == "failed"])

        if video_ok > 0 or image_ok > 0:
            db.insert_final_url(final_url, target_url)

        log(f"\n{'=' * 60}")
        log(f"  下载完成!")
        log(f"  视频: 成功 {video_ok}/{len(all_video_urls)}, 失败 {video_fail}")
        log(f"  图片: 成功 {image_ok}/{len(all_image_urls)}, 失败 {image_fail}")
        log(f"  保存位置: {DOWNLOAD_DIR}")
        log(f"{'=' * 60}")

        await browser.close()


def _read_multiline_input() -> str:
    """读取多行输入，支持粘贴一大段文字，按 Ctrl+Z (Windows) 或 Ctrl+D (Unix) 结束"""
    print("请粘贴包含抖音链接的文字内容（支持一大段文字，完成后按 Ctrl+Z 回车）:")
    print("-" * 50)
    try:
        lines = sys.stdin.read().strip()
    except KeyboardInterrupt:
        return ""
    print("-" * 50)
    return lines


def main():
    if len(sys.argv) > 1:
        raw_text = sys.argv[1].strip()
    else:
        raw_text = _read_multiline_input()

    if not raw_text:
        print("未输入任何内容，退出")
        return

    urls = extract_douyin_urls(raw_text)

    if not urls:
        print("❌ 未从输入内容中提取到有效的抖音链接")
        print("   支持的链接格式: v.douyin.com/xxx, douyin.com/video/xxx, douyin.com/note/xxx 等")
        return

    urls = deduplicate_douyin_urls(urls)

    if len(urls) == 1:
        log(f"✅ 提取到 1 个抖音链接，开始处理...")
        asyncio.run(process_url(urls[0]))
    else:
        log(f"✅ 提取到 {len(urls)} 个抖音链接（已去重）:")
        for i, url in enumerate(urls, 1):
            log(f"  [{i}] {url}")
        log("")
        for idx, url in enumerate(urls, 1):
            log(f"\n{'#' * 60}")
            log(f"  处理第 {idx}/{len(urls)} 个链接")
            log(f"{'#' * 60}")
            asyncio.run(process_url(url))


if __name__ == "__main__":
    main()