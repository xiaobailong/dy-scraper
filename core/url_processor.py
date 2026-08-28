# -*- coding: utf-8 -*-
"""
单 URL 处理管线

将 goto → 验证 → 提取 → 合并 → 下载 → 保存 封装为一个原子处理单元。
"""

import asyncio
from pathlib import Path

from common.logger import log
import core.metadata as metadata
from core.downloader import deduplicate_videos, download_files, extract_urls_from_network, sort_images_by_quality
from common.utils import is_ui_asset, normalize_url
from common.image_dedup import ImageDedupChecker
from entity.page_context import PageContext
from entity.scrape_stats import ScrapeStats

try:
    from playwright.async_api import Page
except ImportError:
    Page = None


class UrlProcessor:
    """单 URL 处理管线。

    封装了从页面导航到结果保存的完整流程，包括：
    - 页面加载与异常校验
    - 元数据提取（SSR/API/DOM）
    - 媒体 URL 合并去重
    - 并发下载与去重
    - 结果持久化（JSON + 数据库）
    """

    def __init__(
        self,
        db,
        md5_registry: set[str],
        video_hash_registry: dict[str, list[str]],
        temp_video_dir: Path,
        temp_image_dir: Path,
        result_dir: Path,
    ):
        self._db = db
        self._md5_registry = md5_registry
        self._video_hash_registry = video_hash_registry
        self._temp_video_dir = temp_video_dir
        self._temp_image_dir = temp_image_dir
        self._result_dir = result_dir

    async def process(
        self,
        page: Page,
        target_url: str,
        url_idx: int,
        url_total: int,
        collected_requests: list[dict],
        detail_responses: list[dict],
        stats: ScrapeStats,
        last_final_url: str | None,
    ) -> tuple["PageContext | None", str]:
        """处理单个 URL，返回 (PageContext, new_last_final_url)。

        如果处理失败（页面不可用/重复等），返回 (None, last_final_url)。
        """
        log(f"\n{'=' * 60}")
        log(f"  [{url_idx}/{url_total}] {target_url}")
        log(f"{'=' * 60}")

        # ── 1. goto ──
        log("[2/6] 访问目标页面...")
        goto_ok = True
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log(f"  页面加载超时或出错: {e}")
            goto_ok = False

        final_url = page.url
        log(f"  最终跳转地址: {final_url}")

        if not goto_ok:
            log(f"  ⚠️ 页面加载失败，跳过当前 URL")
            return None, last_final_url or ""

        # ── 2. 校验 ──
        if not self._validate_url(final_url, last_final_url):
            return None, last_final_url or ""

        # 【关键修复】goto() 完成后才设置切片起点，防止跨页面请求污染
        _req_start = len(collected_requests)
        _detail_start = len(detail_responses)

        # ── 3. 等待渲染 ──
        log("[3/6] 等待页面内容渲染...")
        try:
            await page.wait_for_selector("video", timeout=15000)
            log("  视频元素已加载")
        except Exception:
            log("  未检测到视频元素，继续尝试...")
        await asyncio.sleep(5)

        # ── 4. 创建上下文 ──
        ctx = PageContext(short_url=target_url, final_url=final_url)
        ctx.push_stage("ctx_create")

        # ── 5. 提取元数据 ──
        log("[4/6] 提取页面数据...")
        _new_detail = detail_responses[_detail_start:]
        ctx = await metadata.extract_metadata(page, _new_detail, ctx)
        ctx.push_stage("extract_meta")

        _new_requests = collected_requests[_req_start:]
        ctx.network_video_urls, ctx.network_image_urls = extract_urls_from_network(_new_requests)
        ctx.push_stage("extract_network")

        # ── 6. 合并 URL ──
        self._merge_urls(ctx)
        ctx.push_stage("merge_urls")

        # 输出摘要
        author_info = ctx.author or "(未获取到)"
        if ctx.author_code:
            author_info += f"  (@{ctx.author_code})"
        log(f"\n【页面标题】{ctx.title or '(未获取到)'}")
        log(f"【作者】{author_info}")
        log(f"【视频】{len(ctx.video_urls)} 个  【图片】{len(ctx.image_urls)} 个")

        # ── 7. 下载 ──
        log(f"\n[5/6] 下载文件...")
        log(f"  视频临时目录: {self._temp_video_dir}")
        ctx.video_results = await download_files(
            ctx, self._temp_video_dir, "video",
            md5_registry=self._md5_registry,
            video_hash_registry=self._video_hash_registry,
        )
        ctx.push_stage("download_video")

        log(f"  图片临时目录: {self._temp_image_dir}")
        ctx.image_results = await download_files(
            ctx, self._temp_image_dir, "image", max_workers=8,
            md5_registry=self._md5_registry,
            video_hash_registry=self._video_hash_registry,
        )
        ctx.push_stage("download_image")

        # ── 8. 保存结果 ──
        log(f"\n[6/6] 保存结果...")
        stats.accumulate_page(ctx)
        stats.print_page_result(ctx)

        self._record_to_db(ctx, target_url, final_url)
        ctx.push_stage("db_record")

        result_path = ctx.save_result_json(self._result_dir)
        ctx.push_stage("save_result")
        log(f"  结果已保存: {result_path}")

        ctx.print_stages()
        return ctx, final_url

    # ──────────────────────────────────────────────
    # 内部方法
    # ──────────────────────────────────────────────

    def _validate_url(self, final_url: str, last_final_url: str | None) -> bool:
        """校验 final_url 是否可用"""
        final_info = self._db.get_by_final_url(final_url)
        if final_info:
            log(f"  ⚠️ 长链接重复（最终地址已被处理过），跳过")
            log(f"     首次处理短链接: {final_info['short_url']}")
            log(f"     首次处理时间: {final_info['create_time']}")
            return False

        if last_final_url and final_url == last_final_url:
            log(f"  ⚠️ 跳转前后地址相同，可能未成功进入新页面，跳过")
            return False

        if any(s in final_url for s in ("/notfound", "/404", "/about:blank", "/error")):
            log(f"  ⚠️ 目标页面不存在（{final_url}），跳过")
            return False

        return True

    def _merge_urls(self, ctx: PageContext) -> None:
        """合并视频/图片 URL（优先级：API > SSR > DOM > 网络请求）"""
        if ctx.api_video_urls:
            log(f"  API获取到 {len(ctx.api_video_urls)} 个视频链接（高清），优先使用")
            all_video_urls = deduplicate_videos(list(dict.fromkeys(ctx.api_video_urls)))
        else:
            log(f"  API未获取到视频链接，退到DOM+网络请求")
            all_video_urls = list(dict.fromkeys(ctx.dom_video_urls + ctx.network_video_urls))
            all_video_urls = deduplicate_videos(all_video_urls)

        all_video_urls = [u for u in all_video_urls
                          if not u.startswith("blob:") and not is_ui_asset(u)]

        if ctx.api_image_urls:
            log(f"  API/SSR获取到 {len(ctx.api_image_urls)} 个图片链接，优先使用")
            all_image_urls = list(dict.fromkeys(ctx.api_image_urls))
            all_image_urls = sort_images_by_quality(all_image_urls)
        else:
            log(f"  API/SSR均未获取到图片链接，退到DOM+网络请求")
            all_image_urls = list(dict.fromkeys(ctx.dom_image_urls + ctx.network_image_urls))
            all_image_urls = sort_images_by_quality(all_image_urls)

        all_image_urls = [u for u in all_image_urls
                          if not u.startswith("blob:") and not is_ui_asset(u)
                          and not ImageDedupChecker.is_cover_url(u)
                          and not ImageDedupChecker.is_emoji_sticker_url(u)]

        ctx.video_urls = all_video_urls
        ctx.image_urls = all_image_urls

    def _record_to_db(self, ctx: PageContext, target_url: str, final_url: str) -> None:
        """记录处理结果到数据库"""
        if ctx.has_downloads:
            self._db.insert(
                normalize_url(target_url),
                album_name=ctx.author or "",
                album_code=ctx.author_code or "",
                remark=ctx.title or "",
            )
            self._db.insert_final_url(final_url, target_url)
            log(f"  URL已记录到数据库")
        elif ctx.video_size_skipped_count > 0 or ctx.image_size_skipped_count > 0:
            self._db.insert_skipped(
                normalize_url(target_url),
                album_name=ctx.author or "",
                album_code=ctx.author_code or "",
                remark=ctx.title or "",
            )
            log(f"  URL记录到跳过表（有{ctx.total_url_count}个媒体URL但均因大小被跳过）")
        else:
            log(f"  URL未记录（无媒体URL）")