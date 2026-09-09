# -*- coding: utf-8 -*-
"""SQLite 数据库工具，用于 URL 去重"""

import os
import sqlite3
import time
import random
from datetime import datetime, timedelta

from config import DB_FILE


class FileLock:
    """跨进程文件锁，用于协调多个进程对同一 SQLite 数据库的写入操作"""

    def __init__(self, db_file: str, timeout: float = 60):
        self.lock_file = db_file + ".lock"
        self.timeout = timeout
        self._acquired = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """判断指定 PID 的进程是否仍在运行（跨平台，无需额外依赖）。

        Windows 上 os.kill(pid, 0) 的行为：
          - 进程存在且有权限 → 正常返回
          - 进程存在但无权限访问（如 System 进程）→ PermissionError → 视为存活
          - 进程不存在 → OSError(winerror=11) → 视为已退出
        """
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as e:
            winerr = getattr(e, "winerror", None)
            if winerr is not None:
                return winerr != 11
            errno = getattr(e, "errno", None)
            if errno is not None and errno in (3, 8):
                return False
            return True
        return True

    def _read_lock_info(self) -> tuple[int | None, float | None]:
        """读取锁文件内容，返回 (pid, timestamp)；读不到返回 (None, None)"""
        try:
            with open(self.lock_file, "rb") as f:
                raw = f.read().decode(errors="ignore").strip()
            if not raw:
                return None, None
            lines = raw.splitlines()
            pid = int(lines[0]) if lines and lines[0].isdigit() else None
            ts = float(lines[1]) if len(lines) > 1 and lines[1] else None
            return pid, ts
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            return None, None

    def _try_unlink_with_retry(self, max_wait: float = 5.0) -> bool:
        """尝试删除锁文件，带重试（应对网盘同步进程短暂占用句柄的情况）。返回是否成功删除。"""
        deadline = time.time() + max_wait
        attempt = 0
        while True:
            try:
                os.unlink(self.lock_file)
                return True
            except FileNotFoundError:
                return True
            except PermissionError:
                if time.time() >= deadline:
                    return False
                time.sleep(0.2 + min(attempt * 0.1, 0.5))
                attempt += 1
            except OSError:
                return False

    def acquire(self) -> bool:
        """尝试获取锁，支持超时"""
        start = time.time()
        while True:
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()}\n{time.time()}".encode())
                os.close(fd)
                self._acquired = True
                return True
            except FileExistsError:
                self._cleanup_stale()
                if time.time() - start > self.timeout:
                    return False
                time.sleep(0.5 + random.random() * 0.5)

    def release(self):
        if self._acquired:
            self._try_unlink_with_retry(max_wait=8.0)
            self._acquired = False

    def _cleanup_stale(self):
        """清理僵死锁文件：
        1. 锁中记录的 PID 已不存在 → 立即清理
        2. 锁文件修改时间超过 120 秒 → 视为僵死清理
        """
        try:
            mtime = os.path.getmtime(self.lock_file)
        except FileNotFoundError:
            return
        except OSError:
            return

        pid, _ = self._read_lock_info()
        stale_by_pid = pid is not None and not self._pid_alive(pid)
        stale_by_time = time.time() - mtime > 120

        if stale_by_pid or stale_by_time:
            self._try_unlink_with_retry(max_wait=3.0)

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"无法获取数据库锁 {self.lock_file}，可能有其他进程正在写入")
        return self

    def __exit__(self, *args):
        self.release()
        return False


class DBUtils:
    """SQLite 数据库工具，与 dy_detail_python 共享 details_page 表"""

    def __init__(self, db_file: str = DB_FILE):
        self.db_file = db_file
        self._init_table()
        self._init_skipped_table()
        self._init_config_table()
        self._init_cookies_table()
        self._init_final_url_table()

    def lock_db(self, timeout: float = 60) -> FileLock:
        """获取数据库写锁，用于阻止其他进程同时写入。返回上下文管理器。"""
        return FileLock(self.db_file, timeout=timeout)

    def _init_table(self) -> None:
        conn, cursor = self._connect()
        try:
            cursor.execute("PRAGMA table_info(details_page)")
            columns = [col[1] for col in cursor.fetchall()]
            if not columns:
                cursor.execute("""
                    CREATE TABLE details_page (
                        url TEXT PRIMARY KEY NOT NULL,
                        album_name TEXT DEFAULT "",
                        album_code TEXT DEFAULT "",
                        remark TEXT DEFAULT "",
                        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            else:
                for col, col_type in [
                    ("create_time", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("update_time", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("album_code", "TEXT DEFAULT ''"),
                ]:
                    if col not in columns:
                        cursor.execute(
                            f"ALTER TABLE details_page ADD COLUMN {col} {col_type}"
                        )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def _init_config_table(self) -> None:
        conn, cursor = self._connect()
        try:
            cursor.execute("PRAGMA table_info(config)")
            columns = [col[1] for col in cursor.fetchall()]
            if not columns:
                cursor.execute("""
                    CREATE TABLE config (
                        key TEXT PRIMARY KEY NOT NULL,
                        value TEXT DEFAULT ""
                    )
                """)
                conn.commit()
        finally:
            cursor.close()
            conn.close()

    def _init_cookies_table(self) -> None:
        conn, cursor = self._connect()
        try:
            cursor.execute("PRAGMA table_info(cookies)")
            columns = [col[1] for col in cursor.fetchall()]
            if not columns:
                cursor.execute("""
                    CREATE TABLE cookies (
                        domain TEXT NOT NULL,
                        name TEXT NOT NULL,
                        path TEXT NOT NULL,
                        value TEXT DEFAULT "",
                        secure INTEGER DEFAULT 0,
                        http_only INTEGER DEFAULT 0,
                        same_site TEXT DEFAULT "Lax",
                        expires REAL DEFAULT -1,
                        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (domain, name, path)
                    )
                """)
            else:
                for col, col_type in [
                    ("path", "TEXT NOT NULL DEFAULT '/'"),
                    ("secure", "INTEGER DEFAULT 0"),
                    ("http_only", "INTEGER DEFAULT 0"),
                    ("same_site", "TEXT DEFAULT 'Lax'"),
                    ("expires", "REAL DEFAULT -1"),
                    ("update_time", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                ]:
                    if col not in columns:
                        try:
                            cursor.execute(
                                f"ALTER TABLE cookies ADD COLUMN {col} {col_type}"
                            )
                        except sqlite3.OperationalError:
                            pass
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def _init_final_url_table(self) -> None:
        """创建长链接映射表，用于记录短链接→最终跳转地址的映射，防止同一作品被不同短链接重复处理"""
        conn, cursor = self._connect()
        try:
            cursor.execute("PRAGMA table_info(details_page_final_url)")
            columns = [col[1] for col in cursor.fetchall()]
            if not columns:
                cursor.execute("""
                    CREATE TABLE details_page_final_url (
                        final_url TEXT PRIMARY KEY NOT NULL,
                        short_url TEXT DEFAULT "",
                        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            else:
                for col, col_type in [
                    ("create_time", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                ]:
                    if col not in columns:
                        cursor.execute(
                            f"ALTER TABLE details_page_final_url ADD COLUMN {col} {col_type}"
                        )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def is_final_url_exist(self, final_url: str) -> bool:
        """检查最终跳转地址是否已被处理过"""
        conn, cursor = self._connect()
        try:
            cursor.execute(
                "SELECT 1 FROM details_page_final_url WHERE final_url=?",
                (final_url.strip(),)
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()
            conn.close()

    def get_by_final_url(self, final_url: str) -> dict | None:
        """查询长链接的处理记录，返回 {short_url, create_time} 或 None"""
        conn, cursor = self._connect()
        try:
            cursor.execute(
                "SELECT short_url, create_time FROM details_page_final_url WHERE final_url=?",
                (final_url.strip(),)
            )
            row = cursor.fetchone()
            if row:
                return {"short_url": row[0], "create_time": row[1]}
            return None
        finally:
            cursor.close()
            conn.close()

    def insert_final_url(self, final_url: str, short_url: str = "") -> None:
        """记录最终跳转地址与短链接的映射关系"""
        if self.is_final_url_exist(final_url):
            return
        beijing_time = (datetime.utcnow() + timedelta(hours=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        def _do_write():
            with self.lock_db():
                conn, cursor = self._connect()
                try:
                    cursor.execute(
                        "INSERT INTO details_page_final_url (final_url, short_url, create_time) "
                        "VALUES (?, ?, ?)",
                        (final_url.strip(), short_url.strip(), beijing_time),
                    )
                    conn.commit()
                finally:
                    cursor.close()
                    conn.close()
        self._retry_write(_do_write)

    def _init_skipped_table(self) -> None:
        """创建跳过记录表，与 details_page 结构一致，用于记录无成功下载的 URL"""
        conn, cursor = self._connect()
        try:
            cursor.execute("PRAGMA table_info(details_page_skipped)")
            columns = [col[1] for col in cursor.fetchall()]
            if not columns:
                cursor.execute("""
                    CREATE TABLE details_page_skipped (
                        url TEXT PRIMARY KEY NOT NULL,
                        album_name TEXT DEFAULT "",
                        album_code TEXT DEFAULT "",
                        remark TEXT DEFAULT "",
                        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            else:
                for col, col_type in [
                    ("create_time", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("update_time", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
                    ("album_code", "TEXT DEFAULT ''"),
                ]:
                    if col not in columns:
                        cursor.execute(
                            f"ALTER TABLE details_page_skipped ADD COLUMN {col} {col_type}"
                        )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def get_all_config(self) -> dict[str, str]:
        conn, cursor = self._connect()
        try:
            cursor.execute("SELECT key, value FROM config")
            rows = cursor.fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            cursor.close()
            conn.close()

    def get_config(self, key: str) -> str | None:
        conn, cursor = self._connect()
        try:
            cursor.execute("SELECT value FROM config WHERE key=?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()
            conn.close()

    def set_config(self, key: str, value: str) -> None:
        def _do_write():
            with self.lock_db():
                conn, cursor = self._connect()
                try:
                    cursor.execute(
                        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                        (key, value),
                    )
                    conn.commit()
                finally:
                    cursor.close()
                    conn.close()
        self._retry_write(_do_write)

    def init_config(self, defaults: dict[str, str]) -> None:
        def _do_write():
            with self.lock_db():
                conn, cursor = self._connect()
                try:
                    cursor.execute("SELECT COUNT(*) FROM config")
                    if cursor.fetchone()[0] == 0:
                        cursor.executemany(
                            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                            defaults.items(),
                        )
                        conn.commit()
                finally:
                    cursor.close()
                    conn.close()
        self._retry_write(_do_write)

    def _connect(self):
        conn = sqlite3.connect(self.db_file, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        cursor = conn.cursor()
        return conn, cursor

    def _retry_write(self, operation, max_retries=8, base_delay=0.5):
        """带指数退避的写操作重试，应对数据库被其他进程锁定的情况"""
        last_error = None
        for attempt in range(max_retries):
            try:
                return operation()
            except sqlite3.OperationalError as e:
                last_error = e
                if "readonly" in str(e).lower():
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
                    continue
                raise
        raise last_error # type: ignore

    def is_exist(self, url: str) -> bool:
        conn, cursor = self._connect()
        try:
            cursor.execute("SELECT 1 FROM details_page WHERE url=?", (url.strip(),))
            return cursor.fetchone() is not None
        finally:
            cursor.close()
            conn.close()

    def get_info(self, url: str) -> dict | None:
        """查询 URL 的处理记录，返回 {album_name, album_code, create_time} 或 None"""
        conn, cursor = self._connect()
        try:
            cursor.execute(
                "SELECT album_name, album_code, create_time FROM details_page WHERE url=?",
                (url.strip(),)
            )
            row = cursor.fetchone()
            if row:
                return {"album_name": row[0], "album_code": row[1], "create_time": row[2]}
            return None
        finally:
            cursor.close()
            conn.close()

    def insert(self, url: str, album_name: str = "", album_code: str = "", remark: str = "") -> None:
        if self.is_exist(url):
            return
        beijing_time = (datetime.utcnow() + timedelta(hours=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        def _do_write():
            with self.lock_db():
                conn, cursor = self._connect()
                try:
                    cursor.execute(
                        "INSERT INTO details_page (url, album_name, album_code, remark, create_time, update_time) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (url.strip(), album_name.strip(), album_code.strip(), remark.strip(), beijing_time, beijing_time),
                    )
                    conn.commit()
                finally:
                    cursor.close()
                    conn.close()
        self._retry_write(_do_write)

    def is_skipped_exist(self, url: str) -> bool:
        conn, cursor = self._connect()
        try:
            cursor.execute("SELECT 1 FROM details_page_skipped WHERE url=?", (url.strip(),))
            return cursor.fetchone() is not None
        finally:
            cursor.close()
            conn.close()

    def get_skipped_info(self, url: str) -> dict | None:
        conn, cursor = self._connect()
        try:
            cursor.execute(
                "SELECT album_name, album_code, create_time FROM details_page_skipped WHERE url=?",
                (url.strip(),)
            )
            row = cursor.fetchone()
            if row:
                return {"album_name": row[0], "album_code": row[1], "create_time": row[2]}
            return None
        finally:
            cursor.close()
            conn.close()

    def insert_skipped(self, url: str, album_name: str = "", album_code: str = "", remark: str = "") -> None:
        if self.is_skipped_exist(url) or self.is_exist(url):
            return
        beijing_time = (datetime.utcnow() + timedelta(hours=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        def _do_write():
            with self.lock_db():
                conn, cursor = self._connect()
                try:
                    cursor.execute(
                        "INSERT INTO details_page_skipped (url, album_name, album_code, remark, create_time, update_time) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (url.strip(), album_name.strip(), album_code.strip(), remark.strip(), beijing_time, beijing_time),
                    )
                    conn.commit()
                finally:
                    cursor.close()
                    conn.close()
        self._retry_write(_do_write)

    def get_empty_album_code_urls(self, days: int = 3) -> list[dict]:
        """查询最近 N 天内 album_code 为空的记录，返回 [{url, album_name, create_time}]"""
        conn, cursor = self._connect()
        try:
            cursor.execute(
                "SELECT url, album_name, remark, create_time FROM details_page "
                "WHERE (album_code IS NULL OR album_code = '') "
                "AND create_time >= datetime('now', 'localtime', ?) "
                "ORDER BY create_time DESC",
                (f"-{days} days",)
            )
            rows = cursor.fetchall()
            return [
                {"url": r[0], "album_name": r[1], "remark": r[2], "create_time": r[3]}
                for r in rows
            ]
        finally:
            cursor.close()
            conn.close()

    def get_by_album_code(self, album_code: str) -> list[dict]:
        """查询指定 album_code 的记录，返回 [{url, album_name, create_time}]"""
        conn, cursor = self._connect()
        try:
            cursor.execute(
                "SELECT url, album_name, remark, create_time FROM details_page "
                "WHERE album_code = ? "
                "ORDER BY create_time DESC",
                (album_code,)
            )
            rows = cursor.fetchall()
            return [
                {"url": r[0], "album_name": r[1], "remark": r[2], "create_time": r[3]}
                for r in rows
            ]
        finally:
            cursor.close()
            conn.close()

    def get_all_recent_urls(self, days: int = 3) -> list[dict]:
        """查询最近 N 天内所有记录（用于调试补刷），返回 [{url, album_name, create_time}]"""
        conn, cursor = self._connect()
        try:
            cursor.execute(
                "SELECT url, album_name, remark, album_code, create_time FROM details_page "
                "WHERE create_time >= datetime('now', 'localtime', ?) "
                "ORDER BY create_time DESC",
                (f"-{days} days",)
            )
            rows = cursor.fetchall()
            return [
                {"url": r[0], "album_name": r[1], "remark": r[2], "album_code": r[3], "create_time": r[4]}
                for r in rows
            ]
        finally:
            cursor.close()
            conn.close()

    def update_album_code(self, url: str, album_code: str) -> bool:
        """更新指定 URL 的 album_code 和 update_time，返回是否成功"""
        beijing_time = (datetime.utcnow() + timedelta(hours=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        def _do_write():
            with self.lock_db():
                conn, cursor = self._connect()
                try:
                    cursor.execute(
                        "UPDATE details_page SET album_code=?, update_time=? WHERE url=?",
                        (album_code.strip(), beijing_time, url.strip()),
                    )
                    conn.commit()
                    return cursor.rowcount > 0
                finally:
                    cursor.close()
                    conn.close()
        return self._retry_write(_do_write)

    def get_cookies(self, domain: str = ".douyin.com") -> list[dict]:
        conn, cursor = self._connect()
        try:
            cursor.execute(
                "SELECT name, value, domain, path, secure, http_only, same_site, expires "
                "FROM cookies WHERE domain LIKE ?",
                (f"%{domain}%",),
            )
            rows = cursor.fetchall()
            result = []
            for row in rows:
                cookie = {
                    "name": row[0],
                    "value": row[1],
                    "domain": row[2],
                    "path": row[3],
                    "secure": bool(row[4]),
                    "httpOnly": bool(row[5]),
                    "sameSite": row[6],
                }
                if row[7] and row[7] > 0:
                    cookie["expires"] = row[7]
                result.append(cookie)
            return result
        finally:
            cursor.close()
            conn.close()

    def save_cookies(self, cookies: list[dict]) -> None:
        beijing_time = (datetime.utcnow() + timedelta(hours=8)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        def _do_write():
            with self.lock_db():
                conn, cursor = self._connect()
                try:
                    rows = []
                    for c in cookies:
                        rows.append((
                            c.get("domain", ""),
                            c.get("name", ""),
                            c.get("path", "/"),
                            c.get("value", ""),
                            1 if c.get("secure") or c.get("Secure") else 0,
                            1 if c.get("httpOnly") or c.get("http_only") else 0,
                            c.get("sameSite") or c.get("same_site") or "Lax",
                            c.get("expires") or c.get("Expires") or -1,
                            beijing_time,
                        ))
                    cursor.executemany(
                        "INSERT OR REPLACE INTO cookies "
                        "(domain, name, path, value, secure, http_only, same_site, expires, update_time) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                    conn.commit()
                finally:
                    cursor.close()
                    conn.close()
        self._retry_write(_do_write)

    def clear_cookies(self, domain: str = ".douyin.com") -> int:
        def _do_write() -> int:
            with self.lock_db():
                conn, cursor = self._connect()
                try:
                    cursor.execute("DELETE FROM cookies WHERE domain LIKE ?", (f"%{domain}%",))
                    count = cursor.rowcount
                    conn.commit()
                    return count
                finally:
                    cursor.close()
                    conn.close()
        return self._retry_write(_do_write)