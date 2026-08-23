# -*- coding: utf-8 -*-
"""从本地文件获取爬取目标 URL 列表"""

import re
import subprocess
from datetime import datetime
from pathlib import Path

from common.logger import log
from common.utils import normalize_url


def fetch_urls_from_local_file(file_path: str) -> list[str]:
    """从本地文本文件读取 URL 列表（每行一个或混杂在文本中）"""
    path = Path(file_path)
    if not path.exists():
        log(f"  本地 URL 文件不存在: {file_path}")
        return []

    log(f"正在从本地文件获取 URL 列表: {file_path}")
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        log(f"  读取本地文件失败: {e}")
        return []

    if not content.strip():
        log("  本地文件内容为空")
        return []

    log(f"  文件内容长度: {len(content)} 字符", "debug")

    url_pattern = re.compile(r"https?://[^\s]+")
    urls = list(dict.fromkeys(url_pattern.findall(content)))

    log(f"  正则匹配到 {len(urls)} 个原始 URL", "debug")
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
            log(f"  跳过(有道域名): {clean_u}", "debug")
            continue
        if ".com" not in clean_u:
            skipped_no_com += 1
            log(f"  跳过(非.com): {clean_u}", "debug")
            continue
        url_list.append(normalize_url(clean_u))

    if skipped_no_com:
        log(f"  其中 {skipped_no_com} 个非.com域名被跳过, {skipped_youdao} 个有道域名被跳过", "debug")

    log(f"  提取到 {len(url_list)} 个有效 URL")
    return url_list


def git_pull() -> None:
    """执行 git pull 拉取最新代码"""
    project_dir = Path(__file__).parent.parent
    log("  执行 git pull...")
    try:
        result = subprocess.run(
            ["git", "pull"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout.strip() or result.stderr.strip())
        log(f"  git pull: {output}")
    except Exception as e:
        log(f"  git pull 失败: {e}")


def clear_tmp_and_git_commit_push() -> None:
    """清空 tmp.txt 并执行 git add、git commit、git push 提交整个项目"""
    project_dir = Path(__file__).parent.parent

    # 1. 清空 tmp.txt
    tmp_file = Path(__file__).parent / "tmp.txt"
    try:
        tmp_file.write_text("", encoding="utf-8")
        log("  tmp.txt 已清空")
    except Exception as e:
        log(f"  清空 tmp.txt 失败: {e}")

    # 2. git add
    log("  执行 git add...")
    try:
        subprocess.run(
            ["git", "add", "."],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        log(f"  git add 失败: {e}")

    # 3. git commit
    log("  执行 git commit...")
    commit_msg = f"auto: scrape completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    try:
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (result.stdout.strip() or result.stderr.strip())
        log(f"  git commit: {output}")
    except Exception as e:
        log(f"  git commit 失败: {e}")

    # 4. git push
    log("  执行 git push...")
    try:
        result = subprocess.run(
            ["git", "push"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout.strip() or result.stderr.strip())
        log(f"  git push: {output}")
    except Exception as e:
        log(f"  git push 失败: {e}")