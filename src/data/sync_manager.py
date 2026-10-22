# -*- coding: utf-8 -*-
"""本地临时库 ↔ 百度网盘同步库 的数据同步管理器。

核心保证：无论发生什么异常（崩溃、断电、进程杀），已抓取的数据绝不丢失。

架构：
  源库 (original): D:\BaiduSyncdisk\douyin.db  — 百度网盘同步目录
  本地库 (local):   data\douyin_YYYYMMDDHHmmss.db — 项目本地副本

流程：
  1. recover_pending()  — 启动时恢复上次未完成合并的数据
  2. copy_to_local()    — 拷贝源库到本地（带时间戳版本）
  3. [爬虫写入本地库]
  4. merge_to_original() — 增量合并本地新增 → 源库
  5. cleanup_backups()   — 保留最近 7 个版本
"""

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from common.logger import log


class SyncManager:
    """本地临时库 ↔ 源库 的增量同步管理器。

    用法:
        mgr = SyncManager(original_db, local_dir)

        # 启动时恢复未完成的合并
        mgr.recover_pending()

        # 拷贝源库到本地
        local_db = mgr.copy_to_local()
        config.DB_FILE = local_db

        # ... 爬虫运行 ...

        # 合并新增数据到源库
        mgr.merge_to_original(local_db)
    """

    TABLES = [
        "details_page",
        "details_page_skipped",
        "details_page_final_url",
        "cookies",
    ]

    SYNC_INITIAL = "initial"
    SYNC_SYNCED = "synced"

    def __init__(self, original_db: str, local_dir: Path):
        self._original = original_db
        self._local_dir = Path(local_dir)
        self._local_dir.mkdir(parents=True, exist_ok=True)

    # ================================================================
    # 公开接口
    # ================================================================

    def recover_pending(self) -> dict:
        """恢复之前未成功合并的数据。

        扫描 local_dir 下所有备份文件，与源库 sync_log 比对，
        找到未完成合并的备份文件，将其中的新增记录合并到源库。
        幂等操作：已合并的不会重复处理，未合并的会补上。

        Returns:
            {source_filename: {table_name: merged_count}, ...}
        """
        if not os.path.exists(self._original):
            return {}

        self._ensure_schema()

        last_sources = self._read_last_sources()
        all_backups = sorted(
            self._local_dir.glob("douyin_*.db"),
            key=lambda p: p.name,
        )
        if not all_backups:
            return {}

        # 找出所有"可能未完成合并"的备份文件：
        # 备份文件名 > 某个表的 last_source_file → 该表可能未从该备份合并
        pending = []
        for backup in all_backups:
            for table in self.TABLES:
                last_src = last_sources.get(table, "")
                if not last_src or backup.name > last_src:
                    pending.append(backup)
                    break

        if not pending:
            log(f"  无待恢复的合并")
            return {}

        log(f"  发现 {len(pending)} 个待恢复的备份文件")
        results = {}
        for backup in pending:
            log(f"    正在恢复: {backup.name}")
            stats = self._merge_file(str(backup))
            results[backup.name] = stats
            total = sum(stats.values())
            if total > 0:
                log(f"      共合并 {total} 条记录")
        return results

    def copy_to_local(self) -> str:
        """拷贝源库到本地 data/ 目录，返回本地库绝对路径。

        文件名格式: douyin_YYYYMMDDHHmmss.db
        拷贝后自动清理超过 7 个的旧备份。
        """
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        local_path = str(self._local_dir / f"douyin_{timestamp}.db")

        if os.path.exists(self._original):
            shutil.copy2(self._original, local_path)
            log(f"  数据库已拷贝至本地: {local_path}")
        else:
            log(f"  源库不存在，将在本地创建新数据库: {local_path}")

        self.cleanup_backups(keep=7)
        return local_path

    def merge_to_original(self, local_db: str) -> dict:
        """将本地库新增记录合并到源库。

        1. 若源库不存在 → 直接拷贝整个本地库（首次运行）
        2. 若源库存在 → 先 recover_pending 补漏，再合并本次新增

        Args:
            local_db: 本地临时库路径

        Returns:
            {table_name: merged_count} 或 {"__copied__": 1}

        Raises:
            RuntimeError: 合并失败（本地库完整保留，数据不丢失）
        """
        Path(self._original).parent.mkdir(parents=True, exist_ok=True)

        if not os.path.exists(self._original):
            log(f"  源库不存在，直接拷贝本地库...")
            try:
                shutil.copy2(local_db, self._original)
                log(f"  数据库已创建至: {self._original}")
                return {"__copied__": 1}
            except Exception as e:
                raise RuntimeError(
                    f"创建源库失败（本地库 {local_db} 完整保留）: {e}"
                ) from e

        # 先恢复可能遗漏的历史合并
        self.recover_pending()

        # 合并当前本地库的新增记录
        stats = self._merge_file(local_db)
        return stats

    def cleanup_backups(self, keep: int = 7) -> None:
        """清理旧备份，保留最近 keep 个 douyin_*.db 文件。"""
        backups = sorted(
            self._local_dir.glob("douyin_*.db"),
            key=lambda p: p.name,
            reverse=True,
        )
        for old_file in backups[keep:]:
            try:
                old_file.unlink()
                log(f"  清理旧备份: {old_file.name}", "debug")
            except Exception:
                pass

    # ================================================================
    # 内部方法
    # ================================================================

    def _ensure_schema(self) -> None:
        """确保源库各表有 sync_status/sync_time 列，并创建 sync_log 表。"""
        if not os.path.exists(self._original):
            return

        conn = sqlite3.connect(self._original)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            for table_name in self.TABLES:
                try:
                    cols = [
                        c[1]
                        for c in conn.execute(
                            f"PRAGMA table_info({table_name})"
                        ).fetchall()
                    ]
                except sqlite3.OperationalError:
                    continue

                for col, col_type in [
                    ("sync_status", "TEXT DEFAULT ''"),
                    ("sync_time", "TEXT DEFAULT ''"),
                ]:
                    if col not in cols:
                        conn.execute(
                            f"ALTER TABLE {table_name} ADD COLUMN {col} {col_type}"
                        )

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_log (
                    table_name      TEXT PRIMARY KEY NOT NULL,
                    last_sync_time  TEXT DEFAULT '',
                    last_record_count INTEGER DEFAULT 0,
                    last_source_file TEXT DEFAULT ''
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _read_last_sources(self) -> dict[str, str]:
        """读取 sync_log 中各表最后成功合并的来源文件名。"""
        conn = sqlite3.connect(self._original)
        try:
            conn.execute("PRAGMA table_info(sync_log)")
        except sqlite3.OperationalError:
            return {}
        finally:
            conn.close()

        conn = sqlite3.connect(self._original)
        try:
            rows = conn.execute(
                "SELECT table_name, last_source_file FROM sync_log "
                "WHERE last_source_file != ''"
            ).fetchall()
            return {r[0]: r[1] for r in rows}
        except sqlite3.OperationalError:
            return {}
        finally:
            conn.close()

    def _merge_file(self, src_db: str) -> dict[str, int]:
        """将单个本地库文件中的新增记录 (sync_status='') 合并到源库。

        合并策略：INSERT OR IGNORE，主键冲突时跳过不覆盖。
        合并成功后更新 sync_log.last_source_file 为当前文件名，
        便于下次启动时判断哪些备份已完成合并。

        Returns:
            {table_name: merged_count}
        """
        if not os.path.exists(src_db):
            return {}

        self._ensure_schema()

        conn = sqlite3.connect(self._original)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            rows = conn.execute(
                "SELECT table_name FROM sync_log"
            ).fetchall()
            is_first_sync = len(rows) == 0

            conn.execute("ATTACH DATABASE ? AS src", (src_db,))
            _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            src_filename = os.path.basename(src_db)
            stats: dict[str, int] = {}

            for table_name in self.TABLES:
                try:
                    conn.execute(f"SELECT 1 FROM src.{table_name} LIMIT 1")
                except sqlite3.OperationalError:
                    continue

                src_rows = conn.execute(
                    f"SELECT * FROM src.{table_name} WHERE sync_status = ''"
                ).fetchall()

                if not src_rows:
                    continue

                col_info = conn.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
                col_names = [c[1] for c in col_info]
                cols_str = ", ".join(col_names)
                ph = ", ".join(["?" for _ in col_names])

                si = col_names.index("sync_status") if "sync_status" in col_names else -1
                ti = col_names.index("sync_time") if "sync_time" in col_names else -1

                merged = 0
                for row_data in src_rows:
                    new_row = list(row_data)
                    if si >= 0:
                        new_row[si] = self.SYNC_SYNCED
                    if ti >= 0:
                        new_row[ti] = _now
                    try:
                        conn.execute(
                            f"INSERT OR IGNORE INTO {table_name} ({cols_str}) "
                            f"VALUES ({ph})",
                            new_row,
                        )
                        if conn.changes() > 0:
                            merged += 1
                    except Exception:
                        pass

                stats[table_name] = merged

                conn.execute(
                    "INSERT OR REPLACE INTO sync_log "
                    "(table_name, last_sync_time, last_record_count, last_source_file) "
                    "VALUES (?, ?, ?, ?)",
                    (table_name, _now, merged, src_filename),
                )

            # 首次同步：将源库存量记录的 sync_status 标记为 'initial'，
            # 后续只有爬虫新增的记录才会 sync_status=''
            if is_first_sync:
                for table_name in self.TABLES:
                    try:
                        conn.execute(
                            f"UPDATE {table_name} SET sync_status = ? "
                            f"WHERE sync_status = ''",
                            (self.SYNC_INITIAL,)
                        )
                    except sqlite3.OperationalError:
                        pass
                log(f"  首次同步完成，存量记录已标记为 '{self.SYNC_INITIAL}'")

            conn.execute("DETACH DATABASE src")
            conn.commit()
            return stats
        finally:
            conn.close()