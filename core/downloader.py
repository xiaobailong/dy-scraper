# -*- coding: utf-8 -*-
"""文件下载与网络请求提取"""

import asyncio
import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from config import MAX_FILE_SIZE, MIN_FILE_SIZE
from common.logger import log
from common.utils import clean_title, format_bytes, get_file_size, is_ui_asset, safe_unlink, check_and_dedup_phash, check_and_dedup_video_phash, compute_video_fingerprint, is_emoji_by_dimensions


def _locked_write(file_path: str, data: bytes) -> None:
    """Windows 文件锁写入：打开文件→加锁→写入→解锁→关闭，防止其他进程（如杀毒软件）在写入期间占用"""
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_BINARY)
    try:
        nbytes = max(len(data), 1)
        try:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_LOCK, nbytes)
        except (ImportError, OSError):
            pass
        os.write(fd, data)
        try:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, nbytes)
        except (ImportError, OSError):
            pass
    finally:
        os.close(fd)


def download_file_sync(url: str, save_path: Path, referer: str = "") -> tuple[bool, str, str]:
    """同步下载文件，返回 (成功, 大小描述, md5, 实际下载地址, 页面来源)，超过 MAX_FILE_SIZE 自动终止"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if referer:
            headers["Referer"] = referer

        req = Request(url, headers=headers)
        with urlopen(req, timeout=120) as resp:
            actual_url = resp.geturl()
            chunks = []
            total = 0
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    resp.close()
                    if save_path.exists():
                        safe_unlink(save_path)
                    return False, f"超过{format_bytes(MAX_FILE_SIZE)}限制({format_bytes(total)})", "", "", ""
                chunks.append(chunk)

            data = b"".join(chunks)
            md5 = hashlib.md5(data).hexdigest()
            save_path.parent.mkdir(parents=True, exist_ok=True)
            _locked_write(str(save_path), data)
            if not save_path.exists():
                return False, "文件写入后消失（可能被杀毒软件拦截）", "", "", ""
            return True, format_bytes(len(data)), md5, actual_url, referer
    except Exception as e:
        if save_path.exists():
            safe_unlink(save_path)
        return False, str(e), "", "", ""


async def download_files(
    urls: list[str],
    save_dir: Path,
    referer: str,
    file_type: str,
    title: str = "douyin",
    author: str = "",
    max_workers: int = 5,
    md5_registry: set[str] | None = None,
    video_hash_registry: dict[str, list[str]] | None = None,
) -> list[dict]:
    """异步下载多个文件，文件命名为 {author}_{title}_{时间}_{idx}.{ext}，通过 MD5 去重，直接下载到最终路径"""
    if not urls:
        return []

    if md5_registry is None:
        md5_registry = set()
    if video_hash_registry is None:
        video_hash_registry = {}

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

            file_size = get_file_size(url)
            if file_size is not None:
                if file_size < MIN_FILE_SIZE:
                    log(f"  [{i + 1}/{len(urls)}] 跳过 (小于{format_bytes(MIN_FILE_SIZE)}): {url[:80]}...")
                    results.append({
                        "name": "", "url": url, "path": "", "size": format_bytes(file_size),
                        "md5": "", "status": "skipped_small"
                    })
                    continue
                if file_size > MAX_FILE_SIZE:
                    log(f"  [{i + 1}/{len(urls)}] 跳过 (超过{format_bytes(MAX_FILE_SIZE)}): {url[:80]}...")
                    results.append({
                        "name": "", "url": url, "path": "", "size": format_bytes(file_size),
                        "md5": "", "status": "skipped_large"
                    })
                    continue

            prefix = f"{safe_author}_" if safe_author else ""
            final_name = f"{prefix}{safe_title}_{download_time}_{i}{ext}"
            final_path = save_dir / final_name

            log(f"  [{i + 1}/{len(urls)}] 下载中: {url[:80]}...")
            task = loop.run_in_executor(pool, download_file_sync, url, final_path, referer)
            tasks.append((i, url, final_name, final_path, task))

        for i, url, final_name, final_path, task in tasks:
            success, info, md5, actual_url, page_url = await task
            if success:
                try:
                    actual_size = final_path.stat().st_size
                except FileNotFoundError:
                    log(f"  [{i + 1}/{len(urls)}] 失败 (文件丢失): {final_name}")
                    results.append({
                        "name": final_name, "url": url, "path": "", "size": info,
                        "md5": md5, "status": "failed", "error": "文件写入后消失"
                    })
                    continue
                if actual_size < MIN_FILE_SIZE:
                    safe_unlink(final_path)
                    log(f"  [{i + 1}/{len(urls)}] 删除 (实际小于{format_bytes(MIN_FILE_SIZE)}): {final_name} ({format_bytes(actual_size)})")
                    results.append({
                        "name": final_name, "url": url, "path": "", "size": format_bytes(actual_size),
                        "md5": md5, "status": "skipped_small"
                    })
                    continue

                if md5 in md5_registry:
                    safe_unlink(final_path)
                    log(f"  [{i + 1}/{len(urls)}] 跳过 (MD5重复): {final_name} ({info})")
                    results.append({
                        "name": final_name, "url": url, "path": "", "size": info,
                        "md5": md5, "status": "skipped_duplicate"
                    })
                    continue

                md5_registry.add(md5)

                # 视频下载后检查：pHash 相似度去重（不同码率/编码的同一视频）
                if file_type == "video":
                    if check_and_dedup_video_phash(final_path, video_hash_registry):
                        # 当前文件被删除（是重复且较小）
                        if not final_path.exists():
                            log(f"  [{i + 1}/{len(urls)}] 删除 (视频pHash重复-较小): {final_name} ({info})")
                            results.append({
                                "name": final_name, "url": url, "path": "", "size": info,
                                "md5": md5, "status": "skipped_video_phash_dup"
                            })
                            continue
                        else:
                            # 已有文件被删除，当前文件保留
                            log(f"  [{i + 1}/{len(urls)}] 视频pHash重复，保留当前(较大): {final_name} ({info})")
                    # 将当前视频指纹加入注册表（无论是新视频还是替换了旧视频）
                    if final_path.exists():
                        fingerprint = compute_video_fingerprint(final_path)
                        if fingerprint:
                            video_hash_registry[final_name] = fingerprint

                # 图片下载后检查：表情包尺寸过滤
                if file_type == "image" and is_emoji_by_dimensions(final_path):
                    safe_unlink(final_path)
                    log(f"  [{i + 1}/{len(urls)}] 删除 (表情包): {final_name} ({info})")
                    results.append({
                        "name": final_name, "url": url, "path": "", "size": info,
                        "md5": md5, "status": "skipped_emoji"
                    })
                    continue

                # 图片下载后检查：pHash 相似度去重
                if file_type == "image" and check_and_dedup_phash(final_path, save_dir):
                    if not final_path.exists():
                        log(f"  [{i + 1}/{len(urls)}] 删除 (pHash重复-较小): {final_name} ({info})")
                        results.append({
                            "name": final_name, "url": url, "path": "", "size": info,
                            "md5": md5, "status": "skipped_phash_dup"
                        })
                        continue
                    else:
                        log(f"  [{i + 1}/{len(urls)}] pHash重复，保留当前(较大): {final_name} ({info})")

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


def extract_urls_from_network(requests: list) -> tuple[list[str], list[str]]:
    """从网络请求中提取视频和图片 URL"""
    video_exts = {".mp4", ".m3u8", ".ts", ".webm", ".mov", ".flv", ".avi", ".mkv"}
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico"}
    video_mimes = {"video/", "application/vnd.apple.mpegurl", "application/x-mpegURL"}
    image_mimes = {"image/"}

    video_urls = []
    image_urls = []

    for req in requests:
        url = req.get("url", "")
        content_type = req.get("contentType", "")
        url_lower = url.lower()

        if url.startswith("data:") or "1x1" in url:
            continue
        if is_ui_asset(url):
            continue

        is_video = any(ext in url_lower for ext in video_exts)
        if not is_video:
            is_video = any(content_type.startswith(m) for m in video_mimes)
        if is_video and url not in video_urls:
            video_urls.append(url)
            continue

        is_image = any(ext in url_lower for ext in image_exts)
        if not is_image:
            is_image = any(content_type.startswith(m) for m in image_mimes)
        if is_image and url not in image_urls:
            image_urls.append(url)

    return video_urls, image_urls


def deduplicate_videos(urls: list[str]) -> list[str]:
    """对视频去重：同一 file_id 只保留最高码率，按质量降序排列"""
    groups = {}
    for url in urls:
        br_match = re.search(r'[?&]br=(\d+)', url)
        file_match = re.search(r'/([^/]+)\?', url)
        key = file_match.group(1) if file_match else url
        br = int(br_match.group(1)) if br_match else 0
        if key not in groups or br > groups[key][1]:
            groups[key] = (url, br)
    return sorted([v[0] for v in groups.values()], key=lambda u: _video_quality_score(u), reverse=True)


def _video_quality_score(url: str) -> int:
    """从视频 URL 提取质量分数"""
    br_match = re.search(r'[?&]br=(\d+)', url)
    if br_match:
        return int(br_match.group(1))
    return 0


def sort_images_by_quality(urls: list[str]) -> list[str]:
    """同一图片去重：按 URL 路径分组，每组只保留最高分辨率版本，过滤垃圾缩略图"""
    groups: dict[str, list[tuple[str, int]]] = {}
    for u in urls:
        key = _image_identity(u)
        score = _image_quality_score(u)
        groups.setdefault(key, []).append((u, score))

    _MIN_AREA = 200 * 200  # 图片面积低于此值视为垃圾缩略图
    result = []
    for key, candidates in groups.items():
        candidates.sort(key=lambda x: x[1], reverse=True)
        best_url, best_score = candidates[0]
        # 过滤垃圾缩略图：最高分辨率仍低于最小面积阈值
        if best_score > 0 and best_score < _MIN_AREA:
            continue
        result.append(best_url)
    return result


def _image_identity(url: str) -> str:
    """提取图片的身份标识，同一图片的不同分辨率版本和不同格式应有相同标识。
    去掉 URL 中的尺寸标记（如 _720x1280、~tplv-dy-xxx）、文件扩展名和查询参数。"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path = parsed.path
    # 去掉后缀中的尺寸标记
    path = re.sub(r'[~_](\d{2,4})x(\d{2,4})', '', path)
    # 去掉 ~tplv-dy-xxx 等模板标记
    path = re.sub(r'~tplv-[a-z0-9-]+', '', path)
    # 去掉文件扩展名，同一图片不同格式（webp/jpeg）应归为一组
    path = re.sub(r'\.[a-z0-9]+$', '', path, flags=re.IGNORECASE)
    return path


def _image_quality_score(url: str) -> int:
    """从 URL 估算图片质量分数（越高越好）。URL 中常含 '720x1280' 这样的尺寸。"""
    dim_match = re.search(r'[~_](\d{2,4})x(\d{2,4})', url)
    if dim_match:
        w, h = int(dim_match.group(1)), int(dim_match.group(2))
        return w * h
    return 0