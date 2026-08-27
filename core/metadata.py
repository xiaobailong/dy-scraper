# -*- coding: utf-8 -*-
"""
抖音页面元数据提取模块
从页面 SSR 数据、DOM、API 响应中提取作者信息（昵称 + 抖音号）

提取策略（按优先级从高到低兜底）：
  策略1: 深度遍历 SSR 数据（__INITIAL_STATE__ / __UNIVERSAL_DATA__ / __NEXT_DATA__）
  策略2: 解析 detail API 响应 JSON
  策略3: DOM 元素提取（作者链接、用户信息区、meta 标签）
  策略4: 页面标题解析

抖音号字段说明：
  - unique_id: 用户自定义抖音号（如 "zhangsan123"），可能为空字符串
  - short_id:  系统分配的数字抖音号（如 "97522453835"），始终存在
  - sec_uid:   用户唯一标识哈希（长字符串），始终存在
  优先取 unique_id，为空则取 short_id
"""

import json
import re
from typing import Any

from common.logger import log
from api.douyin_user import USER_PROFILE_API_SCRIPT


# ============================================================
# 浏览器端 JS 提取脚本
# ============================================================

EXTRACT_SCRIPT = r"""
async () => {
    const result = {
        title: document.title || '',
        author: '',
        authorCode: '',
        secUid: '',
        description: '',
        videoUrls: [],
        imageUrls: [],
        coverUrl: '',
        pageUrl: window.location.href,
        extractSource: '',  // 记录提取来源，便于调试
    };

    // ========================================================
    // 工具: 从当前页面 URL 提取 aweme_id / note_id 用于 SSR 校验
    // 防止 SPA 软跳转时，旧页面的 SSR 变量未被清除造成"串数据"
    // ========================================================
    const _currentPageUrl = window.location.href || '';
    // 从 URL 中提取可能的 aweme id: /video/73955... /note/73955...
    function _extractIdsFromUrl(url) {
        const ids = new Set();
        (url || '').replace(/[\/\?#\.=-](\d{15,22})[\/\?#\.=&-]/g, (_, id) => { ids.add(id); return _; });
        // 末尾数字
        const m = (url || '').match(/(\d{15,22})(?:\?|#|$)/);
        if (m) ids.add(m[1]);
        // 路径段中的数字
        const segs = (url || '').split('/');
        for (const s of segs) {
            const mm = s.match(/^(\d{15,22})$/);
            if (mm) ids.add(mm[1]);
        }
        return ids;
    }
    const _pageIds = _extractIdsFromUrl(_currentPageUrl);
    // 从 aweme_detail / note_detail 对象中找到它的 aweme_id
    function _findDetailId(detail) {
        if (!detail || typeof detail !== 'object') return '';
        for (const k of ['aweme_id', 'note_id', 'item_id', 'id', 'awemeId', 'noteId']) {
            const v = detail[k];
            if (typeof v === 'string' && v) return v;
            if (typeof v === 'number' && v > 0) return String(v);
        }
        return '';
    }
    // 校验 SSR 详情对象是否属于当前页面
    function _detailBelongsToCurrentPage(detail) {
        if (!_pageIds.size) return true;  // 无法从 URL 提取 id 时保守放行
        const did = _findDetailId(detail);
        if (!did) return true;
        return _pageIds.has(did);
    }
    // 从 SSR 根对象中找出 aweme_detail / note_detail，并校验归属
    function _findValidDetail(root) {
        if (!root || typeof root !== 'object') return null;
        for (const k of ['aweme_detail', 'note_detail', 'aweme', 'note', 'itemDetail', 'item_detail']) {
            const d = root[k];
            if (d && typeof d === 'object' && _detailBelongsToCurrentPage(d)) return d;
        }
        return null;
    }

    // ========================================
    // 策略1: 深度遍历 SSR 数据
    // ========================================
    const ssrKeys = ['__INITIAL_STATE__', '__UNIVERSAL_DATA__', '__NEXT_DATA__', '__NUXT__', '__DATA__', '__META_DATA__', 'RENDER_DATA'];
    const ssrAvailable = [];
    for (const key of ssrKeys) {
        if (window[key] !== undefined) ssrAvailable.push(key);
    }
    result.ssrAvailable = ssrAvailable;

    // 抖音号字段名变体（笔记页 RENDER_DATA 可能用不同命名）
    const CODE_KEYS = ['unique_id', 'short_id', 'uid', 'author_uid', 'user_id', 'douyin_id', 'account_id'];
    function _extractCode(obj) {
        for (const k of CODE_KEYS) {
            if (k in obj) {
                const v = obj[k];
                if (typeof v === 'string' && v && v !== '0' && v !== 'undefined') return v;
                if (typeof v === 'number' && v > 0) return String(v);
            }
        }
        return '';
    }

    function deepFind(obj, maxDepth, visited) {
        if (maxDepth === undefined) maxDepth = 12;
        if (visited === undefined) visited = new WeakSet();
        if (maxDepth <= 0 || obj === null || typeof obj !== 'object') return null;
        if (visited.has(obj)) return null;
        visited.add(obj);

        const found = { nickname: '', unique_id: '', short_id: '', sec_uid: '' };

        if (Array.isArray(obj)) {
            for (let i = 0; i < obj.length; i++) {
                const child = deepFind(obj[i], maxDepth - 1, visited);
                if (child) {
                    if (!found.nickname && child.nickname) found.nickname = child.nickname;
                    if (!found.unique_id && child.unique_id) found.unique_id = child.unique_id;
                    if (!found.short_id && child.short_id) found.short_id = child.short_id;
                    if (!found.sec_uid && child.sec_uid) found.sec_uid = child.sec_uid;
                    if (found.nickname && (found.unique_id || found.short_id)) break;
                }
            }
        } else {
            const keys = Object.keys(obj);
            if ('nickname' in obj && typeof obj.nickname === 'string' && obj.nickname) {
                found.nickname = obj.nickname;
            }
            // 标准字段
            if ('unique_id' in obj && typeof obj.unique_id === 'string' && obj.unique_id) {
                found.unique_id = obj.unique_id;
            }
            if ('short_id' in obj) {
                const sid = typeof obj.short_id === 'string' ? obj.short_id : String(obj.short_id);
                if (sid && sid !== '0' && sid !== 'undefined') found.short_id = sid;
            }
            // 笔记页可能使用的变体字段名
            if (!found.unique_id && !found.short_id) {
                const code = _extractCode(obj);
                if (code) {
                    // 纯数字 -> short_id，否则 -> unique_id
                    if (/^\d+$/.test(code)) found.short_id = code;
                    else found.unique_id = code;
                }
            }
            if ('sec_uid' in obj && typeof obj.sec_uid === 'string' && obj.sec_uid) {
                found.sec_uid = obj.sec_uid;
            }
            if (found.nickname && (found.unique_id || found.short_id)) return found;

            for (let i = 0; i < keys.length; i++) {
                const v = obj[keys[i]];
                if (v && typeof v === 'object') {
                    const child = deepFind(v, maxDepth - 1, visited);
                    if (child) {
                        if (!found.nickname && child.nickname) found.nickname = child.nickname;
                        if (!found.unique_id && child.unique_id) found.unique_id = child.unique_id;
                        if (!found.short_id && child.short_id) found.short_id = child.short_id;
                        if (!found.sec_uid && child.sec_uid) found.sec_uid = child.sec_uid;
                        if (found.nickname && (found.unique_id || found.short_id)) break;
                    }
                }
            }
        }
        return (found.nickname || found.unique_id || found.short_id || found.sec_uid) ? found : null;
    }

    for (const key of ssrKeys) {
        if (window[key]) {
            try {
                const data = typeof window[key] === 'string'
                    ? JSON.parse(window[key])
                    : window[key];
                // === SSR 归属校验: 若 data 中存在 aweme_detail/note_detail 但不属于当前页面，跳过整个 SSR key ===
                const validDetail = _findValidDetail(data);
                // 如果找到了 aweme_detail/note_detail 但没有一个归属当前页面 -> SSR 是旧页面残留的
                let hasAnyDetail = false;
                try {
                    const _walk = (o) => {
                        if (!o || typeof o !== 'object') return;
                        if (Array.isArray(o)) { for (const x of o) _walk(x); return; }
                        for (const k of Object.keys(o)) {
                            if (['aweme_detail','note_detail','aweme','note'].includes(k) && o[k] && typeof o[k]==='object') { hasAnyDetail = true; return; }
                            _walk(o[k]);
                            if (hasAnyDetail) return;
                        }
                    };
                    _walk(data);
                } catch(e) {}
                if (hasAnyDetail && !validDetail) {
                    // console.debug('[SSR跳过] key=' + key + ' 的详情对象不属于当前URL, 判定为旧页面残留');
                    continue;
                }
                const found = deepFind(data);
                if (found) {
                    if (found.nickname) result.author = found.nickname;
                    if (found.unique_id) {
                        result.authorCode = found.unique_id;
                    } else if (found.short_id) {
                        result.authorCode = found.short_id;
                    }
                    if (found.sec_uid) result.secUid = found.sec_uid;
                    if (result.author) {
                        result.extractSource = 'ssr_deep:' + key;
                        break;
                    }
                }
            } catch(e) {}
        }
    }

    // ========================================
    // 策略1b: SSR 正则兜底（扩展字段名变体）
    // ========================================
    if (!result.author) {
        for (const key of ssrKeys) {
            if (window[key]) {
                try {
                    const data = typeof window[key] === 'string'
                        ? JSON.parse(window[key])
                        : window[key];
                    // SSR 归属校验（同策略1）
                    let validDetail = _findValidDetail(data);
                    let hasAnyDetail = false;
                    try {
                        (function _walk(o) {
                            if (!o || typeof o !== 'object') return;
                            if (Array.isArray(o)) { for (const x of o) _walk(x); return; }
                            for (const k of Object.keys(o)) {
                                if (['aweme_detail','note_detail','aweme','note'].includes(k) && o[k] && typeof o[k]==='object') { hasAnyDetail = true; return; }
                                _walk(o[k]); if (hasAnyDetail) return;
                            }
                        })(data);
                    } catch(e) {}
                    if (hasAnyDetail && !validDetail) continue;

                    const jsonStr = JSON.stringify(data);
                    const nickMatch = jsonStr.match(/"nickname"\s*:\s*"([^"]+)"/);
                    if (nickMatch) result.author = nickMatch[1];
                    const uidMatch = jsonStr.match(/"unique_id"\s*:\s*"([^"]+)"/);
                    if (uidMatch && !result.authorCode) result.authorCode = uidMatch[1];
                    if (!result.authorCode) {
                        const sidMatch = jsonStr.match(/"short_id"\s*:\s*"([^"]+)"|"short_id"\s*:\s*(\d+)/);
                        if (sidMatch) result.authorCode = sidMatch[1] || sidMatch[2];
                    }
                    // 笔记页变体字段名
                    if (!result.authorCode) {
                        for (const ck of ['uid', 'author_uid', 'user_id', 'douyin_id', 'account_id']) {
                            const re = new RegExp('"' + ck + '"\\s*:\\s*"([^"]+)"');
                            const m = jsonStr.match(re);
                            if (m && m[1] && m[1] !== '0') {
                                result.authorCode = m[1];
                                break;
                            }
                        }
                    }
                    if (result.author) {
                        result.extractSource = 'ssr_regex:' + key;
                        break;
                    }
                } catch(e) {}
            }
        }
    }

    // ========================================
    // 策略2: 从 RENDER_DATA 提取（图文页核心数据源）
    // RENDER_DATA 可能在 window 上（已解析的对象）或 script#RENDER_DATA 标签中（JSON 字符串）
    // ========================================
    if (!result.author || !result.authorCode) {
        let renderData = null;
        const renderEl = document.getElementById('RENDER_DATA');
        if (renderEl && renderEl.textContent) {
            try {
                renderData = JSON.parse(renderEl.textContent);
            } catch(e) {
                try {
                    renderData = JSON.parse(decodeURIComponent(renderEl.textContent));
                } catch(e2) {}
            }
        }
        if (!renderData && window.RENDER_DATA && typeof window.RENDER_DATA === 'object') {
            renderData = window.RENDER_DATA;
        }
        if (renderData) {
            // SSR 归属校验
            const validDetail = _findValidDetail(renderData);
            let hasAnyDetail = false;
            try {
                (function _walk(o) {
                    if (!o || typeof o !== 'object') return;
                    if (Array.isArray(o)) { for (const x of o) _walk(x); return; }
                    for (const k of Object.keys(o)) {
                        if (['aweme_detail','note_detail','aweme','note'].includes(k) && o[k] && typeof o[k]==='object') { hasAnyDetail = true; return; }
                        _walk(o[k]); if (hasAnyDetail) return;
                    }
                })(renderData);
            } catch(e) {}
            if (hasAnyDetail && !validDetail) {
                // 旧页面残留 RENDER_DATA，不使用策略2
            } else {
                // 先用深度遍历
                const found = deepFind(renderData);
                if (found) {
                    if (found.nickname && !result.author) result.author = found.nickname;
                    if (found.unique_id && !result.authorCode) result.authorCode = found.unique_id;
                    else if (found.short_id && !result.authorCode) result.authorCode = found.short_id;
                    if (found.sec_uid && (!result.secUid || result.secUid === 'self')) result.secUid = found.sec_uid;
                    if (result.author) result.extractSource = 'render_data_deep';
                }
                // 深度遍历没找到，用正则兜底（含变体字段名）
                if (!result.authorCode) {
                    try {
                        const jsonStr = JSON.stringify(renderData);
                        const uidMatch = jsonStr.match(/"unique_id"\s*:\s*"([^"]+)"/);
                        if (uidMatch) result.authorCode = uidMatch[1];
                        if (!result.authorCode) {
                            const sidMatch = jsonStr.match(/"short_id"\s*:\s*"([^"]+)"|"short_id"\s*:\s*(\d+)/);
                            if (sidMatch) result.authorCode = sidMatch[1] || sidMatch[2];
                        }
                        if (!result.authorCode) {
                            for (const ck of ['uid', 'author_uid', 'user_id', 'douyin_id', 'account_id']) {
                            const re = new RegExp('"' + ck + '"\\s*:\\s*"([^"]+)"');
                            const m = jsonStr.match(re);
                            if (m && m[1] && m[1] !== '0') {
                                result.authorCode = m[1];
                                break;
                            }
                        }
                    if (result.authorCode && !result.extractSource) result.extractSource = 'render_data_regex';
                    }
                } catch(e) {}
                }
            }
        }
    }

    // ========================================
    // 策略3: DOM 提取
    // ========================================
    // 3a: 在内容区域找作者链接（排除导航栏的"我的"等）
    if (!result.author) {
        const allLinks = document.querySelectorAll('a[href*="/user/"]');
        for (const link of allLinks) {
            const href = link.getAttribute('href') || '';
            // 跳过导航链接
            if (href === '/user/self' || href.startsWith('/user/self?')) continue;
            const text = (link.textContent || '').trim();
            if (!text || text.length > 30) continue;
            // 排除导航文字
            if (/^(我的|首页|消息|朋友|我|推荐|热门|同城|关注|粉丝|获赞|作品|喜欢|私信|直播|商城|搜索)$/.test(text)) continue;
            // 检查是否在导航区域内
            if (link.closest('nav') || link.closest('[class*="nav"]') || link.closest('[class*="header"]') || link.closest('[class*="tab"]')) continue;
            result.author = text;
            result.extractSource = 'dom:author_link';
            if (!result.secUid || result.secUid === 'self') {
                const secMatch = href.match(/\/user\/([^/?]+)/);
                if (secMatch && secMatch[1] && secMatch[1] !== 'self') result.secUid = secMatch[1];
            }
            break;
        }
    }

    if (!result.author) {
        // 3b: 带粉丝数的作者信息区
        const userInfo = document.querySelector('[data-e2e="user-info"]');
        if (userInfo) {
            const text = userInfo.textContent.trim();
            const match = text.match(/^([^\s粉丝]+?)(?:粉丝|获赞|关注)/);
            if (match) {
                result.author = match[1].trim();
                result.extractSource = 'dom:user_info';
            } else {
                const parts = text.split(/[粉丝获赞关注]/);
                if (parts[0] && parts[0].trim().length < 30) {
                    result.author = parts[0].trim();
                    result.extractSource = 'dom:user_info';
                }
            }
        }
    }

    if (!result.author) {
        // 3c: 常见作者选择器
        const authorSelectors = [
            '[data-e2e="user-title"]',
            '[data-e2e="video-author"]',
            'span[class*="nickname"]',
            'span[class*="accountName"]',
            'a[class*="author"]',
            '.account-name',
        ];
        for (const sel of authorSelectors) {
            const el = document.querySelector(sel);
            if (el && el.textContent && el.textContent.trim() && el.textContent.trim().length < 30) {
                const t = el.textContent.trim();
                if (!/^(我的|首页|消息|关注|推荐|热门)$/.test(t)) {
                    result.author = t;
                    result.extractSource = 'dom:selector:' + sel;
                    break;
                }
            }
        }
    }

    // 3d: 从 DOM 提取 sec_uid（排除 /user/self）
    if (!result.secUid || result.secUid === 'self') {
        const allLinks = document.querySelectorAll('a[href*="/user/"]');
        for (const link of allLinks) {
            const href = link.getAttribute('href') || '';
            if (href === '/user/self' || href.startsWith('/user/self?')) continue;
            const secMatch = href.match(/\/user\/([^/?]+)/);
            if (secMatch && secMatch[1] && secMatch[1] !== 'self') {
                result.secUid = secMatch[1];
                break;
            }
        }
    }

    if (!result.authorCode) {
        // 3e: 页面文本中直接显示抖音号
        const body = document.body.innerText || '';
        const douyinIdMatch = body.match(/抖音号[：:]\s*(\S+)/);
        if (douyinIdMatch && douyinIdMatch[1].length < 30) {
            result.authorCode = douyinIdMatch[1];
            result.extractSource = 'dom:douyin_id_text';
        }
    }

    // ========================================
    // 策略4: 页面标题解析
    // ========================================
    if (!result.author && result.title) {
        const titleMatch = result.title.match(/^(.+?)[在的-]/);
        if (titleMatch && titleMatch[1].trim().length < 20) {
            result.author = titleMatch[1].trim();
            result.extractSource = 'title_parse';
        }
    }

    // ========================================
    // 策略5: 通过 sec_uid 调用用户信息 API 获取抖音号
    // ========================================
""" + USER_PROFILE_API_SCRIPT + r"""
    // ========================================
    // 通用: meta description
    // ========================================
    const metaDesc = document.querySelector('meta[name="description"]');
    if (metaDesc) result.description = metaDesc.getAttribute('content') || '';

    // ========================================
    // 通用: 提取媒体 URL
    // ========================================
    const contentArea = document.querySelector('video')?.closest('div[class*="video"]')
        || document.querySelector('video')?.parentElement?.parentElement
        || document;

    contentArea.querySelectorAll('video').forEach(video => {
        if (video.src && !result.videoUrls.includes(video.src)) {
            result.videoUrls.push(video.src);
        }
        video.querySelectorAll('source').forEach(source => {
            if (source.src && !result.videoUrls.includes(source.src)) {
                result.videoUrls.push(source.src);
            }
        });
    });

    contentArea.querySelectorAll('img').forEach(img => {
        const src = img.src;
        if (!src || src.startsWith('data:') || src.includes('1x1')) return;
        if (src.includes('100x100') || src.includes('avatar') || src.includes('twemoji')) return;
        if (!result.imageUrls.includes(src)) {
            result.imageUrls.push(src);
        }
    });

    contentArea.querySelectorAll('video[poster]').forEach(video => {
        const poster = video.getAttribute('poster');
        if (poster && !result.coverUrl) {
            result.coverUrl = poster;
        }
    });

    return result;
}
"""


# ============================================================
# 笔记页兜底: 从页面 SSR 数据提取图片 URL
# （笔记页没有 API 响应，图片数据全在 RENDER_DATA 等 SSR 变量中）
# ============================================================

async def extract_images_from_ssr(page, page_url: str = "") -> list[str]:
    """笔记页没有 API 响应，图片数据全在 RENDER_DATA 等 SSR 变量中。
    优先通过 JSON 解析提取，正则兜底。
    page_url: 当前页面URL，用于校验 SSR 中的详情对象归属（防止 SPA 软跳转时 SSR 残留旧页面）
    """
    try:
        ssr_json = await page.evaluate("""
            () => {
                const keys = ['RENDER_DATA', '__INITIAL_STATE__', '__UNIVERSAL_DATA__'];
                for (const k of keys) {
                    if (window[k]) {
                        const d = typeof window[k] === 'string' ? JSON.parse(window[k]) : window[k];
                        return JSON.stringify(d);
                    }
                }
                const el = document.getElementById('RENDER_DATA');
                if (el && el.textContent) {
                    try { return JSON.stringify(JSON.parse(el.textContent)); } catch(e) {}
                    try { return JSON.stringify(JSON.parse(decodeURIComponent(el.textContent))); } catch(e) {}
                }
                return null;
            }
        """)
        if not ssr_json:
            return []

        try:
            data = json.loads(ssr_json)
        except json.JSONDecodeError:
            data = None

        # 方法1: JSON 结构解析 — 从 note detail 中提取 images
        if isinstance(data, dict):
            urls = _extract_images_from_ssr_dict(data, page_url)
            if urls:
                return urls

        # 方法2: 正则兜底 — 在原始 JSON 字符串中搜索 douyinpic.com 图片链接
        # 注意：正则兜底无法做归属校验，可能混入旧内容，但仅在方法1无结果时启用
        urls = set()
        for m in re.finditer(r'"https?://[^"\s]*?douyinpic\.com[^"\s]*?"', ssr_json):
            u = m.group(0)[1:-1]
            if not any(x in u.lower() for x in ('avatar', '100x100', '1x1', 'twemoji')):
                urls.add(u)
        return list(urls)
    except Exception:
        return []


def _extract_images_from_ssr_dict(data: dict, page_url: str = "") -> list[str]:
    """从 SSR JSON 对象中提取图片 URL，遍历常见路径。
    若 aweme_detail/note_detail 存在则做归属校验，不属于当前页面的会被跳过。"""
    urls = set()

    # 路径1: note_detail.images (笔记页)
    note = data.get("note_detail") or data.get("note")
    if isinstance(note, dict):
        if _detail_belongs_to_current_page(note, page_url):
            images = note.get("images") or []
            for img in images if isinstance(images, list) else []:
                if isinstance(img, dict):
                    for key in ("download_url_list", "url_list"):
                        for u in (img.get(key) or []):
                            if isinstance(u, str) and u.startswith("http"):
                                urls.add(u)

    # 路径2: aweme_detail.images (视频图文)
    aweme = data.get("aweme_detail") or data.get("aweme")
    if isinstance(aweme, dict):
        if _detail_belongs_to_current_page(aweme, page_url):
            images = aweme.get("images") or []
            for img in images if isinstance(images, list) else []:
                if isinstance(img, dict):
                    for key in ("download_url_list", "url_list"):
                        for u in (img.get(key) or []):
                            if isinstance(u, str) and u.startswith("http"):
                                urls.add(u)

    # 路径3: 递归查找所有包含 images 数组的对象（不带归属校验，仅在前两条路径未找到时使用）
    if not urls:
        urls.update(_deep_find_images(data))

    return list(urls)


def _deep_find_images(obj, depth=0) -> set:
    """递归查找对象树中所有包含 images 数组的节点，提取图片 URL"""
    if depth > 10:
        return set()
    urls = set()
    if isinstance(obj, dict):
        images = obj.get("images")
        if isinstance(images, list):
            for img in images:
                if isinstance(img, dict):
                    for key in ("download_url_list", "url_list"):
                        for u in (img.get(key) or []):
                            if isinstance(u, str) and u.startswith("http"):
                                urls.add(u)
        for v in obj.values():
            urls.update(_deep_find_images(v, depth + 1))
    elif isinstance(obj, list):
        for v in obj:
            urls.update(_deep_find_images(v, depth + 1))
    return urls


# ============================================================
# Python 端: 从 detail API 响应中提取媒体 URL（视频 + 图片）
# ============================================================

def _extract_ids_from_url(url: str) -> set[str]:
    """从 URL 中提取可能的 aweme/note id（15-22 位数字），用于交叉校验"""
    ids: set[str] = set()
    if not url:
        return ids
    for m in re.finditer(r'(?:^|[^0-9])(\d{15,22})(?:[^0-9]|$)', url):
        ids.add(m.group(1))
    return ids


def _extract_detail_id(detail: dict) -> str:
    """从 aweme_detail / note_detail 对象中提取 aweme_id/note_id"""
    if not isinstance(detail, dict):
        return ""
    for key in ("aweme_id", "note_id", "item_id", "id"):
        v = detail.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, int) and v > 0:
            return str(v)
    return ""


def _detail_belongs_to_current_page(detail: dict, page_url: str) -> bool:
    """校验详情对象的 id 是否在当前页面 URL 中出现"""
    page_ids = _extract_ids_from_url(page_url)
    if not page_ids:
        return True
    did = _extract_detail_id(detail)
    if not did:
        return True
    return did in page_ids


async def extract_media_from_api_responses(detail_responses: list, page_url: str = "") -> tuple[list[str], list[str]]:
    """
    从 detail API 响应中提取高质量视频和图片 URL。
    支持 aweme_detail (视频) 和 note_detail (笔记) 两种结构。
    优先级: bit_rate 高码率 > download_addr > play_addr
    返回: (video_urls: 已按质量排序, image_urls: 已按分辨率排序)

    page_url: 当前页面 URL，用于校验响应是否属于当前页面（防止上一页面 pending 回调混入）
    """
    all_video_urls: list[tuple[str, int]] = []  # (url, quality_score)
    all_image_urls: list[tuple[str, int]] = []  # (url, quality_score)

    for resp in detail_responses:
        try:
            body = await resp.text()
        except Exception:
            continue

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue

        # 在 aweme_detail 或 note_detail 中查找媒体内容
        for detail_key in ("aweme_detail", "note_detail"):
            detail = data.get(detail_key)
            if not isinstance(detail, dict):
                continue
            # ==== 归属校验：若响应中的 aweme_id 不在当前页面URL，说明是上一页面的响应，丢弃 ====
            if not _detail_belongs_to_current_page(detail, page_url):
                log(f"  [API归属校验] 丢弃 {detail_key} 响应 (id={_extract_detail_id(detail)[:20]} 不在当前URL)", "debug")
                continue
            _extract_video_from_detail(detail, all_video_urls)
            _extract_images_from_detail(detail, all_image_urls)

        # 兜底：直接在顶层 data 中查找（兼容旧结构）
        if isinstance(data, dict) and not all_video_urls and not all_image_urls:
            if _detail_belongs_to_current_page(data, page_url):
                _extract_video_from_detail(data, all_video_urls)
                _extract_images_from_detail(data, all_image_urls)

    # 按质量分数降序排序，去重保持顺序
    seen_v = set()
    video_urls = []
    for u, _ in sorted(all_video_urls, key=lambda x: x[1], reverse=True):
        if u not in seen_v and not u.startswith("blob:"):
            seen_v.add(u)
            video_urls.append(u)

    seen_i = set()
    image_urls = []
    for u, _ in sorted(all_image_urls, key=lambda x: x[1], reverse=True):
        if u not in seen_i and not u.startswith("data:") and not u.startswith("blob:"):
            seen_i.add(u)
            image_urls.append(u)

    return video_urls, image_urls


def _extract_video_from_detail(detail: dict, all_video_urls: list) -> None:
    """从 aweme_detail / note_detail 中提取视频 URL"""
    video = detail.get("video")
    if not isinstance(video, dict):
        return

    best_url = None
    best_score = -1

    # 方案1: download_addr（无水印原画，最高优先级）
    dl_urls = (video.get("download_addr") or {}).get("url_list") or []
    if dl_urls:
        best_url = dl_urls[0]
        best_score = 9999999

    # 方案2: bit_rate 列表（多码率，取最高码率的第一条 CDN）
    bit_rates = video.get("bit_rate") or []
    if isinstance(bit_rates, list):
        for br_item in bit_rates:
            br_val = br_item.get("bit_rate", 0)
            urls = (br_item.get("play_addr") or {}).get("url_list") or []
            if urls and br_val > best_score:
                best_url = urls[0]
                best_score = br_val

    # 方案3: play_addr（播放地址，兜底）
    if best_url is None:
        play_urls = (video.get("play_addr") or {}).get("url_list") or []
        if play_urls:
            best_url = play_urls[0]
            best_score = 5000000

    # 方案4: play_addr_h264（最后兜底）
    if best_url is None:
        h264_urls = (video.get("play_addr_h264") or {}).get("url_list") or []
        if h264_urls:
            best_url = h264_urls[0]
            best_score = 4000000

    if best_url:
        all_video_urls.append((best_url, best_score))


def _extract_images_from_detail(detail: dict, all_image_urls: list) -> None:
    """从 aweme_detail / note_detail 中提取图片 URL（图文笔记）"""
    images = detail.get("images") or []
    if not isinstance(images, list):
        return

    for img in images:
        if not isinstance(img, dict):
            continue
        best_score = -1
        best_url = None

        # download_url_list 是原始分辨率，优先
        dl_list = img.get("download_url_list") or []
        for u in dl_list:
            score = _image_quality_score(u) + 10000000
            if score > best_score:
                best_score = score
                best_url = u

        url_list = img.get("url_list") or []
        for u in url_list:
            score = _image_quality_score(u)
            if score > best_score:
                best_score = score
                best_url = u

        if best_url:
            all_image_urls.append((best_url, best_score))


def _image_quality_score(url: str) -> int:
    """从 URL 估算图片质量分数（越高越好）。URL 中常含 '720x1280' 这样的尺寸。"""
    dim_match = re.search(r'[~_](\d{2,4})x(\d{2,4})', url)
    if dim_match:
        w, h = int(dim_match.group(1)), int(dim_match.group(2))
        return w * h  # 像素总数
    return 0


# ============================================================
# Python 端: 从 detail API 响应中提取作者信息
# ============================================================

# extractSource 来源中，下这些可能混入登录用户数据，不可靠，需要 API 覆盖
_UNRELIABLE_SOURCES = frozenset({"dom:user_info", "title_parse", "meta_tags", "render_data_deep"})


async def extract_from_api_responses(detail_responses: list, page_data: dict) -> dict:
    """
    从 /aweme/v1/web/aweme/detail/ 或 /aweme/v1/web/note/ API 响应中提取作者信息，
    作为浏览器端提取的补充。

    优先级: unique_id > short_id > sec_uid
    """
    if not detail_responses:
        return page_data

    page_url = page_data.get("pageUrl") or ""
    author_found = bool(page_data.get("author"))
    code_found = bool(page_data.get("authorCode"))

    # DOM 提取的 author/authorCode 可能不准确（如侧边栏混入登录用户，或上一页面残留值），
    # 如果 extractSource 是不可靠来源，则强制用 API 响应覆盖
    extract_source = page_data.get("extractSource", "")
    _force_override = extract_source in _UNRELIABLE_SOURCES
    if code_found and _force_override:
        log(f"  [API调试] extractSource={extract_source} 不可靠，强制用API覆盖authorCode", "debug")
        code_found = False
    if author_found and _force_override:
        log(f"  [API调试] extractSource={extract_source} 不可靠，强制用API覆盖author", "debug")
        author_found = False

    for resp in detail_responses:
        try:
            body = await resp.text()
        except Exception:
            continue

        # 尝试解析 JSON
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            # 回退到正则（正则无法做归属校验，所以仅在当前请求URL本身含当前页ID时才使用）
            _resp_url_ok = True
            if page_url:
                _pid = _extract_ids_from_url(page_url)
                _rid = _extract_ids_from_url(resp.url)
                # 如果请求URL本身含有当前页ID，才信任这个响应
                if _pid and _rid and _pid.isdisjoint(_rid):
                    _resp_url_ok = False
            if _resp_url_ok:
                if not author_found:
                    m = re.search(r'"nickname"\s*:\s*"([^"]+)"', body)
                    if m:
                        page_data["author"] = m.group(1)
                        author_found = True
                        if not page_data.get("extractSource"):
                            page_data["extractSource"] = "api:detail_regex"
                if not code_found:
                    m = re.search(r'"unique_id"\s*:\s*"([^"]+)"', body)
                    if m:
                        page_data["authorCode"] = m.group(1)
                        code_found = True
                        if not page_data.get("extractSource"):
                            page_data["extractSource"] = "api:detail_regex"
                    else:
                        m = re.search(r'"short_id"\s*:\s*"([^"]+)"|"short_id"\s*:\s*(\d+)', body)
                        if m:
                            page_data["authorCode"] = m.group(1) or m.group(2)
                            code_found = True
                            if not page_data.get("extractSource"):
                                page_data["extractSource"] = "api:detail_regex"
            if author_found and code_found:
                break
            continue

        # JSON 解析成功：先定位到 detail 对象，做归属校验，再取 author
        _detail_obj = None
        for _dk in ("aweme_detail", "note_detail"):
            _d = data.get(_dk)
            if isinstance(_d, dict):
                _detail_obj = _d
                break
        if _detail_obj and page_url and not _detail_belongs_to_current_page(_detail_obj, page_url):
            # 这个响应不属于当前页面（上一页面 pending 回调），跳过
            log(f"  [API归属校验] 作者信息丢弃（aweme_id={_extract_detail_id(_detail_obj)[:20]} 不在当前URL）", "debug")
            continue

        # JSON 解析成功：先定位到 author 对象，再从中提取，避免混入其他用户的 short_id
        author_obj = _extract_author_object(data)
        log(f"  [API调试] URL={resp.url[:100]}, 找到author对象={author_obj is not None}, "
            f"keys={list(author_obj.keys())[:10] if author_obj else 'N/A'}", "debug")
        if author_obj:
            log(f"  [API调试] author.nickname={str(author_obj.get('nickname',''))[:30]}, "
                f"unique_id={str(author_obj.get('unique_id',''))[:30]}, "
                f"short_id={str(author_obj.get('short_id',''))[:30]}, "
                f"sec_uid={str(author_obj.get('sec_uid',''))[:30]}", "debug")
        if author_obj:
            if not author_found and author_obj.get("nickname"):
                page_data["author"] = author_obj["nickname"]
                author_found = True
            if not code_found:
                uid = author_obj.get("unique_id")
                if isinstance(uid, str) and uid:
                    page_data["authorCode"] = uid
                    code_found = True
                else:
                    sid = author_obj.get("short_id")
                    if isinstance(sid, (int, float)) and sid > 0:
                        page_data["authorCode"] = str(int(sid))
                        code_found = True
                    elif isinstance(sid, str) and sid and sid != "0":
                        page_data["authorCode"] = sid
                        code_found = True
            if author_obj.get("sec_uid"):
                page_data["secUid"] = author_obj["sec_uid"]
            if author_found or code_found:
                page_data["extractSource"] = "api:detail_author_obj"

        # 如果 author 对象中没找到 code，再在 author 对象内深度遍历
        if author_obj and not code_found:
            found = _deep_find_author(author_obj, max_depth=6)
            if found:
                if not author_found and found.get("nickname"):
                    page_data["author"] = found["nickname"]
                    author_found = True
                if not code_found:
                    if found.get("unique_id"):
                        page_data["authorCode"] = found["unique_id"]
                        code_found = True
                    elif found.get("short_id"):
                        page_data["authorCode"] = found["short_id"]
                        code_found = True
                if found.get("sec_uid"):
                    page_data["secUid"] = found["sec_uid"]
                if author_found or code_found:
                    page_data["extractSource"] = "api:detail_author_deep"

        if author_found and code_found:
            break

    return page_data


def _extract_author_object(data: dict) -> dict | None:
    """从 API 响应中定位 author 对象（视频/笔记两种结构）"""
    # /aweme/v1/web/aweme/detail/ 结构
    aweme = data.get("aweme_detail")
    if isinstance(aweme, dict):
        author = aweme.get("author")
        if isinstance(author, dict):
            return author

    # /aweme/v1/web/note/ 结构
    note = data.get("note_detail")
    if isinstance(note, dict):
        author = note.get("author")
        if isinstance(author, dict):
            return author

    # 兼容直接在顶层
    author = data.get("author")
    if isinstance(author, dict):
        return author

    # 兜底：遍历顶层键值，找第一个包含 author 子对象的
    for key in ("aweme", "note", "item", "detail", "data"):
        inner = data.get(key)
        if isinstance(inner, dict):
            author = inner.get("author")
            if isinstance(author, dict):
                return author

    return None


def _deep_find_author(obj: Any, max_depth: int = 12, _visited: set | None = None) -> dict | None:
    """递归查找作者信息，返回 {nickname, unique_id, short_id, sec_uid}"""
    if max_depth <= 0 or obj is None or not isinstance(obj, (dict, list)):
        return None
    if _visited is None:
        _visited = set()
    obj_id = id(obj)
    if obj_id in _visited:
        return None
    _visited.add(obj_id)

    found = {"nickname": "", "unique_id": "", "short_id": "", "sec_uid": ""}

    if isinstance(obj, list):
        for item in obj:
            child = _deep_find_author(item, max_depth - 1, _visited)
            if child:
                _merge_found(found, child)
                if _is_sufficient(found):
                    return found
    else:
        # 检查当前层级
        if "nickname" in obj and isinstance(obj["nickname"], str) and obj["nickname"]:
            found["nickname"] = obj["nickname"]
        if "unique_id" in obj and isinstance(obj["unique_id"], str) and obj["unique_id"]:
            found["unique_id"] = obj["unique_id"]
        if "short_id" in obj:
            sid = obj["short_id"]
            if isinstance(sid, (int, float)) and sid > 0:
                found["short_id"] = str(int(sid))
            elif isinstance(sid, str) and sid and sid != "0":
                found["short_id"] = sid
        if "sec_uid" in obj and isinstance(obj["sec_uid"], str) and obj["sec_uid"]:
            found["sec_uid"] = obj["sec_uid"]

        if _is_sufficient(found):
            return found

        for v in obj.values():
            if isinstance(v, (dict, list)):
                child = _deep_find_author(v, max_depth - 1, _visited)
                if child:
                    _merge_found(found, child)
                    if _is_sufficient(found):
                        return found

    return found if any(found.values()) else None


def _merge_found(target: dict, source: dict) -> None:
    for key in ("nickname", "unique_id", "short_id", "sec_uid"):
        if not target[key] and source.get(key):
            target[key] = source[key]


def _is_sufficient(found: dict) -> bool:
    return bool(found["nickname"] and (found["unique_id"] or found["short_id"]))


# ============================================================
# 主入口: 组合浏览器端 + Python 端提取
# ============================================================

async def extract_metadata(page, detail_responses: list) -> dict:
    """
    从页面中提取元数据，组合浏览器端 JS 和 Python 端 API 解析。

    返回:
        {
            title, author, authorCode, secUid, description,
            videoUrls, imageUrls, coverUrl, pageUrl, extractSource,
            ssrAvailable, apiResponseCount,
            apiVideoUrls, apiImageUrls  # 新增: API 高清链接
        }
    """
    page_data = await page.evaluate(EXTRACT_SCRIPT)
    page_data["apiResponseCount"] = len(detail_responses)

    if page_data.get("extractSource"):
        log(f"  浏览器提取来源: {page_data['extractSource']}, SSR可用: {page_data.get('ssrAvailable', [])}", "debug")
    else:
        log(f"  浏览器提取未找到作者, SSR可用: {page_data.get('ssrAvailable', [])}, API响应: {len(detail_responses)}", "debug")

    if page_data.get("_apiError"):
        log(f"  浏览器端用户API调用失败: {page_data['_apiError']}", "debug")

    page_data = await extract_from_api_responses(detail_responses, page_data)

    # 策略6 (Python端兜底): 有 sec_uid 时，如果 authorCode 为空或来自不可靠来源，
    # 用 Playwright 调用用户信息 API 获取准确值
    extract_source = page_data.get("extractSource", "")
    code_from_dom = extract_source in _UNRELIABLE_SOURCES
    author_from_dom = code_from_dom  # 同样，author 名字也可能不准
    needs_api_code = (not page_data.get("authorCode")) or code_from_dom
    needs_api_author = (not page_data.get("author")) or author_from_dom
    if (needs_api_code or needs_api_author) and page_data.get("secUid") and page_data["secUid"] != "self":
        log(f"  [策略6] 尝试通过sec_uid={page_data['secUid'][:30]}... 调用用户API "
            f"(extractSource={extract_source}, authorCode={page_data.get('authorCode','') or '(空)'}, "
            f"author={page_data.get('author','') or '(空)'})", "debug")
        info = await _fetch_user_info_by_secuid(page, page_data["secUid"])
        if info:
            if info.get("code") and (needs_api_code or not page_data.get("authorCode")):
                page_data["authorCode"] = info["code"]
                if not page_data.get("extractSource") or code_from_dom:
                    page_data["extractSource"] = "api:user_profile_py"
            if info.get("nickname") and (needs_api_author or not page_data.get("author")):
                page_data["author"] = info["nickname"]
                if not page_data.get("extractSource") or author_from_dom:
                    page_data["extractSource"] = "api:user_profile_py"

    current_page_url = page_data.get("pageUrl") or ""

    api_videos, api_images = await extract_media_from_api_responses(detail_responses, current_page_url)
    page_data["apiVideoUrls"] = api_videos
    page_data["apiImageUrls"] = api_images

    # 笔记页兜底: API 无图片时，从页面 SSR 数据提取（优先于 DOM+网络请求）
    if not api_images:
        ssr_images = await extract_images_from_ssr(page, current_page_url)
        if ssr_images:
            page_data["apiImageUrls"] = ssr_images
            log(f"  SSR兜底提取到 {len(ssr_images)} 个图片链接", "debug")

    if page_data.get("authorCode") and not page_data.get("extractSource", "").startswith("api"):
        log(f"  API响应补充提取成功: {page_data.get('authorCode')}", "debug")

    return page_data


async def _fetch_user_info_by_secuid(page, sec_uid: str) -> dict | None:
    """Python 端兜底：通过 sec_uid 调用用户信息 API 获取抖音号和昵称。
    返回: {"code": "...", "nickname": "..."} 或 None"""
    try:
        result = await page.evaluate("""
            async (secUid) => {
                try {
                    const controller = new AbortController();
                    const timeout = setTimeout(() => controller.abort(), 8000);
                    const resp = await fetch(
                        `/aweme/v1/web/user/profile/other/?sec_user_id=${encodeURIComponent(secUid)}`,
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
                    if (!resp.ok) return JSON.stringify({error: 'HTTP ' + resp.status});
                    const data = await resp.json();
                    const user = data && data.user;
                    if (!user) return JSON.stringify({error: 'no_user', hasData: !!data});
                    const code = user.unique_id || '';
                    const sid = user.short_id ? String(user.short_id) : '';
                    return JSON.stringify({
                        unique_id: code,
                        short_id: (sid && sid !== '0') ? sid : '',
                        nickname: (user.nickname || '').substring(0, 30),
                        sec_uid: (user.sec_uid || '').substring(0, 30),
                    });
                } catch(e) {
                    return JSON.stringify({error: (e.message || '').substring(0, 100)});
                }
            }
        """, sec_uid)
        if not result:
            return None
        try:
            info = json.loads(result)
            if isinstance(info, dict):
                if info.get("error"):
                    log(f"  [策略6详情] 错误: {info['error']}", "debug")
                    return None
                code = info.get("unique_id") or info.get("short_id") or ""
                nickname = info.get("nickname") or ""
                log(f"  [策略6详情] unique_id={info.get('unique_id','')[:30]}, "
                    f"short_id={info.get('short_id','')[:30]}, "
                    f"nickname={nickname[:30]}", "debug")
                return {"code": code, "nickname": nickname}
        except json.JSONDecodeError:
            pass
        return None
    except Exception as e:
        log(f"  [策略6详情] 异常: {e}", "debug")
        return None