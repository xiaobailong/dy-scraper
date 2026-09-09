# -*- coding: utf-8 -*-
"""
全局抓取统计：跨所有 URL 的下载统计与打印

封装统计数据的累加、汇总和格式化输出，避免散落在 scraper 主循环中。
"""

from dataclasses import dataclass, field

from common.logger import log


# 下载状态 → 统计分类的映射
_STATUS_MAP = {"downloaded": "success", "skipped_duplicate": "success"}


@dataclass
class MediaStats:
    """单个媒体类型（视频/图片）的下载统计"""
    success: int = 0
    failed: int = 0
    skipped_small: int = 0
    skipped_large: int = 0
    skipped_dup: int = 0
    skipped_phash_dup: int = 0  # 仅视频使用

    @property
    def total(self) -> int:
        return (self.success + self.failed + self.skipped_small +
                self.skipped_large + self.skipped_dup + self.skipped_phash_dup)

    def accumulate_results(self, results: list[dict]) -> None:
        """从下载结果列表累加统计"""
        for r in results:
            key = _STATUS_MAP.get(r["status"], r["status"])
            if hasattr(self, key):
                setattr(self, key, getattr(self, key) + 1)


@dataclass
class ScrapeStats:
    """全局抓取统计，跨所有 URL 累加，封装打印输出"""
    video: MediaStats = field(default_factory=MediaStats)
    image: MediaStats = field(default_factory=MediaStats)
    url_total: int = 0               # 本次要处理的 URL 总数
    urls_with_downloads: int = 0     # 有成功下载的 URL 数
    skipped_url_count: int = 0       # 因已处理而跳过的 URL 数

    def accumulate_page(self, ctx) -> None:
        """从 PageContext 累加本页的下载统计"""
        self.video.accumulate_results(ctx.video_results)
        self.image.accumulate_results(ctx.image_results)
        if ctx.has_downloads:
            self.urls_with_downloads += 1

    def print_page_result(self, ctx) -> None:
        """打印单页面的下载结果摘要"""
        dup_info = ""
        if ctx.video_dup_count or ctx.image_dup_count:
            dup_info = f"  去重: 视频{ctx.video_dup_count}个 图片{ctx.image_dup_count}个"
        log(f"  视频: {ctx.video_success_count}/{len(ctx.video_urls)}"
            f"  图片: {ctx.image_success_count}/{len(ctx.image_urls)}{dup_info}")

    # ──────────────────────────────────────────────
    # 汇总打印
    # ──────────────────────────────────────────────

    def print_final_summary(self, url_source: str = "") -> None:
        """打印最终汇总统计"""
        vs = self.video
        im = self.image
        total_success = vs.success + im.success
        total_failed = vs.failed + im.failed
        total_skipped = (vs.skipped_small + vs.skipped_large + vs.skipped_dup +
                         vs.skipped_phash_dup +
                         im.skipped_small + im.skipped_large + im.skipped_dup)

        log(f"\n{'=' * 60}")
        log(f"  全部完成! 共处理 {self.url_total} 个 URL")
        log(f"{'=' * 60}")

        # 汇总
        vs_skipped = vs.skipped_small + vs.skipped_large + vs.skipped_dup + vs.skipped_phash_dup
        im_skipped = im.skipped_small + im.skipped_large + im.skipped_dup
        total_urls = self.url_total + self.skipped_url_count
        log(f"\n  ┌─ 汇总 ────────────────────────────────────")
        log(f"  │  URL 总数: {total_urls} 个")
        log(f"  │  本次处理: {self.url_total} 个")
        if self.skipped_url_count:
            log(f"  │  跳过(已处理URL): {self.skipped_url_count} 个")
        log(f"  │  有成功下载的 URL: {self.urls_with_downloads} 个")
        log(f"  │  文件总数: {vs.total + im.total} 个 (视频 {vs.total}, 图片 {im.total})")
        log(f"  │  下载成功: {total_success} 个 (视频 {vs.success}, 图片 {im.success})")
        log(f"  │  下载失败: {total_failed} 个 (视频 {vs.failed}, 图片 {im.failed})")
        log(f"  │  跳过(文件): {total_skipped} 个 (视频 {vs_skipped}, 图片 {im_skipped})")
        log(f"  └──────────────────────────────────────────")
        log(f"{'=' * 60}")

    # ──────────────────────────────────────────────
    # 判断方法
    # ──────────────────────────────────────────────

    @property
    def all_urls_successful(self) -> bool:
        """是否所有 URL 均成功（均有下载且无失败）"""
        return (self.urls_with_downloads == self.url_total
                and self.video.failed == 0
                and self.image.failed == 0)