# -*- coding: utf-8 -*-
"""
details_page 一次性去重迁移脚本
用法: python main/migrate_details_page.py [db_path]
"""
import sqlite3
import sys
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_DIR, 'src'))

import config

DB = sys.argv[1] if len(sys.argv) > 1 else config.DB_FILE
print(f"目标数据库: {DB}")

# 1. 检查是否需要迁移
conn = sqlite3.connect(DB)
conn.execute("PRAGMA busy_timeout=10000")
cur = conn.cursor()

cur.execute("PRAGMA table_info(details_page)")
cols = {row[1]: row[5] for row in cur.fetchall()}
if not cols:
    print("details_page 表不存在，无需迁移")
    conn.close()
    sys.exit(0)

url_pk = cols.get("url", 0)
if url_pk and url_pk > 0:
    print("url 已有 PRIMARY KEY，无需迁移")
    conn.close()
    sys.exit(0)

cur.execute("PRAGMA index_list(details_page)")
for idx in cur.fetchall():
    if idx[2]:  # unique
        cur.execute(f"PRAGMA index_info({idx[1]})")
        idx_cols = [c[2] for c in cur.fetchall()]
        if idx_cols == ["url"]:
            print("url 已有唯一索引，无需迁移")
            conn.close()
            sys.exit(0)

# 2. 统计现状
old_rows = cur.execute("SELECT COUNT(*) FROM details_page").fetchone()[0]
old_unique = cur.execute(
    "SELECT COUNT(DISTINCT url) FROM details_page"
).fetchone()[0]
pc = conn.execute("PRAGMA page_count").fetchone()[0]
old_mb = pc * cur.execute("PRAGMA page_size").fetchone()[0] / 1024 / 1024

print(f"迁移前: {old_rows:,} 行 ({old_unique:,} 唯一), {old_mb:.1f} MB")
print(f"其中 {old_rows - old_unique:,} 行重复 ({(old_rows - old_unique) / old_rows * 100:.1f}%)")

# 3. 确认
answer = input("\n是否执行去重重建? [y/N] ").strip().lower()
if answer != "y":
    print("已取消")
    conn.close()
    sys.exit(0)

# 4. 执行迁移
print("正在重建…")
conn.execute("PRAGMA journal_mode=DELETE")
conn.executescript("""
    CREATE TABLE details_page_new (
        url TEXT NOT NULL,
        album_name TEXT DEFAULT '',
        album_code TEXT DEFAULT '',
        remark TEXT DEFAULT '',
        create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        sync_status TEXT DEFAULT '',
        sync_time TEXT DEFAULT ''
    );
    CREATE UNIQUE INDEX idx_details_page_url ON details_page_new(url);
    INSERT INTO details_page_new
    SELECT url,
           MAX(album_name), MAX(album_code), MAX(remark),
           MIN(create_time), MIN(update_time),
           MAX(sync_status), MIN(sync_time)
    FROM details_page GROUP BY url;
    DROP TABLE details_page;
    ALTER TABLE details_page_new RENAME TO details_page;
""")
conn.commit()

# 5. 结果
new_rows = cur.execute("SELECT COUNT(*) FROM details_page").fetchone()[0]
pc = conn.execute("PRAGMA page_count").fetchone()[0]
new_mb = pc * conn.execute("PRAGMA page_size").fetchone()[0] / 1024 / 1024
conn.close()

print(f"\n迁移完成: {new_rows:,} 行, {new_mb:.1f} MB")
print(f"节省 {old_mb - new_mb:.1f} MB ({(old_mb - new_mb) / old_mb * 100:.0f}%)")
print(f"\n可选手动执行 VACUUM 回收碎片空间: sqlite3 {DB} 'VACUUM'")