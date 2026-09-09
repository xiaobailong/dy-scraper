# -*- coding: utf-8 -*-
"""抖音用户信息 API（user/profile/other）

通过 sec_uid 调用抖音用户主页 API，获取用户昵称和抖音号。
"""


# 用户信息 API 路径
DOUYIN_USER_PROFILE_API_PATH = "/aweme/v1/web/user/profile/other/"


# 浏览器端调用用户信息 API 的 JS 脚本片段
# 在 EXTRACT_SCRIPT 中，当 SSR 和 DOM 都未提取到抖音号时，
# 通过 fetch 调用用户主页 API 获取 unique_id / short_id
USER_PROFILE_API_SCRIPT = """
    if (!result.authorCode && result.secUid && result.secUid !== 'self') {
        try {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), 8000);
            const resp = await fetch(
                `/aweme/v1/web/user/profile/other/?sec_user_id=${encodeURIComponent(result.secUid)}`,
                {
                    signal: controller.signal,
                    headers: {
                        'referer': window.location.href,
                        'accept': 'application/json',
                    },
                    credentials: 'include',
                }
            );
            clearTimeout(timeout);
            if (resp.ok) {
                const data = await resp.json();
                const found = deepFind(data);
                if (found) {
                    if (!result.authorCode && found.unique_id) result.authorCode = found.unique_id;
                    if (!result.authorCode && found.short_id) result.authorCode = found.short_id;
                    if (!result.author && found.nickname) result.author = found.nickname;
                    if (result.authorCode) result.extractSource = 'api:user_profile';
                }
                if (!result.authorCode && data.user) {
                    const u = data.user;
                    if (u.unique_id && typeof u.unique_id === 'string') result.authorCode = u.unique_id;
                    else if (u.short_id) {
                        const sid = typeof u.short_id === 'string' ? u.short_id : String(u.short_id);
                        if (sid && sid !== '0') result.authorCode = sid;
                    }
                    if (result.authorCode) result.extractSource = 'api:user_profile_direct';
                }
            }
        } catch(e) {
            result._apiError = (e.message || '').substring(0, 100);
        }
    }
"""


if __name__ == "__main__":
    import asyncio as _asyncio
    import json as _json
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
        print("  抖音用户信息 API 模块 - 真实调用样例")
        print("=" * 60)

        # ==== 1. 用 Playwright 打开图文页面，拦截 note API 获取 sec_uid ====
        print(f"\n[1] 打开图文页面，拦截 note API 获取 sec_uid...")
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

            # 6. 开始拦截 note API 响应
            note_responses = []

            def _collect_note(response):
                if "/aweme/v1/web/note/" in response.url:
                    note_responses.append(response)

            page.on("response", _collect_note)

            print(f"  访问: {TEST_NOTE_URL}")
            await page.goto(TEST_NOTE_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            print(f"  最终 URL: {page.url}")
            print(f"  拦截到 {len(note_responses)} 个 note API 响应")

            sec_uid = None
            for i, resp in enumerate(note_responses, 1):
                print(f"\n  --- note API 响应 [{i}] ---")
                print(f"  URL: {resp.url}")
                print(f"  Status: {resp.status}")
                try:
                    data = await resp.json()
                    aweme = data.get("aweme", {}).get("detail", {})
                    author = aweme.get("author", {}) or aweme.get("author_info", {})
                    if author:
                        sec_uid = author.get("sec_uid")
                        print(f"  nickname:   {author.get('nickname', 'N/A')}")
                        print(f"  unique_id:  {author.get('unique_id', 'N/A')}")
                        print(f"  sec_uid:    {sec_uid}")
                        print(f"  short_id:   {author.get('short_id', 'N/A')}")
                except Exception as e:
                    print(f"  解析失败: {e}")

            # ==== 2. 如果获取到 sec_uid，拦截用户信息 API 响应 ====
            if sec_uid:
                print(f"\n[2] 用 sec_uid 导航到用户主页，拦截用户信息 API...")
                user_responses = []

                def _collect_user(response):
                    if DOUYIN_USER_PROFILE_API_PATH in response.url:
                        user_responses.append(response)

                page.on("response", _collect_user)

                user_page_url = f"https://www.douyin.com/user/{sec_uid}"
                print(f"  访问: {user_page_url}")
                await page.goto(user_page_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(5000)

                print(f"  拦截到 {len(user_responses)} 个用户信息 API 响应")
                for i, resp in enumerate(user_responses, 1):
                    print(f"\n  --- 用户信息 API 响应 [{i}] ---")
                    print(f"  URL: {resp.url}")
                    print(f"  Status: {resp.status}")
                    try:
                        data = await resp.json()
                        print(f"  顶层 keys: {list(data.keys())[:10]}")
                        user = data.get("user", {})
                        if user:
                            print(f"  nickname:   {user.get('nickname', 'N/A')}")
                            print(f"  unique_id:  {user.get('unique_id', 'N/A')}")
                            print(f"  short_id:   {user.get('short_id', 'N/A')}")
                            print(f"  sec_uid:    {user.get('sec_uid', 'N/A')}")
                            print(f"  signature:  {(user.get('signature', '') or '')[:80]}")
                            print(f"  follower_count: {user.get('follower_count', 'N/A')}")
                            print(f"  aweme_count:    {user.get('aweme_count', 'N/A')}")
                    except Exception as e:
                        print(f"  解析失败: {e}")
            else:
                print(f"\n[2] 跳过: 未获取到 sec_uid（需登录态）")

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

        # ==== 3. 常量信息 ====
        print(f"\n[3] API 常量:")
        print(f"  DOUYIN_USER_PROFILE_API_PATH = {DOUYIN_USER_PROFILE_API_PATH!r}")
        print(f"  USER_PROFILE_API_SCRIPT 长度: {len(USER_PROFILE_API_SCRIPT)} 字符")

    _asyncio.run(_real_test())