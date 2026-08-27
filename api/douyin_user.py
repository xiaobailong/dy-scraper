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
    print("=" * 60)
    print("  抖音用户信息 API 模块 - 调用样例")
    print("=" * 60)

    print(f"\n[1] API 路径常量:")
    print(f"  DOUYIN_USER_PROFILE_API_PATH = {DOUYIN_USER_PROFILE_API_PATH!r}")

    print(f"\n[2] USER_PROFILE_API_SCRIPT 长度: {len(USER_PROFILE_API_SCRIPT)} 字符")
    print(f"  脚本预览（前 200 字）:")
    print(f"  {USER_PROFILE_API_SCRIPT.strip()[:200]}...")

    print("\n[3] 在 core/metadata.py 中的集成方式:")
    print("""
    # metadata.py 中通过字符串拼接将脚本注入 EXTRACT_SCRIPT:
    from api.douyin_user import USER_PROFILE_API_SCRIPT

    EXTRACT_SCRIPT = r'''
        // ... 策略1-4 的 JS 代码 ...
    ''' + USER_PROFILE_API_SCRIPT + r'''
        // ... 后续通用提取逻辑 ...
    '''
    """)