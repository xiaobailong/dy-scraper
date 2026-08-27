# -*- coding: utf-8 -*-
"""
补刷抖音号脚本
从 details_page 表中查询最近 3 天 album_code 为空的记录，
重新访问页面提取抖音号并更新数据库，同时输出诊断信息用于排查失败原因。
"""
import asyncio
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright
import core.metadata as metadata
from data.db_utils import DBUtils
from common.utils import normalize_url
from config import CHROME_PATH
from api.douyin_detail import create_detail_response_collector


async def run():
    db = DBUtils()

    def log(msg):
        print(str(msg), flush=True)

    log("=" * 60)
    log("  补刷抖音号 - 查询最近 3 天所有记录（调试模式）")
    log("=" * 60)

    records = db.get_all_recent_urls(days=3)
    if not records:
        log("  没有需要补刷的记录，退出")
        return

    log(f"  共 {len(records)} 条待补刷记录\n")
    for i, r in enumerate(records):
        log(f"  [{i+1}] {r['album_name'] or '(无昵称)'}  |  现有code={r.get('album_code','') or '(空)'}  |  {r['url'][:80]}...  |  {r['create_time']}")

    detail_responses = []

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        }
        if CHROME_PATH and os.path.exists(CHROME_PATH):
            launch_kwargs["executable_path"] = CHROME_PATH
            log(f"  浏览器: {CHROME_PATH}")
        else:
            log("  浏览器: Playwright 自带")

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        page.on("response", create_detail_response_collector(detail_responses))

        results = []
        updated = 0
        fail_reasons = defaultdict(list)  # 按失败原因分组

        for idx, rec in enumerate(records, 1):
            detail_responses.clear()
            url = rec["url"]
            old_name = rec.get("album_name") or ""
            old_remark = rec.get("remark") or ""

            log(f"\n{'=' * 60}")
            log(f"  [{idx}/{len(records)}] {url[:100]}")
            log(f"  DB记录: 昵称={old_name}, 标题={old_remark[:50]}")
            log(f"{'=' * 60}")

            normalized = normalize_url(url)
            detail_responses.clear()  # 清空上一页的 API 响应

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"  加载失败: {e}")
                fail_reasons["页面加载失败"].append({"url": url, "error": str(e)})
                continue

            final_url = page.url
            log(f"  跳转: {final_url}")
            page_type = "笔记" if "/note/" in final_url else "视频"
            log(f"  页面类型: {page_type}")

            try:
                await page.wait_for_selector('video', timeout=10000)
                log("  视频元素已加载")
            except Exception:
                log("  无视频元素（可能是笔记页）")
            await asyncio.sleep(5)

            try:
                page_data = await metadata.extract_metadata(page, detail_responses)
            except Exception as e:
                log(f"  >>> 提取失败(页面跳转): {e}")
                continue

            author = page_data.get("author") or ""
            code = page_data.get("authorCode") or ""
            sec = page_data.get("secUid") or ""
            src = page_data.get("extractSource") or ""
            ssr = page_data.get("ssrAvailable") or []
            api_count = page_data.get("apiResponseCount", 0)

            log(f"  昵称: {author or '(空)'}")
            log(f"  抖音号: {code or '(空)'}")
            log(f"  sec_uid: {sec[:30] if sec else '(空)'}")
            log(f"  提取来源: {src or '(空)'}")
            log(f"  SSR可用: {ssr}")
            log(f"  API响应数: {api_count}")
            if page_data.get("_apiError"):
                log(f"  JS用户API错误: {page_data['_apiError']}")

            if code:
                db.update_album_code(normalized, code)
                updated += 1
                log(f"  >>> 已更新抖音号: {code}")
            else:
                reason = _diagnose_failure(page_data, page_type, final_url)
                log(f"  >>> 未获取到抖音号，原因: {reason}")
                fail_reasons[reason].append({
                    "url": url,
                    "final_url": final_url,
                    "author": author,
                    "sec_uid": sec[:30] if sec else "",
                    "ssr": ssr,
                    "api_count": api_count,
                    "page_type": page_type,
                })

            results.append({
                "url": url,
                "final_url": final_url,
                "author": author,
                "authorCode": code,
                "secUid": sec[:30] if sec else "",
                "extractSource": src,
                "ssrAvailable": ssr,
                "apiResponseCount": api_count,
                "pageType": page_type,
                "updated": bool(code),
            })

        await browser.close()

        log(f"\n{'=' * 60}")
        log(f"  汇总")
        log(f"{'=' * 60}")
        log(f"  总记录: {len(records)}")
        log(f"  成功补刷: {updated}")
        log(f"  仍然失败: {len(records) - updated}")

        if fail_reasons:
            log(f"\n{'=' * 60}")
            log(f"  失败原因分析")
            log(f"{'=' * 60}")
            for reason, items in fail_reasons.items():
                log(f"\n  [{reason}] 共 {len(items)} 条:")
                for item in items:
                    log(f"    - {item['url'][:80]}")
                    log(f"      作者: {item.get('author', '') or '(空)'}, sec_uid: {item.get('sec_uid', '') or '(空)'}")
                    log(f"      SSR: {item.get('ssr', [])}, API: {item.get('api_count', 0)}, 类型: {item.get('page_type', '')}")

        log(f"\n{'=' * 60}")
        log(f"  详细结果 JSON")
        log(f"{'=' * 60}")
        log(json.dumps(results, ensure_ascii=False, indent=2))


def _diagnose_failure(page_data: dict, page_type: str, final_url: str) -> str:
    """根据页面数据诊断为何未提取到抖音号"""
    ssr = page_data.get("ssrAvailable") or []
    api_count = page_data.get("apiResponseCount", 0)
    author = page_data.get("author") or ""
    sec_uid = page_data.get("secUid") or ""
    src = page_data.get("extractSource") or ""
    api_error = page_data.get("_apiError") or ""

    if not author:
        if not ssr:
            return "SSR数据完全不可用（页面可能未完全加载或被反爬）"
        if src:
            return f"SSR数据中无作者信息（来源:{src}）"
        return "SSR数据中无nickname字段（作者信息未嵌入页面）"

    if not sec_uid:
        if not ssr:
            return "无sec_uid且SSR不可用（无法调用用户API）"
        return "SSR数据中无sec_uid字段（无法调用用户API补刷抖音号）"

    if api_error:
        return f"用户API调用失败: {api_error}"

    if api_count == 0:
        return f"笔记页无detail API，且用户API也未返回抖音号（sec_uid:{sec_uid[:20]}...，可能用户未设置抖音号）"

    return f"有sec_uid和API响应但仍未提取到（页面类型:{page_type}，sec_uid:{sec_uid[:20]}...）"


asyncio.run(run())