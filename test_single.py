# -*- coding: utf-8 -*-
"""独立测试单个 URL 的元数据提取效果"""
import asyncio
import json
import traceback

from playwright.async_api import async_playwright
import core.metadata as metadata

# ============================================================
# 测试 URL（修改这里切换测试目标）
# ============================================================
TEST_URL = "https://v.douyin.com/6kxBNOnyAmU"


async def main():
    try:
        await _run()
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()


async def _run():
    detail_responses = []

    def on_response(response):
        if "/aweme/v1/web/aweme/detail/" in response.url:
            detail_responses.append(response)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=r"D:\Tools\DevTools\web\chrome-win64\chrome.exe",
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        page.on("response", on_response)

        print(f"URL: {TEST_URL}", flush=True)
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=30000)
        print(f"跳转: {page.url}", flush=True)
        await asyncio.sleep(5)

        page_data = await metadata.extract_metadata(page, detail_responses)

        print(f"\n元数据提取结果:", flush=True)
        print(f"  title:       {page_data.get('title')}", flush=True)
        print(f"  author:      {page_data.get('author')}", flush=True)
        print(f"  authorCode:  {page_data.get('authorCode')}", flush=True)
        print(f"  secUid:      {page_data.get('secUid')}", flush=True)
        print(f"  extractSrc:  {page_data.get('extractSource')}", flush=True)
        print(f"  ssrAvailable:{page_data.get('ssrAvailable')}", flush=True)
        print(f"  description: {page_data.get('description', '')[:100]}", flush=True)
        print(f"  videoUrls:   {len(page_data.get('videoUrls', []))} 个", flush=True)
        print(f"  imageUrls:   {len(page_data.get('imageUrls', []))} 个", flush=True)

        print(f"\nJSON:", flush=True)
        print(json.dumps({k: v for k, v in page_data.items() if k not in ('videoUrls', 'imageUrls')}, ensure_ascii=False, indent=2), flush=True)

        await browser.close()
        print("done", flush=True)


if __name__ == "__main__":
    asyncio.run(main())