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
    print("=" * 60)
    print("  抖音详情 API 模块 - 调用样例")
    print("=" * 60)

    # 1. 测试 URL 匹配
    print("\n[1] is_detail_api_response() - 判断 URL 是否为抖音详情 API 响应:")
    test_urls = [
        "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=123456",
        "https://www.douyin.com/aweme/v1/web/note/?note_id=789012",
        "https://www.douyin.com/video/123456",  # 非 API
        "https://www.douyin.com/aweme/v1/web/user/profile/other/",  # 非详情 API
    ]
    for url in test_urls:
        match = is_detail_api_response(url)
        print(f"  {'[OK]' if match else '[  ]'} {url}")

    # 2. 展示 create_detail_response_collector 用法（伪代码）
    print("\n[2] create_detail_response_collector() - 在 Playwright 中的用法:")
    print("""
    import asyncio
    from playwright.async_api import async_playwright
    from api.douyin_detail import create_detail_response_collector

    async def demo():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            detail_responses = []
            page.on("response", create_detail_response_collector(detail_responses))

            await page.goto("https://www.douyin.com/video/123456")
            print(f"捕获到 {len(detail_responses)} 个详情 API 响应")

            await browser.close()

    asyncio.run(demo())
    """)