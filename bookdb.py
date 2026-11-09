#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书籍分章入库模块（零第三方依赖，仅用标准库）。

职责：
  - 管理 SQLite（books / chapters / parse_jobs），WAL 模式、线程安全；
  - 提供原始块（RawBlock）的归一化（按目标字数拆分/合并）；
  - 解析器通过 register_parser 注册，避免与 bookserver 循环导入；
  - 后台 worker 在 P4 接入。

业务主键沿用书籍相对路径 rel，与现有进度、封面、/u/ 链接对齐。
"""

import os
import re
import sqlite3
import threading
import time
import zlib
from html import escape as html_escape

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BOOK_DB", os.path.join(_HERE, "data", "books.db"))

CHAPTER_TARGET = 3000          # 目标字数
CHAPTER_MIN = 2000             # 单章下限，低于此值尝试与相邻章合并
CHAPTER_MAX = 4000             # 单章上限，超过此值在段落边界拆分

_PARA_RE = re.compile(r"<p\b.*?</p>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_P_OPEN_RE = re.compile(r"^(<p\b[^>]*>)(.*)(</p>)$", re.S)
_LONG_PARA_LIMIT = 400

# 解析器注册表：ext -> callable(rel) -> list[RawBlock]
_PARSERS = {}
# 由 bookserver 注入的路径解析：rel -> 绝对路径
_PATH_RESOLVER = None

_lock = threading.Lock()       # 串行化所有写操作
_conn = None


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
class RawBlock:
    """一个原始章节块（解析器产出，归一化前）。"""

    __slots__ = ("title", "html", "text", "source")

    def __init__(self, html, title=None, text=None, source="native"):
        self.title = title
        self.html = html
        self.text = text if text is not None else _strip_tags(html)
        self.source = source

    def __repr__(self):
        return "RawBlock(title=%r, chars=%d)" % (self.title, len(self.text))


def _strip_tags(html):
    return _TAG_RE.sub("", html or "")


def _split_long_para(para):
    """保护逻辑：单个超长 <p> 按字数拆成多个（真实数据受 wrap_long 限制不会触发）。"""
    m = _P_OPEN_RE.match(para)
    if not m:
        return [para]
    open_tag, inner, _ = m.groups()
    text = _strip_tags(inner)
    if len(text) <= _LONG_PARA_LIMIT:
        return [para]
    cm = re.search(r'class="([^"]*)"', open_tag)
    base = cm.group(1).split() if cm else []
    chunks = []
    for i in range(0, len(text), _LONG_PARA_LIMIT):
        cls = list(base) if i == 0 else list(set(base + ["cont"]))
        tag = ('<p class="%s">' % " ".join(cls)) if cls else "<p>"
        chunks.append(tag + html_escape(text[i:i + _LONG_PARA_LIMIT]) + "</p>")
    return chunks


# ----------------------------------------------------------------------------
# 连接与初始化
# ----------------------------------------------------------------------------
def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """初始化数据库表（幂等）。"""
    global _conn
    conn = _connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS books (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          rel           TEXT NOT NULL UNIQUE,
          title         TEXT,
          author        TEXT,
          ext           TEXT,
          size          INTEGER,
          mtime_ns      INTEGER,
          cover_url     TEXT,
          parse_status  TEXT NOT NULL DEFAULT 'pending',
          parse_note    TEXT,
          chapter_count INTEGER DEFAULT 0,
          total_chars   INTEGER DEFAULT 0,
          content_hash  TEXT,
          parsed_at     INTEGER
        );

        CREATE TABLE IF NOT EXISTS chapters (
          id           INTEGER PRIMARY KEY AUTOINCREMENT,
          book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
          ordinal      INTEGER NOT NULL,
          title        TEXT,
          content_html TEXT NOT NULL,
          plain_text   TEXT,
          char_count   INTEGER DEFAULT 0,
          source       TEXT DEFAULT 'native',
          UNIQUE(book_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_chapters_book ON chapters(book_id, ordinal);

        CREATE TABLE IF NOT EXISTS parse_jobs (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          rel        TEXT NOT NULL,
          priority   INTEGER DEFAULT 100,
          status     TEXT DEFAULT 'queued',
          attempts   INTEGER DEFAULT 0,
          last_err   TEXT,
          created_at INTEGER,
          updated_at INTEGER
        );
        """
    )
    conn.commit()
    _conn = conn
    return conn


def get_conn():
    if _conn is None:
        init_db()
    return _conn


# ----------------------------------------------------------------------------
# 注册机制（依赖注入，避免循环导入）
# ----------------------------------------------------------------------------
def register_parser(ext, func):
    """注册某格式的原始块解析函数：func(rel) -> list[RawBlock]。"""
    _PARSERS[ext.lower()] = func


def get_parser(ext):
    return _PARSERS.get((ext or "").lower())


def register_path_resolver(func):
    """注册 rel -> 绝对路径 的解析函数。"""
    global _PATH_RESOLVER
    _PATH_RESOLVER = func


def resolve_path(rel):
    if _PATH_RESOLVER is None:
        raise RuntimeError("path resolver 未注册")
    return _PATH_RESOLVER(rel)


# ----------------------------------------------------------------------------
# 归一化
# ----------------------------------------------------------------------------
def _expand_stream(blocks):
    """把 RawBlock 展开成段落流，每段记录所属块与块首标题。"""
    stream = []
    for bi, block in enumerate(blocks):
        paras = _PARA_RE.findall(block.html or "")
        first = True
        for raw_para in paras:
            for para in _split_long_para(raw_para):
                stream.append({
                    "p": para,
                    "len": len(_strip_tags(para)),
                    "heading": block.title if first else None,
                    "block_title": block.title,
                    "block_index": bi,
                    "src": block.source,
                })
                first = False
    return stream


def normalize_blocks(blocks):
    """按目标字数把原始块归一化为章节 dict 列表。

    返回每项：{title, html, plain_text, char_count, source}
    """
    stream = _expand_stream(blocks)
    if not stream:
        return []
    block_titles = [b.title for b in blocks]

    # 第一步：按标题边界 + 字数上限聚合
    rough = []
    cur_paras, cur_len, cur_src = [], 0, "native"
    cur_start_block, cur_end_block = None, None

    def flush():
        if cur_paras:
            rough.append({
                "paras": list(cur_paras),
                "len": cur_len,
                "source": cur_src,
                "start_block": cur_start_block,
                "end_block": cur_end_block,
            })

    for item in stream:
        bi = item["block_index"]
        heading = item["heading"]
        if heading:
            if cur_paras and cur_len >= CHAPTER_MIN:
                # 当前章已够长，遇到新标题封章
                flush()
                cur_paras, cur_len, cur_src = [], 0, item["src"]
                cur_start_block, cur_end_block = bi, bi
            elif not cur_paras:
                cur_src, cur_start_block, cur_end_block = item["src"], bi, bi
            # 当前章太短：并入新块，保留首块标题，merge 标记在组装时按跨块判定
        # 加入前判断：会超上限就先封章，保证单章不超 MAX
        if cur_paras and cur_len + item["len"] > CHAPTER_MAX:
            flush()
            cur_paras, cur_len, cur_src = [], 0, item["src"]
            cur_start_block, cur_end_block = bi, bi
        if not cur_paras:
            cur_start_block, cur_end_block = bi, bi
        cur_paras.append(item["p"])
        cur_len += item["len"]
        cur_end_block = bi
    flush()

    # 第二步：合并过短的相邻章
    merged = []
    for g in rough:
        if (merged and g["len"] < CHAPTER_MIN
                and merged[-1]["len"] + g["len"] <= CHAPTER_MAX):
            merged[-1]["paras"].extend(g["paras"])
            merged[-1]["len"] += g["len"]
            merged[-1]["end_block"] = g["end_block"]
            merged[-1]["source"] = "merged"
        else:
            merged.append(g)

    # 统计每个原始块被切成多少片
    block_pieces = {}
    for g in merged:
        block_pieces[g["start_block"]] = block_pieces.get(g["start_block"], 0) + 1
    block_seen = {}

    # 第三步：组装输出
    out = []
    auto_no = 0
    for g in merged:
        bi = g["start_block"]
        piece = block_seen.get(bi, 0)
        block_seen[bi] = piece + 1
        block_title = block_titles[bi]
        if block_title:
            if block_pieces[bi] <= 1:
                title = block_title
            else:
                title = block_title if piece == 0 else "%s（续%d）" % (block_title, piece)
        else:
            auto_no += 1
            title = "第 %d 节" % auto_no
        html = "".join(g["paras"])
        plain = _strip_tags(html)
        if g["source"] == "merged" or g["start_block"] != g["end_block"]:
            source = "merged"
        elif block_pieces[bi] > 1:
            source = "split"
        else:
            source = g["source"]
        out.append({
            "title": title,
            "html": html,
            "plain_text": plain,
            "char_count": len(plain),
            "source": source,
        })
    return out


# ----------------------------------------------------------------------------
# 数据访问：books
# ----------------------------------------------------------------------------
def get_status(rel):
    row = get_conn().execute("SELECT * FROM books WHERE rel=?", (rel,)).fetchone()
    return dict(row) if row else None


def upsert_book(rel, ext, status, **fields):
    """插入或更新书籍行，返回 book_id。"""
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT id FROM books WHERE rel=?", (rel,)).fetchone()
        now = int(time.time())
        if row is None:
            conn.execute(
                "INSERT INTO books(rel, ext, parse_status, parsed_at) VALUES(?,?,?,?)",
                (rel, ext, status, now),
            )
            book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            book_id = row[0]
            conn.execute(
                "UPDATE books SET parse_status=?, ext=? WHERE id=?",
                (status, ext, book_id),
            )
        sets, vals = [], []
        for key, val in fields.items():
            sets.append("%s=?" % key)
            vals.append(val)
        if sets:
            vals.append(book_id)
            conn.execute("UPDATE books SET %s WHERE id=?" % ",".join(sets), vals)
        conn.commit()
        return book_id


def set_status(rel, status, note=None):
    with _lock:
        conn = get_conn()
        conn.execute(
            "UPDATE books SET parse_status=?, parse_note=? WHERE rel=?",
            (status, note, rel),
        )
        conn.commit()


# ----------------------------------------------------------------------------
# 数据访问：chapters
# ----------------------------------------------------------------------------
def save_chapters(rel, ext, chapters, content_hash=None, meta=None):
    """事务内替换某书的全部章节并更新元数据。"""
    with _lock:
        conn = get_conn()
        now = int(time.time())
        row = conn.execute("SELECT id FROM books WHERE rel=?", (rel,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO books(rel, ext, parse_status, parsed_at) VALUES(?,?,?,?)",
                (rel, ext, "parsed", now),
            )
            book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            book_id = row[0]
        conn.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
        total_chars = 0
        for ordinal, ch in enumerate(chapters):
            total_chars += ch["char_count"]
            conn.execute(
                "INSERT INTO chapters(book_id, ordinal, title, content_html,"
                " char_count, source) VALUES(?,?,?,?,?,?)",
                (book_id, ordinal, ch["title"], _pack_html(ch["html"]),
                 ch["char_count"], ch["source"]),
            )
        conn.execute(
            "UPDATE books SET parse_status='parsed', chapter_count=?, total_chars=?,"
            " content_hash=?, parsed_at=? WHERE id=?",
            (len(chapters), total_chars, content_hash, now, book_id),
        )
        if meta:
            sets, vals = [], []
            for key, val in meta.items():
                sets.append("%s=?" % key)
                vals.append(val)
            vals.append(book_id)
            conn.execute("UPDATE books SET %s WHERE id=?" % ",".join(sets), vals)
        conn.commit()
        return len(chapters)


def _pack_html(html):
    """正文入库前 zlib 压缩：单章多为 10~20KB 中文，压缩后通常只剩 40%~50%。"""
    try:
        return zlib.compress(html.encode("utf-8"), 6)
    except Exception:
        return html


def _unpack_html(v):
    """读取兼容：新数据是压缩后的 BLOB，老数据是纯 TEXT，直接返回。"""
    if isinstance(v, (bytes, bytearray)):
        try:
            return zlib.decompress(bytes(v)).decode("utf-8", "replace")
        except Exception:
            return ""
    return v or ""


def get_toc(rel):
    conn = get_conn()
    row = conn.execute("SELECT id FROM books WHERE rel=?", (rel,)).fetchone()
    if row is None:
        return None
    rows = conn.execute(
        "SELECT ordinal, title, char_count FROM chapters WHERE book_id=?"
        " ORDER BY ordinal", (row[0],)
    ).fetchall()
    return [dict(r) for r in rows]


def get_chapter(rel, ordinal):
    conn = get_conn()
    row = conn.execute("SELECT id FROM books WHERE rel=?", (rel,)).fetchone()
    if row is None:
        return None
    r = conn.execute(
        "SELECT ordinal, title, content_html, char_count FROM chapters"
        " WHERE book_id=? AND ordinal=?", (row[0], ordinal)
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["content_html"] = _unpack_html(d["content_html"])
    return d


def chapter_count(rel):
    conn = get_conn()
    row = conn.execute(
        "SELECT chapter_count, parse_status FROM books WHERE rel=?", (rel,)
    ).fetchone()
    return (row[0], row[1]) if row else (0, None)


def delete_book(rel):
    """删除书及其章节（级联）。"""
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM books WHERE rel=?", (rel,))
        conn.execute("DELETE FROM parse_jobs WHERE rel=?", (rel,))
        conn.commit()


# ----------------------------------------------------------------------------
# 数据访问：parse_jobs
# ----------------------------------------------------------------------------
def enqueue(rel, priority=100):
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT id FROM parse_jobs WHERE rel=? AND status IN ('queued','running')",
            (rel,),
        ).fetchone()
        now = int(time.time())
        if row is None:
            conn.execute(
                "INSERT INTO parse_jobs(rel, priority, status, created_at, updated_at)"
                " VALUES(?,?, 'queued', ?, ?)",
                (rel, priority, now, now),
            )
        else:
            conn.execute(
                "UPDATE parse_jobs SET priority=?, updated_at=? WHERE id=?",
                (priority, now, row[0]),
            )
        conn.commit()


def claim_job():
    """取一个优先级最高的 queued 任务并置 running，返回 (rel, attempts) 或 None。"""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT id, rel, attempts FROM parse_jobs WHERE status='queued'"
            " ORDER BY priority, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        now = int(time.time())
        conn.execute(
            "UPDATE parse_jobs SET status='running', updated_at=? WHERE id=?",
            (now, row[0]),
        )
        conn.commit()
        return row[1], row[2]


def finish_job(rel, ok=True, err=None):
    with _lock:
        conn = get_conn()
        now = int(time.time())
        status = "done" if ok else "failed"
        conn.execute(
            "UPDATE parse_jobs SET status=?, last_err=?, updated_at=? WHERE rel=?"
            " AND status='running'",
            (status, err, now, rel),
        )
        conn.commit()


def requeue_job(rel):
    """失败任务退避后重新入队，attempts+1；返回新 attempts，无 running 行返回 None。"""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT attempts FROM parse_jobs WHERE rel=? AND status='running'",
            (rel,),
        ).fetchone()
        if row is None:
            return None
        attempts = row[0] + 1
        now = int(time.time())
        conn.execute(
            "UPDATE parse_jobs SET status='queued', attempts=?, updated_at=?"
            " WHERE rel=?",
            (attempts, now, rel),
        )
        conn.commit()
        return attempts
