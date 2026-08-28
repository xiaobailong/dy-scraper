# -*- coding: utf-8 -*-
"""
页面上下文：封装单个页面的所有元数据，保证原子性

类似 Spring Boot 的 RequestContext，所有函数围绕此对象传递数据。
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from common.logger import log


# 下载状态 → 统计分类的映射
_STATUS_MAP = {"downloaded": "success", "skipped_duplicate": "success"}


# ============================================================
# 调用跟踪：环节对象 + 队列
# ============================================================

@dataclass
class Stage:
    """处理环节对象，记录代码块调用轨迹"""
    code: str             # 环节唯一码，如 "goto" / "extract_meta" / "download_video"
    timestamp: str        # 入队列时间，格式 "HH:MM:SS.fff"


@dataclass
class PageContext:
    """单页面上下文，封装一个页面的所有元数据和下载结果。

    设计原则：
    1. 一个 PageContext 对应一个目标 URL（短链接），保证数据隔离
    2. 所有提取/下载函数都通过此对象读写数据，避免参数散落
    3. 每个字段有明确的来源标注，便于调试和追溯
    4. 内建调用追踪队列，每次关键代码块执行后入队一个 Stage
    """
    # ── 核心标识 ──
    short_url: str = ""
    final_url: str = ""       # goto() 后的最终跳转地址

    # ── 作者/内容信息 ──
    title: str = ""
    author: str = ""
    author_code: str = ""
    sec_uid: str = ""
    description: str = ""
    cover_url: str = ""
    extract_source: str = ""  # 数据提取来源，如 "ssr" / "api:aweme_detail" / "dom" 等

    # ── 媒体 URL（按来源分层，便于调试和降级兜底） ──
    api_video_urls: list[str] = field(default_factory=list)
    api_image_urls: list[str] = field(default_factory=list)
    dom_video_urls: list[str] = field(default_factory=list)
    dom_image_urls: list[str] = field(default_factory=list)
    network_video_urls: list[str] = field(default_factory=list)
    network_image_urls: list[str] = field(default_factory=list)

    # ── 最终合并后的 URL（去重、过滤、按质量排序后） ──
    video_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)

    # ── 下载结果 ──
    video_results: list[dict] = field(default_factory=list)
    image_results: list[dict] = field(default_factory=list)

    # ── 调试信息 ──
    api_response_count: int = 0
    ssr_available: list = field(default_factory=list)
    debug_info: dict = field(default_factory=dict)

    # ── 时间戳 ──
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S"))

    # ── 调用追踪队列 ──
    stages: list[Stage] = field(default_factory=list)

    # ──────────────────────────────────────────────
    # 调用追踪方法
    # ──────────────────────────────────────────────

    def push_stage(self, code: str) -> None:
        """入队一个环节对象，记录当前代码块调用"""
        self.stages.append(Stage(
            code=code,
            timestamp=datetime.now().strftime("%H:%M:%S.%f")[:-3],
        ))

    def print_stages(self) -> None:
        """打印当前 URL 的完整调用追踪队列"""
        if not self.stages:
            log("  [调用追踪] 无环节记录")
            return
        log(f"  ┌─ 调用追踪队列 ({len(self.stages)} 个环节) ────")
        for i, s in enumerate(self.stages):
            log(f"  │  [{i+1:02d}] {s.timestamp}  {s.code}")
        log(f"  └{'─' * 46}")

    # ──────────────────────────────────────────────
    # 统计方法
    # ──────────────────────────────────────────────

    def _count_results(self, results: list[dict], statuses: tuple[str, ...]) -> int:
        """统计指定状态的下载结果数量"""
        return len([r for r in results if r["status"] in statuses])

    @property
    def video_success_count(self) -> int:
        return self._count_results(self.video_results, ("downloaded", "skipped_duplicate"))

    @property
    def video_dup_count(self) -> int:
        return self._count_results(self.video_results, ("skipped_duplicate",))

    @property
    def video_size_skipped_count(self) -> int:
        return self._count_results(self.video_results, ("skipped_small", "skipped_large"))

    @property
    def image_success_count(self) -> int:
        return self._count_results(self.image_results, ("downloaded", "skipped_duplicate"))

    @property
    def image_dup_count(self) -> int:
        return self._count_results(self.image_results, ("skipped_duplicate",))

    @property
    def image_size_skipped_count(self) -> int:
        return self._count_results(self.image_results, ("skipped_small", "skipped_large"))

    @property
    def has_downloads(self) -> bool:
        return self.video_success_count > 0 or self.image_success_count > 0

    @property
    def has_media_urls(self) -> bool:
        return bool(self.video_urls) or bool(self.image_urls)

    @property
    def total_url_count(self) -> int:
        return len(self.video_urls) + len(self.image_urls)

    # ──────────────────────────────────────────────
    # 累加方法（供 ScrapeStats 使用）
    # ──────────────────────────────────────────────

    def accumulate_to(self, stats: dict[str, dict[str, int]], media_type: str) -> None:
        """将本页的下载结果累加到全局统计 dict 中"""
        results = self.video_results if media_type == "video" else self.image_results
        for r in results:
            key = _STATUS_MAP.get(r["status"], r["status"])
            if key in stats[media_type]:
                stats[media_type][key] += 1

    # ──────────────────────────────────────────────
    # 结果 JSON 构建
    # ──────────────────────────────────────────────

    def build_result_json(self) -> dict:
        """构建结果 JSON（用于保存到 result 目录）"""
        return {
            "targetUrl": self.short_url,
            "finalUrl": self.final_url,
            "title": self.title,
            "author": self.author,
            "authorCode": self.author_code,
            "description": self.description,
            "coverUrl": self.cover_url,
            "scrapeTime": datetime.now().isoformat(),
            "downloadStats": {
                "videos": {
                    "total": len(self.video_urls),
                    "success": self.video_success_count,
                    "duplicate": self.video_dup_count,
                },
                "images": {
                    "total": len(self.image_urls),
                    "success": self.image_success_count,
                    "duplicate": self.image_dup_count,
                },
            },
            "videos": self.video_results,
            "images": self.image_results,
        }

    def save_result_json(self, result_dir: Path) -> Path:
        """保存结果 JSON 到文件，返回文件路径"""
        from common.utils import clean_title
        result_dir.mkdir(parents=True, exist_ok=True)
        data = self.build_result_json()
        result_json = json.dumps(data, ensure_ascii=False, indent=2)
        result_json_bytes = result_json.encode("utf-8")
        result_md5 = hashlib.md5(result_json_bytes).hexdigest()[:8]
        safe_title = clean_title(self.title)
        safe_author = clean_title(self.author) if self.author else ""
        prefix = f"{safe_author}_" if safe_author else ""
        result_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_name = f"{prefix}{safe_title}_{result_time}_{result_md5}.json"
        result_path = result_dir / result_name
        result_path.write_text(result_json, encoding="utf-8")
        return result_path

    # ──────────────────────────────────────────────
    # 兼容旧代码
    # ──────────────────────────────────────────────

    def to_dict(self) -> dict:
        """转换为字典（兼容旧代码中 page_data 的访问方式）"""
        result = {
            "pageUrl": self.final_url,
            "title": self.title,
            "author": self.author,
            "authorCode": self.author_code,
            "secUid": self.sec_uid,
            "description": self.description,
            "coverUrl": self.cover_url,
            "extractSource": self.extract_source,
            "videoUrls": self.dom_video_urls,
            "imageUrls": self.dom_image_urls,
            "apiVideoUrls": self.api_video_urls,
            "apiImageUrls": self.api_image_urls,
            "apiResponseCount": self.api_response_count,
            "ssrAvailable": self.ssr_available,
        }
        result.update(self.debug_info)
        return result

    def __getitem__(self, key: str):
        return self.to_dict()[key]

    def get(self, key: str, default=None):
        return self.to_dict().get(key, default)