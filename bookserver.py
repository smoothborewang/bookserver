#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量电子书库服务（零第三方依赖）

功能：
  /               书库首页（卡片列表 + 搜索 + 上传 + 删除）
  /login          访问码登录
  /api/books      书籍列表 JSON
  /file/<path>    下载原文件（支持 Range，PDF 内嵌阅读用）
  /read/<path>    EPUB 在线阅读（epub.js），PDF 用浏览器内嵌
  /opds           OPDS 目录，供 KOReader / 静读天下 订阅（支持 HTTP Basic）
  /upload         网页上传（multipart/form-data，字段名 files）
  /delete         删除书籍（JSON：{"rel": "..."}）
  /static/<f>     前端静态资源

环境变量：
  BOOK_BASE   书库目录
  BOOK_STATIC 静态目录
  BOOK_PORT   端口
  BOOK_CODE   访问码；为空则关闭认证
"""

import bisect
import base64
import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import struct
import threading
import time
import zipfile
import posixpath
import bookdb
import kf8
from datetime import datetime, timezone
from html import unescape as _hunescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse
from xml.sax.saxutils import escape as xesc
import xml.etree.ElementTree as _ET

BASE = os.environ.get("BOOK_BASE", "/home/pi/books/library")
STATIC = os.environ.get("BOOK_STATIC", "/home/pi/bookserver/static")
PORT = int(os.environ.get("BOOK_PORT", "8080"))
HOST = os.environ.get("BOOK_HOST", "0.0.0.0")
CODE = os.environ.get("BOOK_CODE", "").strip()

# 分章阅读灰度开关：1/true/yes 启用新模板，其余全量退回旧实时解析路径
DB_READER_ENABLED = os.environ.get("DB_READER_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on")

EXTS = (".epub", ".pdf", ".mobi", ".azw3", ".txt", ".cbz", ".cbr", ".djvu", ".fb2")

# 网页上传总量上限：这台机器内存只有 512MB，请求体是整块读入内存的
MAX_UPLOAD = 96 * 1024 * 1024

COOKIE_NAME = "bs_auth"
COOKIE_MAXAGE = 30 * 24 * 3600
TOKEN = hashlib.sha256(("bookshelf:" + CODE).encode("utf-8")).hexdigest() if CODE else ""

MIME = {
    ".epub": "application/epub+zip",
    ".pdf": "application/pdf",
    ".mobi": "application/x-mobipocket-ebook",
    ".azw3": "application/vnd.amazon.ebook",
    ".txt": "text/plain; charset=utf-8",
    ".cbz": "application/vnd.comicbook+zip",
    ".cbr": "application/vnd.comicbook-rar",
    ".djvu": "image/vnd.djvu",
    ".fb2": "application/x-fictionbook+xml",
}

STATIC_MIME = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
}

EPUB_MIME = {
    ".xhtml": "application/xhtml+xml; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".opf": "application/xml; charset=utf-8",
    ".ncx": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".js": "application/javascript; charset=utf-8",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
}


def u_key(rel):
    """把书籍相对路径压成可放进 URL 的键（可逆、无需状态）"""
    return base64.urlsafe_b64encode(rel.encode("utf-8")).decode("ascii").rstrip("=")


def u_rel(key):
    pad = "=" * (-len(key) % 4)
    try:
        rel = base64.urlsafe_b64decode(key + pad).decode("utf-8")
    except Exception:
        return None
    return safe_rel(rel)


def zip_member(z, inner):
    """在 epub 包里定位成员：容忍 ./ 前缀、URL 编码与大小写差异"""
    inner = (inner or "").lstrip("/")
    if not inner or ".." in inner.split("/"):
        return None
    for cand in (inner, unquote(inner), "./" + inner.lstrip("./")):
        try:
            return z.getinfo(cand)
        except KeyError:
            continue
    low = inner.lstrip("./").lower()
    for n in z.namelist():
        if n.lstrip("./").lower() == low:
            try:
                return z.getinfo(n)
            except KeyError:
                continue
    return None


_cache = {"t": 0.0, "books": []}

# 阅读进度：rel -> {"cfi": 位置, "pct": 百分比, "t": 时间戳}
PROGRESS_FILE = os.environ.get("BOOK_PROGRESS", os.path.join(os.path.dirname(BASE.rstrip("/")), "progress.json"))
_progress = {}
_plock = threading.Lock()
_last_save = [0.0]


def load_progress():
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    _progress[k] = v
    except (OSError, ValueError):
        pass


def save_progress(force=False):
    now = time.time()
    if not force and now - _last_save[0] < 12:
        return  # 限流：最多每 12 秒落盘一次，兼顾 SD 卡寿命与换设备时的新鲜度
    tmp = PROGRESS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_progress, fh, ensure_ascii=False)
        os.replace(tmp, PROGRESS_FILE)
        _last_save[0] = now
    except OSError:
        pass


def scan_books(force=False):
    now = time.time()
    if not force and now - _cache["t"] < 30:
        return _cache["books"]
    books = []
    try:
        for root, dirs, files in os.walk(BASE):
            dirs.sort()
            for f in sorted(files, key=lambda s: s.lower()):
                low = f.lower()
                if not low.endswith(EXTS):
                    continue
                p = os.path.join(root, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                rel = os.path.relpath(p, BASE).replace(os.sep, "/")
                d = rel.rsplit("/", 1)[0] if "/" in rel else ""
                pr = _progress.get(rel) or {}
                try:
                    pct = float(pr.get("pct") or 0)
                except (TypeError, ValueError):
                    pct = 0.0
                books.append(
                    {
                        "rel": rel,
                        "dir": d,
                        "name": os.path.splitext(f)[0],
                        "ext": os.path.splitext(low)[1].lstrip("."),
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                        "pct": max(0, min(100, int(round(pct)))),
                        "readAt": int(pr.get("t") or 0),
                        "readN": int(pr.get("n") or 0),
                        "readSec": int(pr.get("tot") or 0),
                    }
                )
    except FileNotFoundError:
        pass
    _cache.update(t=now, books=books)
    return books


_BOOKS_JSON_CACHE = {}


def books_json(books):
    """缓存书单的 JSON 序列化结果（scan_books 在缓存期内返回同一对象）。"""
    k = id(books)
    s = _BOOKS_JSON_CACHE.get(k)
    if s is None:
        s = json.dumps(books, ensure_ascii=False)
        if len(_BOOKS_JSON_CACHE) > 4:
            _BOOKS_JSON_CACHE.clear()
        _BOOKS_JSON_CACHE[k] = s
    return s


def list_dirs():
    """分类（= 书库下的目录）清单：返回 [{"dir": 相对路径, "n": 本数}]，根目录单独统计不列出。"""
    cnt = {}
    for b in scan_books():
        d = b.get("dir") or ""
        if d:
            cnt[d] = cnt.get(d, 0) + 1
    # 把空分类目录也带上，方便先建分类再往里放书
    try:
        for root, dirs, files in os.walk(BASE):
            dirs.sort()
            for sub in dirs:
                if sub.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(root, sub), BASE).replace(os.sep, "/")
                cnt.setdefault(rel, 0)
    except OSError:
        pass
    return [{"dir": k, "n": cnt[k]} for k in sorted(cnt)]


def make_dir(name):
    """新建一级分类目录，返回目录名。"""
    n = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or "")).strip(" .")
    if not n:
        raise ValueError("分类名不能为空")
    if len(n) > 40:
        raise ValueError("分类名太长（最多 40 字）")
    p = os.path.join(BASE, n)
    if os.path.isdir(p):
        raise FileExistsError("分类已存在")
    os.makedirs(p)
    _cache["t"] = 0.0
    return n


def move_book(rel, target):
    """把一本书移到某个分类（target 为空表示移回书库根目录），返回新的 rel。"""
    base_real = os.path.realpath(BASE)
    src = os.path.realpath(os.path.join(BASE, rel))
    if not src.startswith(base_real + os.sep) or not os.path.isfile(src):
        raise FileNotFoundError("书不存在")
    tdir = safe_rel(target or "")
    dst_dir = os.path.realpath(os.path.join(BASE, tdir)) if tdir else base_real
    if not (dst_dir == base_real or dst_dir.startswith(base_real + os.sep)) or not os.path.isdir(dst_dir):
        raise FileNotFoundError("目标分类不存在")
    name = os.path.basename(src)
    dst = os.path.join(dst_dir, name)
    if dst == src:
        return rel  # 已经在这个分类里
    if os.path.exists(dst):
        stem, ext = os.path.splitext(name)
        n = 1
        while os.path.exists(dst):
            dst = os.path.join(dst_dir, "%s(%d)%s" % (stem, n, ext))
            n += 1
    os.rename(src, dst)
    new_rel = os.path.relpath(dst, BASE).replace(os.sep, "/")
    if DB_READER_ENABLED:             # 库里的记录跟着书走，否则会留下旧路径孤儿行 + 新路径重复解析
        try:
            bookdb.move_book(rel, new_rel)
        except Exception as e:
            print("move_book db sync failed: %s" % e)
    with _plock:                      # 阅读进度跟着书走
        old = _progress.pop(rel, None)
        if old is not None:
            _progress[new_rel] = old
            save_progress(force=True)
    for ext in (".jpg", ".png", ".gif", ".webp"):   # 旧封面缓存按 rel 哈希，直接清掉重生成
        try:
            os.remove(_cover_target(rel) + ext)
        except OSError:
            pass
    _cache["t"] = 0.0
    return new_rel


BOOKS_ROOT = os.path.dirname(BASE.rstrip("/"))
COVER_DIR = os.path.join(BOOKS_ROOT, ".covers")
TRASH_DIR = os.path.join(BOOKS_ROOT, ".trash")
TRASH_DAYS = int(os.environ.get("BOOK_TRASH_DAYS", "30") or 30)
_trash_lock = threading.Lock()


def _cover_target(rel):
    return os.path.join(COVER_DIR, hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16])


def epub_cover_bytes(path):
    """从 EPUB 里找出封面图，返回 (文件名, 字节)；找不到返回 None。"""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            opf = None
            try:
                c = z.read("META-INF/container.xml").decode("utf-8", "ignore")
                m = re.search(r'full-path="([^"]+)"', c)
                if m:
                    opf = m.group(1)
            except KeyError:
                pass
            if not opf:
                cands = [n for n in names if n.lower().endswith(".opf")]
                if cands:
                    opf = cands[0]
            href, base = None, ""
            if opf:
                base = posixpath.dirname(opf)
                opf_xml = z.read(opf).decode("utf-8", "ignore")
                m = re.search(r'<item[^>]*properties="[^"]*cover-image[^"]*"[^>]*>', opf_xml)
                if m:
                    hm = re.search(r'href="([^"]+)"', m.group(0))
                    if hm:
                        href = hm.group(1)
                if not href:
                    cm = re.search(r'<meta[^>]*name="cover"[^>]*content="([^"]+)"', opf_xml)
                    if cm:
                        im = re.search(r'<item[^>]*id="%s"[^>]*>' % re.escape(cm.group(1)), opf_xml)
                        if im:
                            hm = re.search(r'href="([^"]+)"', im.group(0))
                            if hm:
                                href = hm.group(1)
            if not href:
                for n in names:
                    low = n.lower()
                    if "cover" in low and low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                        href, base = n, ""
                        break
            if not href:
                return None
            full = posixpath.normpath(posixpath.join(base, href)) if base else href
            if full not in names:
                want = posixpath.basename(href)
                hits = [n for n in names if posixpath.basename(n) == want]
                if not hits:
                    return None
                full = hits[0]
            return (full, z.read(full))
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return None


def cover_for(rel):
    """取（必要时现场生成并缓存）封面，返回 (字节, 扩展名) 或 None。"""
    path = os.path.join(BASE, rel)
    if not os.path.isfile(path):
        return None
    tgt = _cover_target(rel)
    for ext in (".jpg", ".png", ".gif", ".webp"):
        if os.path.exists(tgt + ext):
            try:
                with open(tgt + ext, "rb") as fh:
                    return (fh.read(), ext)
            except OSError:
                return None
    if not rel.lower().endswith(".epub"):
        return None
    got = epub_cover_bytes(path)
    if not got:
        return None
    full, data = got
    ext = os.path.splitext(full)[1].lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in (".jpg", ".png", ".gif", ".webp"):
        ext = ".jpg"
    if len(data) <= 3 * 1024 * 1024:
        try:
            os.makedirs(COVER_DIR, exist_ok=True)
            tmp = tgt + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, tgt + ext)
        except OSError:
            pass
    return (data, ext)


# ---------------- 回收站 ----------------

def _trash_index():
    return os.path.join(TRASH_DIR, "index.json")


def _trash_load():
    try:
        with open(_trash_index(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _trash_save(idx):
    try:
        os.makedirs(TRASH_DIR, exist_ok=True)
        tmp = _trash_index() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(idx, fh, ensure_ascii=False)
        os.replace(tmp, _trash_index())
    except OSError:
        pass


def trash_put(rel):
    src = os.path.join(BASE, rel)
    if not os.path.isfile(src):
        raise FileNotFoundError(rel)
    os.makedirs(TRASH_DIR, exist_ok=True)
    name = "%d_%s" % (int(time.time()), os.path.basename(rel))
    dst = os.path.join(TRASH_DIR, name)
    if os.path.exists(dst):
        name = "%d_%s" % (int(time.time() * 1000), os.path.basename(rel))
        dst = os.path.join(TRASH_DIR, name)
    shutil.move(src, dst)
    try:
        size = os.path.getsize(dst)
    except OSError:
        size = 0
    with _trash_lock:
        idx = _trash_load()
        idx[name] = {"rel": rel, "name": os.path.splitext(os.path.basename(rel))[0],
                     "ts": int(time.time()), "size": size}
        _trash_save(idx)
    _cache["t"] = 0.0
    return name


def trash_list():
    with _trash_lock:
        idx = _trash_load()
    out = []
    for k, v in idx.items():
        if not os.path.isfile(os.path.join(TRASH_DIR, k)):
            continue
        out.append({"id": k, "rel": v.get("rel", ""), "name": v.get("name", k),
                    "ts": int(v.get("ts") or 0), "size": int(v.get("size") or 0)})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def trash_restore(tid):
    with _trash_lock:
        idx = _trash_load()
    meta = idx.get(tid)
    src = os.path.join(TRASH_DIR, tid)
    if not meta or not os.path.isfile(src):
        raise FileNotFoundError(tid)
    rel = safe_rel(meta.get("rel") or "")
    dst = os.path.join(BASE, rel)
    if os.path.exists(dst):
        raise FileExistsError(rel)
    d = os.path.dirname(dst)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    shutil.move(src, dst)
    with _trash_lock:
        idx2 = _trash_load()
        idx2.pop(tid, None)
        _trash_save(idx2)
    _cache["t"] = 0.0
    return rel


def trash_purge(tid):
    p = os.path.join(TRASH_DIR, tid)
    try:
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass
    with _trash_lock:
        idx = _trash_load()
        idx.pop(tid, None)
        _trash_save(idx)


def trash_cleanup():
    cutoff = time.time() - TRASH_DAYS * 86400
    with _trash_lock:
        idx = _trash_load()
    dead = [k for k, v in idx.items() if int(v.get("ts") or 0) < cutoff]
    for k in dead:
        try:
            p = os.path.join(TRASH_DIR, k)
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
    if dead:
        with _trash_lock:
            idx2 = _trash_load()
            for k in dead:
                idx2.pop(k, None)
            _trash_save(idx2)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return "%.1fTB" % n


def safe_rel(path):
    path = unquote(path).replace("\\", "/")
    parts = [p for p in path.split("/") if p and p not in (".", "..")]
    return "/".join(parts)


TXT_MAX = 2 * 1024 * 1024


def read_text_file(p, limit=TXT_MAX):
    """读纯文本，自动识别 UTF-8 / GB18030；返回 (文本, 是否被截断)。"""
    try:
        with open(p, "rb") as fh:
            raw = fh.read(limit + 1)
    except OSError:
        return ("", False)
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    for enc in ("utf-8", "gb18030"):
        try:
            return (raw.decode(enc), truncated)
        except UnicodeDecodeError:
            continue
    return (raw.decode("utf-8", "replace"), truncated)


def _wrap_long(t, limit=120):
    """把超长段落按句切成若干块，避免整段占满整页留出大片空白。"""
    if len(t) <= limit:
        return [t]
    sents = [s for s in re.split(r"(?<=[\u3002\uff01\uff1f\uff1b.!?])", t) if s]
    out, buf = [], ""
    for s in sents:
        if buf and len(buf) + len(s) > limit:
            out.append(buf)
            buf = ""
        buf += s
        while len(buf) > int(limit * 1.6):
            out.append(buf[:limit])
            buf = buf[limit:]
    if buf:
        out.append(buf)
    return out


# 章节标题识别（借鉴开源阅读 legado 的本地 TXT 目录规则）：
# 行首匹配「第N章/节/回/卷/部/集/篇/幕」或 序章/楔子/引子/前言/正文/后记/番外 等关键词
_CHAPTER_RE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9０-９零〇一二三四五六七八九十百千万两]+\s*[章节回卷部集篇幕]"
    r"|卷[ \t]*[0-9０-９零〇一二三四五六七八九十百千万两]+"
    r"|[0-9零〇一二三四五六七八九十百千]+[、.]"
    r"|序(?:章|言)?(?:\s|$)|楔子|引子|前言|序言|正文|后记|尾声|番外|外传|终章|完结感言|完结语"
    r")"
)


def _is_chapter_title(t):
    if not t or len(t) > 30:
        return False
    if re.search(r"[。！？!?；;]", t):      # 含句末标点的是正文，不是标题
        return False
    return bool(_CHAPTER_RE.match(t))


def text_to_html(text):
    """把纯文本切成段落，返回 (HTML 片段, 目录 JSON)。
       目录项 {"p": 段落元素序号, "t": 标题}，供前端跳转。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    blocks = re.split(r"\n[ \t]*\n", text)
    good = [b for b in blocks if b.strip()]
    # 空行分不出段落时（整本一行一段），退回按行分段
    if len(good) < max(20, len(lines) // 10):
        good = [ln for ln in lines if ln.strip()]
    ch_set = set()
    for gi, b in enumerate(good):
        if _is_chapter_title(b.strip("\n").strip()):
            ch_set.add(gi)
    out, toc = [], []
    pidx = 0
    for gi, b in enumerate(good):
        t = b.strip("\n").strip()
        if not t:
            continue
        is_ch = gi in ch_set
        for wi, part in enumerate(_wrap_long(t)):
            classes = []
            if wi > 0:
                classes.append("cont")
            if is_ch and wi == 0:
                classes.append("ch")
            cls = (' class="%s"' % " ".join(classes)) if classes else ""
            out.append("<p%s>%s</p>" % (cls, xesc(part).replace("\n", "<br>")))
            if is_ch and wi == 0:
                toc.append({"p": pidx, "t": t})
            pidx += 1
    return "".join(out), json.dumps(toc, ensure_ascii=False)


# ---------------- MOBI 正文提取（纯标准库） ----------------

def _mobi_unpack(data):
    """PalmDOC 解压（对齐 Kindle 标准实现，保证原生 filepos 坐标可对齐）：
       0x01-0x08 复制随后 N 字节；0x00-0x7F 单字节原样；
       0x80-0xBF 双字节回引（必须逐字节复制以支持重叠回引）；
       0xC0-0xFF「空格 + 反码字符」。

       注意：回引若用切片批量复制（out[-dist:...]），重叠回引（dist < ln）
       会复制到错误字节，表现为正文个别字被顶替；而越界回引若不停止，
       会在记录尾部反复追加同一字节，解码后成为连续乱码。两者都必须
       按「取一位、追加一位、位置前移」的方式处理。
    """
    out = bytearray()
    p, n = 0, len(data)
    while p < n:
        c = data[p]
        p += 1
        if 1 <= c <= 8:
            out.extend(data[p:p + c])
            p += c
        elif c < 128:
            out.append(c)
        elif c >= 192:
            out.append(0x20)
            out.append(c ^ 0x80)
        else:
            if p >= n:
                break
            v = (c << 8) | data[p]
            p += 1
            dist = (v >> 3) & 0x07FF
            ln = (v & 7) + 3
            src = len(out) - dist
            for _ in range(ln):
                if 0 <= src < len(out):
                    out.append(out[src])
                src += 1
    return bytes(out)


def _mobi_plain(s):
    """把 MOBI 内嵌 HTML 转成分行纯文本。"""
    s = re.sub(r"<(style|script)\b.*?</\1>", "", s, flags=re.S | re.I)
    s = re.sub(r"<{2,}", "<", s)            # 记录尾部填充的连续 '<' 会吞掉后文，先归一
    s = re.sub(r"<mbp:pagebreak\s*/?>", "\n\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote|section)>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _hunescape(s).replace("\xa0", " ")
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]*\n\s*", "\n\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---------------- MOBI 原生目录（NCX/INDX，纯标准库） ----------------

_MOBI_INDX_WORDS = ("len", "nul1", "type", "gen", "start", "count", "code",
                    "lng", "total", "ordt", "ligt", "nligt", "nctoc")
_MOBI_TOKEN_RE = re.compile(r"<[^>]*>|[^<]+", re.S)
_MOBI_CLOSE_RE = r"</(?:p|div|h[1-6]|li|tr|blockquote|section)\s*>"


def _mobi_vwi(data, offset):
    """变长整数：每字节低 7 位载荷，最高位 1 表示结束。返回 (消耗字节, 值)。"""
    value = 0
    consumed = 0
    while True:
        c = data[offset + consumed]
        consumed += 1
        if c & 0x80:
            return consumed, (value << 7) | (c & 0x7F)
        value = (value << 7) | (c & 0x7F)


def _mobi_nbits(value):
    n = 0
    while value:
        n += value & 1
        value >>= 1
    return n


def _mobi_indx_hdr(data):
    """解析 INDX 记录头（13 个 uint32，从 data[4] 起）。"""
    if data[:4] != b"INDX":
        return None
    vals = struct.unpack(">%dL" % len(_MOBI_INDX_WORDS),
                         data[4:4 + 4 * len(_MOBI_INDX_WORDS)])
    return dict(zip(_MOBI_INDX_WORDS, vals))


def _mobi_tagx(start, data):
    """读取 TAGX 标签段，返回 (控制字节数, 标签表)。"""
    tags = []
    if data[start:start + 4] != b"TAGX":
        return 0, tags
    first = struct.unpack_from(">I", data, start + 4)[0]
    cbc = struct.unpack_from(">I", data, start + 8)[0]
    for i in range(12, first, 4):
        p = start + i
        tags.append((data[p], data[p + 1], data[p + 2], data[p + 3]))
    return cbc, tags


def _mobi_ctoc(data):
    """读取 CTOC 文本记录：每项 (偏移, 名称)。"""
    out = {}
    offset = 0
    while offset < len(data):
        if data[offset] == 0:
            break
        idx = offset
        used, ilen = _mobi_vwi(data, offset)
        offset += used
        out[idx] = data[offset:offset + ilen]
        offset += ilen
    return out


def _mobi_tagmap(cbc, tag_table, edata, start, end):
    """解析单个 entry 的标签，返回 {tag: [值...]}。"""
    tags, hmap = [], {}
    cbi, dstart = 0, start + cbc
    for tag, vpe, mask, eflag in tag_table:
        if eflag == 1:
            cbi += 1
            continue
        value = edata[start + cbi] & mask
        if not value:
            continue
        if value == mask:
            if _mobi_nbits(mask) > 1:
                used, value = _mobi_vwi(edata, dstart)
                dstart += used
                tags.append((tag, None, value, vpe))
            else:
                tags.append((tag, 1, None, vpe))
        else:
            m = mask
            while (m & 1) == 0:
                m >>= 1
                value >>= 1
            tags.append((tag, value, None, vpe))
    for tag, vcount, vbytes, vpe in tags:
        values = []
        if vcount is not None:
            for _ in range(vcount):
                for _ in range(vpe):
                    used, data = _mobi_vwi(edata, dstart)
                    dstart += used
                    values.append(data)
        else:
            total = 0
            while total < vbytes:
                used, data = _mobi_vwi(edata, dstart)
                dstart += used
                total += used
                values.append(data)
        hmap[tag] = values
    return hmap


def _mobi_index_data(load, idx):
    """主流程：返回 (outtbl[[label, tagMap]], ctoc_text)。"""
    outtbl, ctoc = [], {}
    if idx == 0xFFFFFFFF:
        return outtbl, ctoc
    main = load(idx)
    mh = _mobi_indx_hdr(main)
    if mh is None:
        return outtbl, ctoc
    index_count = mh["count"]
    rec_off, base = 0, idx + index_count + 1
    for j in range(mh["nctoc"]):
        for k, v in _mobi_ctoc(load(base + j)).items():
            ctoc[k + rec_off] = v
        rec_off += 0x10000
    cbc, tag_table = _mobi_tagx(mh["len"], main)
    for i in range(idx + 1, idx + 1 + index_count):
        data = load(i)
        h = _mobi_indx_hdr(data)
        idxt, ecnt = h["start"], h["count"]
        poses = [struct.unpack_from(">H", data, idxt + 4 + 2 * j)[0]
                 for j in range(ecnt)]
        poses.append(idxt)
        for j in range(ecnt):
            sp, ep = poses[j], poses[j + 1]
            tlen = data[sp]
            label = data[sp + 1:sp + 1 + tlen]
            outtbl.append([label, _mobi_tagmap(cbc, tag_table, data,
                                               sp + 1 + tlen, ep)])
    return outtbl, ctoc


def _mobi_scan_blocks(html):
    """剥离 HTML 标签的同时记录每个文本块在源 HTML 中的字节偏移。
       返回 [(块文本, 源字节偏移)]，块切分与 _mobi_plain + text_to_html 对齐。"""
    blocks, cur = [], []
    cur_start, skip, bpos = 0, None, 0

    def flush():
        if cur:
            blocks.append((list(cur), cur_start))
        cur.clear()

    for m in _MOBI_TOKEN_RE.finditer(html):
        tok = m.group()
        tlen = len(tok.encode("utf-8"))
        if tok.startswith("<"):
            low = tok.lower()
            if skip:
                if low.startswith("</" + skip):
                    skip = None
                bpos += tlen
                continue
            mm = re.match(r"<(style|script)(?:\s|>)", low)
            if mm:
                skip = mm.group(1)
                bpos += tlen
                continue
            if re.match(r"<mbp:pagebreak", low):
                flush()
                bpos += tlen
                continue
            if re.match(r"<br\b", low):
                cur.append("\n")
                bpos += tlen
                continue
            if re.match(_MOBI_CLOSE_RE, low):
                flush()
                bpos += tlen
                continue
            bpos += tlen
        else:
            if not cur:
                cur_start = bpos
            for ch in tok:
                cur.append(ch)
                bpos += len(ch.encode("utf-8"))
    flush()
    out = []
    for chars, start in blocks:
        s = _hunescape("".join(chars)).replace("\xa0", " ")
        s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
        s = re.sub(r"[ \t]+\n", "\n", s).strip()
        if s:
            out.append((s, start))
    return out


def mobi_ncx_chapters(buf, r0, raw, offs, nrec):
    """用已解压正文 buf 与 MOBI 原生 NCX 构建目录，返回 [{p, t}]；
       p 为最终段落元素序号，t 为章节名。无 NCX 时返回 []。"""
    if len(r0) < 0xF8:
        return []

    def load(i):
        e = offs[i + 1] if i + 1 < nrec else len(raw)
        return raw[offs[i]:e]

    ncxidx = struct.unpack_from(">L", r0, 0xF4)[0]
    outtbl, ctoc = _mobi_index_data(load, ncxidx)
    if not outtbl:
        return []
    blocks = _mobi_scan_blocks(buf.decode("utf-8", "replace"))
    pidx, bpidx, bbyte, btext = 0, [], [], []
    for s, byte in blocks:
        bpidx.append(pidx)
        bbyte.append(byte)
        btext.append(s)
        pidx += len(_wrap_long(s))
    chapters = []
    for order, (label, tag_map) in enumerate(outtbl):
        pos = tag_map.get(1, [-1])[0]
        noffs = tag_map.get(3, [-1])[0]
        name = ctoc.get(noffs, b"?").decode("utf-8", "replace").strip()
        # 首个 entry 通常是指向书内目录页的根项，跳过
        if order == 0 and ("目录" in name or name.lower() in ("toc", "contents")):
            continue
        sbi = min(bisect.bisect_left(bbyte, pos), len(blocks) - 1)
        bi = sbi
        # filepos 可能落在标题前几个段落，向后精确匹配章节名定位标题块
        for k in range(sbi, min(len(blocks), sbi + 12)):
            if btext[k].strip() == name:
                bi = k
                break
        chapters.append({"p": bpidx[bi], "t": name})
    return chapters


_MOBI_CACHE = {}
_MOBI_RENDER_CACHE = {}


def mobi_to_text(path):
    """读取 .mobi / 部分 .azw3 的正文与原生目录。
       返回 (纯文本, 章节列表)；不支持或无 NCX 时章节为 []。"""
    try:
        st = os.stat(path)
    except OSError:
        return "", []
    key = (path, st.st_mtime_ns, st.st_size)
    if key in _MOBI_CACHE:
        return _MOBI_CACHE[key]
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if len(raw) < 120:
            return "", []
        nrec = struct.unpack_from(">H", raw, 76)[0]
        if not nrec or nrec > 200000:
            return "", []
        offs = [struct.unpack_from(">I", raw, 78 + i * 8)[0] for i in range(nrec)]
        r0 = raw[offs[0]:offs[1] if nrec > 1 else len(raw)]
        if len(r0) < 16:
            return "", []
        comp = struct.unpack_from(">H", r0, 0)[0]
        if comp not in (1, 2):          # 17480 = Huffman 序列压缩，暂不支持
            return "", []
        rc = struct.unpack_from(">H", r0, 8)[0]
        textlen = struct.unpack_from(">I", r0, 4)[0]
        enc = 65001
        if r0[16:20] == b"MOBI" and len(r0) >= 32:
            e = struct.unpack_from(">I", r0, 28)[0]
            if e in (65001, 1252):
                enc = e
        buf = bytearray()
        # 关键：MOBI 正文每条记录固定 4096 字节，但解压后常多出 1~20 字节的尾部填充
        # （填充字节直接写进压缩流，解压后表现为零星 0x00/0x8x 垃圾），若整段拼接，
        # 这些垃圾会插在记录中间、把相邻 UTF-8 字节冲散成 U+FFFD —— 必须在此处截断。
        left = textlen if textlen > 0 else None
        for i in range(1, 1 + rc):
            if i >= nrec:
                break
            end = offs[i + 1] if i + 1 < nrec else len(raw)
            chunk = raw[offs[i]:end]
            dec = _mobi_unpack(chunk) if comp == 2 else chunk
            if left is None:
                buf.extend(dec)
            else:
                take = min(4096, left, len(dec))
                buf.extend(dec[:take])
                left -= take
                if left <= 0:
                    break
        raw_html = bytes(buf)
        text = raw_html.decode("utf-8" if enc == 65001 else "cp1252", "replace")
        # 余下的替换字符只可能来自异常记录，直接剔除，不给读者看占位符。
        text = text.replace("\ufffd", "")
    except Exception:
        return "", []
    ncx_toc = []
    if enc == 65001:
        try:
            ncx_toc = mobi_ncx_chapters(raw_html, r0, raw, offs, nrec)
        except Exception:
            ncx_toc = []
    text = _mobi_plain(text)
    result = (text, ncx_toc)
    if len(_MOBI_CACHE) > 3:
        _MOBI_CACHE.clear()
    _MOBI_CACHE[key] = result
    return result


# ---------------- 分章入库：TXT / MOBI 原始块解析 ----------------

_PARA_SPLIT_RE = re.compile(r"<p\b.*?</p>", re.S)


def _read_full_text(p):
    """后台入库用：完整读取文本并识别编码（不受在线阅读 TXT_MAX 截断）。"""
    try:
        with open(p, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    if raw[:3] == b"\xef\xbb\xbf":
        raw = raw[3:]
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _html_to_blocks(body_html, toc):
    """按目录 [{p,t}] 把整本段落 HTML 切成 RawBlock；目录不足则整本 auto 均分。"""
    paras = _PARA_SPLIT_RE.findall(body_html)
    if not paras:
        return []
    if len(toc) < 2:
        return [bookdb.RawBlock(body_html, title=None, source="auto")]
    blocks = []
    for k, entry in enumerate(toc):
        # 首章从段落 0 开始，把标题前的版权页等内容并入首章
        lo = entry["p"] if k > 0 else 0
        hi = toc[k + 1]["p"] if k + 1 < len(toc) else len(paras)
        blocks.append(bookdb.RawBlock("".join(paras[lo:hi]),
                                      title=entry["t"], source="native"))
    return blocks


def parse_txt_blocks(rel):
    """TXT：完整读取 → 统一 HTML → 按章节正则目录切块。"""
    text = _read_full_text(os.path.join(BASE, rel))
    if not text.strip():
        return []
    body_html, toc_json = text_to_html(text)
    return _html_to_blocks(body_html, json.loads(toc_json))


def parse_mobi_blocks(rel):
    """MOBI：优先原生 NCX 目录，无 NCX 退回文本正则；段落索引已对齐。"""
    text, ncx_toc = mobi_to_text(os.path.join(BASE, rel))
    if not text.strip():
        return []
    body_html, toc_json = text_to_html(text)
    toc = ncx_toc if len(ncx_toc) >= 2 else json.loads(toc_json)
    return _html_to_blocks(body_html, toc)


# ---------------- 分章入库：EPUB 解析（纯标准库） ----------------

_OPF_NS = "{http://www.idpf.org/2007/opf}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"
_NCX_NS = "{http://www.daisy.org/z3986/2005/ncx/}"
_BLOCK_TAGS = ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
               "blockquote", "li", "tr", "section")
_HEAD_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


class _EpubHtmlCleaner(HTMLParser):
    """把 EPUB 内 xhtml 清洗成统一 <p> 体系，同时记录锚点边界。

    self.seq 为有序项：("p", 段落HTML) 或 ("anchor", 锚点id)。
    """

    def __init__(self, base_dir, ukey):
        super().__init__(convert_charrefs=True)
        self.base_dir = base_dir
        self.ukey = ukey
        self.seq = []
        self.buf = []
        self.skip = 0
        self.is_heading = False
        self.align_center = False
        self.has_bold = False

    def _emit(self):
        text = "".join(self.buf).strip()
        if text:
            tag = '<p class="ch">' if self.is_heading else "<p>"
            self.seq.append(("p", tag + xesc(text).replace("\n", "<br>") + "</p>"))
        self.buf = []

    def _img_url(self, src):
        src = unquote(src or "").lstrip("/")
        path = posixpath.normpath(posixpath.join(self.base_dir, src))
        return "/u/%s/%s" % (self.ukey, path)

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip:
            return
        anchor_id = ad.get("id") or ad.get("name")
        if anchor_id:
            self._emit()
            self.seq.append(("anchor", anchor_id))
        if tag in _BLOCK_TAGS:
            self._emit()
            self.is_heading = tag in _HEAD_TAGS
            self.align_center = ad.get("align", "") == "center"
            self.has_bold = False
        elif tag == "br":
            self.buf.append("\n")
        elif tag == "img":
            self._emit()
            url = self._img_url(ad.get("src", ""))
            alt = xesc(ad.get("alt", ""))
            self.seq.append(("p", '<p><img src="%s" alt="%s"></p>' % (url, alt)))
        elif tag in ("b", "strong"):
            self.has_bold = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS:
            if self.align_center and self.has_bold and len("".join(self.buf).strip()) <= 30:
                self.is_heading = True
            self._emit()
            self.is_heading = False

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)


def _join_epub(base_dir, href):
    href = (href or "").lstrip("/")
    if base_dir:
        return posixpath.normpath(posixpath.join(base_dir, href))
    return posixpath.normpath(href)


def _epub_rootfile(z):
    container = z.read("META-INF/container.xml").decode("utf-8", "replace")
    m = re.search(r'<rootfile\b[^>]*full-path="([^"]+)"', container)
    if not m:
        raise ValueError("container.xml 缺少 rootfile")
    return m.group(1)


def _parse_opf(opf_xml):
    """返回 (manifest, spine_idref列表, toc_id, title, creator)。"""
    root = _ET.fromstring(opf_xml)
    manifest = {}
    manifest_el = root.find(_OPF_NS + "manifest")
    if manifest_el is not None:
        for item in manifest_el:
            manifest[item.get("id")] = {
                "href": item.get("href"),
                "media": item.get("media-type"),
                "properties": item.get("properties"),
            }
    spine_ids = []
    spine_el = root.find(_OPF_NS + "spine")
    toc_id = spine_el.get("toc") if spine_el is not None else None
    if spine_el is not None:
        for ir in spine_el:
            spine_ids.append(ir.get("idref"))
    meta_el = root.find(_OPF_NS + "metadata")
    title = creator = None
    if meta_el is not None:
        t = meta_el.find(_DC_NS + "title")
        c = meta_el.find(_DC_NS + "creator")
        title = t.text if t is not None else None
        creator = c.text if c is not None else None
    return manifest, spine_ids, toc_id, title, creator


def _parse_ncx(ncx_xml):
    """按文档顺序展开 navPoint，返回 [{label, src}]，跳过目录根项。"""
    root = _ET.fromstring(ncx_xml)
    navmap = root.find(_NCX_NS + "navMap")
    if navmap is None:
        return []
    out = []

    def walk(np):
        label_el = np.find(_NCX_NS + "navLabel/" + _NCX_NS + "text")
        label = label_el.text if label_el is not None and label_el.text else ""
        content = np.find(_NCX_NS + "content")
        src = content.get("src") if content is not None else ""
        out.append({"label": label.strip(), "src": src})
        for child in np.findall(_NCX_NS + "navPoint"):
            walk(child)

    for np in navmap:
        walk(np)
    # 跳过指向目录页的根项
    if out and out[0]["label"].lower() in ("目录", "toc", "contents"):
        out = out[1:]
    return out


def _split_src(src):
    f, _, anchor = (src or "").partition("#")
    return f, anchor


def _split_seq_by_anchors(seq, anchors, file_label):
    """按 NCX 锚点把清洗序列切成 (title, 段落HTML) 列表。"""
    target = {aid: label for aid, label in anchors}
    groups = []
    cur, cur_title = [], file_label
    for typ, val in seq:
        if typ == "anchor" and val in target:
            if cur:
                groups.append((cur_title, "".join(cur)))
                cur = []
            cur_title = target[val]
        elif typ == "p":
            cur.append(val)
    if cur:
        groups.append((cur_title, "".join(cur)))
    return groups


def parse_epub_blocks(rel):
    """EPUB：container→OPF→spine/NCX，按逻辑章锚点切块，图片改 /u/ 按需取。"""
    p = os.path.join(BASE, rel)
    ukey = u_key(rel)
    with zipfile.ZipFile(p) as z:
        opf_href = _epub_rootfile(z)
        opf_dir = posixpath.dirname(opf_href)
        manifest, spine_ids, toc_id, title, creator = _parse_opf(
            z.read(opf_href).decode("utf-8", "replace"))

        # 定位 NCX 文件
        ncx_href = None
        if toc_id and toc_id in manifest:
            ncx_href = manifest[toc_id]["href"]
        if ncx_href is None:
            for m in manifest.values():
                if m["media"] == "application/x-dtbncx+xml":
                    ncx_href = m["href"]
                    break
        ncx = []
        if ncx_href:
            ncx_path = _join_epub(opf_dir, ncx_href)
            try:
                ncx = _parse_ncx(z.read(ncx_path).decode("utf-8", "replace"))
            except Exception:
                ncx = []

        blocks = []
        for sid in spine_ids:
            item = manifest.get(sid)
            if not item:
                continue
            href = _join_epub(opf_dir, item["href"])
            if not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            base_dir = posixpath.dirname(href)
            cleaner = _EpubHtmlCleaner(base_dir, ukey)
            cleaner.feed(z.read(href).decode("utf-8", "replace"))

            # 收集落在该文件的 NCX 锚点（保持 NCX 顺序）
            anchors, file_label = [], None
            for e in ncx:
                f, anchor = _split_src(e["src"])
                if _join_epub(opf_dir, f) != href:
                    continue
                if anchor:
                    anchors.append((anchor, e["label"]))
                elif file_label is None:
                    file_label = e["label"]

            if anchors:
                for btitle, bhtml in _split_seq_by_anchors(
                        cleaner.seq, anchors, file_label):
                    if bhtml:
                        blocks.append(bookdb.RawBlock(bhtml, title=btitle,
                                                      source="native"))
            else:
                bhtml = "".join(v for t, v in cleaner.seq if t == "p")
                if bhtml:
                    blocks.append(bookdb.RawBlock(bhtml, title=file_label,
                                                  source="native"))
        return blocks


# ---------------- azw3（KF8）分章解析 ----------------
# KF8 完整解析约 10 秒，缓存 KF8Book 实例（按 path/mtime/size），避免每次取图重解析
_KF8_BOOK_CACHE = {}


def _get_kf8_book(path):
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    book = _KF8_BOOK_CACHE.get(key)
    if book is None:
        book = kf8.KF8Book(path=path)
        if len(_KF8_BOOK_CACHE) >= 3:
            _KF8_BOOK_CACHE.clear()
        _KF8_BOOK_CACHE[key] = book
    return book


class _Kf8HtmlCleaner(HTMLParser):
    """把 KF8 单个 part 的 XHTML 清洗成统一片段。

    输出 self.seq：
      ("p", 段落/图片HTML)
      ("h", 标题文字, 标题HTML)   # 标题HTML 为空串表示隐藏边界（扉页/版权页）
    head 内的 kindle:flow CSS 全部丢弃，改用阅读器自带样式。
    """

    def __init__(self, ukey):
        super().__init__(convert_charrefs=True)
        self.ukey = ukey
        self.seq = []
        self.buf = []
        self.in_head = False
        self.skip = 0
        self.is_heading = False
        self.heading_title = None
        self.heading_hidden = False

    def _emit_p(self):
        text = "".join(self.buf).strip()
        if text:
            self.seq.append(
                ("p", "<p>" + xesc(text).replace("\n", "<br>") + "</p>"))
        self.buf = []

    @staticmethod
    def _embed_num(src):
        # src 形如 kindle:embed:0002?mime=image/jpg，编号为 Base32
        m = re.search(r"kindle:embed:([0-9A-V]+)", src or "")
        return int(m.group(1), 32) if m else None

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag == "head":
            self.in_head = True
            return
        if self.in_head:
            return
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS:
            self._emit_p()
            self.is_heading = tag in _HEAD_TAGS
            if self.is_heading:
                # hidden 空标题的真实标题在 title 属性里（扉页/版权页）
                self.heading_title = ad.get("title")
                self.heading_hidden = ad.get("hidden") is not None
        elif tag == "br":
            self.buf.append("\n")
        elif tag == "img":
            self._emit_p()
            num = self._embed_num(ad.get("src", ""))
            alt = xesc(ad.get("alt", ""))
            if num is not None:
                url = "/kimg/%s/%d" % (self.ukey, num)
                self.seq.append(
                    ("p", '<p><img src="%s" alt="%s"></p>' % (url, alt)))

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False
            return
        if self.in_head:
            return
        if tag in ("script", "style"):
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag in _BLOCK_TAGS:
            text = "".join(self.buf).strip()
            if self.is_heading:
                htext = text or self.heading_title or ""
                if htext:
                    if self.heading_hidden or not text:
                        # 隐藏空标题：只给边界，不输出可见 HTML
                        self.seq.append(("h", htext, ""))
                    else:
                        html = '<p class="ch">' + xesc(text) + "</p>"
                        self.seq.append(("h", htext, html))
            else:
                self._emit_p()
            self.is_heading = False
            self.heading_title = None
            self.heading_hidden = False
            self.buf = []

    def handle_data(self, data):
        if not self.in_head and not self.skip:
            self.buf.append(data)


def _norm_title(s):
    """标题规范化：去脚注[数字]标记与所有空白，用于和 NCX 标题匹配。"""
    s = re.sub(r"\[\d+\]", "", s)
    return re.sub(r"\s+", "", s)


def parse_azw3_blocks(rel):
    """azw3(KF8)：解压重组 part，按 NCX 真实标题在 part 内切分，图片走 /kimg。"""
    path = os.path.join(BASE, rel)
    ukey = u_key(rel)
    book = _get_kf8_book(path)

    # NCX 真实标题 -> 规范化键映射（保留首次出现的原始标题）
    ncx_map = {}
    for t in book.toc:
        ncx_map.setdefault(_norm_title(t["title"]), t["title"])

    used = set()
    blocks = []
    cur_html, cur_title = [], None

    def flush():
        html = "".join(cur_html).strip()
        if html:
            blocks.append(bookdb.RawBlock(
                html, title=cur_title,
                source="native" if cur_title else "auto"))

    for part in book.parts:
        cleaner = _Kf8HtmlCleaner(ukey)
        cleaner.feed(part.decode("utf-8", "replace"))
        for item in cleaner.seq:
            if item[0] == "h":
                _, htext, hhtml = item
                key = _norm_title(htext)
                real = ncx_map.get(key)
                if real and key not in used:
                    # 命中 NCX：章节边界，先封存当前块
                    flush()
                    cur_html, cur_title = [], real
                    used.add(key)
                    if hhtml:
                        cur_html.append(hhtml)
                elif hhtml:
                    # 副标题或重复标题：不切分，作为普通段落保留
                    cur_html.append(hhtml)
            else:
                cur_html.append(item[1])
    flush()
    return blocks


# ---------------- 分章入库：构建与后台 worker ----------------

_CHAPTER_EXTS = ("txt", "mobi", "epub")
_SKIP_EXTS = ("pdf",)
_BACKOFF = (2, 5, 15)          # 三次尝试前的退避秒数
_MAX_ATTEMPTS = 3


def _file_hash(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_chapters(rel):
    """解析单书并入库。parsed 返回 True；不支持/空内容标记 skipped 返回 False。"""
    ext = rel.rsplit(".", 1)[-1].lower()
    p = os.path.join(BASE, rel)
    if ext in _SKIP_EXTS or bookdb.get_parser(ext) is None:
        bookdb.upsert_book(rel, ext, "skipped")
        bookdb.set_status(rel, "skipped", "格式不入库或无解析器")
        return False
    st = os.stat(p)
    # 先确保 books 行存在并置 parsing，后续任何 set_status 都能生效
    bookdb.upsert_book(rel, ext, "parsing",
                       size=st.st_size, mtime_ns=st.st_mtime_ns)
    chash = _file_hash(p)
    blocks = bookdb.get_parser(ext)(rel)
    chapters = bookdb.normalize_blocks(blocks)
    if not chapters:
        bookdb.set_status(rel, "skipped", "解析为空")
        return False
    bookdb.save_chapters(rel, ext, chapters, content_hash=chash,
                         meta={"size": st.st_size, "mtime_ns": st.st_mtime_ns})
    return True


def _lower_priority():
    """把当前线程降到最低调度优先级：后台解析/扫描不能跟前台请求抢这颗单核。"""
    try:
        os.nice(19)                    # Linux 上 nice 是按线程生效的，不影响主线程
    except (OSError, AttributeError):
        pass
    try:
        import subprocess
        tid = threading.get_native_id()
        subprocess.call(["/usr/bin/ionice", "-t", "-c3", "-p", str(tid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _worker_loop():
    _lower_priority()
    while True:
        claimed = bookdb.claim_job()
        if claimed is None:
            time.sleep(0.25)                  # 空闲，降低轮询开销
            continue
        rel, attempts = claimed
        try:
            bookdb.set_status(rel, "parsing")
            build_chapters(rel)               # skipped 情况内部已处理
            bookdb.finish_job(rel, True)
        except Exception as e:
            err = ("%s: %s" % (type(e).__name__, e))[:300]
            fatal = isinstance(e, (zipfile.BadZipFile, KeyError,
                                   FileNotFoundError, ValueError))
            if fatal:
                bookdb.set_status(rel, "skipped", err)
                bookdb.finish_job(rel, True, err)
            elif attempts + 1 >= _MAX_ATTEMPTS:
                bookdb.set_status(rel, "failed", err)
                bookdb.finish_job(rel, False, err)
            else:
                bookdb.set_status(rel, "failed", err)
                bookdb.requeue_job(rel)
                time.sleep(_BACKOFF[min(attempts, len(_BACKOFF) - 1)])
        else:
            time.sleep(0.05)                  # 让单核，避免挤占前台


def start_worker():
    t = threading.Thread(target=_worker_loop, name="chapter-worker", daemon=True)
    t.start()
    return t


def enqueue_existing(books):
    """服务启动：未 parsed 的书补入队列（priority=100），PDF 跳过。"""
    n = 0
    for b in books:
        rel, ext = b["rel"], b["ext"]
        if ext in _SKIP_EXTS:
            continue
        stt = bookdb.get_status(rel)
        if stt is None or stt["parse_status"] != "parsed":
            bookdb.enqueue(rel, 100)
            n += 1
    return n


# ==================== 每日阅读统计 ====================
DAILY_FILE = os.path.join(os.path.dirname(PROGRESS_FILE), "daily.json")
_daily = {"days": {}}
_daily_lock = threading.Lock()
_daily_last = [0.0]


def load_daily():
    try:
        with open(DAILY_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d.get("days"), dict):
            _daily["days"] = d["days"]
    except (OSError, ValueError):
        pass


def _flush_daily():
    tmp = DAILY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_daily, fh)
        os.replace(tmp, DAILY_FILE)
        _daily_last[0] = time.time()
    except OSError:
        pass


def add_daily(sec):
    """把本次上报的阅读秒数累加进今天的桶，落盘限流 30 秒保护 SD 卡。"""
    if sec <= 0:
        return
    key = time.strftime("%Y-%m-%d")
    with _daily_lock:
        _daily["days"][key] = round(float(_daily["days"].get(key) or 0) + sec, 1)
        now = time.time()
        if now - _daily_last[0] >= 30:
            _flush_daily()


def daily_data():
    with _daily_lock:
        return dict(_daily["days"])


# ==================== 目录监听自动入库 ====================
def _known_rels():
    rels = set()
    for root, dirs, files in os.walk(BASE):
        for f in files:
            if f.lower().endswith(EXTS):
                rels.add(os.path.relpath(os.path.join(root, f), BASE).replace(os.sep, "/"))
    return rels


def _watch_loop():
    _lower_priority()
    known = _known_rels()
    while True:
        time.sleep(20)
        try:
            cur = _known_rels()
            if cur != known:
                added = cur - known
                known = cur
                books = scan_books(force=True)
                for b in books:
                    if b["rel"] in added and b["ext"] not in _SKIP_EXTS:
                        bookdb.enqueue(b["rel"], 100)
        except Exception:
            time.sleep(10)


def start_watcher():
    t = threading.Thread(target=_watch_loop, name="lib-watcher", daemon=True)
    t.start()
    return t


# ==================== 存储分析 / 重复 / 空目录 ====================
def storage_info():
    by_ext, by_dir, name_size, size_groups = {}, {}, {}, {}
    total, count = 0, 0
    empty_dirs = []
    for root, dirs, files in os.walk(BASE):
        rel_dir = os.path.relpath(root, BASE).replace(os.sep, "/")
        if not dirs and not files and rel_dir != ".":
            empty_dirs.append(rel_dir)
        for f in files:
            if not f.lower().endswith(EXTS):
                continue
            p = os.path.join(root, f)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            rel = os.path.relpath(p, BASE).replace(os.sep, "/")
            top = rel.split("/", 1)[0] if "/" in rel else "(根目录)"
            ext = os.path.splitext(f)[1].lstrip(".").lower()
            e = by_ext.setdefault(ext, {"count": 0, "size": 0})
            e["count"] += 1
            e["size"] += sz
            d = by_dir.setdefault(top, {"count": 0, "size": 0})
            d["count"] += 1
            d["size"] += sz
            total += sz
            count += 1
            name_size.setdefault((f, sz), []).append(rel)
            size_groups.setdefault(sz, []).append(rel)
    dups = [{"name": k[0], "size": k[1], "rels": v}
            for k, v in name_size.items() if len(v) > 1]
    same_size = [{"size": sz, "rels": v}
                 for sz, v in size_groups.items() if len(v) > 1]
    return {
        "total": total, "count": count,
        "byExt": [{"ext": k, **v} for k, v in sorted(
            by_ext.items(), key=lambda kv: -kv[1]["size"])],
        "byDir": [{"dir": k, **v} for k, v in sorted(
            by_dir.items(), key=lambda kv: -kv[1]["size"])],
        "dups": dups, "sameSize": same_size, "emptyDirs": sorted(empty_dirs),
    }


def remove_empty_dirs():
    removed = []
    for root, dirs, files in os.walk(BASE, topdown=False):
        rel = os.path.relpath(root, BASE)
        if rel != "." and not os.listdir(root):
            os.rmdir(root)
            removed.append(rel.replace(os.sep, "/"))
    if removed:
        scan_books(force=True)
    return removed


# ==================== Legado 书源适配 ====================
def legado_origin(headers):
    host = headers.get("Host") or "192.168.0.121:8080"
    return "http://" + host


def legado_search(key, origin):
    key = (key or "").strip().lower()
    out = []
    for b in scan_books():
        if key and key not in b["name"].lower() and key not in b["rel"].lower():
            continue
        rel = quote(b["rel"], safe="/")
        out.append({
            "name": b["name"], "author": "", "kind": (b["dir"] or ""),
            "bookUrl": "%s/api/legado/info?rel=%s" % (origin, rel),
            "coverUrl": "%s/cover/%s" % (origin, rel),
            "intro": "%s · %s · 已读 %d%%" % (b["ext"].upper(), human_size(b["size"]), b["pct"]),
        })
    return out


def legado_info(rel, origin):
    if not os.path.isfile(os.path.join(BASE, rel)):
        return None
    return {
        "name": os.path.splitext(os.path.basename(rel))[0],
        "tocUrl": "%s/api/legado/toc?rel=%s" % (origin, quote(rel, safe="/")),
    }


def legado_toc(rel, origin):
    toc = bookdb.get_toc(rel) or []
    chapters = []
    for r in toc:
        chapters.append({
            "name": r["title"],
            "url": "%s/api/legado/content?rel=%s&i=%d"
                   % (origin, quote(rel, safe="/"), r["ordinal"]),
        })
    return {"chapters": chapters}


def legado_content(rel, idx):
    ch = bookdb.get_chapter(rel, idx)
    if not ch:
        return None
    return {"content": ch["content_html"]}


def legado_source(origin):
    """生成「阅读（Legado）」书源 JSON，规则全部走本服务的 JSON API。"""
    return {
        "bookSourceUrl": origin,
        "bookSourceName": "我的书库（树莓派）",
        "bookSourceType": 0,
        "enabled": True,
        "enabledExplore": False,
        "searchUrl": "{{origin}}/api/legado/search?key={{key}}",
        "ruleSearch": {
            "bookList": "$[*]",
            "name": "$.name", "author": "$.author", "intro": "$.intro",
            "kind": "$.kind", "bookUrl": "$.bookUrl", "coverUrl": "$.coverUrl",
        },
        "ruleBookInfo": {
            "name": "$.name", "tocUrl": "$.tocUrl", "canReName": "true",
        },
        "ruleToc": {
            "chapterList": "$.chapters[*]",
            "chapterName": "$.name", "chapterUrl": "$.url",
        },
        "ruleContent": {"content": "$.content"},
    }


# ==================== PWA 资源 ====================
MANIFEST_JSON = {
    "name": "我的书库",
    "short_name": "书库",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f7f8fa",
    "theme_color": "#1a73e8",
    "icons": [
        {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml",
         "purpose": "any maskable"},
    ],
}

SW_JS = """// 书库 service worker：离线壳缓存
var CACHE = 'bookshelf-v1';
var SHELL = ['/', '/static/icon.svg'];
self.addEventListener('install', function(e){
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(function(c){ return c.addAll(SHELL); }).catch(function(){}));
});
self.addEventListener('activate', function(e){
  e.waitUntil(caches.keys().then(function(keys){
    return Promise.all(keys.filter(function(k){ return k !== CACHE; }).map(function(k){ return caches.delete(k); }));
  }).then(function(){ return self.clients.claim(); }));
});
self.addEventListener('fetch', function(e){
  if (e.request.method !== 'GET') return;
  var u = new URL(e.request.url);
  if (u.pathname === '/api/books') {
    // 书单：网络优先，离线时回退缓存
    e.respondWith(fetch(e.request).then(function(r){
      var copy = r.clone();
      caches.open(CACHE).then(function(c){ c.put(e.request, copy); }).catch(function(){});
      return r;
    }).catch(function(){ return caches.match(e.request); }));
    return;
  }
  e.respondWith(caches.match(e.request).then(function(hit){
    return hit || fetch(e.request).then(function(r){
      if (r && r.status === 200 && (u.pathname.indexOf('/static/') === 0)) {
        var copy = r.clone();
        caches.open(CACHE).then(function(c){ c.put(e.request, copy); }).catch(function(){});
      }
      return r;
    }).catch(function(){ return caches.match('/'); });
  }));
});
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="96" fill="#1a73e8"/>
<g fill="#fff"><rect x="128" y="104" width="112" height="304" rx="14"/>
<rect x="272" y="104" width="112" height="304" rx="14" opacity=".82"/></g>
<rect x="128" y="104" width="256" height="26" fill="#1557b0"/>
</svg>
"""


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>登录 · 我的书库</title>
<style>
:root{--bd:#e3e6ea;--fg:#1f2328;--mut:#6b7280}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
 font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg);background:#f7f8fa}
.box{width:100%;max-width:360px;background:#fff;border:1px solid var(--bd);border-radius:14px;padding:26px 22px;
 box-shadow:0 6px 24px rgba(31,35,40,.07)}
h1{font-size:20px;margin:0 0 6px;font-weight:600}
p.hint{color:var(--mut);font-size:13px;margin:0 0 18px}
input{width:100%;padding:12px 14px;border:1px solid var(--bd);border-radius:10px;font-size:16px;outline:none}
input:focus{border-color:#8ab4f8;box-shadow:0 0 0 3px rgba(138,180,248,.18)}
button{width:100%;margin-top:14px;padding:13px;border:0;border-radius:10px;background:#1a73e8;color:#fff;
 font-size:16px;font-weight:500;cursor:pointer}
button:active{filter:brightness(.94)}
.err{color:#c5221f;font-size:13px;margin-top:12px}
@media (max-width:480px){.box{padding:22px 18px;border-radius:12px}input,button{font-size:16px}}
</style></head><body>
<form class="box" method="post" action="/login">
  <h1>我的书库</h1>
  <p class="hint">输入访问码继续</p>
  <input type="hidden" name="next" value="__NEXT__">
  <input name="code" type="password" inputmode="text" autocomplete="current-password"
         placeholder="访问码" autofocus required>
  <button type="submit">进入书库</button>
  __ERR__
</form></body></html>
"""

HOME_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<link rel="manifest" href="/manifest.json">
<title>我的书库</title>
<style>
:root{--bd:#e3e6ea;--fg:#1f2328;--mut:#6b7280;--bg:#f7f8fa;--pri:#1a73e8}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg);background:var(--bg)}
header{position:sticky;top:0;z-index:9;background:#fff;border-bottom:1px solid var(--bd);padding:14px 18px;
 display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:600;white-space:nowrap}
#q{flex:1;min-width:180px;max-width:420px;padding:8px 12px;border:1px solid var(--bd);border-radius:8px;
 font-size:14px;outline:none;-webkit-appearance:none}
#q:focus{border-color:#8ab4f8;box-shadow:0 0 0 3px rgba(138,180,248,.18)}
#cnt{color:var(--mut);font-size:13px;white-space:nowrap}
main{padding:16px 18px;display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(152px,1fr))}
.card{background:#fff;border:1px solid var(--bd);border-radius:10px;padding:10px;display:flex;flex-direction:column;gap:8px}
.card:hover{border-color:#c9d4e3;box-shadow:0 2px 10px rgba(31,35,40,.06)}
.cover{position:relative;display:block;width:100%;aspect-ratio:2/3;border-radius:8px;overflow:hidden;
 background:linear-gradient(140deg,#eaf0f8,#d5e1ef);text-decoration:none}
.cover img{width:100%;height:100%;object-fit:cover;display:block}
.cover .ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
 font-size:30px;font-weight:600;color:#8ea7c2;letter-spacing:2px}
.cover .ext{position:absolute;right:6px;top:6px;background:rgba(255,255,255,.88);color:#41505f;
 font-size:10px;padding:1px 5px;border-radius:4px;text-transform:uppercase;letter-spacing:.5px}
.title{font-weight:600;font-size:14px;line-height:1.35;word-break:break-word;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.meta{color:var(--mut);font-size:12px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.tag{background:#eef2f7;border-radius:4px;padding:1px 6px;text-transform:uppercase;font-size:11px;color:#41505f}
.tag.pct{background:#e8f0fe;color:#1a73e8;text-transform:none}
.tag.when{background:#f1f3f5;color:#5f6672;text-transform:none}
.hisbar{padding:12px 18px 0;font-size:14px}
.hisbar a{color:var(--pri);text-decoration:none;margin-right:18px}
.path{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}
.acts{display:flex;gap:8px;margin-top:2px;flex-wrap:wrap}
a.btn,.btn{font-size:13px;text-decoration:none;padding:6px 12px;border-radius:6px;border:1px solid var(--bd);
 color:var(--fg);background:#fff;cursor:pointer;font-family:inherit;line-height:1.4;
 display:inline-flex;align-items:center;justify-content:center}
a.btn.pri{background:var(--pri);border-color:var(--pri);color:#fff}
a.btn:hover,.btn:hover{filter:brightness(.97)}
.btn.del{color:#8a8f98}
.btn.del:hover{color:#c5221f;border-color:#f0b7b5}
.btn.del.armed{color:#fff;background:#c5221f;border-color:#c5221f}
#upBtn,#viewBtn{background:#fff;border:1px solid var(--bd);border-radius:8px;padding:8px 14px;font-size:14px;
 cursor:pointer;color:var(--fg);font-family:inherit;white-space:nowrap}
#upBtn{display:inline-flex;align-items:center;justify-content:center;user-select:none;-webkit-user-select:none}
/* 文件选择框：视觉隐藏但仍在布局里（display:none 时部分浏览器不响应 label 点击） */
.fhidden{position:absolute;width:1px;height:1px;margin:-1px;padding:0;border:0;overflow:hidden;
 clip:rect(0 0 0 0);white-space:nowrap}
#upBtn:active,#viewBtn:active{border-color:#8ab4f8}#viewBtn{min-width:64px}
#prog{position:relative;display:inline-block;width:120px;height:8px;background:#e8ecf1;border-radius:4px;overflow:hidden;vertical-align:middle}
#bar{position:absolute;left:0;top:0;bottom:0;width:0;background:var(--pri);transition:width .15s}
#ptxt{position:absolute;left:128px;top:-5px;font-size:12px;color:var(--mut);white-space:nowrap}
.hide{display:none!important}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:24px;max-width:90%;background:#1f2328;color:#fff;
 padding:10px 16px;border-radius:8px;font-size:13px;line-height:1.5;z-index:99;box-shadow:0 4px 16px rgba(0,0,0,.25)}
.empty{grid-column:1/-1;text-align:center;color:var(--mut);padding:48px 0;line-height:1.8}

/* ---------- 分类条 ---------- */
.cats{display:flex;flex-wrap:wrap;gap:8px;padding:10px 18px 2px}
.chip{flex:0 0 auto;font-size:13px;padding:6px 12px;border-radius:16px;border:1px solid var(--bd);
 background:#fff;color:var(--fg);cursor:pointer;white-space:nowrap;font-family:inherit;line-height:1.4;max-width:100%;
 overflow:hidden;text-overflow:ellipsis}
.chip.on{background:var(--pri);border-color:var(--pri);color:#fff}
.chip .n{opacity:.65;font-size:12px;margin-left:4px}
.chip.ghost{color:var(--mut)}

/* ---------- 移到分类弹层 ---------- */
.mv{position:fixed;inset:0;background:rgba(31,35,40,.4);z-index:120;display:flex;align-items:flex-end;justify-content:center}
.mvbox{width:100%;max-width:520px;background:#fff;border-radius:14px 14px 0 0;padding:16px;box-sizing:border-box;
 max-height:76vh;overflow:auto;box-shadow:0 -4px 20px rgba(0,0,0,.18)}
.mvh{font-weight:600;font-size:15px}
.mvsub{color:var(--mut);font-size:12.5px;margin:3px 0 12px;word-break:break-all}
.mvl{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.mvi{display:flex;gap:8px;margin-bottom:12px}
.mvi input{flex:1;min-width:0;padding:9px 12px;font-size:15px;border:1px solid var(--bd);border-radius:8px;font-family:inherit}
.mvb{display:flex;gap:8px}
.mvb .btn{flex:1;padding:10px 12px}

/* ---------- 书名列表视图 ---------- */
body[data-view="list"] main{grid-template-columns:1fr;gap:8px;min-width:0}
body[data-view="list"] .row{display:flex;gap:12px;align-items:center;background:#fff;border:1px solid var(--bd);
 border-radius:10px;padding:10px 14px;min-width:0}
body[data-view="list"] .row:hover{border-color:#c9d4e3;box-shadow:0 1px 6px rgba(31,35,40,.05)}
.rt{flex:1;min-width:0;cursor:pointer}
.rname{font-weight:600;font-size:14px;line-height:1.45;word-break:break-word;
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.rsub{color:var(--mut);font-size:12px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.racts{display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end;min-width:0}
body[data-view="list"] .racts a,body[data-view="list"] .racts .btn{padding:6px 10px;font-size:12.5px;min-height:32px;flex:0 1 auto}

/* ---------- 手机端适配 ---------- */
@media (max-width:640px){
  header{padding:10px 12px;gap:8px;position:sticky}
  h1{font-size:16px}
  #cnt{order:2;margin-left:auto;font-size:12px}
  #upBtn{order:3;padding:9px 12px;font-size:14px}
  #q{order:4;flex-basis:100%;min-width:0;max-width:none;padding:10px 12px;font-size:16px}
  #prog{order:5;flex-basis:100%;width:100%}
  #ptxt{position:static;display:block;margin-top:2px}
  main{padding:12px;gap:10px;grid-template-columns:repeat(2,1fr)}
  .hisbar{padding:10px 12px 0}
  .card{padding:9px;gap:7px;border-radius:12px}
  .title{font-size:13.5px}
  .meta{font-size:11.5px;gap:4px}
  .acts{gap:6px}
  a.btn,.btn{padding:8px 4px;font-size:12px;min-height:38px;border-radius:7px;flex:1}
  .btn.del{flex:1;min-width:0}
  #viewBtn{order:3;padding:9px 10px;font-size:14px;min-width:58px}
  body[data-view="list"] main{padding:10px 12px;gap:7px}
  body[data-view="list"] .row{padding:9px 10px;gap:6px;flex-wrap:wrap}
  body[data-view="list"] .rt{flex:1 1 100%;width:100%}
  body[data-view="list"] .racts{flex:1 1 100%;justify-content:flex-start;gap:6px}
  body[data-view="list"] .racts a,body[data-view="list"] .racts .btn{flex:0 1 auto;padding:7px 8px;font-size:11.5px;min-height:32px}
  body[data-view="list"] .rname{font-size:13.5px;-webkit-line-clamp:2}
  .toast{bottom:16px;font-size:13.5px;width:calc(100% - 24px);text-align:center}
  .empty{padding:32px 4px;font-size:14px}
  .cats{padding:8px 12px 0;gap:7px}
  .chip{font-size:12.5px;padding:7px 11px}
  .mvbox{border-radius:12px 12px 0 0;padding:14px}
}
</style></head><body>
<header><h1>我的书库</h1><input id="q" type="search" placeholder="搜索书名或目录…" autocomplete="off">
<span id="cnt"></span>
<button id="viewBtn" type="button">列表</button>
<label id="upBtn" for="file">上传书籍</label>
<input type="file" id="file" multiple accept=".epub,.pdf,.mobi,.azw3,.txt,.cbz,.cbr,.djvu,.fb2" class="fhidden">
<span id="prog" class="hide"><span id="bar"></span><span id="ptxt"></span></span></header>
<div class="hisbar"><a href="/history">阅读记录 ›</a><a href="/stats">阅读统计 ›</a><a href="/storage">存储分析 ›</a><a href="/api/legado">Legado书源 ›</a><a href="/trash">回收站 ›</a></div>
<div class="cats" id="cats"></div>
<main id="list"></main>
<div class="mv hide" id="mv">
  <div class="mvbox">
    <div class="mvh">移到分类</div>
    <div class="mvsub" id="mvname"></div>
    <div class="mvl" id="mvlist"></div>
    <div class="mvi"><input id="mvnew" type="text" placeholder="新建分类，如：科幻" autocomplete="off">
      <button class="btn" type="button" id="mvnewbtn">新建并移入</button></div>
    <div class="mvb"><button class="btn" type="button" id="mvcancel">取消</button></div>
  </div>
</div>
<script>
var ALL = [];
var DIRS = [];
var CAT = "__all";
var CUR = null;
var VIEW = "list";
try { VIEW = localStorage.getItem("bookView") || "list"; } catch(e){}
try { if (!localStorage.getItem("bookViewV2")) { VIEW = "list"; localStorage.setItem("bookView", "list"); localStorage.setItem("bookViewV2", "1"); } } catch(e){}
if (VIEW !== "list") VIEW = "grid";
function setView(v){
  VIEW = (v === "list") ? "list" : "grid";
  document.body.setAttribute("data-view", VIEW);
  var vb = document.getElementById("viewBtn");
  if (vb) vb.textContent = (VIEW === "list" ? "封面" : "列表");
  try { localStorage.setItem("bookView", VIEW); } catch(e){}
  render();
}
function hs(n){var u=['B','KB','MB','GB'],i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return (i?n.toFixed(1):n)+u[i]}
function when(ts){
  if(!ts) return '';
  var d = Math.floor((Date.now()/1000) - ts);
  if(d < 60) return '刚刚';
  if(d < 3600) return Math.floor(d/60)+' 分钟前';
  if(d < 86400) return Math.floor(d/3600)+' 小时前';
  if(d < 86400*30) return Math.floor(d/86400)+' 天前';
  var dt = new Date(ts*1000);
  return (dt.getMonth()+1)+'月'+dt.getDate()+'日';
}
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function render(){
  var q = document.getElementById('q').value.trim().toLowerCase();
  var list = ALL.filter(function(b){
    if (CAT !== "__all"){
      var d = b.dir || "";
      if (CAT === "__root"){ if (d) return false; }
      else if (d !== CAT) return false;
    }
    return !q || (b.name+' '+b.rel).toLowerCase().indexOf(q)>=0;
  });
  document.getElementById('cnt').textContent = list.length + ' / ' + ALL.length + ' 本';
  var el = document.getElementById('list');
  if(!list.length){
    el.innerHTML = '<div class="empty">'+(ALL.length?'没有匹配的书':'书库还是空的，点右上角「上传书籍」加几本')+'</div>';
    return;
  }
  if (VIEW === "list"){
    el.innerHTML = list.map(function(b){
      var canRead = b.ext==='epub'||b.ext==='pdf'||b.ext==='txt'||b.ext==='mobi'||b.ext==='azw3';
      var enc = encodeURI(b.rel);
      var link = canRead ? '/read/'+enc : '/file/'+enc;
      return '<div class="row">'+
        '<div class="rt" data-go="'+link+'"><div class="rname">'+esc(b.name)+'</div>'+
        '<div class="rsub">'+(b.dir && CAT==="__all" ? esc(b.dir)+' · ' : '')+b.ext+' · '+hs(b.size)+
        (b.pct?' · 已读 '+b.pct+'%':'')+(b.readAt?' · '+when(b.readAt):'')+'</div></div>'+
        '<div class="racts">'+(canRead?'<a class="btn pri" href="/read/'+enc+'">阅读</a>':'')+
        '<a class="btn" href="/file/'+enc+'" download>下载</a>'+
        '<button class="btn" type="button" data-move="'+esc(b.rel)+'">分类</button>'+
        '<button class="btn del" type="button" data-rel="'+esc(b.rel)+'">删除</button></div></div>';
    }).join('');
    return;
  }
  el.innerHTML = list.map(function(b){
    var canRead = b.ext==='epub'||b.ext==='pdf'||b.ext==='txt'||b.ext==='mobi';
    var enc = encodeURI(b.rel);
    var link = canRead ? '/read/'+enc : '/file/'+enc;
    return '<div class="card">'+
      '<a class="cover" href="'+link+'"><span class="ph">'+esc(String(b.name||'?').slice(0,1))+'</span>'+
        '<img loading="lazy" decoding="async" src="/cover/'+enc+'" alt="" onerror="this.remove()">'+
        '<span class="ext">'+b.ext+'</span></a>'+
      '<div class="title">'+esc(b.name)+'</div>'+
      '<div class="meta"><span>'+hs(b.size)+'</span>'+
      (b.dir && CAT==="__all" ? '<span class="tag">'+esc(b.dir)+'</span>' : '')+
      (b.pct ? '<span class="tag pct">已读 '+b.pct+'%</span>' : '')+
      (b.readAt ? '<span class="tag when">'+when(b.readAt)+'</span>' : '')+'</div>'+
      '<div class="acts">'+(canRead?'<a class="btn pri" href="/read/'+enc+'">阅读</a>':'')+
      '<a class="btn" href="/file/'+enc+'" download>下载</a>'+
      '<button class="btn" type="button" data-move="'+esc(b.rel)+'">分类</button>'+
      '<button class="btn del" type="button" data-rel="'+esc(b.rel)+'">删除</button></div></div>';
  }).join('');
}
/* ---------- 分类 ---------- */
function postJSON(url, data){
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)})
    .then(function(r){ return r.json().then(function(j){ return j||{} }).catch(function(){ return {error:'服务器返回异常（HTTP '+r.status+'）'} }) })
    .catch(function(e){ return {error:String(e)} });
}
function buildCats(){
  var el = document.getElementById('cats');
  if(!el) return;
  var groups = {}, un = 0;
  ALL.forEach(function(b){ var d=b.dir||""; if(!d){ un++; return; } groups[d]=(groups[d]||0)+1; });
  DIRS.forEach(function(x){ if(x && x.dir && groups[x.dir]===undefined) groups[x.dir]=0; });
  var names = Object.keys(groups).sort(function(a,b){ return a.localeCompare(b,'zh'); });
  var h = '<button class="chip'+(CAT==='__all'?' on':'')+'" type="button" data-cat="__all">全部<span class="n">'+ALL.length+'</span></button>';
  if(un || CAT==='__root') h += '<button class="chip'+(CAT==='__root'?' on':'')+'" type="button" data-cat="__root">未分类<span class="n">'+un+'</span></button>';
  names.forEach(function(d){
    h += '<button class="chip'+(CAT===d?' on':'')+'" type="button" data-cat="'+esc(d)+'">'+esc(d)+'<span class="n">'+groups[d]+'</span></button>';
  });
  h += '<button class="chip ghost" type="button" data-new="1">＋ 新建分类</button>';
  el.innerHTML = h;
}
document.getElementById('cats').addEventListener('click', function(e){
  var t = e.target;
  while(t && t !== this){
    if(t.getAttribute){
      if(t.getAttribute('data-new')){ newCat(); return; }
      var c = t.getAttribute('data-cat');
      if(c){ CAT = c; buildCats(); render(); return; }
    }
    t = t.parentNode;
  }
});
function newCat(){
  var n = prompt('新建分类（会在书库里建同名文件夹）：');
  if(!n) return;
  postJSON('/mkdir', {name:n}).then(function(r){
    if(!r.ok){ toast(r.error||'创建失败'); return; }
    toast('已创建分类：'+(r.dir||n));
    load();
  });
}
function closeMove(){ document.getElementById('mv').classList.add('hide'); CUR = null; }
function openMove(rel){
  CUR = rel;
  document.getElementById('mvname').textContent = rel;
  var cur = rel.indexOf('/')>=0 ? rel.substring(0, rel.lastIndexOf('/')) : '';
  var seen = {}, dirs = [];
  ALL.forEach(function(b){ var d=b.dir||''; if(d && !seen[d]){ seen[d]=1; dirs.push(d); } });
  DIRS.forEach(function(x){ if(x && x.dir && !seen[x.dir]){ seen[x.dir]=1; dirs.push(x.dir); } });
  dirs.sort(function(a,b){ return a.localeCompare(b,'zh'); });
  var h = '<button class="chip'+(cur===''?' on':'')+'" type="button" data-mv="">未分类（书库根目录）</button>';
  dirs.forEach(function(d){
    h += '<button class="chip'+(d===cur?' on':'')+'" type="button" data-mv="'+esc(d)+'">'+esc(d)+'</button>';
  });
  document.getElementById('mvlist').innerHTML = h;
  document.getElementById('mvnew').value = '';
  document.getElementById('mv').classList.remove('hide');
}
function doMove(dir){
  var rel = CUR;
  if(!rel) return;
  closeMove();
  postJSON('/move', {rel:rel, dir:dir}).then(function(r){
    if(!r.ok){ toast('移动失败：'+(r.error||'未知错误')); return; }
    toast('已移到：'+(dir || '未分类'));
    load();
  });
}
document.getElementById('mvlist').addEventListener('click', function(e){
  var t = e.target;
  while(t && t !== this){
    if(t.getAttribute && t.getAttribute('data-mv') !== null){ doMove(t.getAttribute('data-mv')); return; }
    t = t.parentNode;
  }
});
document.getElementById('mvcancel').onclick = closeMove;
document.getElementById('mv').addEventListener('click', function(e){ if(e.target === this) closeMove(); });
document.getElementById('mvnewbtn').onclick = function(){
  var n = document.getElementById('mvnew').value.trim();
  if(!n){ toast('先填一个分类名'); return; }
  var rel = CUR;
  postJSON('/mkdir', {name:n}).then(function(r){
    var dir = r.dir || n;
    if(!r.ok && !/已存在/.test(r.error||'')){ toast(r.error||'创建失败'); return; }
    closeMove();
    postJSON('/move', {rel:rel, dir:dir}).then(function(r2){
      if(!r2.ok){ toast('移动失败：'+(r2.error||'未知错误')); load(); return; }
      toast('已移到：'+dir);
      load();
    });
  });
};

document.getElementById('q').addEventListener('input', render);
document.getElementById('viewBtn').onclick = function(){ setView(VIEW === 'list' ? 'grid' : 'list') };
setView(VIEW);

function toast(msg){
  var t = document.createElement('div');
  t.className = 'toast'; t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t) }, 4000);
}

function load(){
  return Promise.all([
    fetch('/api/books').then(function(r){return r.json()}),
    fetch('/api/dirs').then(function(r){return r.json()}).catch(function(){return {dirs:[]}})
  ]).then(function(res){
    ALL = res[0] || [];
    DIRS = (res[1] && res[1].dirs) || [];
    if (CAT !== "__all" && CAT !== "__root"){
      var alive = ALL.some(function(b){ return (b.dir||"") === CAT; }) ||
                  DIRS.some(function(x){ return x && x.dir === CAT; });
      if (!alive) CAT = "__all";
    }
    buildCats(); render();
  }).catch(function(e){
    document.getElementById('list').innerHTML='<div class="empty">读取书库失败：'+esc(String(e))+'</div>';
  });
}

function del(btn){
  var rel = btn.getAttribute('data-rel');
  if(!btn.getAttribute('data-armed')){
    btn.setAttribute('data-armed','1');
    btn.textContent = '确认删除';
    btn.classList.add('armed');
    setTimeout(function(){
      if(btn.getAttribute('data-armed')){
        btn.removeAttribute('data-armed'); btn.textContent='删除'; btn.classList.remove('armed');
      }
    }, 3000);
    return;
  }
  btn.disabled = true; btn.textContent = '删除中…';
  fetch('/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rel: rel})})
    .then(function(r){ return r.json().then(function(j){ return {s:r.status, j:j} }) })
    .then(function(o){
      if(o.s===200 && o.j.ok){ toast('已移入回收站：'+o.j.name+'（可在「回收站」恢复）'); load(); }
      else { toast('删除失败：'+(o.j && o.j.error ? o.j.error : ('HTTP '+o.s)));
             btn.disabled=false; btn.removeAttribute('data-armed'); btn.textContent='删除'; btn.classList.remove('armed'); }
    })
    .catch(function(e){ toast('删除失败：'+e); btn.disabled=false; btn.textContent='删除'; btn.classList.remove('armed'); });
}
document.getElementById('list').addEventListener('click', function(e){
  var t = e.target;
  while(t && t !== this){
    if(t.classList && t.classList.contains('del')){ del(t); return; }
    if(t.getAttribute && t.getAttribute('data-move')){ openMove(t.getAttribute('data-move')); return; }
    t = t.parentNode;
  }
  var row = e.target.closest ? e.target.closest('.rt') : null;
  if (row && row.getAttribute('data-go')) location.href = row.getAttribute('data-go');
});

var fileInput = document.getElementById('file');
/* 上传入口是 <label for="file">，由浏览器原生触发；不再用 JS 去点 display:none 的 input，
   那样在部分手机浏览器/微信里会被拦掉，表现为按钮没反应 */
fileInput.onchange = function(){
  if(!fileInput.files || !fileInput.files.length) return;
  var fd = new FormData();
  for(var i=0;i<fileInput.files.length;i++) fd.append('files', fileInput.files[i]);
  var prog = document.getElementById('prog'), bar = document.getElementById('bar'), ptxt = document.getElementById('ptxt');
  prog.classList.remove('hide'); bar.style.width='0%'; ptxt.textContent='准备中';
  var xhr = new XMLHttpRequest();
  xhr.open('POST','/upload');
  xhr.upload.onprogress = function(e){
    if(e.lengthComputable){
      var p = Math.round(e.loaded/e.total*100);
      bar.style.width = p+'%'; ptxt.textContent = p+'%';
    }
  };
  xhr.onload = function(){
    prog.classList.add('hide');
    var r = null; try { r = JSON.parse(xhr.responseText) } catch(e){}
    if(xhr.status===200 && r){
      var msg = '已上传 '+(r.ok?r.ok.length:0)+' 个文件';
      if(r.ok && r.ok.length) msg += '：'+r.ok.join('、');
      if(r.fail && r.fail.length) msg += '；失败：'+r.fail.join('、');
      toast(msg);
    } else {
      toast('上传失败：'+(r&&r.error ? r.error : ('HTTP '+xhr.status)));
    }
    fileInput.value=''; load();
  };
  xhr.onerror = function(){ prog.classList.add('hide'); toast('上传失败：网络错误'); fileInput.value=''; };
  xhr.send(fd);
};
load();

/* ---- PWA：注册 service worker（HTTP 局域网下浏览器会拒绝，属预期） ---- */
if('serviceWorker' in navigator){
  window.addEventListener('load',function(){
    navigator.serviceWorker.register('/sw.js').catch(function(){});
  });
}
/* ---- 后台轮询：书单变化（含监听入库的新文件）时自动刷新 ---- */
var lastSig='';
setInterval(function(){
  if(document.hidden) return;
  fetch('/api/books').then(function(r){return r.text();}).then(function(t){
    var sig='';
    try{ sig=JSON.stringify(JSON.parse(t).map(function(b){return b.rel+b.size+b.pct;})); }catch(e){ return; }
    if(lastSig && sig!==lastSig){ location.reload(); }
    lastSig=sig;
  }).catch(function(){});
},20000);
</script></body></html>
"""

HISTORY_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>阅读记录 · 我的书库</title>
<style>
:root{--bd:#e3e6ea;--fg:#1f2328;--mut:#6b7280;--bg:#f7f8fa;--pri:#1a73e8}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg);background:var(--bg)}
header{position:sticky;top:0;z-index:9;background:#fff;border-bottom:1px solid var(--bd);padding:14px 18px;
 display:flex;gap:12px;align-items:center}
header a{color:var(--pri);text-decoration:none;font-size:14px;white-space:nowrap}
h1{font-size:18px;margin:0;font-weight:600}
#cnt{color:var(--mut);font-size:13px;margin-left:auto}
main{padding:16px 18px;display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{background:#fff;border:1px solid var(--bd);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:9px}
.title{font-weight:600;font-size:15px;line-height:1.35;word-break:break-word}
.meta{color:var(--mut);font-size:12.5px;display:flex;gap:10px;flex-wrap:wrap}
.track{height:6px;background:#eef1f5;border-radius:3px;overflow:hidden}
.track i{display:block;height:100%;background:var(--pri)}
a.btn{align-self:flex-start;font-size:13px;text-decoration:none;padding:7px 14px;border-radius:6px;background:var(--pri);color:#fff}
.empty{grid-column:1/-1;text-align:center;color:var(--mut);padding:48px 0;line-height:1.9}
@media (max-width:640px){
  header{padding:12px}
  main{padding:12px;grid-template-columns:1fr}
  a.btn{min-height:44px;display:inline-flex;align-items:center;padding:10px 16px}
}
</style></head><body>
<header><a href="/">‹ 书库</a><h1>阅读记录</h1><span id="cnt"></span></header>
<main id="list"></main>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function when(ts){
  if(!ts) return '—';
  var d = Math.floor((Date.now()/1000) - ts);
  if(d < 60) return '刚刚';
  if(d < 3600) return Math.floor(d/60)+' 分钟前';
  if(d < 86400) return Math.floor(d/3600)+' 小时前';
  if(d < 86400*30) return Math.floor(d/86400)+' 天前';
  var dt = new Date(ts*1000);
  return (dt.getMonth()+1)+'月'+dt.getDate()+'日';
}
function dur(sec){
  if(sec < 60) return '不到 1 分钟';
  var m = Math.round(sec/60);
  if(m < 60) return m + ' 分钟';
  return Math.floor(m/60) + ' 小时 ' + (m%60) + ' 分';
}
fetch('/api/books').then(function(r){return r.json()}).then(function(all){
  var list = all.filter(function(b){ return b.readAt || b.pct })
                 .sort(function(a,b){ return (b.readAt||0)-(a.readAt||0) });
  document.getElementById('cnt').textContent = list.length + ' 本有阅读记录';
  var el = document.getElementById('list');
  if(!list.length){
    el.innerHTML = '<div class="empty">还没有阅读记录<br>去书架里打开一本书，进度和时长会自动记下来</div>';
    return;
  }
  el.innerHTML = list.map(function(b){
    var enc = encodeURI(b.rel);
    var canRead = b.ext === 'epub' || b.ext === 'pdf' || b.ext === 'txt' || b.ext === 'mobi' || b.ext === 'azw3';
    return '<div class="card"><div class="title">'+esc(b.name)+'</div>'+
      '<div class="meta"><span>已读 '+b.pct+'%</span>'+
      '<span>累计 '+dur(b.readSec||0)+'</span>'+
      '<span>上次 '+when(b.readAt)+'</span>'+
      (b.readN ? '<span>打开 '+b.readN+' 次</span>' : '')+'</div>'+
      '<div class="track"><i style="width:'+Math.max(1,Math.min(100,b.pct||0))+'%"></i></div>'+
      (canRead ? '<a class="btn" href="/read/'+enc+'">继续阅读</a>' : '')+
      '</div>';
  }).join('');
}).catch(function(e){
  document.getElementById('list').innerHTML = '<div class="empty">读取失败：'+esc(String(e))+'</div>';
});
</script></body></html>
"""


TRASH_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>回收站 · 我的书库</title>
<style>
:root{--bd:#e3e6ea;--fg:#1f2328;--mut:#6b7280;--bg:#f7f8fa;--pri:#1a73e8}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg);background:var(--bg)}
header{position:sticky;top:0;z-index:9;background:#fff;border-bottom:1px solid var(--bd);padding:14px 18px;
 display:flex;gap:12px;align-items:center}
header a{color:var(--pri);text-decoration:none;font-size:14px;white-space:nowrap}
h1{font-size:18px;margin:0;font-weight:600}
#cnt{color:var(--mut);font-size:13px;margin-left:auto}
main{padding:16px 18px;display:grid;gap:12px;grid-template-columns:1fr}
.card{background:#fff;border:1px solid var(--bd);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:9px}
.title{font-weight:600;font-size:15px;word-break:break-word}
.meta{color:var(--mut);font-size:12.5px;display:flex;gap:10px;flex-wrap:wrap}
.acts{display:flex;gap:8px;flex-wrap:wrap}
.btn{font-size:13px;padding:8px 16px;border-radius:6px;border:1px solid var(--bd);background:#fff;
 color:var(--fg);cursor:pointer;font-family:inherit;min-height:38px}
.btn.pri{background:var(--pri);border-color:var(--pri);color:#fff}
.btn.danger{color:#8a8f98}
.btn.danger.armed{background:#c5221f;border-color:#c5221f;color:#fff}
.note{color:var(--mut);font-size:12.5px;padding:0 18px 8px}
.empty{text-align:center;color:var(--mut);padding:48px 0;line-height:1.9}
.toast{position:fixed;left:50%;transform:translateX(-50%);bottom:24px;max-width:90%;background:#1f2328;color:#fff;
 padding:10px 16px;border-radius:8px;font-size:13px;z-index:99}
@media (max-width:640px){
  header{padding:12px}
  main{padding:12px}
  .btn{flex:1;min-height:44px}
  .note{padding:0 12px 8px}
}
</style></head><body>
<header><a href="/">‹ 书库</a><h1>回收站</h1><span id="cnt"></span></header>
<div class="note">删除的书会先放到这里，30 天内可以恢复；到期自动清理。</div>
<main id="list"></main>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
function hs(n){var u=['B','KB','MB','GB'],i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return (i?n.toFixed(1):n)+u[i]}
function when(ts){
  if(!ts) return '—';
  var d = Math.floor((Date.now()/1000) - ts);
  if(d < 60) return '刚刚';
  if(d < 3600) return Math.floor(d/60)+' 分钟前';
  if(d < 86400) return Math.floor(d/3600)+' 小时前';
  return Math.floor(d/86400)+' 天前';
}
var ITEMS = [];
function toast(msg){
  var t = document.createElement('div'); t.className='toast'; t.textContent=msg;
  document.body.appendChild(t);
  setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t) }, 3500);
}
function render(){
  var el = document.getElementById('list');
  document.getElementById('cnt').textContent = ITEMS.length + ' 项';
  if(!ITEMS.length){
    el.innerHTML = '<div class="empty">回收站是空的<br>在书库里删除的书会先出现在这里</div>';
    return;
  }
  el.innerHTML = ITEMS.map(function(it){
    return '<div class="card"><div class="title">'+esc(it.name)+'</div>'+
      '<div class="meta"><span>'+hs(it.size)+'</span><span>'+esc(it.rel)+'</span>'+
      '<span>删除于 '+when(it.ts)+'</span></div>'+
      '<div class="acts">'+
      '<button class="btn pri" type="button" data-act="restore" data-id="'+esc(it.id)+'">恢复</button>'+
      '<button class="btn danger" type="button" data-act="purge" data-id="'+esc(it.id)+'">彻底删除</button>'+
      '</div></div>';
  }).join('');
}
function post(url, id){
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id: id})})
    .then(function(r){ return r.json().then(function(j){ return {s:r.status, j:j} }) });
}
document.getElementById('list').addEventListener('click', function(e){
  var btn = e.target;
  while(btn && btn !== this && !(btn.getAttribute && btn.getAttribute('data-act'))) btn = btn.parentNode;
  if(!btn || btn === this) return;
  var act = btn.getAttribute('data-act'), id = btn.getAttribute('data-id');
  if(act === 'restore'){
    btn.disabled = true; btn.textContent = '恢复中…';
    post('/restore', id).then(function(o){
      if(o.s === 200 && o.j.ok){ toast('已恢复到书库'); load(); }
      else { toast('恢复失败：'+(o.j && o.j.error ? o.j.error : ('HTTP '+o.s))); btn.disabled=false; btn.textContent='恢复'; }
    }).catch(function(err){ toast('恢复失败：'+err); btn.disabled=false; btn.textContent='恢复'; });
    return;
  }
  if(!btn.getAttribute('data-armed')){
    btn.setAttribute('data-armed','1'); btn.classList.add('armed'); btn.textContent='确认彻底删除';
    setTimeout(function(){
      if(btn.getAttribute('data-armed')){ btn.removeAttribute('data-armed'); btn.classList.remove('armed'); btn.textContent='彻底删除'; }
    }, 3000);
    return;
  }
  btn.disabled = true; btn.textContent = '删除中…';
  post('/purge', id).then(function(o){
    if(o.s === 200 && o.j.ok){ toast('已彻底删除'); load(); }
    else { toast('删除失败：'+(o.j && o.j.error ? o.j.error : ('HTTP '+o.s))); btn.disabled=false; btn.removeAttribute('data-armed'); btn.textContent='彻底删除'; }
  }).catch(function(err){ toast('删除失败：'+err); btn.disabled=false; btn.textContent='彻底删除'; });
});
function load(){
  return fetch('/api/trash').then(function(r){ return r.json() })
    .then(function(d){ ITEMS = d || []; render(); })
    .catch(function(e){ document.getElementById('list').innerHTML = '<div class="empty">读取失败：'+esc(String(e))+'</div>' });
}
load();
</script></body></html>
"""


STATS_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>阅读统计 · 我的书库</title>
<style>
:root{--bd:#e3e6ea;--mut:#6b7280;--pri:#1a73e8}
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
 color:#1f2328;background:#f7f8fa}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--bd);padding:12px 18px;
 display:flex;align-items:center;gap:12px}
h1{font-size:17px;margin:0;font-weight:600}
a.back{color:var(--pri);text-decoration:none;font-size:14px}
main{padding:18px;max-width:920px;margin:0 auto;display:grid;gap:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.sc{background:#fff;border:1px solid var(--bd);border-radius:12px;padding:14px}
.sc .n{font-size:24px;font-weight:700;margin-top:2px}
.sc .l{color:var(--mut);font-size:13px}
.box{background:#fff;border:1px solid var(--bd);border-radius:12px;padding:16px}
.box h2{font-size:15px;margin:0 0 12px}
.heat{display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,13px);gap:4px;overflow-x:auto}
.cell{width:13px;border-radius:3px;background:#eef1f4}
.legend{display:flex;gap:6px;align-items:center;color:var(--mut);font-size:12px;margin-top:10px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #eef0f3}
th{color:var(--mut);font-weight:500}
</style></head><body>
<header><a class="back" href="/">‹ 书库</a><h1>阅读统计</h1></header>
<main>
<div class="cards" id="cards"></div>
<div class="box"><h2>近 12 周热力图</h2><div class="heat" id="heat"></div>
 <div class="legend">少 <span class="cell" style="background:#c6dbfa"></span>
 <span class="cell" style="background:#84b4f3"></span><span class="cell" style="background:#3f86e8"></span>
 <span class="cell" style="background:#1557b0"></span> 多</div></div>
<div class="box"><h2>近 30 天明细</h2><table><thead><tr><th>日期</th><th>阅读时长</th></tr></thead>
 <tbody id="days"></tbody></table></div>
</main>
<script>
function fmt(sec){
  sec=Math.round(sec);
  var h=Math.floor(sec/3600), m=Math.round((sec%3600)/60);
  return (h?h+' 小时 ':'')+(m?m+' 分钟':(h?'':'0 分钟'));
}
function keyOf(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
fetch('/api/daily').then(function(r){return r.json();}).then(function(days){
  var entries=Object.keys(days.days||{}).map(function(k){return [k,days.days[k]];});
  var totalSec=entries.reduce(function(a,x){return a+x[1];},0);
  var today=keyOf(new Date());
  var cards=[
    ['累计阅读',fmt(totalSec)],['阅读天数',entries.length+' 天'],
    ['今天',fmt(days.days[today]||0)],
    ['日均',fmt(entries.length?totalSec/entries.length:0)],
  ];
  document.getElementById('cards').innerHTML=cards.map(function(x){
    return '<div class="sc"><div class="l">'+x[0]+'</div><div class="n">'+x[1]+'</div></div>';
  }).join('');
  var heat=document.getElementById('heat'), maxV=Math.max.apply(null,entries.map(function(x){return x[1];}).concat([1]));
  var end=new Date();
  var start=new Date(end); start.setDate(start.getDate()-83);
  start.setDate(start.getDate()-start.getDay());
  for(var d=new Date(start); d<=end; d.setDate(d.getDate()+1)){
    var v=days.days[keyOf(d)]||0, level=0;
    if(v>0){level=Math.min(4,1+Math.floor(v/maxV*4));}
    var colors=['#eef1f4','#c6dbfa','#84b4f3','#3f86e8','#1557b0'];
    var c=document.createElement('span'); c.className='cell'; c.style.background=colors[level];
    c.title=keyOf(d)+' '+fmt(v); heat.appendChild(c);
  }
  var tbody=document.getElementById('days');
  var rowHtml=[];
  for(var i=29;i>=0;i--){
    var dd=new Date(); dd.setDate(dd.getDate()-i); var k=keyOf(dd);
    rowHtml.push('<tr><td>'+k+'</td><td>'+fmt(days.days[k]||0)+'</td></tr>');
  }
  tbody.innerHTML=rowHtml.join('');
});
</script></body></html>
"""


STORAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>存储分析 · 我的书库</title>
<style>
:root{--bd:#e3e6ea;--mut:#6b7280;--pri:#1a73e8}
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
 color:#1f2328;background:#f7f8fa}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--bd);padding:12px 18px;
 display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600}
a.back{color:var(--pri);text-decoration:none;font-size:14px}
main{padding:18px;max-width:960px;margin:0 auto;display:grid;gap:14px}
.box{background:#fff;border:1px solid var(--bd);border-radius:12px;padding:16px}
.box h2{font-size:15px;margin:0 0 10px;display:flex;justify-content:space-between;align-items:center}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid #eef0f3;vertical-align:top}
th{color:var(--mut);font-weight:500}
.rel{font-size:12.5px;color:#41505f;display:block}
button{border:1px solid var(--bd);background:#fff;border-radius:8px;padding:8px 14px;font-size:13.5px;
 cursor:pointer;font-family:inherit}
button.pri{background:var(--pri);border-color:var(--pri);color:#fff}
.mut{color:var(--mut)}
.bar{height:8px;background:#e8f0fe;border-radius:4px;overflow:hidden;margin-top:4px}
.bar i{display:block;height:100%;background:var(--pri)}
</style></head><body>
<header><a class="back" href="/">‹ 书库</a><h1>存储分析</h1><span id="sum" class="mut"></span></header>
<main>
<div class="box"><h2>按文件类型</h2><table><thead><tr><th>类型</th><th>数量</th><th>占用</th><th style="width:30%">占比</th></tr></thead><tbody id="ext"></tbody></table></div>
<div class="box"><h2>按顶层目录</h2><table><thead><tr><th>目录</th><th>数量</th><th>占用</th><th style="width:30%">占比</th></tr></thead><tbody id="dir"></tbody></table></div>
<div class="box"><h2>重复文件（同名同大小）</h2><div id="dups"></div></div>
<div class="box"><h2>同大小疑似重复</h2><div id="same"></div></div>
<div class="box"><h2>空目录 <button class="pri" id="clean" type="button">一键清理空目录</button></h2><div id="empty"></div></div>
</main>
<script>
function hs(n){var u=['B','KB','MB','GB'],i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return (i?n.toFixed(1):n)+u[i]}
function rows(list,total,label){
  return list.map(function(x){
    var pct=(x.size/total*100).toFixed(1);
    return '<tr><td>'+(x[label]||'')+'</td><td>'+x.count+'</td><td>'+hs(x.size)+'</td>'+
      '<td><div>'+pct+'%</div><div class="bar"><i style="width:'+pct+'%"></i></div></td></tr>';
  }).join('');
}
function relList(rels){return rels.map(function(r){return '<span class="rel">'+r+'</span>';}).join('');}
fetch('/api/storage').then(function(r){return r.json();}).then(function(d){
  document.getElementById('sum').textContent=d.count+' 本 · 共 '+hs(d.total);
  document.getElementById('ext').innerHTML=rows(d.byExt,d.total,'ext');
  document.getElementById('dir').innerHTML=rows(d.byDir,d.total,'dir');
  document.getElementById('dups').innerHTML=d.dups.length?'<table><tbody>'+d.dups.map(function(x){
    return '<tr><td>'+x.name+'<br><span class="mut">'+hs(x.size)+'</span></td><td>'+relList(x.rels)+'</td></tr>';
  }).join('')+'</tbody></table>':'<span class="mut">没有重复文件</span>';
  document.getElementById('same').innerHTML=d.sameSize.length?'<table><tbody>'+d.sameSize.map(function(x){
    return '<tr><td>'+hs(x.size)+'</td><td>'+relList(x.rels)+'</td></tr>';
  }).join('')+'</tbody></table>':'<span class="mut">没有疑似重复</span>';
  document.getElementById('empty').innerHTML=d.emptyDirs.length?relList(d.emptyDirs):'<span class="mut">没有空目录</span>';
});
document.getElementById('clean').onclick=function(){
  fetch('/clean-empty',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
    alert('已清理 '+(d.removed||[]).length+' 个空目录'); location.reload();
  });
};
</script></body></html>
"""


TXT_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__TITLE__</title>
<style>
@font-face{font-family:'LXGW WenKai';font-style:normal;font-weight:400;font-display:swap;
 src:url('/static/fonts/lxgw-gb2312.woff2') format('woff2')}
body{--pgbg:#ffffff;--pgfg:#222222;--bar:#f5f6f8;--barfg:#222222;--bd:#dcdfe3;--btn:#ffffff;--btnfg:#333333;--btnact:#eef0f3;--mut:#8a8f98}
body[data-theme="green"]{--pgbg:#e9f5e3;--pgfg:#181818;--bar:#d8e9d1;--barfg:#141414;--bd:#bfd7b6;--btn:#f2fbee;--btnfg:#181818;--btnact:#cfe4c6;--mut:#5b7553}
body[data-theme="dark"]{--pgbg:#2b2b2b;--pgfg:#e8e8e8;--bar:#1f1f1f;--barfg:#eeeeee;--bd:#3a3a3a;--btn:#333333;--btnfg:#eeeeee;--btnact:#3d3d3d;--mut:#9a9a9a}
html,body{margin:0;height:100%;background:var(--pgbg);color:var(--pgfg);overflow:hidden;
 font-family:-apple-system,"PingFang SC","Microsoft YaHei",serif;
 font-size:calc(16px * var(--fscale,1));line-height:1.9}
#top{position:fixed;top:0;left:0;right:0;height:48px;display:flex;align-items:center;gap:8px;padding:0 12px;
 background:var(--bar);border-bottom:1px solid var(--bd);color:var(--barfg);z-index:9}
#top .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--barfg);font-size:14px}
button{background:var(--btn);color:var(--btnfg);border:1px solid var(--bd);border-radius:6px;padding:6px 12px;
 font-size:13px;cursor:pointer;font-family:inherit;min-height:36px}
button:active{background:var(--btnact)}
#back{white-space:nowrap}
#theme{min-width:40px;padding:6px 8px;font-weight:600}
#pg{color:var(--mut);font-size:12px;min-width:48px;text-align:center}
#seek{position:fixed;top:48px;left:0;right:0;height:4px;background:var(--btnact);z-index:9}
#seek i{display:block;height:100%;width:0;background:var(--barfg);opacity:.65}
#pages{position:fixed;top:52px;left:0;right:0;bottom:0;overflow:hidden}
.page{position:absolute;inset:0;padding:16px 20px 24px;overflow-y:auto;display:none;box-sizing:border-box}
.page.on{display:block}
#src{position:absolute;left:-10000px;top:0;visibility:hidden}
.page p,#src p{margin:0 0 .85em;text-indent:2em;word-break:break-word;text-align:justify}
.page p.cont,#src p.cont{text-indent:0}
.page p.ch,#src p.ch{text-indent:0;text-align:center;font-weight:600;margin-top:.35em}
.page p:last-child{margin-bottom:0}
.page img,#src img{max-width:100%;height:auto;max-height:calc(100vh - 160px)}
#tocWrap{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:20}
#tocBox{position:absolute;left:0;top:0;bottom:0;width:82%;max-width:340px;background:var(--bar);
 display:flex;flex-direction:column;border-right:1px solid var(--bd)}
#tocHead{padding:12px 14px;font-size:15px;font-weight:600;color:var(--barfg);
 display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--bd)}
#tocHead span{flex:1}
#tocHead button{min-height:32px;padding:4px 10px}
#tocList{flex:1;overflow-y:auto;padding:6px 0}
.toc-item{padding:11px 16px;font-size:14px;color:var(--barfg);cursor:pointer;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toc-item:active{background:var(--btnact)}
.toc-item.cur{color:#1a73e8;font-weight:600}
#tip{position:fixed;top:52px;left:0;right:0;padding:4px 20px;color:var(--mut);font-size:12px;
 z-index:8;background:var(--pgbg)}
#panel{position:fixed;left:0;right:0;bottom:0;background:var(--bar);border-top:1px solid var(--bd);
 padding:10px 14px 20px;z-index:10;box-shadow:0 -3px 14px rgba(0,0,0,.12)}
.prow{display:flex;align-items:center;gap:10px;margin:9px 0}
.plab{width:42px;flex:none;font-size:13px;color:var(--mut)}
#fsz{min-width:56px;text-align:center;font-size:13px;color:var(--barfg)}
.tbtn.on,.ffbtn.on{background:var(--barfg);color:var(--bar)}
.ffbtn.na{opacity:.42}.fnote{font-size:12px;opacity:.62;margin-left:8px}
.hide{display:none!important}
@media (max-width:640px){
 #top{gap:6px;padding:0 8px}
 #top .t{font-size:13px}
 #back{padding:9px 8px}
 .page{padding:14px 16px 20px}
}
</style></head><body>
<div id="top"><button id="back" type="button">‹ 书库</button>
<span class="t">__TITLE__</span>
<button id="tocBtn" type="button">目录</button>
<button id="theme" type="button" title="阅读设置">Aa</button>
<span id="pg">1/1</span></div>
<div id="seek"><i id="seekBar"></i></div>
<div id="tip" class="hide"></div>
<div id="pages"></div>
<div id="src">__TEXT__</div>
<div id="panel" class="hide">
  <div class="prow"><span class="plab">字号</span>
    <button id="fminus" type="button">A−</button><span id="fsz">100%</span><button id="fplus" type="button">A＋</button></div>
  <div class="prow"><span class="plab">字体</span>
    <button class="ffbtn" type="button" data-f="sys">默认</button>
    <button class="ffbtn" type="button" data-f="song">宋体</button>
    <button class="ffbtn" type="button" data-f="hei">黑体</button>
    <button class="ffbtn" type="button" data-f="kai">楷体</button>
    <button class="ffbtn" type="button" data-f="lxgw">霞鹜文楷</button><span id="fnote" class="fnote"></span></div>
  <div class="prow"><span class="plab">配色</span>
    <button class="tbtn" type="button" data-t="light">白</button>
    <button class="tbtn" type="button" data-t="green">绿</button>
    <button class="tbtn" type="button" data-t="dark">暗</button></div>
  <div class="prow"><span class="plab">翻页</span>
    <select id="flipMode">
      <option value="std">标准（左右分区）</option>
      <option value="one">单手（半屏点按）</option>
      <option value="float">悬浮按钮</option>
    </select></div>
  <div class="prow"><span class="plab">跳转</span>
    <input id="jumpVal" type="number" min="1" step="1" inputmode="numeric" placeholder="页码">
    <select id="jumpUnit"><option value="page">页</option><option value="pct">%</option></select>
    <button id="jumpBtn" type="button">跳转</button></div>
  <div class="prow"><span class="plab">朗读</span>
    <button id="ttsBtn" type="button">开始朗读</button>
    <select id="ttsRate">
      <option value="0.8">0.8x</option><option value="1" selected>1.0x</option>
      <option value="1.3">1.3x</option><option value="1.6">1.6x</option>
    </select></div>
</div>
<div id="tocWrap" class="hide"><div id="tocBox">
  <div id="tocHead"><span>目录</span><button id="tocClose" type="button">关闭</button></div>
  <div id="tocList"></div>
</div></div>
<script>
var REL = __REL__;
var TOC = __TOC__;
var THEMES = {light:1, green:1, dark:1};
var curTheme = "light";
try { var sv = localStorage.getItem("bookTheme"); if (sv && sv in THEMES) curTheme = sv; } catch(e) {}
function applyTheme(t){
  if (!(t in THEMES)) t = "light";
  curTheme = t;
  document.body.setAttribute("data-theme", t);
  try { localStorage.setItem("bookTheme", t); } catch(e) {}
  var bs = document.querySelectorAll(".tbtn");
  for (var i=0;i<bs.length;i++) bs[i].classList.toggle("on", bs[i].getAttribute("data-t") === t);
}

var FONT_MIN = 70, FONT_MAX = 220, FONT_STEP = 10, curFont = 100;
try { var fv = parseInt(localStorage.getItem("bookFont"), 10); if (fv >= FONT_MIN && fv <= FONT_MAX) curFont = fv; } catch(e) {}
function applyFont(v, repag){
  curFont = Math.max(FONT_MIN, Math.min(FONT_MAX, v));
  document.getElementById("fsz").textContent = curFont + "%";
  document.body.style.setProperty("--fscale", curFont / 100);
  try { localStorage.setItem("bookFont", curFont); } catch(e) {}
  if (repag) setTimeout(function(){ paginate(true); }, 0);
}

var FONTS = {
  sys:  "",
  song: "'Songti SC','SimSun','Noto Serif CJK SC','Source Han Serif SC','Source Han Serif CN',serif",
  hei:  "'PingFang SC','Microsoft YaHei','Noto Sans CJK SC','Source Han Sans SC','Noto Sans CJK','Droid Sans Fallback',sans-serif",
  kai:  "'Kaiti SC','KaiTi','STKaiti','AR PL UKai CN','AR PL UKai',serif",
  lxgw: "'LXGW WenKai','Kaiti SC','KaiTi','STKaiti',serif"
};
/* 判断一套字体在本机是否真能与其他字形区分：整片回退到系统默认 = 等于没有 */
function famUsable(css){
  if (!css) return true;
  try {
    var cv = document.createElement("canvas"); cv.width = 64; cv.height = 64;
    var c2 = cv.getContext("2d");
    function shot(f){
      c2.clearRect(0, 0, 64, 64);
      c2.font = "42px " + f;
      c2.fillStyle = "#000";
      c2.fillText("字", 4, 48);
      return c2.getImageData(0, 0, 64, 64).data;
    }
    var base = shot("sans-serif"), tgt = shot(css), n = 0;
    for (var i = 3; i < base.length; i += 4) if (Math.abs(base[i] - tgt[i]) > 12) n++;
    return n > 25;
  } catch(e){ return true; }
}
function markFam(){
  var bs = document.querySelectorAll(".ffbtn");
  for (var i = 0; i < bs.length; i++){
    var k = bs[i].getAttribute("data-f");
    var ok = (k === "sys" || k === "lxgw") ? true : famUsable(FONTS[k] || "");
    bs[i].classList.toggle("na", !ok);
  }
}
var curFontFam = "sys";
try { var fk = localStorage.getItem("bookFontFam"); if (fk && FONTS.hasOwnProperty(fk)) curFontFam = fk; } catch(e){}
function applyFontFam(k, repag){
  if (!FONTS.hasOwnProperty(k)) k = "sys";
  curFontFam = k;
  var note = document.getElementById("fnote");
  if (note) note.textContent = (k !== "sys" && k !== "lxgw" && !famUsable(FONTS[k])) ? "本机没有这款字体，显示为系统默认" : "";
  document.body.style.setProperty("font-family",
    FONTS[k] || "-apple-system,'PingFang SC','Microsoft YaHei',serif");
  try { localStorage.setItem("bookFontFam", k); } catch(e) {}
  var bs = document.querySelectorAll(".ffbtn");
  for (var i=0;i<bs.length;i++) bs[i].classList.toggle("on", bs[i].getAttribute("data-f") === k);
  /* 切到霞鹜文楷要等字体真正到达再重排，否则用的是回退字体的度量 */
  if (repag) (k === "lxgw" ? warmFonts() : Promise.resolve()).then(function(){ paginate(true); });
}

var pages = document.getElementById("pages");
var src = document.getElementById("src");
var P = Array.prototype.slice.call(src.querySelectorAll("p"));
var PGEL = [], paraPage = [], total = 1, cur = 0;

function pageRatio(){ return total > 1 ? cur / (total - 1) : 1; }

/* ---- 分页：按页面真实可视高度测量，段落放不下时按字符续排 ---- */
function _txtPos(el,n){
  var w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null, false), acc = 0, node;
  while ((node = w.nextNode())) {
    var L = node.nodeValue.length;
    if (acc + L >= n) return { n: node, o: n - acc };
    acc += L;
  }
  return null;
}
function _frag(el,n,left){
  var hit = _txtPos(el, n), r = document.createRange();
  if (left) { r.setStart(el,0); if (hit) r.setEnd(hit.n,hit.o); else r.setEnd(el,el.childNodes.length); }
  else { if (hit) r.setStart(hit.n,hit.o); else r.setStart(el,el.childNodes.length); r.setEnd(el,el.childNodes.length); }
  return r.cloneContents();
}
function _over(p){ return p.scrollHeight > p.clientHeight + 1; }
function _splitToFit(el,pg){
  if (el.querySelector("img") || el.querySelector("table") || el.querySelector("pre")) return null;
  var txt = (el.textContent || ""), len = txt.length;
  if (len < 2) return null;
  var lo = 1, hi = len - 1, best = 0;
  while (lo <= hi) {
    var mid = (lo + hi) >> 1, t = el.cloneNode(false);
    t.appendChild(_frag(el, mid, true));
    pg.appendChild(t);
    var ok = !_over(pg);
    pg.removeChild(t);
    if (ok) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  if (best < 1) return null;
  /* 尽量在标点处断开，别把一句话拦腰截断 */
  var cut = best, from = Math.max(0, best - 14), win = txt.slice(from, best);
  for (var j=win.length-1;j>=0;j--){
    if ("。！？；：，、….,!?;:".indexOf(win.charAt(j)) >= 0){ cut = from + j + 1; break; }
  }
  if (cut < 1 || cut > best) cut = best;
  var a = el.cloneNode(false); a.appendChild(_frag(el, cut, true));
  var b = el.cloneNode(false); b.appendChild(_frag(el, cut, false));
  if ((b.className || "").indexOf("cont") < 0) b.className = (b.className ? b.className + " " : "") + "cont";
  return [a, b];
}

/* ---------- 分页结果缓存：同一章在同样字号/字体/宽度下不必重算 ---------- */
var _pgCache = {}, _pgKeys = [], _PGC_MAX = 6;
function pgKey(){
  return REL + "|" + curChap + "|" + pages.clientWidth + "|" +
         pages.clientHeight + "|" + curFont + "|" + curFontFam;
}
function pgRestore(c, ratio){
  pages.innerHTML = c.h;
  PGEL = [].slice.call(pages.querySelectorAll(".page"));
  if (!PGEL.length) return false;
  paraPage = c.p.slice();
  for (var i=0;i<PGEL.length;i++){ PGEL[i].style.visibility = ""; PGEL[i].classList.remove("on"); }
  total = PGEL.length; cur = -1;
  showPage(Math.round(ratio * (total - 1)));
  return true;
}
function pgSave(k){
  try{
    _pgCache[k] = { h: pages.innerHTML, p: paraPage.slice() };
    _pgKeys.push(k);
    while (_pgKeys.length > _PGC_MAX){ delete _pgCache[_pgKeys.shift()]; }
  }catch(e){}
}

function paginate(keep){
  var ratio = keep ? pageRatio() : 0;
  if (pages.clientHeight < 80 && document.body.clientHeight > 80){
    requestAnimationFrame(function(){ paginate(keep); });
    return;
  }
  var pk = pgKey();
  if (_pgCache[pk]){
    try { if (pgRestore(_pgCache[pk], ratio)) return; }
    catch(e){ delete _pgCache[pk]; }
  }
  while (pages.firstChild) pages.removeChild(pages.firstChild);
  src.textContent = "";
  for (var i=0;i<P.length;i++){ P[i].__oidx = i; src.appendChild(P[i]); }
  var items = [].slice.call(P), idx = 0, pg = null;
  PGEL = []; paraPage = new Array(P.length);
  function mk(){
    pg = document.createElement("div");
    pg.className = "page on";
    pg.style.visibility = "hidden";
    pages.appendChild(pg);
    PGEL.push(pg);
  }
  mk();
  while (idx < items.length){
    var el = items[idx], oi = el.__oidx;
    pg.appendChild(el);
    if (_over(pg)){
      pg.removeChild(el);
      var parts = _splitToFit(el, pg);
      if (parts){
        pg.appendChild(parts[0]);
        parts[0].__oidx = oi; parts[1].__oidx = oi;
        if (paraPage[oi] === undefined) paraPage[oi] = PGEL.length - 1;
        items.splice(idx, 1, parts[0], parts[1]);
        mk(); idx++; continue;
      }
      if (!pg.childElementCount){
        pg.appendChild(el);
        if (paraPage[oi] === undefined) paraPage[oi] = PGEL.length - 1;
        idx++; continue;
      }
      mk(); continue;
    }
    if (paraPage[oi] === undefined) paraPage[oi] = PGEL.length - 1;
    idx++;
  }
  while (PGEL.length > 1 && !PGEL[PGEL.length-1].childElementCount){
    pages.removeChild(PGEL[PGEL.length-1]); PGEL.pop();
  }
  for (var q=0;q<PGEL.length;q++){ PGEL[q].style.visibility = ""; PGEL[q].classList.remove("on"); }
  total = PGEL.length;
  pgSave(pk);
  showPage(Math.round(ratio * (total - 1)));
}

function showPage(n){
  if (n < 0) n = 0;
  if (n > total - 1) n = total - 1;
  if (PGEL[cur] && cur !== n) PGEL[cur].classList.remove("on");
  cur = n;
  var el = PGEL[cur];
  if (el){ el.classList.add("on"); el.scrollTop = 0; }
  document.getElementById("pg").textContent = (cur + 1) + "/" + total;
  document.getElementById("seekBar").style.width = (total > 1 ? (cur / (total - 1) * 100) : 100) + "%";
  saveSoon();
}
function nextPage(){ if (cur < total - 1) showPage(cur + 1); }
function prevPage(){ if (cur > 0) showPage(cur - 1); }

/* ---------- TTS 朗读（Web Speech） ---------- */
var TTS_ON=false, TTS_WAIT=500;
function ttsCurrentText(){ var el=PGEL[cur]; return el ? el.innerText : ""; }
function ttsLoop(){
  if(!TTS_ON) return;
  var text=ttsCurrentText();
  if(!text){ if(cur<total-1){ nextPage(); setTimeout(ttsLoop,TTS_WAIT); } return; }
  var u=new SpeechSynthesisUtterance(text);
  u.rate=parseFloat(document.getElementById("ttsRate").value);
  u.lang="zh-CN";
  u.onend=function(){
    if(!TTS_ON) return;
    if(cur<total-1){ nextPage(); setTimeout(ttsLoop,TTS_WAIT); }
    else ttsStop();
  };
  u.onerror=u.onend;
  speechSynthesis.speak(u);
}
function ttsStart(){
  if(!("speechSynthesis" in window)){ toast("当前浏览器不支持语音合成"); return; }
  TTS_ON=true;
  document.getElementById("ttsBtn").textContent="停止朗读";
  speechSynthesis.cancel();
  ttsLoop();
}
function ttsStop(){
  TTS_ON=false;
  try{ speechSynthesis.cancel(); }catch(e){}
  document.getElementById("ttsBtn").textContent="开始朗读";
}
document.getElementById("ttsBtn").onclick=function(){ TTS_ON?ttsStop():ttsStart(); };

var panel = document.getElementById("panel");
document.getElementById("theme").onclick = function(){ panel.classList.toggle("hide") };
Array.prototype.forEach.call(document.querySelectorAll(".tbtn"), function(b){
  b.onclick = function(){ applyTheme(b.getAttribute("data-t")) };
});
Array.prototype.forEach.call(document.querySelectorAll(".ffbtn"), function(b){
  b.onclick = function(){ applyFontFam(b.getAttribute("data-f"), true) };
});
markFam();
document.getElementById("fminus").onclick = function(){ applyFont(curFont - FONT_STEP, true) };
document.getElementById("fplus").onclick = function(){ applyFont(curFont + FONT_STEP, true) };
document.getElementById("back").onclick = function(){ location.href = "/" };

/* ---------- 翻页模式（标准 / 单手 / 悬浮按钮）· 跳转 · 音量键 ---------- */
var FLIP_MODE = "std";
try { var _fm = localStorage.getItem("bookFlipMode"); if (_fm === "std" || _fm === "one" || _fm === "float") FLIP_MODE = _fm; } catch(e){}
function volKey(e){
  var c = e.keyCode || e.which || 0, k = e.key || "";
  if (c === 24 || k === "VolumeUp" || k === "AudioVolumeUp") return 1;
  if (c === 25 || k === "VolumeDown" || k === "AudioVolumeDown") return -1;
  return 0;
}
function xToast(msg){
  var t = document.createElement("div");
  t.style.cssText = "position:fixed;left:50%;top:64px;transform:translateX(-50%);z-index:120;padding:8px 14px;border-radius:10px;background:rgba(0,0,0,.78);color:#fff;font-size:13px;white-space:nowrap";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function(){ if (t.parentNode) t.parentNode.removeChild(t); }, 2000);
}
function applyFlipMode(m){
  if (m !== "std" && m !== "one" && m !== "float") m = "std";
  FLIP_MODE = m;
  try { localStorage.setItem("bookFlipMode", m); } catch(e){}
  var sel = document.getElementById("flipMode"); if (sel) sel.value = m;
  var fl = document.getElementById("floats");
  if (fl) fl.classList.toggle("hide", m !== "float");
  var pgc = document.getElementById("pages");
  if (pgc) pgc.style.paddingBottom = (m === "float") ? "66px" : "";
  if (typeof paginate === "function" && typeof P !== "undefined" && P.length){
    setTimeout(function(){ paginate(true); }, 0);
  }
}
(function(){
  var bar = document.createElement("div");
  bar.id = "floats"; bar.className = "hide";
  bar.style.cssText = "position:fixed;left:0;right:0;bottom:10px;display:flex;gap:10px;padding:0 12px;z-index:70;pointer-events:none";
  bar.innerHTML = '<button id="fPrev" type="button" style="pointer-events:auto;flex:1;height:50px;font-size:15px;opacity:.9;border-radius:12px">上一页</button>'
    + '<button id="fNext" type="button" style="pointer-events:auto;flex:1;height:50px;font-size:15px;opacity:.9;border-radius:12px">下一页</button>';
  document.body.appendChild(bar);
  document.getElementById("fPrev").onclick = function(){ panel.classList.add("hide"); prevPage(); };
  document.getElementById("fNext").onclick = function(){ panel.classList.add("hide"); nextPage(); };
})();
document.getElementById("flipMode").onchange = function(){ applyFlipMode(this.value); };
var _jv = document.getElementById("jumpVal");
if (_jv) _jv.style.cssText = "width:74px;padding:8px;font-size:13px;border-radius:8px;border:1px solid var(--bd,#ccc);background:transparent;color:inherit";
var _ju = document.getElementById("jumpUnit");
if (_ju) _ju.style.cssText = "padding:8px;font-size:13px;border-radius:8px;border:1px solid var(--bd,#ccc);background:transparent;color:inherit";
function doJump(){
  var el = document.getElementById("jumpVal"), v = parseFloat(el.value);
  if (!isFinite(v) || v <= 0){ xToast("请输入页码或百分比"); return; }
  var unit = document.getElementById("jumpUnit").value;
  if (unit === "pct"){
    v = Math.max(0, Math.min(100, v));
    showPage(Math.round(v / 100 * (total - 1)));
  } else {
    if (v > total) xToast("本书共 " + total + " 页");
    showPage(Math.round(v) - 1);
  }
  panel.classList.add("hide");
}
document.getElementById("jumpBtn").onclick = doJump;
document.getElementById("jumpVal").addEventListener("keydown", function(e){
  if (e.key === "Enter"){ e.preventDefault(); doJump(); }
});
applyFlipMode(FLIP_MODE);

/* ---------- 鼠标滚轮翻页 ---------- */
var _wAcc = 0, _wT = 0;
function wheelDir(dy){
  if (!dy) return 0;
  _wAcc += dy;
  var now = Date.now();
  if (now - _wT < 300 || Math.abs(_wAcc) < 30) return 0;   /* 节流，触控板的连续小增量合并成一次 */
  _wT = now;
  var d = _wAcc > 0 ? 1 : -1;
  _wAcc = 0;
  return d;
}
function wheelEv(e){
  if (e.ctrlKey) return;                                    /* 触控板双指缩放不拦 */
  var d = wheelDir(e.deltaY);
  if (!d) return;
  var el = PGEL[cur];
  /* 当前页比屏幕高时，先让正文自己滚完再翻页 */
  if (el && el.scrollHeight - el.clientHeight > 8){
    var atTop = el.scrollTop <= 0, atBot = el.scrollTop + el.clientHeight >= el.scrollHeight - 2;
    if (d > 0 && !atBot) return;
    if (d < 0 && !atTop) return;
  }
  e.preventDefault();
  if (d > 0) nextPage(); else prevPage();
}
(function(){
  var pc = document.getElementById("pages");
  if (pc) pc.addEventListener("wheel", wheelEv, {passive:false});
})();

/* ---------- 目录（章节识别结果，借鉴开源阅读） ---------- */
var tocWrap = document.getElementById("tocWrap");
var tocList = document.getElementById("tocList");
function currentChapter(){
  var last = -1;
  for (var i=0;i<TOC.length;i++){
    if ((paraPage[TOC[i].p] || 0) <= cur) last = i;
  }
  return last;
}
function renderToc(){
  tocList.innerHTML = "";
  var cc = currentChapter();
  for (var i=0;i<TOC.length;i++){
    (function(item, isCur){
      var d = document.createElement("div");
      d.className = "toc-item" + (isCur ? " cur" : "");
      d.textContent = item.t;
      d.onclick = function(){
        var pg = paraPage[item.p];
        if (pg != null) showPage(pg);
        tocWrap.classList.add("hide");
      };
      tocList.appendChild(d);
    })(TOC[i], i === cc);
  }
}
document.getElementById("tocBtn").onclick = function(){ renderToc(); tocWrap.classList.remove("hide"); };
document.getElementById("tocClose").onclick = function(){ tocWrap.classList.add("hide"); };
tocWrap.addEventListener("click", function(e){ if (e.target === tocWrap) tocWrap.classList.add("hide"); });
if (TOC.length < 2) document.getElementById("tocBtn").classList.add("hide");

var tx = 0, ty = 0, tMoved = 0;
pages.addEventListener("touchstart", function(e){
  if (e.touches.length !== 1) return;
  tx = e.touches[0].clientX; ty = e.touches[0].clientY;
}, {passive:true});
pages.addEventListener("touchend", function(e){
  var t = e.changedTouches[0];
  var dx = t.clientX - tx, dy = t.clientY - ty;
  if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)){
    tMoved = Date.now();
    if (dx < 0) nextPage(); else prevPage();
  }
}, {passive:true});
pages.addEventListener("click", function(e){
  if (Date.now() - tMoved < 400) return;
  var w = pages.clientWidth;
  if (FLIP_MODE === "one"){ if (e.clientX < w * 0.5) prevPage(); else nextPage(); return; }
  if (e.clientX < w * 0.38) prevPage();
  else if (e.clientX > w * 0.62) nextPage();
});
document.addEventListener("keydown", function(e){
  var vk = volKey(e);
  if (vk){ e.preventDefault(); if (vk > 0) nextPage(); else prevPage(); return; }
  if (e.key === "ArrowRight" || e.key === "ArrowDown" || e.key === "PageDown" || e.key === " ") { e.preventDefault(); nextPage(); }
  else if (e.key === "ArrowLeft" || e.key === "ArrowUp" || e.key === "PageUp") { e.preventDefault(); prevPage(); }
});

var rt = null;
window.addEventListener("resize", function(){
  if (rt) clearTimeout(rt);
  rt = setTimeout(function(){ paginate(true); }, 350);
});

/* ---------- 进度与阅读记录 ---------- */
var SEC = 0, TM = null, opened = false, BASE_T = 0, SEEN_T = 0;
setInterval(function(){ if (!document.hidden) SEC += 5; }, 5000);
function saveNow(){
  var r = pageRatio();
  var pct = Math.round(r * 1000) / 10;
  var sec = SEC; SEC = 0;
  var pos = "txtp:" + r.toFixed(4);
  try { localStorage.setItem("bookPage:" + REL, pos); } catch(e) {}
  try {
    fetch("/progress", {method:"POST", headers:{"Content-Type":"application/json"}, keepalive:true,
      body: JSON.stringify({rel: REL, cfi: pos, pct: pct, sec: sec, open: opened ? 0 : 1, base: BASE_T})})
      .then(function(x){ return x.json(); })
      .then(function(d){ if (d && d.t) BASE_T = d.t; })
      .catch(function(){});
    opened = true;
  } catch(e) {}
}
function saveSoon(){ if (TM) clearTimeout(TM); TM = setTimeout(saveNow, 2500); }
window.addEventListener("pagehide", saveNow);
document.addEventListener("visibilitychange", function(){ if (document.hidden) saveNow(); });

function restore(pos){
  var r = null;
  if (typeof pos === "string" && pos.indexOf("txtp:") === 0) r = parseFloat(pos.slice(5));
  if (r === null || isNaN(r)){
    try { var lp = localStorage.getItem("bookPage:" + REL); if (lp && lp.indexOf("txtp:") === 0) r = parseFloat(lp.slice(5)); } catch(e){}
  }
  if (r === null || isNaN(r)) return;
  showPage(Math.round(r * (total - 1)));
}

applyTheme(curTheme);
applyFontFam(curFontFam, false);
applyFont(curFont, false);
function warmFonts(){
  try{
    if(!document.fonts||!document.fonts.load) return Promise.resolve();
    /* 只有当前确实选了霞鹜文楷才去拉那 1.6MB 字体，选系统字体时不下载 */
    if(curFontFam!=="lxgw") return Promise.resolve();
    var p=document.fonts.load("16px 'LXGW WenKai'").then(function(){ return document.fonts.ready; },
      function(){ return document.fonts.ready; });
    return Promise.race([p,new Promise(function(res){ setTimeout(res,2500); })]);
  }catch(e){ return Promise.resolve(); }
}
warmFonts().then(function(){ paginate(false); });
if(document.fonts){
  var _fontReT=0;
  document.fonts.addEventListener("loadingdone",function(){
    clearTimeout(_fontReT);
    _fontReT=setTimeout(function(){ if(P.length) paginate(true); },120);
  });
}
fetch("/api/progress?rel=" + encodeURIComponent(REL))
  .then(function(r){ return r.json() })
  .then(function(d){ if (d && d.t) BASE_T = d.t; restore(d && d.cfi); })
  .catch(function(){ restore(null); });

/* 跨设备：每 30 秒查一次服务端进度，别的设备读到更后面就提示一句，点了才跳 */
function otherTip(text, onClick){
  var el = document.getElementById("otip");
  if (!el){
    el = document.createElement("div");
    el.id = "otip";
    el.style.cssText = "position:fixed;left:12px;right:12px;bottom:18px;z-index:99;padding:10px 14px;"
      + "border-radius:10px;background:var(--bar,#333);color:var(--barfg,#fff);border:1px solid var(--bd,#555);"
      + "font-size:13px;box-shadow:0 4px 14px rgba(0,0,0,.22);cursor:pointer";
    document.body.appendChild(el);
  }
  el.onclick = function(){ onClick(); el.remove(); };
  el.textContent = text;
}
function checkOther(){
  if (document.hidden) return;
  fetch("/api/progress?rel=" + encodeURIComponent(REL))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d || !d.t) return;
      if (d.t <= BASE_T || d.t <= SEEN_T) return;   // 没有更新的写入 / 已经提示过
      SEEN_T = d.t;
      var op = parseFloat(d.pct || 0);
      var mine = Math.round(pageRatio() * 1000) / 10;
      if (Math.abs(op - mine) < 2) return;
      var rr = op / 100;
      otherTip("其他设备已读到 " + op.toFixed(0) + "% ，点此跳转", function(){
        BASE_T = SEEN_T;                 // 点了才对齐服务端，之后本机的写入才算新进度
        showPage(Math.round(rr * (total - 1)));
      });
    })
    .catch(function(){});
}
setInterval(checkOther, 30000);
</script></body></html>
"""


DB_READER_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__TITLE__</title>
<style>
@font-face{font-family:'LXGW WenKai';font-style:normal;font-weight:400;font-display:swap;
 src:url('/static/fonts/lxgw-gb2312.woff2') format('woff2')}
body{--pgbg:#ffffff;--pgfg:#222222;--bar:#f5f6f8;--barfg:#222222;--bd:#dcdfe3;--btn:#ffffff;--btnfg:#333333;--btnact:#eef0f3;--mut:#8a8f98}
body[data-theme="green"]{--pgbg:#e9f5e3;--pgfg:#181818;--bar:#d8e9d1;--barfg:#141414;--bd:#bfd7b6;--btn:#f2fbee;--btnfg:#181818;--btnact:#cfe4c6;--mut:#5b7553}
body[data-theme="dark"]{--pgbg:#2b2b2b;--pgfg:#e8e8e8;--bar:#1f1f1f;--barfg:#eeeeee;--bd:#3a3a3a;--btn:#333333;--btnfg:#eeeeee;--btnact:#3d3d3d;--mut:#9a9a9a}
html,body{margin:0;height:100%;background:var(--pgbg);color:var(--pgfg);overflow:hidden;
 font-family:-apple-system,"PingFang SC","Microsoft YaHei",serif;
 font-size:calc(16px * var(--fscale,1));line-height:1.9}
#top{position:fixed;top:0;left:0;right:0;height:48px;display:flex;align-items:center;gap:8px;padding:0 12px;
 background:var(--bar);border-bottom:1px solid var(--bd);color:var(--barfg);z-index:9}
#top .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--barfg);font-size:14px}
button{background:var(--btn);color:var(--btnfg);border:1px solid var(--bd);border-radius:6px;padding:6px 12px;
 font-size:13px;cursor:pointer;font-family:inherit;min-height:36px}
button:active{background:var(--btnact)}
#back{white-space:nowrap}
#theme{min-width:40px;padding:6px 8px;font-weight:600}
#pg{color:var(--mut);font-size:12px;min-width:48px;text-align:center}
#seek{position:fixed;top:48px;left:0;right:0;height:4px;background:var(--btnact);z-index:9}
#seek i{display:block;height:100%;width:0;background:var(--barfg);opacity:.65}
#pages{position:fixed;top:52px;left:0;right:0;bottom:0;overflow:hidden}
.page{position:absolute;inset:0;padding:16px 20px 24px;overflow-y:auto;display:none;box-sizing:border-box}
.page.on{display:block}
#src{position:absolute;left:-10000px;top:0;visibility:hidden}
.page p,#src p{margin:0 0 .85em;text-indent:2em;word-break:break-word;text-align:justify}
.page p.cont,#src p.cont{text-indent:0}
.page p.ch,#src p.ch{text-indent:0;text-align:center;font-weight:600;margin-top:.35em}
.page p:last-child{margin-bottom:0}
.page img,#src img{max-width:100%;height:auto;max-height:calc(100vh - 160px)}
#loading{position:fixed;inset:52px 0 0;display:none;align-items:center;justify-content:center;
 flex-direction:column;gap:10px;background:var(--pgbg);color:var(--mut);z-index:7;font-size:14px}
#loading.on{display:flex}
.spin{width:30px;height:30px;border:3px solid var(--bd);border-top-color:var(--mut);
 border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#tocWrap{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:20}
#tocBox{position:absolute;left:0;top:0;bottom:0;width:82%;max-width:340px;background:var(--bar);
 display:flex;flex-direction:column;border-right:1px solid var(--bd)}
#tocHead{padding:12px 14px;font-size:15px;font-weight:600;color:var(--barfg);
 display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--bd)}
#tocHead span{flex:1}
#tocHead button{min-height:32px;padding:4px 10px}
#tocList{flex:1;overflow-y:auto;padding:6px 0}
.toc-item{padding:11px 16px;font-size:14px;color:var(--barfg);cursor:pointer;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toc-item:active{background:var(--btnact)}
.toc-item.cur{color:#1a73e8;font-weight:600}
#tip{position:fixed;top:52px;left:0;right:0;padding:4px 20px;color:var(--mut);font-size:12px;
 z-index:8;background:var(--pgbg)}
#panel{position:fixed;left:0;right:0;bottom:0;background:var(--bar);border-top:1px solid var(--bd);
 padding:10px 14px 20px;z-index:10;box-shadow:0 -3px 14px rgba(0,0,0,.12)}
.prow{display:flex;align-items:center;gap:10px;margin:9px 0}
.plab{width:42px;flex:none;font-size:13px;color:var(--mut)}
#fsz{min-width:56px;text-align:center;font-size:13px;color:var(--barfg)}
.tbtn.on,.ffbtn.on{background:var(--barfg);color:var(--bar)}
.ffbtn.na{opacity:.42}.fnote{font-size:12px;opacity:.62;margin-left:8px}
.hide{display:none!important}
@media (max-width:640px){
 #top{gap:6px;padding:0 8px}
 #top .t{font-size:13px}
 #back{padding:9px 8px}
 .page{padding:14px 16px 20px}
}
</style></head><body>
<div id="top"><button id="back" type="button">‹ 书库</button>
<span class="t" id="chapTitle">__TITLE__</span>
<button id="tocBtn" type="button">目录</button>
<button id="theme" type="button" title="阅读设置">Aa</button>
<span id="pg">1/1</span></div>
<div id="seek"><i id="seekBar"></i></div>
<div id="tip" class="hide"></div>
<div id="pages"></div>
<div id="loading"><div class="spin"></div><span>加载中…</span></div>
<div id="src"></div>
<div id="panel" class="hide">
  <div class="prow"><span class="plab">字号</span>
    <button id="fminus" type="button">A−</button><span id="fsz">100%</span><button id="fplus" type="button">A＋</button></div>
  <div class="prow"><span class="plab">字体</span>
    <button class="ffbtn" type="button" data-f="sys">默认</button>
    <button class="ffbtn" type="button" data-f="song">宋体</button>
    <button class="ffbtn" type="button" data-f="hei">黑体</button>
    <button class="ffbtn" type="button" data-f="kai">楷体</button>
    <button class="ffbtn" type="button" data-f="lxgw">霞鹜文楷</button><span id="fnote" class="fnote"></span></div>
  <div class="prow"><span class="plab">配色</span>
    <button class="tbtn" type="button" data-t="light">白</button>
    <button class="tbtn" type="button" data-t="green">绿</button>
    <button class="tbtn" type="button" data-t="dark">暗</button></div>
  <div class="prow"><span class="plab">翻页</span>
    <select id="flipMode">
      <option value="std">标准（左右分区）</option>
      <option value="one">单手（半屏点按）</option>
      <option value="float">悬浮按钮</option>
    </select></div>
  <div class="prow"><span class="plab">跳转</span>
    <input id="jumpVal" type="number" min="1" step="1" inputmode="numeric" placeholder="页码">
    <select id="jumpUnit"><option value="page">本章页</option><option value="pct">全书%</option></select>
    <button id="jumpBtn" type="button">跳转</button></div>
  <div class="prow"><span class="plab">朗读</span>
    <button id="ttsBtn" type="button">开始朗读</button>
    <select id="ttsRate">
      <option value="0.8">0.8x</option><option value="1" selected>1.0x</option>
      <option value="1.3">1.3x</option><option value="1.6">1.6x</option>
    </select></div>
</div>
<div id="tocWrap" class="hide"><div id="tocBox">
  <div id="tocHead"><span>目录</span><button id="tocClose" type="button">关闭</button></div>
  <div id="tocList"></div>
</div></div>
<script>
var REL = __REL__;
var TOC = null, totalChapters = 0, curChap = 0;
var chapCache = {};
var pages = document.getElementById("pages"), src = document.getElementById("src");
var P = [], PGEL = [], paraPage = [], total = 1, cur = 0;

function encRel(){ return encodeURIComponent(REL); }

/* ---------- 主题 / 字号 / 字体（与 TXT 页一致） ---------- */
var THEMES = {light:1, green:1, dark:1}, curTheme = "light";
try { var sv = localStorage.getItem("bookTheme"); if (sv && sv in THEMES) curTheme = sv; } catch(e) {}
function applyTheme(t){
  if (!(t in THEMES)) t = "light";
  curTheme = t; document.body.setAttribute("data-theme", t);
  try { localStorage.setItem("bookTheme", t); } catch(e) {}
  var bs = document.querySelectorAll(".tbtn");
  for (var i=0;i<bs.length;i++) bs[i].classList.toggle("on", bs[i].getAttribute("data-t")===t);
}
var FONT_MIN=70, FONT_MAX=220, FONT_STEP=10, curFont=100;
try { var fv=parseInt(localStorage.getItem("bookFont"),10); if(fv>=FONT_MIN&&fv<=FONT_MAX) curFont=fv; } catch(e){}
function applyFont(v, repag){
  curFont=Math.max(FONT_MIN,Math.min(FONT_MAX,v));
  document.getElementById("fsz").textContent=curFont+"%";
  document.body.style.setProperty("--fscale",curFont/100);
  try { localStorage.setItem("bookFont",curFont); } catch(e){}
  if (repag && P.length) setTimeout(function(){ paginate(true); },0);
}
var FONTS = {
 sys:"",
 song:"'Songti SC','SimSun','Noto Serif CJK SC','Source Han Serif SC','Source Han Serif CN',serif",
 hei:"'PingFang SC','Microsoft YaHei','Noto Sans CJK SC','Source Han Sans SC','Noto Sans CJK','Droid Sans Fallback',sans-serif",
 kai:"'Kaiti SC','KaiTi','STKaiti','AR PL UKai CN','AR PL UKai',serif",
 lxgw:"'LXGW WenKai','Kaiti SC','KaiTi','STKaiti',serif"
};
function famUsable(css){
  if(!css) return true;
  try{
    var cv=document.createElement("canvas"); cv.width=64; cv.height=64;
    var c2=cv.getContext("2d");
    function shot(f){ c2.clearRect(0,0,64,64); c2.font="42px "+f; c2.fillStyle="#000"; c2.fillText("字",4,48); return c2.getImageData(0,0,64,64).data; }
    var base=shot("sans-serif"),tgt=shot(css),n=0;
    for(var i=3;i<base.length;i+=4) if(Math.abs(base[i]-tgt[i])>12) n++;
    return n>25;
  }catch(e){ return true; }
}
function markFam(){
  var bs=document.querySelectorAll(".ffbtn");
  for(var i=0;i<bs.length;i++){
    var k=bs[i].getAttribute("data-f");
    var ok=(k==="sys"||k==="lxgw")?true:famUsable(FONTS[k]||"");
    bs[i].classList.toggle("na",!ok);
  }
}
var curFontFam="sys";
try{ var fk=localStorage.getItem("bookFontFam"); if(fk&&FONTS.hasOwnProperty(fk)) curFontFam=fk; }catch(e){}
function applyFontFam(k, repag){
  if(!FONTS.hasOwnProperty(k)) k="sys";
  curFontFam=k;
  var note=document.getElementById("fnote");
  if(note) note.textContent=(k!=="sys"&&k!=="lxgw"&&!famUsable(FONTS[k]))?"本机没有这款字体，显示为系统默认":"";
  document.body.style.setProperty("font-family",FONTS[k]||"-apple-system,'PingFang SC','Microsoft YaHei',serif");
  try{ localStorage.setItem("bookFontFam",k); }catch(e){}
  var bs=document.querySelectorAll(".ffbtn");
  for(var i=0;i<bs.length;i++) bs[i].classList.toggle("on",bs[i].getAttribute("data-f")===k);
  if(repag&&P.length){
    /* 切到霞鹜文楷要等字体到齐再重排，否则按回退字体的度量算出来是错的 */
    (k==="lxgw"?warmFonts():Promise.resolve()).then(function(){ paginate(true); });
  }
}

/* ---------- 章内分页（真实测量：填满可视高度，段落放不下则按字符续排） ---------- */
function inRatio(){ return total>1 ? cur/(total-1) : 1; }
function _txtPos(el,n){
  var w=document.createTreeWalker(el,NodeFilter.SHOW_TEXT,null,false),acc=0,node;
  while((node=w.nextNode())){
    var L=node.nodeValue.length;
    if(acc+L>=n) return {n:node,o:n-acc};
    acc+=L;
  }
  return null;
}
function _frag(el,n,left){
  var hit=_txtPos(el,n),r=document.createRange();
  if(left){ r.setStart(el,0); if(hit)r.setEnd(hit.n,hit.o); else r.setEnd(el,el.childNodes.length); }
  else { if(hit)r.setStart(hit.n,hit.o); else r.setStart(el,el.childNodes.length); r.setEnd(el,el.childNodes.length); }
  return r.cloneContents();
}
function _over(p){ return p.scrollHeight > p.clientHeight + 1; }
function _splitToFit(el,pg){
  if(el.querySelector("img")||el.querySelector("table")||el.querySelector("pre")) return null;
  var txt=el.textContent||"", len=txt.length;
  if(len<2) return null;
  var lo=1,hi=len-1,best=0;
  while(lo<=hi){
    var mid=(lo+hi)>>1,t=el.cloneNode(false);
    t.appendChild(_frag(el,mid,true));
    pg.appendChild(t);
    var ok=!_over(pg);
    pg.removeChild(t);
    if(ok){ best=mid; lo=mid+1; } else { hi=mid-1; }
  }
  if(best<1) return null;
  /* 尽量在标点处断开，别把一句话拦腰截断 */
  var cut=best, from=Math.max(0,best-14), win=txt.slice(from,best);
  for(var j=win.length-1;j>=0;j--){
    if("。！？；：，、….,!?;:".indexOf(win.charAt(j))>=0){ cut=from+j+1; break; }
  }
  if(cut<1||cut>best) cut=best;
  var a=el.cloneNode(false); a.appendChild(_frag(el,cut,true));
  var b=el.cloneNode(false); b.appendChild(_frag(el,cut,false));
  if((b.className||"").indexOf("cont")<0) b.className=(b.className?b.className+" ":"")+"cont";
  return [a,b];
}
/* ---------- 分页结果缓存：同一章在同样字号/字体/宽度下不必重算 ---------- */
var _pgCache={}, _pgKeys=[], _PGC_MAX=6;
function pgKey(){
  return REL+"|"+curChap+"|"+pages.clientWidth+"|"+pages.clientHeight+
         "|"+curFont+"|"+curFontFam;
}
function pgRestore(c,ratio){
  pages.innerHTML=c.h;
  PGEL=[].slice.call(pages.querySelectorAll(".page"));
  if(!PGEL.length) return false;
  paraPage=c.p.slice();
  for(var i=0;i<PGEL.length;i++){ PGEL[i].style.visibility=""; PGEL[i].classList.remove("on"); }
  total=PGEL.length; cur=-1;
  showPage(Math.round(ratio*(total-1)));
  return true;
}
function pgSave(k){
  try{
    _pgCache[k]={h:pages.innerHTML,p:paraPage.slice()};
    _pgKeys.push(k);
    while(_pgKeys.length>_PGC_MAX){ delete _pgCache[_pgKeys.shift()]; }
  }catch(e){}
}

function paginate(keep){
  var ratio=keep?inRatio():0;
  if(pages.clientHeight<80&&document.body.clientHeight>80){
    requestAnimationFrame(function(){ paginate(keep); });
    return;
  }
  var pk=pgKey();
  if(_pgCache[pk]){
    try{ if(pgRestore(_pgCache[pk],ratio)) return; }catch(e){ delete _pgCache[pk]; }
  }
  while(pages.firstChild) pages.removeChild(pages.firstChild);
  src.textContent="";
  for(var i=0;i<P.length;i++){ P[i].__oidx=i; src.appendChild(P[i]); }
  var items=[].slice.call(P), idx=0, pg=null;
  PGEL=[]; paraPage=new Array(P.length);
  function mk(){
    pg=document.createElement("div"); pg.className="page on";
    pg.style.visibility="hidden"; pages.appendChild(pg); PGEL.push(pg);
  }
  mk();
  while(idx<items.length){
    var el=items[idx], oi=el.__oidx;
    pg.appendChild(el);
    if(_over(pg)){
      pg.removeChild(el);
      var parts=_splitToFit(el,pg);
      if(parts){
        pg.appendChild(parts[0]);
        parts[0].__oidx=oi; parts[1].__oidx=oi;
        if(paraPage[oi]===undefined) paraPage[oi]=PGEL.length-1;
        items.splice(idx,1,parts[0],parts[1]);
        mk(); idx++; continue;
      }
      if(!pg.childElementCount){
        pg.appendChild(el);   /* 图片/表格段无法按字符切：整段成页 */
        if(paraPage[oi]===undefined) paraPage[oi]=PGEL.length-1;
        idx++; continue;
      }
      mk(); continue;
    }
    if(paraPage[oi]===undefined) paraPage[oi]=PGEL.length-1;
    idx++;
  }
  while(PGEL.length>1&&!PGEL[PGEL.length-1].childElementCount){
    pages.removeChild(PGEL[PGEL.length-1]); PGEL.pop();
  }
  for(var q=0;q<PGEL.length;q++){ PGEL[q].style.visibility=""; PGEL[q].classList.remove("on"); }
  total=PGEL.length;
  pgSave(pk);
  showPage(Math.round(ratio*(total-1)));
}
function showPage(n){
  if(n<0)n=0; if(n>total-1)n=total-1;
  if(PGEL[cur]&&cur!==n) PGEL[cur].classList.remove("on");
  cur=n;
  var el=PGEL[cur];
  if(el){ el.classList.add("on"); el.scrollTop=0; }
  document.getElementById("pg").textContent=(cur+1)+"/"+total;
  document.getElementById("seekBar").style.width=(total>1?(cur/(total-1)*100):100)+"%";
  saveSoon();
}
function nextPage(){
  if(cur<total-1) showPage(cur+1);
  else if(curChap<totalChapters-1) loadChapter(curChap+1,0);
}
function prevPage(){
  if(cur>0) showPage(cur-1);
  else if(curChap>0) loadChapter(curChap-1,1);
}

/* ---------- TTS 朗读（Web Speech，跨章等待更久） ---------- */
var TTS_ON=false, TTS_WAIT=1600;
function ttsCurrentText(){ var el=PGEL[cur]; return el ? el.innerText : ""; }
function ttsLoop(){
  if(!TTS_ON) return;
  var text=ttsCurrentText();
  if(!text){ if(cur<total-1 || curChap<totalChapters-1){ nextPage(); setTimeout(ttsLoop,TTS_WAIT); } return; }
  var u=new SpeechSynthesisUtterance(text);
  u.rate=parseFloat(document.getElementById("ttsRate").value);
  u.lang="zh-CN";
  u.onend=function(){
    if(!TTS_ON) return;
    if(cur<total-1 || curChap<totalChapters-1){ nextPage(); setTimeout(ttsLoop,TTS_WAIT); }
    else ttsStop();
  };
  u.onerror=u.onend;
  speechSynthesis.speak(u);
}
function ttsStart(){
  if(!("speechSynthesis" in window)){ toast("当前浏览器不支持语音合成"); return; }
  TTS_ON=true;
  document.getElementById("ttsBtn").textContent="停止朗读";
  speechSynthesis.cancel();
  ttsLoop();
}
function ttsStop(){
  TTS_ON=false;
  try{ speechSynthesis.cancel(); }catch(e){}
  document.getElementById("ttsBtn").textContent="开始朗读";
}
document.getElementById("ttsBtn").onclick=function(){ TTS_ON?ttsStop():ttsStart(); };

/* ---------- 章节按需加载与预取 ---------- */
function setLoading(on){ document.getElementById("loading").classList.toggle("on",on); }
function fetchChapter(i){
  if(chapCache[i]) return Promise.resolve(chapCache[i]);
  return fetch("/api/chapter/"+encRel()+"?i="+i)
    .then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); })
    .then(function(d){ chapCache[i]=d; return d; });
}
function prefetch(i){ if(i>=0&&i<totalChapters&&!chapCache[i]) fetchChapter(i).catch(function(){}); }

/* 分页前先把本章图片加载完，否则按 0 高度测量会导致整页溢出 */
function warmFonts(){
  try{
    if(!document.fonts||!document.fonts.load) return Promise.resolve();
    if(curFontFam!=="lxgw") return Promise.resolve();   /* 选系统字体就不拉这 1.6MB */
    var p=document.fonts.load("16px 'LXGW WenKai'").then(function(){ return document.fonts.ready; },
      function(){ return document.fonts.ready; });
    return Promise.race([p,new Promise(function(res){ setTimeout(res,2500); })]);
  }catch(e){ return Promise.resolve(); }
}
function warmImages(){
  var ims=src.querySelectorAll("img"), ps=[];
  for(var i=0;i<ims.length;i++){
    (function(im){
      if(im.complete&&im.naturalWidth) return;
      ps.push(new Promise(function(res){
        var done=false, f=function(){ if(done)return; done=true; res(); };
        im.addEventListener("load",f); im.addEventListener("error",f);
        setTimeout(f,1500);
      }));
    })(ims[i]);
  }
  return ps.length?Promise.all(ps):Promise.resolve();
}
function loadChapter(i, ratio){
  if(i<0||i>=totalChapters) return Promise.resolve();
  setLoading(true);
  return fetchChapter(i).then(function(d){
    curChap=i;
    src.innerHTML=d.html;
    P=Array.prototype.slice.call(src.querySelectorAll("p"));
    var pw0=pages.clientWidth;
    document.getElementById("chapTitle").textContent=d.title;
    prefetch(i+1); prefetch(i-1);
    return Promise.all([warmImages(),warmFonts()]).then(function(){
      paginate(false);
      if(ratio!=null&&ratio>0) showPage(Math.round(ratio*(total-1)));
      setLoading(false);
      updateTocCur();
      // 首帧布局校验：若容器宽度在分页后才稳定（弱设备/初始布局），按新宽度重分页
      requestAnimationFrame(function(){
        requestAnimationFrame(function(){
          if(P.length&&pages.clientWidth!==pw0) paginate(true);
        });
      });
    });
  }).catch(function(){
    setLoading(false);
    var tip=document.getElementById("tip");
    tip.textContent="章节加载失败，请检查网络后重试";
    tip.classList.remove("hide");
  });
}

/* ---------- 目录 ---------- */
var tocWrap=document.getElementById("tocWrap"), tocList=document.getElementById("tocList");
function renderToc(){
  tocList.innerHTML="";
  for(var i=0;i<TOC.length;i++){
    (function(item){
      var d=document.createElement("div");
      d.className="toc-item"+(item.ordinal===curChap?" cur":"");
      d.textContent=item.title;
      d.onclick=function(){ tocWrap.classList.add("hide"); loadChapter(item.ordinal,0); };
      tocList.appendChild(d);
    })(TOC[i]);
  }
}
function updateTocCur(){
  var items=tocList.querySelectorAll(".toc-item");
  for(var i=0;i<items.length;i++) items[i].classList.toggle("cur",TOC[i].ordinal===curChap);
}
document.getElementById("tocBtn").onclick=function(){ if(TOC) renderToc(); tocWrap.classList.remove("hide"); };
document.getElementById("tocClose").onclick=function(){ tocWrap.classList.add("hide"); };
tocWrap.addEventListener("click",function(e){ if(e.target===tocWrap) tocWrap.classList.add("hide"); });

/* ---------- 交互绑定 ---------- */
var panel=document.getElementById("panel");
document.getElementById("theme").onclick=function(){ panel.classList.toggle("hide"); };
Array.prototype.forEach.call(document.querySelectorAll(".tbtn"),function(b){ b.onclick=function(){ applyTheme(b.getAttribute("data-t")); }; });
Array.prototype.forEach.call(document.querySelectorAll(".ffbtn"),function(b){ b.onclick=function(){ applyFontFam(b.getAttribute("data-f"),true); }; });
markFam();
document.getElementById("fminus").onclick=function(){ applyFont(curFont-FONT_STEP,true); };
document.getElementById("fplus").onclick=function(){ applyFont(curFont+FONT_STEP,true); };
document.getElementById("back").onclick=function(){ location.href="/"; };

/* ---------- 翻页模式（标准 / 单手 / 悬浮按钮）· 跳转 · 音量键 ---------- */
var FLIP_MODE="std";
try{ var _fm=localStorage.getItem("bookFlipMode"); if(_fm==="std"||_fm==="one"||_fm==="float") FLIP_MODE=_fm; }catch(e){}
function volKey(e){
  var c=e.keyCode||e.which||0,k=e.key||"";
  if(c===24||k==="VolumeUp"||k==="AudioVolumeUp") return 1;
  if(c===25||k==="VolumeDown"||k==="AudioVolumeDown") return -1;
  return 0;
}
function xToast(msg){
  var t=document.createElement("div");
  t.style.cssText="position:fixed;left:50%;top:64px;transform:translateX(-50%);z-index:120;padding:8px 14px;border-radius:10px;background:rgba(0,0,0,.78);color:#fff;font-size:13px;white-space:nowrap";
  t.textContent=msg;
  document.body.appendChild(t);
  setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t); },2000);
}
function applyFlipMode(m){
  if(m!=="std"&&m!=="one"&&m!=="float") m="std";
  FLIP_MODE=m;
  try{ localStorage.setItem("bookFlipMode",m); }catch(e){}
  var sel=document.getElementById("flipMode"); if(sel) sel.value=m;
  var fl=document.getElementById("floats"); if(fl) fl.classList.toggle("hide",m!=="float");
  var pgc=document.getElementById("pages");
  if(pgc) pgc.style.paddingBottom=(m==="float")?"66px":"";
  if(typeof paginate==="function"&&typeof P!=="undefined"&&P.length){
    setTimeout(function(){ paginate(true); },0);
  }
}
(function(){
  var bar=document.createElement("div");
  bar.id="floats"; bar.className="hide";
  bar.style.cssText="position:fixed;left:0;right:0;bottom:10px;display:flex;gap:10px;padding:0 12px;z-index:70;pointer-events:none";
  bar.innerHTML='<button id="fPrev" type="button" style="pointer-events:auto;flex:1;height:50px;font-size:15px;opacity:.9;border-radius:12px">上一页</button>'
    +'<button id="fNext" type="button" style="pointer-events:auto;flex:1;height:50px;font-size:15px;opacity:.9;border-radius:12px">下一页</button>';
  document.body.appendChild(bar);
  document.getElementById("fPrev").onclick=function(){ panel.classList.add("hide"); prevPage(); };
  document.getElementById("fNext").onclick=function(){ panel.classList.add("hide"); nextPage(); };
})();
document.getElementById("flipMode").onchange=function(){ applyFlipMode(this.value); };
var _jv=document.getElementById("jumpVal");
if(_jv) _jv.style.cssText="width:74px;padding:8px;font-size:13px;border-radius:8px;border:1px solid var(--bd,#ccc);background:transparent;color:inherit";
var _ju=document.getElementById("jumpUnit");
if(_ju) _ju.style.cssText="padding:8px;font-size:13px;border-radius:8px;border:1px solid var(--bd,#ccc);background:transparent;color:inherit";
function doJump(){
  var el=document.getElementById("jumpVal"), v=parseFloat(el.value);
  if(!isFinite(v)||v<=0){ xToast("请输入页码或百分比"); return; }
  var unit=document.getElementById("jumpUnit").value;
  if(unit==="pct"){
    v=Math.max(0,Math.min(100,v));
    var p=v/100, ch=Math.floor(p*totalChapters);
    if(ch>totalChapters-1) ch=totalChapters-1;
    if(ch<0) ch=0;
    var r=p*totalChapters-ch;
    if(r<0) r=0; if(r>1) r=1;
    loadChapter(ch,r);
  }else{
    if(v>total) xToast("本章共 "+total+" 页");
    showPage(Math.round(v)-1);
  }
  panel.classList.add("hide");
}
document.getElementById("jumpBtn").onclick=doJump;
document.getElementById("jumpVal").addEventListener("keydown",function(e){
  if(e.key==="Enter"){ e.preventDefault(); doJump(); }
});
applyFlipMode(FLIP_MODE);

/* ---------- 鼠标滚轮翻页 ---------- */
var _wAcc=0,_wT=0;
function wheelDir(dy){
  if(!dy) return 0;
  _wAcc+=dy;
  var now=Date.now();
  if(now-_wT<300||Math.abs(_wAcc)<30) return 0;
  _wT=now;
  var d=_wAcc>0?1:-1;
  _wAcc=0;
  return d;
}
function wheelEv(e){
  if(e.ctrlKey) return;
  var d=wheelDir(e.deltaY);
  if(!d) return;
  var el=PGEL[cur];
  if(el&&el.scrollHeight-el.clientHeight>8){
    var atTop=el.scrollTop<=0, atBot=el.scrollTop+el.clientHeight>=el.scrollHeight-2;
    if(d>0&&!atBot) return;
    if(d<0&&!atTop) return;
  }
  e.preventDefault();
  if(d>0) nextPage(); else prevPage();
}
if(typeof pages!=="undefined"&&pages) pages.addEventListener("wheel",wheelEv,{passive:false});

if(document.fonts){
  var _fontReT=0;
  document.fonts.addEventListener("loadingdone",function(){
    clearTimeout(_fontReT);
    _fontReT=setTimeout(function(){ if(P&&P.length) paginate(true); },120);
  });
}

var tx=0,ty=0,tMoved=0;
pages.addEventListener("touchstart",function(e){ if(e.touches.length!==1)return; tx=e.touches[0].clientX; ty=e.touches[0].clientY; },{passive:true});
pages.addEventListener("touchend",function(e){
  var t=e.changedTouches[0], dx=t.clientX-tx, dy=t.clientY-ty;
  if(Math.abs(dx)>40&&Math.abs(dx)>Math.abs(dy)){ tMoved=Date.now(); if(dx<0)nextPage();else prevPage(); }
},{passive:true});
pages.addEventListener("click",function(e){
  if(Date.now()-tMoved<400)return;
  var w=pages.clientWidth;
  if(FLIP_MODE==="one"){ if(e.clientX<w*0.5)prevPage(); else nextPage(); return; }
  if(e.clientX<w*0.38)prevPage(); else if(e.clientX>w*0.62)nextPage();
});
document.addEventListener("keydown",function(e){
  var vk=volKey(e);
  if(vk){ e.preventDefault(); if(vk>0)nextPage(); else prevPage(); return; }
  if(e.key==="ArrowRight"||e.key==="ArrowDown"||e.key==="PageDown"||e.key===" "){ e.preventDefault(); nextPage(); }
  else if(e.key==="ArrowLeft"||e.key==="ArrowUp"||e.key==="PageUp"){ e.preventDefault(); prevPage(); }
});
var rt=null;
window.addEventListener("resize",function(){ if(rt)clearTimeout(rt); rt=setTimeout(function(){ if(P.length)paginate(true); },350); });

/* ---------- 进度与续读 ---------- */
var SEC=0,TM=null,opened=false,BASE_T=0,SEEN_T=0;
setInterval(function(){ if(!document.hidden)SEC+=5; },5000);
function globalRatio(){
  return totalChapters>1?(curChap+inRatio())/totalChapters:1;
}
function saveNow(){
  var pct=Math.round(globalRatio()*1000)/10, sec=SEC; SEC=0;
  var pos="dbp:"+curChap+":"+inRatio().toFixed(4);
  try{ localStorage.setItem("bookPage:"+REL,pos); }catch(e){}
  try{
    fetch("/progress",{method:"POST",headers:{"Content-Type":"application/json"},keepalive:true,
      body:JSON.stringify({rel:REL,cfi:pos,pct:pct,sec:sec,open:opened?0:1,base:BASE_T})})
      .then(function(x){return x.json();})
      .then(function(d){ if(d&&d.t) BASE_T=d.t; })
      .catch(function(){});
    opened=true;
  }catch(e){}
}
function saveSoon(){ if(TM)clearTimeout(TM); TM=setTimeout(saveNow,2500); }
window.addEventListener("pagehide",saveNow);
document.addEventListener("visibilitychange",function(){ if(document.hidden)saveNow(); });
function parsePos(cfi){
  var ord=0,ratio=0;
  if(typeof cfi==="string"&&cfi.indexOf("dbp:")===0){
    var p=cfi.split(":"); ord=parseInt(p[1],10)||0; ratio=parseFloat(p[2])||0;
  }
  return {ord:ord,ratio:ratio};
}

/* ---------- 启动：先取目录，再按进度定位 ---------- */
applyTheme(curTheme); applyFontFam(curFontFam,false); applyFont(curFont,false);
fetch("/api/toc/"+encRel())
  .then(function(r){ if(!r.ok)throw new Error("toc"); return r.json(); })
  .then(function(d){
    TOC=d.chapters; totalChapters=d.chapter_count;
    return fetch("/api/progress?rel="+encRel()).then(function(r){return r.json();})
      .then(function(pg){ if(pg&&pg.t) BASE_T=pg.t; return pg&&pg.cfi; }).catch(function(){ return null; });
  })
  .then(function(cfi){
    var pos=parsePos(cfi);
    if(!cfi){
      try{ var lp=localStorage.getItem("bookPage:"+REL); if(lp) pos=parsePos(lp); }catch(e){}
    }
    if(pos.ord>=totalChapters) pos.ord=0;
    return loadChapter(pos.ord,pos.ratio);
  })
  .catch(function(){
    var tip=document.getElementById("tip");
    tip.textContent="目录加载失败，请刷新重试";
    tip.classList.remove("hide");
  });

/* 跨设备：别的设备写入了更新的进度就提示一句，点了才跳章 */
function otherTip(text, onClick){
  var el=document.getElementById("otip");
  if(!el){
    el=document.createElement("div"); el.id="otip";
    el.style.cssText="position:fixed;left:12px;right:12px;bottom:18px;z-index:99;padding:10px 14px;"
      +"border-radius:10px;background:var(--bar,#333);color:var(--barfg,#fff);border:1px solid var(--bd,#555);"
      +"font-size:13px;box-shadow:0 4px 14px rgba(0,0,0,.22);cursor:pointer";
    document.body.appendChild(el);
  }
  el.onclick=function(){ onClick(); el.remove(); };
  el.textContent=text;
}
function checkOther(){
  if(document.hidden) return;
  fetch("/api/progress?rel="+encRel())
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(!d||!d.t) return;
      if(d.t<=BASE_T||d.t<=SEEN_T) return;      // 没有更新的写入 / 已经提示过
      SEEN_T=d.t;
      var op=parseFloat(d.pct||0), mine=Math.round(globalRatio()*1000)/10;
      if(Math.abs(op-mine)<2) return;
      var pos=parsePos(d.cfi||"");
      if(pos.ord>=totalChapters) return;
      otherTip("其他设备已读到 "+op.toFixed(0)+"% ，点此跳转", function(){
        BASE_T=SEEN_T;                          // 点了才对齐服务端
        loadChapter(pos.ord,pos.ratio);
      });
    })
    .catch(function(){});
}
setInterval(checkOther, 30000);
</script></body></html>
"""


READ_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__TITLE__</title>
<style>
@font-face{font-family:'LXGW WenKai';font-style:normal;font-weight:400;font-display:swap;
 src:url('/static/fonts/lxgw-gb2312.woff2') format('woff2')}
body{--pgbg:#ffffff;--pgfg:#222222;--bar:#f5f6f8;--barfg:#222222;--bd:#dcdfe3;
 --btn:#ffffff;--btnfg:#333333;--btnact:#eef0f3;--mut:#8a8f98;--shade:rgba(0,0,0,.08)}
body[data-theme="green"]{--pgbg:#e9f5e3;--pgfg:#181818;--bar:#d8e9d1;--barfg:#141414;--bd:#bfd7b6;
 --btn:#f2fbee;--btnfg:#181818;--btnact:#cfe4c6;--mut:#5b7553;--shade:rgba(24,24,24,.10)}
body[data-theme="dark"]{--pgbg:#2b2b2b;--pgfg:#e8e8e8;--bar:#1f1f1f;--barfg:#eeeeee;--bd:#3a3a3a;
 --btn:#333333;--btnfg:#eeeeee;--btnact:#3d3d3d;--mut:#9a9a9a;--shade:rgba(0,0,0,.5)}
html,body{margin:0;height:100%;background:var(--pgbg);color:var(--pgfg);
 font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
#top{height:48px;display:flex;align-items:center;gap:8px;padding:0 12px;background:var(--bar);
 border-bottom:1px solid var(--bd);color:var(--barfg)}
#top .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--barfg)}
button{background:var(--btn);color:var(--btnfg);border:1px solid var(--bd);border-radius:6px;padding:6px 12px;
 font-size:13px;cursor:pointer;font-family:inherit}
button:active{background:var(--btnact)}
#theme{min-width:40px;padding:6px 8px;font-weight:600}
#back{white-space:nowrap;text-decoration:none}
#pg{color:var(--mut);font-size:12px;min-width:36px;text-align:center}
#wrap{position:absolute;top:60px;bottom:0;left:0;right:0}
#seek{position:absolute;left:0;right:0;top:48px;height:12px;background:var(--btnact);z-index:6;cursor:pointer}
#seek i{position:absolute;left:0;top:0;height:100%;width:0;background:var(--barfg);opacity:.7;pointer-events:none}
#viewer{width:100%;height:100%;background:var(--pgbg)}
#toc{position:absolute;top:60px;bottom:0;left:0;width:300px;max-width:80%;background:var(--pgbg);
 border-right:1px solid var(--bd);overflow:auto;-webkit-overflow-scrolling:touch;padding:8px;z-index:5;
 box-shadow:2px 0 12px var(--shade)}
#toc a{display:block;color:var(--pgfg);text-decoration:none;padding:8px;border-radius:4px;font-size:13px}
#toc a:active{background:var(--btnact)}
#panel{position:absolute;left:0;right:0;bottom:0;background:var(--bar);border-top:1px solid var(--bd);
 padding:10px 14px 20px;z-index:7;box-shadow:0 -3px 14px var(--shade)}
.prow{display:flex;align-items:center;gap:10px;margin:9px 0}
.plab{width:42px;flex:none;font-size:13px;color:var(--mut)}
.prow button{min-width:50px}
#fsz{min-width:56px;text-align:center;font-size:13px;color:var(--barfg)}
.tbtn.on,.ffbtn.on{background:var(--barfg);color:var(--bar);border-color:var(--barfg)}
.ffbtn.na{opacity:.42}.fnote{font-size:12px;opacity:.62;margin-left:8px}
.hide{display:none!important}
@media (max-width:640px){
  #top{gap:6px;padding:0 8px}
  #top .t{display:none}
  button{padding:9px 12px;font-size:13px;min-height:36px}
  #theme{padding:9px 6px;min-width:34px}
  #prev,#next{flex:1}
  #back{padding:9px 8px}
  #toc{width:86%;max-width:none}
}
</style></head><body>
<div id="top"><button id="back" type="button" title="返回书库">‹<span class="lbl"> 书库</span></button>
<button id="tocBtn" type="button">目录</button>
<span class="t">__TITLE__</span>
<button id="theme" type="button" title="阅读设置">Aa</button>
<button id="prev" type="button">上一页</button><span id="pg"></span><button id="next" type="button">下一页</button></div>
<div id="wrap"><div id="viewer"></div></div>
<div id="seek"><i id="seekBar"></i></div>
<div id="toc" class="hide"></div>
<div id="panel" class="hide">
  <div class="prow"><span class="plab">字号</span>
    <button id="fminus" type="button">A−</button><span id="fsz">100%</span><button id="fplus" type="button">A＋</button></div>
  <div class="prow"><span class="plab">字体</span>
    <button class="ffbtn" type="button" data-f="sys">默认</button>
    <button class="ffbtn" type="button" data-f="song">宋体</button>
    <button class="ffbtn" type="button" data-f="hei">黑体</button>
    <button class="ffbtn" type="button" data-f="kai">楷体</button>
    <button class="ffbtn" type="button" data-f="lxgw">霞鹜文楷</button><span id="fnote" class="fnote"></span></div>
  <div class="prow"><span class="plab">配色</span>
    <button class="tbtn" type="button" data-t="light">白</button>
    <button class="tbtn" type="button" data-t="green">绿</button>
    <button class="tbtn" type="button" data-t="dark">暗</button></div>
  <div class="prow"><span class="plab">翻页</span>
    <select id="flipMode">
      <option value="std">标准（左右分区）</option>
      <option value="one">单手（半屏点按）</option>
      <option value="float">悬浮按钮</option>
    </select></div>
  <div class="prow"><span class="plab">跳转</span>
    <input id="jumpVal" type="number" min="1" step="1" inputmode="numeric" placeholder="章节号">
    <select id="jumpUnit"><option value="chap">章节</option><option value="pct">全书%</option></select>
    <button id="jumpBtn" type="button">跳转</button></div>
  <div class="prow"><span class="plab">朗读</span>
    <button id="ttsBtn" type="button">开始朗读</button>
    <select id="ttsRate">
      <option value="0.8">0.8x</option><option value="1" selected>1.0x</option>
      <option value="1.3">1.3x</option><option value="1.6">1.6x</option>
    </select></div>
</div>
<script src="/static/jszip.min.js"></script>
<script src="/static/epub.min.js"></script>
<script>
var FILE_URL = __FILE__;
var REL = (function(){
  var p = location.pathname;
  if (p.indexOf("/read/") === 0) p = p.slice(6);
  try { return decodeURIComponent(p); } catch(e){ return p; }
})();
var book = ePub();
var rendition = book.renderTo("viewer", {width:"100%", height:"100%", spread:"auto", allowScriptedContent:true, method:"blobUrl"});

/* ---------- 加载：流式下载 + 进度显示 + 本地缓存（慢网络不白屏，二次打开秒开） ---------- */
var loadMask = document.createElement("div");
loadMask.style.cssText = "position:fixed;left:0;top:0;right:0;bottom:0;z-index:99;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#ffffff;color:#333333;font-family:system-ui,-apple-system,sans-serif;padding:24px;text-align:center";
loadMask.innerHTML = '<div style="font-size:15px;margin-bottom:12px">正在打开《__TITLE__》</div><div id="lmPct" style="font-size:13px;color:#666666;line-height:1.9">准备中…</div>';
document.body.appendChild(loadMask);
function lmSet(t){ var e = document.getElementById("lmPct"); if (e) e.innerHTML = t; }

/* 以下整包下载 + IndexedDB 缓存已停用，改为按章按需加载（保留以备回退）
function idbOpen(){
  return new Promise(function(res){
    try {
      var r = indexedDB.open("bsBookCache", 1);
      r.onupgradeneeded = function(e){ e.target.result.createObjectStore("buf"); };
      r.onsuccess = function(e){ res(e.target.result); };
      r.onerror = function(){ res(null); };
    } catch(e){ res(null); }
  });
}
function idbGet(k){
  return idbOpen().then(function(db){
    if (!db) return null;
    return new Promise(function(res){
      try {
        var q = db.transaction("buf","readonly").objectStore("buf").get(k);
        q.onsuccess = function(){ res(q.result || null); };
        q.onerror = function(){ res(null); };
      } catch(e){ res(null); }
    });
  });
}
function idbPut(k,v){
  return idbOpen().then(function(db){
    if (!db) return;
    try { db.transaction("buf","readwrite").objectStore("buf").put(v,k); } catch(e){}
  }).catch(function(){});
}
function dlWithProgress(url){
  return fetch(url, {credentials:"same-origin"}).then(function(resp){
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    var total = parseInt(resp.headers.get("Content-Length") || "0", 10) || 0;
    if (!resp.body || !resp.body.getReader){ lmSet("正在下载…"); return resp.arrayBuffer(); }
    var reader = resp.body.getReader();
    var chunks = [], got = 0, t0 = Date.now();
    return new Promise(function(resolve, reject){
      function pump(){
        reader.read().then(function(r){
          if (r.done){
            var out = new Uint8Array(got), off = 0;
            for (var i = 0; i < chunks.length; i++){ out.set(chunks[i], off); off += chunks[i].length; }
            resolve(out.buffer);
            return;
          }
          chunks.push(r.value);
          got += r.value.length;
          var sec = Math.max(0.001, (Date.now() - t0) / 1000);
          var kb = Math.round(got / 1024);
          var eta = total ? Math.max(0, Math.round((total - got) / (got / sec))) : 0;
          lmSet("已下载 " + kb + " KB" + (total ? " / " + Math.round(total / 1024) + " KB（" + Math.round(got / total * 100) + "%）" : "") +
                (eta ? " · 还需约 " + eta + " 秒" : "") + "<br/>慢网络时首次打开需要等待，下载完成后会存到本地，下次秒开");
          pump();
        }).catch(reject);
      }
      pump();
    });
  });
}
*/
function resolveStart(){
  fetch("/api/progress?rel=" + encodeURIComponent(REL))
    .then(function(r){ return r.json() })
    .then(function(d){
      if (d && d.t) BASE_T = d.t;
      /* 优先 cfi（页内精确）；没有 cfi 就用记录下来的章节 href */
      var pos = (d && d.cfi) ? d.cfi : ((d && d.href) ? d.href : null);
      startAt(pos || localPos())
    })
    .catch(function(){ startAt(localPos()) });
}
/* ---------- 按章按需加载：不再整包下载，打开时只取目录与当前章节 ---------- */
lmSet("正在加载书籍结构…");
var opening = book.open(FILE_URL);
Promise.resolve(opening).then(function(){
  return book.ready;
}).then(function(){
  if (loadMask.parentNode) loadMask.parentNode.removeChild(loadMask);
  resolveStart();
}).catch(function(e){
  lmSet("加载失败：" + (e && e.message ? e.message : e) + "<br/>网络不稳定或文件异常，请刷新重试");
});

/* ---------- 阅读配色：白底黑字 / 浅绿 / 深灰 ---------- */
var THEMES = {
  light: {bg:"#ffffff", fg:"#222222"},
  green: {bg:"#e9f5e3", fg:"#181818"},
  dark:  {bg:"#2b2b2b", fg:"#e8e8e8"}
};
var curTheme = "light";
try { var sv = localStorage.getItem("bookTheme"); if (sv && THEMES[sv]) curTheme = sv; } catch(e) {}

/* 直接把样式写进 EPUB 内页：epub.js 的 themes.select 对已渲染的页面常不跟随，
   书自带的 color/background 又会盖住继承色，所以这里一次性写死 */
function paint(doc, t){
  if (!doc || !doc.documentElement) return;
  var T = THEMES[t] || THEMES.light;
  try {
    doc.documentElement.style.setProperty("background-color", T.bg, "important");
    if (doc.body){
      doc.body.style.setProperty("background-color", T.bg, "important");
      doc.body.style.setProperty("color", T.fg, "important");
    }
    var head = doc.head || doc.documentElement;
    var s = doc.getElementById("bs-theme");
    if (!s){ s = doc.createElement("style"); s.id = "bs-theme"; head.appendChild(s); }
    s.textContent =
      "html,body{background-color:" + T.bg + " !important;color:" + T.fg + " !important}" +
      "div,section,article,p,li,span,td,th,h1,h2,h3,h4,h5,h6,blockquote,em,strong,u,i{color:" + T.fg + " !important;background-color:transparent !important}" +
      "a{color:" + T.fg + " !important}" +
      "img,svg,figure,table{background-color:transparent !important}";
  } catch(e){}
}
function contentsList(){ try { return rendition.getContents() || []; } catch(e){ return []; } }
function docOf(c){ try { return c.document || (c.window && c.window.document); } catch(e){ return null; } }
function repaintAll(){
  var cs = contentsList();
  for (var i = 0; i < cs.length; i++){ var d = docOf(cs[i]); paint(d, curTheme); paintFont(d); }
}
function applyTheme(t){
  if (!THEMES[t]) t = "light";
  curTheme = t;
  document.body.setAttribute("data-theme", t);
  repaintAll();
  try { localStorage.setItem("bookTheme", t); } catch(e) {}
  var bs = document.querySelectorAll(".tbtn");
  for (var i = 0; i < bs.length; i++) bs[i].classList.toggle("on", bs[i].getAttribute("data-t") === t);
}
applyTheme(curTheme);

/* ---------- 字号 ---------- */
var FONT_MIN = 70, FONT_MAX = 220, FONT_STEP = 10, curFont = 100;
try { var fv = parseInt(localStorage.getItem("bookFont"), 10); if (fv >= FONT_MIN && fv <= FONT_MAX) curFont = fv; } catch(e) {}

/* ---------- 字体 ---------- */
var FONTS = {
  sys:  {css:""},
  song: {css:"'Songti SC','SimSun','Noto Serif CJK SC','Source Han Serif SC','Source Han Serif CN',serif"},
  hei:  {css:"'PingFang SC','Microsoft YaHei','Noto Sans CJK SC','Source Han Sans SC','Noto Sans CJK','Droid Sans Fallback',sans-serif"},
  kai:  {css:"'Kaiti SC','KaiTi','STKaiti','AR PL UKai CN','AR PL UKai',serif"},
  lxgw: {css:"'LXGW WenKai','Kaiti SC','KaiTi','STKaiti',serif"}
};
/* 判断一套字体在本机是否真能与其他字形区分：整片回退到系统默认 = 等于没有 */
function famUsable(css){
  if (!css) return true;
  try {
    var cv = document.createElement("canvas"); cv.width = 64; cv.height = 64;
    var c2 = cv.getContext("2d");
    function shot(f){
      c2.clearRect(0, 0, 64, 64);
      c2.font = "42px " + f;
      c2.fillStyle = "#000";
      c2.fillText("字", 4, 48);
      return c2.getImageData(0, 0, 64, 64).data;
    }
    var base = shot("sans-serif"), tgt = shot(css), n = 0;
    for (var i = 3; i < base.length; i += 4) if (Math.abs(base[i] - tgt[i]) > 12) n++;
    return n > 25;
  } catch(e){ return true; }
}
function markFam(){
  var bs = document.querySelectorAll(".ffbtn");
  for (var i = 0; i < bs.length; i++){
    var k = bs[i].getAttribute("data-f");
    var ok = (k === "sys" || k === "lxgw" || !FONTS[k]) ? true : famUsable(FONTS[k].css);
    bs[i].classList.toggle("na", !ok);
  }
}
var curFontFam = "sys";
try { var ffm = localStorage.getItem("bookFontFam"); if (ffm && FONTS[ffm]) curFontFam = ffm; } catch(e){}

function paintFont(doc){
  if (!doc || !doc.documentElement) return;
  try {
    var head = doc.head || doc.documentElement;
    var s = doc.getElementById("bs-font");
    if (!s){ s = doc.createElement("style"); s.id = "bs-font"; head.appendChild(s); }
    var fam = (FONTS[curFontFam] || FONTS.sys).css;
    var ff = "@font-face{font-family:'LXGW WenKai';font-style:normal;font-weight:400;font-display:swap;" +
             "src:url('/static/fonts/lxgw-gb2312.woff2') format('woff2')}";
    s.textContent = ff + "html,body{font-size:" + curFont + "% !important}" +
                    (fam ? "*{font-family:" + fam + " !important}" : "");
    if (doc.body) doc.body.style.setProperty("font-size", curFont + "%", "important");
  } catch(e){}
}
function applyFont(v){
  curFont = Math.max(FONT_MIN, Math.min(FONT_MAX, v));
  document.getElementById("fsz").textContent = curFont + "%";
  repaintAll();
  try { localStorage.setItem("bookFont", curFont); } catch(e) {}
}
function applyFontFam(k){
  if (!FONTS[k]) k = "sys";
  curFontFam = k;
  var note = document.getElementById("fnote");
  if (note) note.textContent = (k !== "sys" && k !== "lxgw" && !famUsable(FONTS[k].css)) ? "本机没有这款字体，显示为系统默认" : "";
  repaintAll();
  try { localStorage.setItem("bookFontFam", k); } catch(e) {}
  var bs = document.querySelectorAll(".ffbtn");
  for (var i = 0; i < bs.length; i++) bs[i].classList.toggle("on", bs[i].getAttribute("data-f") === k);
}
document.getElementById("fminus").onclick = function(){ applyFont(curFont - FONT_STEP) };
document.getElementById("fplus").onclick = function(){ applyFont(curFont + FONT_STEP) };
applyFontFam(curFontFam);
applyFont(curFont);
markFam();

/* ---------- 设置面板 ---------- */
var panel = document.getElementById("panel");
document.getElementById("theme").onclick = function(){ panel.classList.toggle("hide") };
Array.prototype.forEach.call(document.querySelectorAll(".tbtn"), function(b){
  b.onclick = function(){ applyTheme(b.getAttribute("data-t")) };
});
Array.prototype.forEach.call(document.querySelectorAll(".ffbtn"), function(b){
  b.onclick = function(){ applyFontFam(b.getAttribute("data-f")) };
});

/* ---------- 翻页模式（标准 / 单手 / 悬浮按钮）· 跳转 · 音量键 ---------- */
var FLIP_MODE = "std";
try { var _fm = localStorage.getItem("bookFlipMode"); if (_fm === "std" || _fm === "one" || _fm === "float") FLIP_MODE = _fm; } catch(e){}
function volKey(e){
  var c = e.keyCode || e.which || 0, k = e.key || "";
  if (c === 24 || k === "VolumeUp" || k === "AudioVolumeUp") return 1;
  if (c === 25 || k === "VolumeDown" || k === "AudioVolumeDown") return -1;
  return 0;
}
function xToast(msg){
  var t = document.createElement("div");
  t.style.cssText = "position:fixed;left:50%;top:64px;transform:translateX(-50%);z-index:120;padding:8px 14px;border-radius:10px;background:rgba(0,0,0,.78);color:#fff;font-size:13px;white-space:nowrap";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function(){ if (t.parentNode) t.parentNode.removeChild(t); }, 2000);
}
function applyFlipMode(m){
  if (m !== "std" && m !== "one" && m !== "float") m = "std";
  FLIP_MODE = m;
  try { localStorage.setItem("bookFlipMode", m); } catch(e){}
  var sel = document.getElementById("flipMode"); if (sel) sel.value = m;
  var fl = document.getElementById("floats");
  if (fl) fl.classList.toggle("hide", m !== "float");
  var pgc = document.getElementById("pages");
  if (pgc) pgc.style.paddingBottom = (m === "float") ? "66px" : "";
  if (typeof paginate === "function" && typeof P !== "undefined" && P.length){
    setTimeout(function(){ paginate(true); }, 0);
  }
}
(function(){
  var bar = document.createElement("div");
  bar.id = "floats"; bar.className = "hide";
  bar.style.cssText = "position:fixed;left:0;right:0;bottom:10px;display:flex;gap:10px;padding:0 12px;z-index:70;pointer-events:none";
  bar.innerHTML = '<button id="fPrev" type="button" style="pointer-events:auto;flex:1;height:50px;font-size:15px;opacity:.9;border-radius:12px">上一页</button>'
    + '<button id="fNext" type="button" style="pointer-events:auto;flex:1;height:50px;font-size:15px;opacity:.9;border-radius:12px">下一页</button>';
  document.body.appendChild(bar);
  document.getElementById("fPrev").onclick = function(){ panel.classList.add("hide"); flip("prev"); };
  document.getElementById("fNext").onclick = function(){ panel.classList.add("hide"); flip("next"); };
})();
document.getElementById("flipMode").onchange = function(){ applyFlipMode(this.value); };
var _jv = document.getElementById("jumpVal");
if (_jv) _jv.style.cssText = "width:74px;padding:8px;font-size:13px;border-radius:8px;border:1px solid var(--bd,#ccc);background:transparent;color:inherit";
var _ju = document.getElementById("jumpUnit");
if (_ju) _ju.style.cssText = "padding:8px;font-size:13px;border-radius:8px;border:1px solid var(--bd,#ccc);background:transparent;color:inherit";
function doJump(){
  var el = document.getElementById("jumpVal"), v = parseFloat(el.value);
  if (!isFinite(v) || v <= 0){ xToast("请输入章节号或百分比"); return; }
  var unit = document.getElementById("jumpUnit").value;
  if (unit === "pct"){
    jumpTo(Math.max(0, Math.min(100, v)) / 100);
  } else {
    var n = (book.spine && book.spine.length) ? book.spine.length : 0;
    if (!n){ xToast("书还没加载完，稍后再试"); return; }
    var idx = Math.round(v) - 1;
    if (idx < 0) idx = 0;
    if (idx > n - 1){ idx = n - 1; xToast("本书共 " + n + " 章"); }
    var sec = book.spine.get(idx);
    if (sec){ try { rendition.display(sec.href); } catch(e){} ; setTimeout(refreshPos, 500); }
  }
  panel.classList.add("hide");
}
document.getElementById("jumpBtn").onclick = doJump;
document.getElementById("jumpVal").addEventListener("keydown", function(e){
  if (e.key === "Enter"){ e.preventDefault(); doJump(); }
});
applyFlipMode(FLIP_MODE);

/* ---------- 鼠标滚轮翻页（外层 + EPUB 内页各绑一次） ---------- */
var _wAcc = 0, _wT = 0;
function wheelDir(dy){
  if (!dy) return 0;
  _wAcc += dy;
  var now = Date.now();
  if (now - _wT < 300 || Math.abs(_wAcc) < 30) return 0;
  _wT = now;
  var d = _wAcc > 0 ? 1 : -1;
  _wAcc = 0;
  return d;
}
(function(){
  var v = document.getElementById("viewer");
  if (!v) return;
  v.addEventListener("wheel", function(e){
    if (e.ctrlKey) return;
    var d = wheelDir(e.deltaY);
    if (!d) return;
    e.preventDefault();
    flip(d > 0 ? "next" : "prev");
  }, {passive:false});
})();

/* ---------- 阅读进度 ---------- */
/* REL 已在脚本开头按 location.pathname 算出 */
var LAST_LOC = null, TM = null, SEC = 0, opened = false, BASE_T = 0, SEEN_T = 0;
/* 前台每 5 秒累计一次阅读时长，一起随进度上报 */
setInterval(function(){ if (!document.hidden) SEC += 5; }, 5000);

function pctOf(loc){
  var n = (book.spine && book.spine.length) ? book.spine.length : 0;
  if (!loc || !n) return 0;
  var i = (typeof loc.index === "number") ? loc.index : 0;
  var frac = loc.total ? ((loc.page || 1) - 1) / loc.total : 0;
  return Math.max(0, Math.min(100, ((i + frac) / n) * 100));
}
function pctNow(){ return pctOf(LAST_LOC); }

/* 分章按需加载后，部分浏览器收不到 iframe 的 load 完成，epub.js 的
   relocated 事件和 currentLocation() 就都是空的（位置永远停在开头）。
   所以这里加一条主动采集路径：直接读当前渲染视图绑定的 section。 */
function curSection(){
  try {
    var v = rendition.manager && rendition.manager.views;
    if (v){
      if (v.current && v.current.section) return v.current.section;
      var arr = v._views || [], i;
      for (i = 0; i < arr.length; i++){ if (arr[i].displayed && arr[i].section) return arr[i].section; }
      for (i = 0; i < arr.length; i++){ if (arr[i].section) return arr[i].section; }
    }
  } catch(e){}
  return null;
}
function locNow(){
  var out = null;
  try {
    var lc = rendition.currentLocation();
    if (lc && lc.start && lc.start.cfi){
      var sd = lc.start.displayed || {};
      out = {cfi: lc.start.cfi, href: lc.start.href || "",
             index: (typeof lc.start.index === "number" ? lc.start.index : null),
             page: sd.page || 1, total: sd.total || 1};
    }
  } catch(e){}
  if (!out){
    var s = curSection();
    if (s && typeof s.index === "number"){
      out = {cfi: "", href: s.href || "", index: s.index, page: 1, total: 1};
    }
  }
  return out;
}
function refreshPos(){
  var loc = locNow();
  if (loc) LAST_LOC = loc;
  showPos();
}
function showPos(){
  var pg = document.getElementById("pg");
  if (!LAST_LOC){ pg.textContent = ""; return; }
  var n = (book.spine && book.spine.length) ? book.spine.length : 0;
  var p = Math.round(pctNow());
  pg.textContent = p + "%";
  pg.title = n ? (((LAST_LOC.index || 0) + 1) + "/" + n + " 章") : "";
  if (!seeking) seekBar.style.width = p + "%";
}
function saveNow(){
  var loc = locNow();
  if (loc) LAST_LOC = loc;
  if (!LAST_LOC || (!LAST_LOC.cfi && !LAST_LOC.href)) return;
  var cfi = LAST_LOC.cfi || "", href = LAST_LOC.href || "";
  var pct = Math.round(pctNow() * 10) / 10;
  var sec = SEC; SEC = 0;
  try { localStorage.setItem("bookPos:" + REL, cfi || ("href:" + href)); } catch(e) {}
  try {
    fetch("/progress", {method:"POST", headers:{"Content-Type":"application/json"}, keepalive:true,
      body: JSON.stringify({rel: REL, cfi: cfi, href: href,
        idx: (typeof LAST_LOC.index === "number" ? LAST_LOC.index : -1),
        pct: pct, sec: sec, open: opened ? 0 : 1, base: BASE_T})})
      .then(function(x){ return x.json(); })
      .then(function(d){ if (d && d.t) BASE_T = d.t; })
      .catch(function(){});
    opened = true;
  } catch(e) {}
}
function saveSoon(){ if (TM) clearTimeout(TM); TM = setTimeout(saveNow, 2500); }
window.addEventListener("pagehide", saveNow);
document.addEventListener("visibilitychange", function(){ if (document.hidden) saveNow(); });
/* 兜底：每 8 秒采一次位置，换章了才落盘，避免频繁写 */
var lastIdx = -1;
setInterval(function(){
  if (document.hidden) return;
  refreshPos();
  if (LAST_LOC && typeof LAST_LOC.index === "number" && LAST_LOC.index !== lastIdx){
    lastIdx = LAST_LOC.index;
    saveSoon();
  }
}, 8000);

/* 跨设备：别的设备写入了更新的进度就提示一句，点了才跳 */
function otherTip(text, onClick){
  var el = document.getElementById("otip");
  if (!el){
    el = document.createElement("div");
    el.id = "otip";
    el.style.cssText = "position:fixed;left:12px;right:12px;bottom:18px;z-index:99;padding:10px 14px;"
      + "border-radius:10px;background:var(--bar,#333);color:var(--barfg,#fff);border:1px solid var(--bd,#555);"
      + "font-size:13px;box-shadow:0 4px 14px rgba(0,0,0,.22);cursor:pointer";
    document.body.appendChild(el);
  }
  el.onclick = function(){ onClick(); el.remove(); };
  el.textContent = text;
}
function checkOther(){
  if (document.hidden || !book || !book.spine) return;
  fetch("/api/progress?rel=" + encodeURIComponent(REL))
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (!d || !d.t) return;
      if (d.t <= BASE_T || d.t <= SEEN_T) return;   // 没有更新的写入 / 已经提示过
      SEEN_T = d.t;
      var op = parseFloat(d.pct || 0);
      var mine = Math.round(pctNow() * 10) / 10;
      if (Math.abs(op - mine) < 2) return;
      otherTip("其他设备已读到 " + op.toFixed(0) + "% ，点此跳转", function(){
        BASE_T = SEEN_T;                 // 点了才对齐服务端
        jumpTo(op / 100);
      });
    })
    .catch(function(){});
}
setInterval(checkOther, 30000);

/* ---------- 进度条：点击或拖动跳转 ---------- */
var seek = document.getElementById("seek"), seekBar = document.getElementById("seekBar");
var seeking = false, seekRatio = 0;
function seekFrom(e){
  var r = seek.getBoundingClientRect();
  var cx = (e.touches && e.touches[0]) ? e.touches[0].clientX : e.clientX;
  seekRatio = Math.max(0, Math.min(1, (cx - r.left) / (r.width || 1)));
  seekBar.style.width = (seekRatio * 100) + "%";
}
function jumpTo(r){
  var n = (book.spine && book.spine.length) ? book.spine.length : 0;
  if (!n) return;
  var idx = Math.max(0, Math.min(n - 1, Math.floor(r * n)));
  var sec = book.spine.get(idx);
  if (sec) { try { rendition.display(sec.href); } catch(e){} ; setTimeout(refreshPos, 500); }
}
seek.addEventListener("touchstart", function(e){ seeking = true; seekFrom(e); }, {passive:true});
seek.addEventListener("touchmove", function(e){ if (seeking) seekFrom(e); }, {passive:true});
seek.addEventListener("touchend", function(e){ if (seeking){ seeking = false; jumpTo(seekRatio); } }, {passive:true});
seek.addEventListener("click", function(e){ seekFrom(e); jumpTo(seekRatio); });

/* ---------- 点按 / 左右滑动翻页（绑在 EPUB 内页上） ---------- */
var lastReloc = 0, navT = 0;
function flip(dir){
  var now = Date.now();
  if (now - navT < 200) return;              /* 极短防抖，避免一次触摸触发两下 */
  navT = now;
  try { if (dir === "next") rendition.next(); else rendition.prev(); } catch(e) {}
  setTimeout(refreshPos, 500);               /* 翻页后立刻刷新位置与进度 */
}
/* 键盘翻页：左右方向键 / PageUp·PageDown / 空格 */
document.addEventListener("keydown", function(e){
  var vk = volKey(e);
  if (vk){ e.preventDefault(); flip(vk > 0 ? "next" : "prev"); return; }
  if (e.key === "ArrowRight" || e.key === "PageDown" || e.key === " ") { e.preventDefault(); flip("next"); }
  else if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); flip("prev"); }
});
function tapZone(x, w){
  if (FLIP_MODE === "one"){ if (x < w * 0.5) flip("prev"); else flip("next"); return; }
  if (x < w * 0.38) flip("prev");
  else if (x > w * 0.62) flip("next");
}
rendition.hooks.content.register(function(contents){
  var doc = contents.document || (contents.window && contents.window.document);
  if (!doc || doc.__pgBound) return;
  doc.__pgBound = true;
  paint(doc, curTheme); paintFont(doc);
  try { doc.documentElement.style.touchAction = "manipulation"; } catch(e) {}
  var sx = 0, sy = 0, moved = false, tMoved = 0;
  doc.addEventListener("touchstart", function(e){
    if (e.touches.length !== 1) return;
    sx = e.touches[0].clientX; sy = e.touches[0].clientY; moved = false; tMoved = 0;
  }, {passive:true});
  doc.addEventListener("touchmove", function(e){
    if (e.touches.length !== 1) return;
    var dx = Math.abs(e.touches[0].clientX - sx), dy = Math.abs(e.touches[0].clientY - sy);
    if (dx > 24 && dx > dy * 1.4){ moved = true; tMoved = Date.now(); }
  }, {passive:true});
  doc.addEventListener("touchend", function(e){
    if (!moved) return;
    var d2 = e.changedTouches[0].clientX - sx;
    moved = false; tMoved = Date.now();
    if (Math.abs(d2) > 50) flip(d2 < 0 ? "next" : "prev");
  }, {passive:true});
  doc.addEventListener("click", function(e){
    if (Date.now() - tMoved < 400) return;   /* 滑动之后紧跟的 click 不算点按 */
    var t = e.target;
    while (t && t.nodeType === 1){ if (t.tagName === "A") return; t = t.parentNode; }
    /* 内页是多列布局：iframe 宽度常是视口的两倍，坐标要换算回外层视口口径 */
    var L = 0;
    try { var fe = contents.window.frameElement; if (fe) L = fe.getBoundingClientRect().left; } catch(err) {}
    var vw = document.getElementById("viewer").clientWidth || 1;
    tapZone(e.clientX + L - 0, vw);
  }, false);
  /* EPUB 正文在 iframe 里，滚轮事件要先落到内容层才拿得到 */
  doc.addEventListener("wheel", function(e){
    if (e.ctrlKey) return;
    var d = wheelDir(e.deltaY);
    if (!d) return;
    e.preventDefault();
    flip(d > 0 ? "next" : "prev");
  }, {passive:false});
});
/* 点在 iframe 之外（边缘留白）也按左右分区翻页 */
(function(){
  var v = document.getElementById("viewer");
  v.addEventListener("click", function(e){
    var r = v.getBoundingClientRect();
    tapZone(e.clientX - r.left, v.clientWidth || 1);
  });
})();

/* ---------- 续读上次位置 ---------- */
function localPos(){ try { return localStorage.getItem("bookPos:" + REL); } catch(e){ return null; } }
function startAt(pos){
  var target = null;
  if (pos){ target = String(pos); if (target.indexOf("href:") === 0) target = target.slice(5); }
  try {
    rendition.display(target || undefined);
  } catch(e){
    if (target){ try { rendition.display(); } catch(e2){} }
  }
  /* 有的浏览器不发 relocated，显示完自己采一次位置 */
  setTimeout(refreshPos, 300);
  setTimeout(refreshPos, 1500);
}
/* 续读位置由上面的 resolveStart() 在 book.ready 之后处理 */

/* ---------- 目录与按钮 ---------- */
var toc = document.getElementById("toc");
book.loaded.navigation.then(function(nav){
  toc.innerHTML = nav.toc.map(function(it){
    return '<a href="#" data-h="'+it.href+'">'+it.label.trim()+'</a>';
  }).join("");
  Array.prototype.forEach.call(toc.querySelectorAll("a"), function(a){
    a.addEventListener("click", function(e){
      e.preventDefault(); rendition.display(a.getAttribute("data-h")); toc.classList.add("hide");
      setTimeout(refreshPos, 500);
    });
  });
});
document.getElementById("back").onclick = function(){ location.href = "/" };
document.getElementById("tocBtn").onclick = function(){ toc.classList.toggle("hide") };
document.getElementById("prev").onclick = function(){ flip("prev") };
document.getElementById("next").onclick = function(){ flip("next") };
rendition.on("relocated", function(loc){
  var s = (loc && loc.start) || null;
  if (s && (s.cfi || s.href)){
    var sd = s.displayed || {};
    LAST_LOC = {cfi: s.cfi || "", href: s.href || "",
                index: (typeof s.index === "number" ? s.index : null),
                page: sd.page || 1, total: sd.total || 1};
  } else {
    refreshPos();
  }
  showPos(); saveSoon();
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "BookShelf/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # 可走 gzip 的文本类型（按子串匹配）
    _GZIP_CTYPES = ("text/", "application/json", "application/javascript",
                    "application/xml", "application/xhtml", "+xml")
    _GZIP_MIN = 1024

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None, head_only=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = {
            "Content-Type": ctype,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "Referrer-Policy": "no-referrer-when-downgrade",
        }
        for k, v in (extra or {}).items():
            headers[k] = v
        # 文本响应带 ETag：同一页重复打开走 304，省掉渲染后的压缩与整段传输
        if code == 200 and len(body) >= self._GZIP_MIN:
            etag = '"%s"' % hashlib.md5(body).hexdigest()
            headers["ETag"] = etag
            if not any(h.lower() == "cache-control" for h in headers):
                headers["Cache-Control"] = "private, no-cache"
            inm = self.headers.get("If-None-Match") or ""
            if etag in inm:
                self.send_response(304)
                self.send_header("ETag", etag)
                if "Cache-Control" in headers:
                    self.send_header("Cache-Control", headers["Cache-Control"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        # 文本类响应在客户端支持且体积足够时启用 gzip，弱网下显著省流量
        ae = self.headers.get("Accept-Encoding", "")
        is_text = any(tok in ctype for tok in self._GZIP_CTYPES)
        if (code == 200 and is_text and "gzip" in ae
                and len(body) >= self._GZIP_MIN):
            body = gzip.compress(body, compresslevel=1)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        headers["Content-Length"] = str(len(body))
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def _json(self, code, obj):
        return self._send(code, json.dumps(obj, ensure_ascii=False),
                          "application/json; charset=utf-8")

    def serve_cover(self, rel):
        got = cover_for(rel)
        if not got:
            return self._send(404, b"", "text/plain")
        data, ext = got
        return self._send(200, data, STATIC_MIME.get(ext, "image/jpeg"),
                          extra={"Cache-Control": "public, max-age=2592000"})

    # ---------------- 认证 ----------------

    def is_authed(self):
        if not CODE:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME and v and hmac.compare_digest(v, TOKEN):
                return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                raw = base64.b64decode(auth[6:].strip()).decode("utf-8", "ignore")
                if ":" in raw and hmac.compare_digest(raw.split(":", 1)[1], CODE):
                    return True
            except Exception:
                pass
        return False

    def login_page(self, parsed, error="", next_url="/"):
        page = (LOGIN_HTML
                .replace("__NEXT__", xesc(next_url or "/"))
                .replace("__ERR__", ('<div class="err">%s</div>' % xesc(error)) if error else ""))
        return self._send(200, page)

    def handle_login(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return self._send(400, "请求体无效", "text/plain; charset=utf-8")
        form = parse_qs(self.rfile.read(length).decode("utf-8", "ignore"))
        code = (form.get("code") or [""])[0].strip()
        nxt = (form.get("next") or ["/"])[0]
        if not (nxt.startswith("/") and "//" not in nxt and "\\" not in nxt):
            nxt = "/"
        if CODE and hmac.compare_digest(code, CODE):
            return self._send(302, b"", "text/plain", extra={
                "Location": nxt,
                "Set-Cookie": "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                              % (COOKIE_NAME, TOKEN, COOKIE_MAXAGE),
            })
        return self.login_page(None, "访问码不正确", nxt)

    def logout(self, parsed):
        return self._send(302, b"", "text/plain", extra={
            "Location": "/login",
            "Set-Cookie": "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE_NAME,
        })

    # ---------------- 路由 ----------------

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self.handle_login()
        if not self.is_authed():
            return self._json(401, {"error": "需要登录"})
        if path == "/upload":
            return self.handle_upload()
        if path == "/delete":
            return self.handle_delete()
        if path == "/restore":
            return self.handle_trash("restore")
        if path == "/purge":
            return self.handle_trash("purge")
        if path == "/progress":
            return self.handle_progress()
        if path == "/move":
            return self.handle_move()
        if path == "/mkdir":
            return self.handle_mkdir()
        if path == "/clean-empty":
            return self._json(200, {"ok": True, "removed": remove_empty_dirs()})
        return self._send(404, "<h1>404</h1><p>没有这个接口</p>")

    def do_HEAD(self):
        # 复用 GET 路由；_send 与 serve_file 均已按 HEAD 跳过响应体写入
        return self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/login":
            nxt = (parse_qs(parsed.query).get("next") or ["/"])[0]
            if not (nxt.startswith("/") and "//" not in nxt and "\\" not in nxt):
                nxt = "/"
            return self.login_page(parsed, "", nxt)
        if path == "/logout":
            return self.logout(parsed)

        if not self.is_authed():
            if path.startswith("/static/") or path == "/favicon.ico":
                pass
            elif path.startswith("/api/") or path.startswith("/opds"):
                return self._json(401, {"error": "需要登录"})
            else:
                return self._send(302, b"", "text/plain",
                                  extra={"Location": "/login?next=" + quote(self.path, safe="/")})

        if path == "/" or path == "/index.html":
            return self._send(200, HOME_HTML)

        if path == "/history":
            return self._send(200, HISTORY_HTML)

        if path == "/trash":
            return self._send(200, TRASH_HTML)

        if path == "/api/trash":
            return self._send(200, json.dumps(trash_list(), ensure_ascii=False),
                              "application/json; charset=utf-8")

        if path.startswith("/cover/"):
            return self.serve_cover(safe_rel(path[len("/cover/"):]))

        if path == "/api/books":
            data = scan_books()
            return self._send(200, books_json(data),
                              "application/json; charset=utf-8",
                              extra={"Cache-Control": "private, max-age=30"})

        if path.startswith("/api/toc/"):
            return self.api_toc(safe_rel(path[len("/api/toc/"):]))

        if path.startswith("/api/chapter/"):
            rel = safe_rel(path[len("/api/chapter/"):])
            try:
                idx = int(parse_qs(parsed.query).get("i", ["0"])[0])
            except (TypeError, ValueError):
                return self._json(400, {"error": "i 参数无效"})
            if idx < 0:
                return self._json(400, {"error": "i 参数无效"})
            return self.api_chapter(rel, idx)

        if path == "/api/dirs":
            return self._send(200, json.dumps({"dirs": list_dirs()}, ensure_ascii=False),
                              "application/json; charset=utf-8")

        if path == "/stats":
            return self._send(200, STATS_HTML)

        if path == "/storage":
            return self._send(200, STORAGE_HTML)

        if path == "/api/storage":
            return self._send(200, json.dumps(storage_info(), ensure_ascii=False),
                              "application/json; charset=utf-8")

        if path == "/api/daily":
            return self._send(200, json.dumps({"days": daily_data()}, ensure_ascii=False),
                              "application/json; charset=utf-8")

        if path == "/manifest.json":
            return self._send(200, json.dumps(MANIFEST_JSON, ensure_ascii=False),
                              "application/manifest+json",
                              extra={"Cache-Control": "public, max-age=3600"})

        if path == "/sw.js":
            return self._send(200, SW_JS, "application/javascript; charset=utf-8",
                              extra={"Cache-Control": "no-cache"})

        # ---- Legado 书源适配 ----
        if path == "/api/legado":
            return self._send(200, json.dumps(
                legado_source(legado_origin(self.headers)), ensure_ascii=False),
                              "application/json; charset=utf-8")
        if path.startswith("/api/legado/"):
            return self._legado_route(path, parsed)

        if path == "/api/progress":
            rel = safe_rel((parse_qs(parsed.query).get("rel") or [""])[0])
            with _plock:
                item = dict(_progress.get(rel) or {})
            return self._send(200, json.dumps(item, ensure_ascii=False),
                              "application/json; charset=utf-8")

        if path == "/opds" or path.startswith("/opds/"):
            return self._send(200, self.opds_xml(), "application/atom+xml; charset=utf-8")

        if path.startswith("/file/"):
            return self.serve_file(safe_rel(path[len("/file/"):]))

        if path.startswith("/read/"):
            return self.read_page(safe_rel(path[len("/read/"):]))

        if path.startswith("/u/"):
            return self.serve_unpacked(path[len("/u/"):])

        if path.startswith("/kimg/"):
            return self.serve_kimg(path[len("/kimg/"):])

        if path.startswith("/static/"):
            return self.serve_static(safe_rel(path[len("/static/"):]))

        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        return self._send(404, "<h1>404</h1><p>没有这个页面</p>")

    # ---------------- 阅读进度 ----------------

    def handle_progress(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "Content-Length 无效"})
        if length <= 0 or length > 16384:
            return self._json(400, {"error": "请求体无效"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": "JSON 解析失败"})

        rel = safe_rel(str(payload.get("rel", "")))
        cfi = str(payload.get("cfi", ""))[:512]
        href = str(payload.get("href", ""))[:512]
        # 分章加载时部分浏览器拿不到 cfi，允许退回用章节 href 记录位置
        if not rel or (not cfi and not href) or not os.path.isfile(os.path.join(BASE, rel)):
            return self._json(400, {"error": "参数不合法"})
        try:
            idx = int(payload.get("idx"))
        except (TypeError, ValueError):
            idx = -1
        if not 0 <= idx <= 100000:
            idx = -1
        try:
            pct = float(payload.get("pct", 0))
        except (TypeError, ValueError):
            pct = 0.0
        pct = max(0.0, min(100.0, pct))
        try:
            sec = float(payload.get("sec", 0) or 0)
        except (TypeError, ValueError):
            sec = 0.0
        sec = max(0.0, min(300.0, sec))       # 单次最多记 5 分钟，防止异常值
        opened = 1 if payload.get("open") else 0
        # 客户端带上"我上次同步到的服务端时间戳"，用于识别别的设备已经写过更新的进度
        try:
            base_t = int(payload.get("base") or 0)
        except (TypeError, ValueError):
            base_t = 0

        with _plock:
            old = dict(_progress.get(rel) or {})
            try:
                old_pct = float(old.get("pct") or 0)
            except (TypeError, ValueError):
                old_pct = 0.0
            old_t = int(old.get("t") or 0)
            # 服务端时间戳单调递增，避免同一秒内的两次写入分不出先后
            new_t = max(int(time.time()), old_t + 1)
            # 别的设备在这次同步之后写过，而这台设备报的位置没有明显前进
            # （位置相同或更靠前）→ 判为旧页面回写，只记时长，不覆盖位置
            if base_t and old_t > base_t and pct <= old_pct + 1.5:
                # 旧页面回写：位置和上次一样，但服务端已被别的设备更新过。
                # 只累加阅读时长，不覆盖位置，否则会把这台设备的旧位置盖到最新进度上。
                rec = dict(old)
                rec["n"] = int(old.get("n") or 0) + opened
                rec["tot"] = int(round(float(old.get("tot") or 0) + sec))
                _progress[rel] = rec
                save_progress()
                add_daily(sec)
                return self._json(200, {"ok": True, "t": rec.get("t"), "stale": True})
            rec = {
                "cfi": cfi,
                "pct": round(pct, 1),
                "t": new_t,
                "n": int(old.get("n") or 0) + opened,
                "tot": int(round(float(old.get("tot") or 0) + sec)),
            }
            if href:
                rec["href"] = href
            if idx >= 0:
                rec["idx"] = idx
            _progress[rel] = rec
            save_progress()
        add_daily(sec)
        if abs(pct - old_pct) >= 1:
            _cache["t"] = 0.0  # 让首页卡片上的「已读」及时跟上
        return self._json(200, {"ok": True, "t": rec["t"]})

    # ---------------- 删除 ----------------

    # ---------------- 分类整理 ----------------

    def _read_json(self, limit=65536):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, self._json(400, {"error": "Content-Length 无效"})
        if length <= 0 or length > limit:
            return None, self._json(400, {"error": "请求体无效"})
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")), None
        except Exception:
            return None, self._json(400, {"error": "JSON 解析失败"})

    def handle_move(self):
        payload, err = self._read_json()
        if err:
            return err
        rel = safe_rel(str(payload.get("rel", "")))
        if not rel or os.path.splitext(rel)[1].lower() not in EXTS:
            return self._json(400, {"error": "参数不合法"})
        try:
            new_rel = move_book(rel, str(payload.get("dir", "") or ""))
        except FileNotFoundError as exc:
            return self._json(404, {"error": str(exc)})
        except OSError as exc:
            return self._json(500, {"error": "移动失败：%s" % exc})
        return self._json(200, {"ok": True, "rel": new_rel})

    def handle_mkdir(self):
        payload, err = self._read_json()
        if err:
            return err
        try:
            name = make_dir(str(payload.get("name", "")))
        except FileExistsError:
            return self._json(409, {"error": "分类已存在"})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except OSError as exc:
            return self._json(500, {"error": "创建失败：%s" % exc})
        return self._json(200, {"ok": True, "dir": name})

    def handle_delete(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "Content-Length 无效"})
        if length <= 0 or length > 65536:
            return self._json(400, {"error": "请求体无效"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": "JSON 解析失败"})
        rel = safe_rel(str(payload.get("rel", "")))
        ext = os.path.splitext(rel)[1].lower()
        if not rel or ext not in EXTS:
            return self._json(400, {"error": "参数不合法"})

        base_real = os.path.realpath(BASE)
        real = os.path.realpath(os.path.join(BASE, rel))
        if not real.startswith(base_real + os.sep) or not os.path.isfile(real):
            return self._json(404, {"error": "文件不存在"})
        try:
            tid = trash_put(rel)          # 先进回收站，可恢复
        except FileNotFoundError:
            return self._json(404, {"error": "文件不存在"})
        except OSError as exc:
            return self._json(500, {"error": "删除失败：%s" % exc})
        with _plock:
            if _progress.pop(rel, None) is not None:
                save_progress(force=True)  # 连阅读进度一起清掉
        _cache["t"] = 0.0
        return self._json(200, {"ok": True, "name": os.path.basename(real), "trash": tid})

    # ---------------- 回收站 ----------------

    def handle_trash(self, action):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "Content-Length 无效"})
        if length <= 0 or length > 65536:
            return self._json(400, {"error": "请求体无效"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": "JSON 解析失败"})
        tid = str(payload.get("id", ""))
        if not tid or "/" in tid or "\\" in tid or ".." in tid:
            return self._json(400, {"error": "参数不合法"})
        try:
            if action == "restore":
                rel = trash_restore(tid)
                return self._json(200, {"ok": True, "rel": rel})
            trash_purge(tid)
            return self._json(200, {"ok": True})
        except FileNotFoundError:
            return self._json(404, {"error": "回收站里没有这一项"})
        except FileExistsError:
            return self._json(409, {"error": "同名文件已存在，先处理它再恢复"})
        except OSError as exc:
            return self._json(500, {"error": "操作失败：%s" % exc})

    # ---------------- 上传 ----------------

    def handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._json(400, {"error": "需要 multipart/form-data"})
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype)
        if not m:
            return self._json(400, {"error": "缺少 boundary"})
        boundary = (m.group(1) or m.group(2)).strip()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(411, {"error": "Content-Length 无效"})
        if length <= 0:
            return self._json(411, {"error": "缺少 Content-Length"})
        if length > MAX_UPLOAD:
            return self._json(413, {"error": "本次上传超过 %d MB 上限" % (MAX_UPLOAD // 1048576)})

        body = self.rfile.read(length)
        ok, fail = [], []
        delim = b"--" + boundary.encode("utf-8")

        for chunk in body.split(delim):
            if not chunk:
                continue
            if chunk.startswith(b"\r\n"):
                chunk = chunk[2:]
            if chunk in (b"", b"--", b"\r\n"):
                continue
            if chunk.endswith(b"\r\n"):
                chunk = chunk[:-2]
            if b"\r\n\r\n" not in chunk:
                continue
            raw_head, data = chunk.split(b"\r\n\r\n", 1)
            head = raw_head.decode("utf-8", "replace")
            fm = re.search(r'filename="([^"]*)"', head)
            if not fm:
                continue
            name = fm.group(1).split("/")[-1].split("\\")[-1].strip()
            name = re.sub(r"[\x00-\x1f]", "", name)
            if not name:
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in EXTS:
                fail.append("%s（不支持的格式）" % name)
                continue
            if not data:
                fail.append("%s（空文件）" % name)
                continue
            try:
                os.makedirs(BASE, exist_ok=True)
                target = self._unique_path(name)
                tmp = target + ".upload"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                # EPUB 是 zip 包，校验完整性；损坏/截断则拒绝，不污染书库
                if ext == ".epub":
                    try:
                        with zipfile.ZipFile(tmp) as zz:
                            bad = zz.testzip()
                        if bad is not None:
                            raise zipfile.BadZipFile
                    except Exception:
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        fail.append("%s（文件损坏或不完整，请重新上传）" % name)
                        continue
                os.replace(tmp, target)
                ok.append(os.path.basename(target))
                # 触发后台分章解析（高优先级，不阻塞响应）
                rel_new = os.path.relpath(target, BASE).replace(os.sep, "/")
                if not rel_new.lower().endswith(_SKIP_EXTS):
                    bookdb.enqueue(rel_new, 10)
            except OSError as exc:
                fail.append("%s（%s）" % (name, exc))

        _cache["t"] = 0.0
        return self._json(200, {"ok": ok, "fail": fail})

    @staticmethod
    def _unique_path(name):
        base, ext = os.path.splitext(name)
        target = os.path.join(BASE, name)
        n = 1
        while os.path.exists(target):
            target = os.path.join(BASE, "%s(%d)%s" % (base, n, ext))
            n += 1
        return target

    # ---------------- 文件与页面 ----------------

    def serve_unpacked(self, rest):
        """按需从 epub 里取单个文件：epub.js 目录模式下逐章加载用，不必整包下发"""
        key, _, inner = rest.partition("/")
        rel = u_rel(key)
        if not rel or not rel.lower().endswith(".epub"):
            return self._send(404, "404", "text/plain; charset=utf-8")
        p = os.path.join(BASE, rel)
        if not os.path.isfile(p):
            return self._send(404, "404", "text/plain; charset=utf-8")
        if not inner:
            inner = "META-INF/container.xml"
        try:
            with zipfile.ZipFile(p) as z:
                info = zip_member(z, inner)
                if info is None:
                    return self._send(404, "404", "text/plain; charset=utf-8")
                data = z.read(info)
                fname = info.filename
        except (OSError, zipfile.BadZipFile, RuntimeError, KeyError):
            return self._send(404, "404", "text/plain; charset=utf-8")
        ext = os.path.splitext(fname)[1].lower()
        ctype = EPUB_MIME.get(ext) or mimetypes.guess_type(fname)[0] or "application/octet-stream"
        return self._send(200, data, ctype, extra={"Cache-Control": "public, max-age=3600"})

    def serve_kimg(self, rest):
        """按需从 azw3(KF8) 取单张图片：/kimg/<ukey>/<embed编号>，不预抽取。"""
        key, _, num_s = rest.partition("/")
        rel = u_rel(key)
        if not rel or not rel.lower().endswith(".azw3"):
            return self._send(404, "404", "text/plain; charset=utf-8")
        try:
            num = int(num_s)
        except ValueError:
            return self._send(404, "404", "text/plain; charset=utf-8")
        p = os.path.join(BASE, rel)
        if not os.path.isfile(p):
            return self._send(404, "404", "text/plain; charset=utf-8")
        try:
            book = _get_kf8_book(p)
            img = book.get_image(num)
        except (OSError, KF8Error):
            return self._send(404, "404", "text/plain; charset=utf-8")
        if img is None:
            return self._send(404, "404", "text/plain; charset=utf-8")
        data, ctype = img
        return self._send(200, data, ctype,
                          extra={"Cache-Control": "public, max-age=2592000, immutable"})

    def serve_static(self, rel):
        p = os.path.join(STATIC, rel)
        if not os.path.isfile(p):
            return self._send(404, "404", "text/plain; charset=utf-8")
        ctype = STATIC_MIME.get(os.path.splitext(p)[1].lower(), "application/octet-stream")
        with open(p, "rb") as fh:
            data = fh.read()
        return self._send(200, data, ctype,
                          extra={"Cache-Control": "public, max-age=2592000, immutable"})

    def read_page(self, rel):
        p = os.path.join(BASE, rel)
        if not os.path.isfile(p):
            return self._send(404, "404 文件不存在", "text/plain; charset=utf-8")
        ext = os.path.splitext(p)[1].lower()
        name = os.path.splitext(os.path.basename(p))[0]
        url = "/file/" + quote(rel, safe="/")
        title = str(xesc(name))
        if ext == ".pdf":
            html = ("<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>%s</title>"
                    "<style>html,body{margin:0;height:100%%}iframe{border:0;width:100%%;height:100%%}</style>"
                    "</head><body><iframe src='%s'></iframe></body></html>" % (title, url))
            return self._send(200, html)
        if ext != ".epub" and ext != ".txt" and ext != ".mobi" and ext != ".azw3":
            return self._send(302, b"", "text/plain", extra={"Location": url})

        # 灰度：已分章 parsed 且开关开启 → 新分章模板；其余落回旧实时解析
        if DB_READER_ENABLED:
            stt = bookdb.get_status(rel)
            if stt and stt["parse_status"] == "parsed":
                dhtml = (DB_READER_HTML.replace("__TITLE__", title)
                         .replace("__REL__", json.dumps(rel)))
                return self._send(200, dhtml)
        if ext == ".azw3":
            # 尚未分章（后台解析中），先退回下载，解析完成后自动走分章模板
            return self._send(302, b"", "text/plain", extra={"Location": url})
        if ext == ".mobi":
            try:
                st = os.stat(p)
                rkey = (p, st.st_mtime_ns, st.st_size)
            except OSError:
                rkey = None
            rendered = _MOBI_RENDER_CACHE.get(rkey) if rkey else None
            if rendered:
                body_html, toc_json = rendered
            else:
                text, ncx_toc = mobi_to_text(p)
                if not text:
                    return self._send(302, b"", "text/plain", extra={"Location": url})
                body_html, toc_json = text_to_html(text)
                if "<p" not in body_html:
                    # 解析出的正文为空，不返回空白页，退回下载
                    return self._send(302, b"", "text/plain", extra={"Location": url})
                if ncx_toc:      # 有原生结构化目录时优先使用，替代文本正则猜测
                    toc_json = json.dumps(ncx_toc, ensure_ascii=False)
                if rkey:
                    if len(_MOBI_RENDER_CACHE) > 3:
                        _MOBI_RENDER_CACHE.clear()
                    _MOBI_RENDER_CACHE[rkey] = (body_html, toc_json)
            html = (TXT_HTML.replace("__TITLE__", title)
                            .replace("__REL__", json.dumps(rel))
                            .replace("__TOC__", toc_json)
                            .replace("__TEXT__", body_html))
            return self._send(200, html)
        if ext == ".txt":
            text, truncated = read_text_file(p)
            body_html, toc_json = text_to_html(text)
            if "<p" not in body_html:
                return self._send(302, b"", "text/plain", extra={"Location": url})
            html = (TXT_HTML.replace("__TITLE__", title)
                            .replace("__REL__", json.dumps(rel))
                            .replace("__TOC__", toc_json)
                            .replace("__TEXT__", body_html))
            if truncated:
                html = html.replace('<div id="tip" class="hide"></div>',
                                    '<div id="tip">文件较大，只加载了前 %d MB，其余请下载阅读</div>'
                                    % (TXT_MAX // (1024 * 1024)))
            return self._send(200, html)
        hurl = "/u/" + u_key(rel) + "/"
        html = READ_HTML.replace("__TITLE__", title).replace("__FILE__", json.dumps(hurl))
        return self._send(200, html)

    # ---------------- 分章阅读 API ----------------

    def api_toc(self, rel):
        if not os.path.isfile(os.path.join(BASE, rel)):
            return self._json(404, {"error": "文件不存在"})
        toc = bookdb.get_toc(rel)
        if toc is None:
            return self._json(404, {"error": "尚未分章入库"})
        total_chars = sum(c["char_count"] for c in toc)
        return self._json(200, {
            "rel": rel,
            "chapters": toc,
            "chapter_count": len(toc),
            "total_chars": total_chars,
        })

    def api_chapter(self, rel, idx):
        ch = bookdb.get_chapter(rel, idx)
        if ch is None:
            return self._json(404, {"error": "章节不存在"})
        cnt, _status = bookdb.chapter_count(rel)
        payload = {
            "ordinal": ch["ordinal"],
            "title": ch["title"],
            "html": ch["content_html"],
            "char_count": ch["char_count"],
            "total": cnt,
            "has_prev": idx > 0,
            "has_next": idx < cnt - 1,
        }
        # 章节内容不变即长期强缓存，gzip 由 _send 自动处理
        return self._send(200, json.dumps(payload, ensure_ascii=False),
                          "application/json; charset=utf-8",
                          extra={"Cache-Control": "public, max-age=2592000, immutable"})

    def serve_file(self, rel):
        p = os.path.join(BASE, rel)
        if not os.path.isfile(p):
            return self._send(404, "404 文件不存在", "text/plain; charset=utf-8")
        size = os.path.getsize(p)
        ext = os.path.splitext(p)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(p)[0] or "application/octet-stream"
        rng = self.headers.get("Range")

        fname = os.path.basename(p).replace('"', "")
        mode = "inline" if ext in (".epub", ".pdf", ".txt") else "attachment"
        try:
            fname.encode("latin-1")
            disp = '%s; filename="%s"' % (mode, fname)
        except UnicodeEncodeError:
            # 中文等非 latin-1 文件名走 RFC 5987，避免 send_header 编码失败
            disp = "%s; filename*=UTF-8''%s" % (mode, quote(fname))

        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end or start >= size:
                    return self._send(416, b"", "text/plain",
                                      extra={"Content-Range": "bytes */%d" % size})
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", disp)
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as fh:
            fh.seek(start)
            remain = length
            while remain > 0:
                chunk = fh.read(min(65536, remain))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except BrokenPipeError:
                    return
                remain -= len(chunk)

    def _legado_route(self, path, parsed):
        qs = parse_qs(parsed.query)
        rel = safe_rel((qs.get("rel") or [""])[0])
        origin = legado_origin(self.headers)
        sub = path[len("/api/legado/"):]
        if sub == "search":
            data = legado_search((qs.get("key") or [""])[0], origin)
        elif sub == "info":
            data = legado_info(rel, origin)
        elif sub == "toc":
            data = legado_toc(rel, origin)
        elif sub == "content":
            try:
                idx = int((qs.get("i") or ["0"])[0])
            except ValueError:
                return self._json(400, {"error": "i 参数无效"})
            data = legado_content(rel, idx)
        else:
            return self._json(404, {"error": "unknown route"})
        if data is None:
            return self._json(404, {"error": "not found"})
        return self._send(200, json.dumps(data, ensure_ascii=False),
                          "application/json; charset=utf-8")

    def opds_xml(self):
        books = scan_books()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        host = self.headers.get("Host") or ("%s:%d" % (HOST, PORT))
        base_url = "http://" + host
        kind = "application/atom+xml;profile=opds-catalog;kind=acquisition"
        entries = []
        for b in books:
            mime = MIME.get("." + b["ext"], "application/octet-stream")
            upd = datetime.fromtimestamp(b["mtime"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            q = quote(b["rel"], safe="/")
            entries.append(
                f"<entry><title>{xesc(b['name'])}</title><id>urn:b:{xesc(b['rel'])}</id>"
                f"<updated>{upd}</updated>"
                f"<content type='text'>{xesc(human_size(b['size']) + ' · ' + b['ext'].upper())}</content>"
                f"<link rel='http://opds-spec.org/acquisition' href='{base_url}/file/{q}' type='{mime}'/>"
                f"<link rel='http://opds-spec.org/image' href='{base_url}/cover/{q}' type='image/jpeg'/>"
                f"<link rel='http://opds-spec.org/image/thumbnail' href='{base_url}/cover/{q}' type='image/jpeg'/>"
                f"<link rel='alternate' href='{base_url}/read/{q}' type='text/html'/>"
                "</entry>"
            )
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">'
            f"<id>urn:bookshelf:root</id><title>我的书库</title><updated>{now}</updated>"
            f"<link rel='self' href='{base_url}/opds' type='{kind}'/>"
            f"<link rel='start' href='{base_url}/opds' type='{kind}'/>"
            + "".join(entries) + "</feed>"
        )


def main():
    os.makedirs(BASE, exist_ok=True)
    load_progress()
    load_daily()
    # PWA 图标落地静态目录（已存在则不覆盖）
    icon_path = os.path.join(STATIC, "icon.svg")
    try:
        if not os.path.isfile(icon_path):
            with open(icon_path, "w", encoding="utf-8") as fh:
                fh.write(ICON_SVG)
    except OSError:
        pass
    try:
        trash_cleanup()                      # 清理过期回收站条目
    except Exception:
        pass

    # 分章入库：初始化、注册解析器与路径解析
    bookdb.init_db()
    bookdb.register_parser("txt", parse_txt_blocks)
    bookdb.register_parser("mobi", parse_mobi_blocks)
    bookdb.register_parser("epub", parse_epub_blocks)
    bookdb.register_parser("azw3", parse_azw3_blocks)
    bookdb.register_path_resolver(lambda rel: os.path.join(BASE, rel))

    books = scan_books(True)
    queued = enqueue_existing(books)
    start_worker()
    start_watcher()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.request_queue_size = 32
    print("bookshelf on %s:%d  base=%s  books=%d  auth=%s  progress=%d  queued=%d"
          % (HOST, PORT, BASE, len(books), "on" if CODE else "off",
             len(_progress), queued))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
