# -*- coding: utf-8 -*-
"""
Playwright 浏览器生命周期管理

封装浏览器的启动、反检测、请求收集和关闭。
"""

from pathlib import Path

from common.logger import log
from config import CHROME_PATH
from api.douyin_detail import create_detail_response_collector

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
except ImportError:
    raise SystemExit("请先安装 playwright: pip install playwright")


class BrowserManager:
    """浏览器管理器，封装 Playwright 的完整生命周期。

    用法：
        async with BrowserManager() as bm:
            await bm.page.goto("https://...")
            # bm.collected_requests / bm.detail_responses 自动收集
    """

    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.collected_requests: list[dict] = []
        self.detail_responses: list[dict] = []

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("浏览器尚未启动，请使用 async with BrowserManager() as bm")
        return self._page

    async def __aenter__(self) -> "BrowserManager":
        log("\n[1/6] 启动浏览器...")
        self._playwright = await async_playwright().start()

        launch_kwargs: dict = {
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

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        self._page = await self._context.new_page()

        # 注入反检测脚本
        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        """)

        # 注册网络请求收集器
        self._page.on("response", create_detail_response_collector(self.detail_responses))
        self._page.on("response", lambda response: self.collected_requests.append({
            "url": response.url,
            "contentType": response.headers.get("content-type", ""),
            "contentLength": response.headers.get("content-length"),
        }))

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        log("\n关闭浏览器...")
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        log("完成!")
        return False