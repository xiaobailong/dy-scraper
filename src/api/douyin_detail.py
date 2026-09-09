# -*- coding: utf-8 -*-
"""抖音详情 API（aweme/detail, note）响应处理

处理抖音视频/图集详情页的 API 响应，用于提取高清视频/图片链接。
"""

from typing import Callable


# 抖音详情 API 的 URL 路径特征
DOUYIN_DETAIL_API_PATTERNS = [
    "/aweme/v1/web/aweme/detail/",
    "/aweme/v1/web/note/",
]


def is_detail_api_response(url: str) -> bool:
    """判断 URL 是否为抖音详情 API 响应"""
    for pattern in DOUYIN_DETAIL_API_PATTERNS:
        if pattern in url:
            return True
    return False


def create_detail_response_collector(container: list) -> Callable:
    """创建 Playwright response 事件回调，自动收集抖音详情 API 的响应。

    Args:
        container: 用于存储匹配到的 response 对象的列表

    Returns:
        可注册到 page.on("response", ...) 的回调函数

    Usage:
        detail_responses = []
        page.on("response", create_detail_response_collector(detail_responses))
    """
    def _collect(response):
        if is_detail_api_response(response.url):
            container.append(response)
    return _collect


if __name__ == "__main__":
    import asyncio as _asyncio
    import json as _json
    import os as _os
    import sys as _sys
    from pathlib import Path as _Path
    from playwright.async_api import async_playwright as _async_playwright

    _PROJECT_ROOT = _Path(__file__).resolve().parent.parent
    if str(_PROJECT_ROOT) not in _sys.path:
        _sys.path.insert(0, str(_PROJECT_ROOT))
    from data.db_utils import DBUtils

    _db = DBUtils()

    # 尝试加载项目配置中的 CHROME_PATH
    try:
        from config import CHROME_PATH as _CHROME_PATH
    except Exception:
        _CHROME_PATH = None

    # 配置未设置时，尝试本地常见路径
    _FALLBACK_CHROME_PATHS = [
        r"D:\Tools\DevTools\web\chrome-win64\chrome.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    if not _CHROME_PATH or not _Path(_CHROME_PATH).exists():
        for _p in _FALLBACK_CHROME_PATHS:
            if _Path(_p).exists():
                _CHROME_PATH = _p
                break

    TEST_NOTE_URL = "https://www.douyin.com/note/7675380407248344677?previous_page=app_code_link"
    TEST_NOTE_ID = "7675380407248344677"
    API_NOTE_URL = f"https://www.douyin.com/aweme/v1/web/note/{TEST_NOTE_ID}/"
    API_DETAIL_URL = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={TEST_NOTE_ID}"

    async def _check_is_logged_in(page):
        """检测当前页面是否已登录，返回 bool"""
        try:
            return await page.evaluate("""
                () => {
                    try {
                        const hasSid = document.cookie.indexOf('sessionid=') !== -1;
                        const hasOdin = document.cookie.indexOf('odin_tt=') !== -1;
                        if (hasSid && hasOdin) return true;
                    } catch(e) {}
                    if (document.querySelector('img[alt*="头像"], img.avatar, .avatar img')) return true;
                    try {
                        const userInfo = document.querySelector('[data-e2e="user-info"], .user-info, [class*="user"]');
                        if (userInfo && userInfo.textContent && userInfo.textContent.trim().length > 0) return true;
                    } catch(e) {}
                    return false;
                }
            """)
        except Exception:
            return False

    async def _validate_cookies(page, context):
        """校验已注入的 cookies 是否有效：访问抖音首页，检测登录状态。
        返回 True 表示 cookies 有效，False 表示已过期需重新登录。
        """
        print("  [CHECK] 校验 cookies 有效性...")
        try:
            await page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=20000)
            await _asyncio.sleep(1)
            if await _check_is_logged_in(page):
                print("  [CHECK] cookies 有效，已登录")
                return True
            else:
                print("  [CHECK] cookies 已过期，需要重新登录")
                return False
        except Exception as _e:
            print(f"  [CHECK] 校验请求失败: {_e}，按过期处理")
            return False

    async def _interactive_login(page, context):
        """打开抖音首页，等待用户扫码登录，返回是否检测到登录成功"""
        print("\n[LOGIN] 打开抖音首页，请扫码登录...")
        print("[LOGIN] 登录成功后将自动检测并继续（超时 5 分钟）")
        await page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=60000)

        _max_wait = 300
        _start = _asyncio.get_event_loop().time()
        while _asyncio.get_event_loop().time() - _start < _max_wait:
            if await _check_is_logged_in(page):
                print("[LOGIN] 已检测到登录状态！")
                break
            await _asyncio.sleep(2)
        else:
            print("[LOGIN] 等待超时，按当前状态继续（如未登录将无 API 响应）")

        # 保存 cookies
        try:
            raw_cookies = await context.cookies("https://www.douyin.com")
            douyin_cookies = [c for c in raw_cookies if "douyin" in c.get("domain", "")]
            if douyin_cookies:
                _db.save_cookies(douyin_cookies)
                print(f"[LOGIN] 已保存 {len(douyin_cookies)} 条抖音 cookies 到数据库")
        except Exception as _e:
            print(f"[LOGIN] 保存 cookies 失败: {_e}")
        return True

    async def _real_test():
        print("=" * 60)
        print("  抖音详情 API 模块 - 真实调用样例")
        print("=" * 60)

        # ==== 1. URL 匹配 ====
        print("\n[1] is_detail_api_response() - URL 匹配:")
        for url, expected in [
            (API_NOTE_URL, True),
            (API_DETAIL_URL, True),
            (TEST_NOTE_URL, False),
            ("https://www.douyin.com/aweme/v1/web/user/profile/other/", False),
        ]:
            match = is_detail_api_response(url)
            status = "OK" if match == expected else "FAIL"
            print(f"  [{status}] match={match}  {url}")

        # ==== 2. 启动 Playwright 浏览器，真实拦截 note API 响应 ====
        print(f"\n[2] 启动 Playwright 浏览器，真实拦截 note API 响应...")
        async with _async_playwright() as p:
            _USER_AGENT = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
            _COMMON_ARGS = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ]

            # 1. 从数据库加载 cookies
            _db_cookies = _db.get_cookies(".douyin.com")
            print(f"  从数据库加载到 {len(_db_cookies)} 条抖音 cookies")

            # 2. 启动浏览器（如果没有 cookies，使用非 headless 方便扫码登录）
            _need_login = len(_db_cookies) == 0
            _headless = not _need_login

            launch_kwargs = {"headless": _headless, "args": _COMMON_ARGS}
            if _CHROME_PATH and _Path(_CHROME_PATH).exists():
                launch_kwargs["executable_path"] = _CHROME_PATH
                print(f"  使用本地浏览器: {_CHROME_PATH}")
            print(f"  Headless: {_headless} {'(需要扫码登录)' if _need_login else '(使用数据库 cookies)'}")
            _browser = await p.chromium.launch(**launch_kwargs)
            context = await _browser.new_context(user_agent=_USER_AGENT)

            # 3. 注入 cookies
            if _db_cookies:
                try:
                    await context.add_cookies(_db_cookies)
                    print(f"  Cookies 已注入到浏览器上下文")
                except Exception as _e:
                    print(f"  [WARN] 注入 cookies 失败: {_e}")
                    _need_login = True

            page = await context.new_page()

            # 4. 校验 cookies 有效性（有 cookies 时），过期则清除并重新登录
            if _db_cookies and not _need_login:
                if not await _validate_cookies(page, context):
                    _db.clear_cookies(".douyin.com")
                    print("  [CLEAR] 已清除数据库中过期的 cookies")
                    _need_login = True
                    await context.clear_cookies()

            # 5. 如果需要登录，打开抖音首页扫码
            if _need_login:
                await _interactive_login(page, context)

            # 6. 开始拦截 API 响应
            detail_responses = []
            page.on("response", create_detail_response_collector(detail_responses))

            print(f"  访问: {TEST_NOTE_URL}")
            await page.goto(TEST_NOTE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            print(f"  最终 URL: {page.url}")
            print(f"  拦截到 {len(detail_responses)} 个详情 API 响应")

            for i, resp in enumerate(detail_responses, 1):
                print(f"\n  --- 响应 [{i}] ---")
                print(f"  URL: {resp.url}")
                print(f"  Status: {resp.status}")
                try:
                    data = await resp.json()
                    print(f"  顶层 keys: {list(data.keys())[:10]}")
                    aweme = data.get("aweme", {}).get("detail", {}) or data.get("aweme_detail", {})
                    if aweme:
                        print(f"  desc: {aweme.get('desc', 'N/A')[:60]}")
                        images = aweme.get("images", [])
                        print(f"  图片数量: {len(images)}")
                        for j, img in enumerate(images[:3]):
                            urls = img.get("url_list", [])
                            if urls:
                                print(f"    [{j+1}] {urls[-1][:80]}")
                        author = aweme.get("author", {}) or aweme.get("author_info", {})
                        if author:
                            print(f"  作者: {author.get('nickname', 'N/A')} ({author.get('unique_id', 'N/A')})")
                            print(f"  sec_uid: {author.get('sec_uid', 'N/A')}")
                    else:
                        print(f"  JSON 预览 (前 500 字): {_json.dumps(data, ensure_ascii=False)[:500]}")
                except Exception as e:
                    print(f"  解析 JSON 失败: {e}")
                    try:
                        print(f"  Body 预览 (前 300 字): {await resp.text()[:300]}")
                    except Exception:
                        pass

            # 结束前再保存一次 cookies（刷新 session）
            try:
                raw_cookies = await context.cookies("https://www.douyin.com")
                douyin_cookies = [c for c in raw_cookies if "douyin" in c.get("domain", "")]
                if douyin_cookies:
                    _db.save_cookies(douyin_cookies)
                    print(f"\n  结束时更新 cookies: {len(douyin_cookies)} 条")
            except Exception:
                pass

            await _browser.close()

    _asyncio.run(_real_test())