# -*- coding: utf-8 -*-
"""有道云笔记 API

获取爬取目标 URL 列表。
"""

import json
import re

import requests

try:
    from config import YOUDAO_API
    from common.logger import log
    from common.utils import normalize_url
except ImportError:
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).parent.parent))
    from config import YOUDAO_API
    from common.logger import log
    from common.utils import normalize_url


def _parse_youdao_content(content: str) -> str | None:
    """解析有道云笔记v2 JSON格式，提取所有key为'8'的文本节点（含嵌套）"""
    if not content or not content.startswith('{'):
        return None
    try:
        data = json.loads(content)

        def _extract(obj, texts: list):
            if isinstance(obj, dict):
                if "8" in obj and isinstance(obj["8"], str):
                    texts.append(obj["8"])
                for v in obj.values():
                    _extract(v, texts)
            elif isinstance(obj, list):
                for item in obj:
                    _extract(item, texts)

        texts = []
        _extract(data, texts)
        return "\n".join(texts) if texts else None
    except (json.JSONDecodeError, Exception):
        return None


def fetch_urls_from_youdao() -> list[str]:
    """从有道云笔记 API 获取 URL 列表"""
    log("正在从有道云获取 URL 列表...")
    try:
        resp = requests.get(
            YOUDAO_API,
            headers={
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0.0.0 Safari/537.36",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", "")
        if not content:
            log("  有道云返回内容为空")
            return []

        parsed = _parse_youdao_content(content)
        if parsed:
            content = parsed
        content = content.replace("\\n", "\n").replace("\\r\\n", "\n")

        log(f"  [调试] 解析后内容长度: {len(content)} 字符", "debug")
        log(f"  [调试] 内容预览(前500字):\n{content[:500]}", "debug")

        url_pattern = re.compile(r"https?://[^\s]+")
        urls = list(dict.fromkeys(url_pattern.findall(content)))

        log(f"  [调试] 正则匹配到 {len(urls)} 个原始 URL:", "debug")
        for u in urls:
            log(f"    {u}", "debug")

        url_list = []
        skipped_no_com = 0
        skipped_youdao = 0
        for u in urls:
            clean_u = u
            clean_u = re.sub(r"[\u4e00-\u9fff]+.*$", "", clean_u)
            clean_u = clean_u.strip("`\"'")
            clean_u = clean_u.rstrip(".,;:!?）)】】]}`\"'*_~")
            if "youdao.com" in clean_u:
                skipped_youdao += 1
                log(f"  [调试] 跳过(有道域名): {clean_u}", "debug")
                continue
            if ".com" not in clean_u:
                skipped_no_com += 1
                log(f"  [调试] 跳过(非.com): {clean_u}", "debug")
                continue
            url_list.append(normalize_url(clean_u))

        if skipped_no_com:
            log(f"  [调试] 其中 {skipped_no_com} 个非.com域名被跳过, {skipped_youdao} 个有道域名被跳过", "debug")

        log(f"  提取到 {len(url_list)} 个有效 URL")
        return url_list
    except Exception as e:
        log(f"  获取有道云内容失败: {e}")
        return []


if __name__ == "__main__":
    print("=" * 60)
    print("  有道云笔记 API 模块 - 真实调用样例")
    print("=" * 60)

    # ==== 1. 真正调用 fetch_urls_from_youdao ====
    print(f"\n[1] 调用 fetch_urls_from_youdao() 获取真实数据:")
    urls = fetch_urls_from_youdao()
    if urls:
        print(f"  获取到 {len(urls)} 个 URL:")
        for i, url in enumerate(urls, 1):
            print(f"    [{i}] {url}")
    else:
        print(f"  未获取到 URL（请检查 YOUDAO_API 配置和网络）")

    # ==== 2. 展示 API 端点和配置 ====
    print(f"\n[2] API 配置信息:")
    print(f"  YOUDAO_API = {YOUDAO_API!r}")
    print(f"\n  在其他模块中的调用方式:")
    print(f"    from api.youdao import fetch_urls_from_youdao")
    print(f"    urls = fetch_urls_from_youdao()")