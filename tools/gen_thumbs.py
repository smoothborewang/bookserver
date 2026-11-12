import os, io, hashlib, sqlite3, sys, time

COV = "/home/pi/books/.covers"
THU = "/home/pi/books/.thumbs"
DB = "/home/pi/hdd/bookserver/data/books.db"
W, Q = 320, 72

from PIL import Image

os.makedirs(THU, exist_ok=True)
conn = sqlite3.connect(DB)
rels = [r[0] for r in conn.execute("SELECT rel FROM books")]

done = skip = fail = 0
t0 = time.time()
for i, rel in enumerate(rels):
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16]
    dst = os.path.join(THU, h + ".jpg")
    if os.path.exists(dst):
        skip += 1
        continue
    src = None
    for ext in (".jpg", ".png", ".gif", ".webp"):
        p = os.path.join(COV, h + ext)
        if os.path.exists(p):
            src = p
            break
    if not src:
        continue          # 无封面缓存，留待运行时按需生成
    try:
        im = Image.open(src)
        im.load()
        w, hh = im.size
        if w > W:
            im = im.resize((W, max(1, int(hh * W / float(w)))), Image.LANCZOS)
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=Q, optimize=True, progressive=True)
        with open(dst + ".tmp", "wb") as fh:
            fh.write(buf.getvalue())
        os.replace(dst + ".tmp", dst)
        done += 1
    except Exception as e:
        fail += 1
        print("FAIL", rel, type(e).__name__, str(e)[:60])

total = sum(os.path.getsize(os.path.join(THU, f)) for f in os.listdir(THU) if f.endswith(".jpg"))
n = len([f for f in os.listdir(THU) if f.endswith(".jpg")])
print("生成 %d /跳过 %d/失败 %d，用时 %.0fs" % (done, skip, fail, time.time() - t0))
print("缩略共 %d 张，合计 %.1f MB，均值 %.0f KB" % (n, total / 1048576, total / max(n, 1) / 1024))
