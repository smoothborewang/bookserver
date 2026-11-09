#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""books.db 瘦身迁移。

背景：chapters 表 content_html 存纯 TEXT（约 400MB 字节），plain_text 全库只有写入、
没有任何读取，是纯冗余（约 350MB 字符）。目标是 content_html 改存 zlib BLOB、丢掉
plain_text，然后 VACUUM 收尾。

分两段执行，避免长时间独占数据库：
  python3 migrate_chapters.py copy     # 逐批复制到 chapters_new（服务可继续跑）
  python3 migrate_chapters.py swap     # 停服务后执行：换表 + VACUUM
"""

import os
import sqlite3
import sys
import time
import zlib

DB = "/home/pi/bookserver/data/books.db"
BATCH = 200

NEW_SQL = """
CREATE TABLE IF NOT EXISTS chapters_new (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  ordinal      INTEGER NOT NULL,
  title        TEXT,
  content_html BLOB NOT NULL,
  char_count   INTEGER DEFAULT 0,
  source       TEXT DEFAULT 'native',
  UNIQUE(book_id, ordinal)
)
"""


def conn():
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def copy():
    con = conn()
    cur = con.cursor()
    cur.execute(NEW_SQL)
    con.commit()
    already = cur.execute("SELECT count(*) FROM chapters_new").fetchone()[0]
    done = 0
    # 按 id 区间推进，已复制过的不重复
    last = cur.execute("SELECT coalesce(max(id),0) FROM chapters_new").fetchone()[0]
    while True:
        rows = cur.execute(
            "SELECT id, book_id, ordinal, title, content_html, char_count, source"
            " FROM chapters WHERE id>? ORDER BY id LIMIT ?", (last, BATCH)).fetchall()
        if not rows:
            break
        vals = []
        for (cid, bid, od, ti, html, cc, src) in rows:
            if isinstance(html, (bytes, bytearray)):
                blob = bytes(html)                     # 已经是压缩数据
            else:
                blob = zlib.compress((html or "").encode("utf-8"), 6)
            vals.append((cid, bid, od, ti, blob, cc or 0, src or "native"))
            last = cid
        cur.executemany(
            "INSERT OR REPLACE INTO chapters_new"
            "(id, book_id, ordinal, title, content_html, char_count, source)"
            " VALUES(?,?,?,?,?,?,?)", vals)
        con.commit()
        done += len(vals)
        print("copied %d rows (last_id=%d)" % (done, last), flush=True)
        time.sleep(0.05)
    total = cur.execute("SELECT count(*) FROM chapters").fetchone()[0]
    copied = cur.execute("SELECT count(*) FROM chapters_new").fetchone()[0]
    print("verify: chapters=%d chapters_new=%d" % (total, copied), flush=True)
    print("COPY_%s" % ("OK" if total == copied else "MISMATCH"), flush=True)
    con.close()


def swap():
    con = conn()
    cur = con.cursor()
    total = cur.execute("SELECT count(*) FROM chapters").fetchone()[0]
    copied = cur.execute("SELECT count(*) FROM chapters_new").fetchone()[0]
    if total != copied:
        print("ABORT: count mismatch %d vs %d" % (total, copied))
        sys.exit(1)
    cur.execute("DROP INDEX IF EXISTS idx_chapters_book")
    cur.execute("DROP TABLE chapters")
    cur.execute("ALTER TABLE chapters_new RENAME TO chapters")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id, ordinal)")
    con.commit()
    print("swap done; size before vacuum=%.1fMB"
          % (os.path.getsize(DB) / 1048576.0), flush=True)
    cur.execute("VACUUM")
    con.commit()
    print("size after vacuum=%.1fMB" % (os.path.getsize(DB) / 1048576.0), flush=True)
    cur.execute("PRAGMA integrity_check")
    print("integrity=%s" % cur.fetchone()[0], flush=True)
    con.close()
    print("SWAP_OK", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "copy":
        copy()
    elif mode == "swap":
        swap()
    else:
        print(__doc__)
        sys.exit(2)
