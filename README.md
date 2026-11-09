# BookShelf / 轻量电子书库（树莓派版）

一个零第三方依赖的本地电子书库服务：单文件 HTTP 服务（`bookserver.py`）+ 内置 SQLite 索引（`bookdb.py`）+ Kindle KF8/AZW3 解析（`kf8.py`），专为树莓派这类低性能 ARM 设备设计。

跑在 Raspberry Pi 2B（armv6、Debian 12 bookworm、1GB 内存）上，`MemoryMax=180M` 内即可稳定服务整库。

## 为什么用它

- **零依赖**：只用 Python 3 标准库，不需要 Flask/Django，不装 wheel，`python3 bookserver.py` 直接起。
- **格式覆盖**：EPUB / TXT / MOBI / AZW3(KF8) / PDF 直接在网页里读，不依赖外部解析工具链。
- **解析可控**：EPUB 按 spine 分章、TXT 用行首正则识别章节、MOBI 走 PalmDOC 解压 + 记录级截断、AZW3 自研 KF8 解析（含 NCX/ctoc 目录）。
- **阅读体验**：整屏分页（不滚动）、字号/字体切换、白底/浅绿/深灰三档配色、浏览器 TTS 朗读。
- **多设备进度**：服务端以单调时间戳裁决写入，避免旧页面迟到回写把新进度覆盖（同 LAN 多设备一致）。
- **OPDS + Legado 书源**：可给静读天下、「阅读」等客户端直接挂载。

## 目录结构

```
bookserver.py      HTTP 服务与全部页面（单文件，约 5k 行）
bookdb.py          SQLite 索引层（建库、分类、检索、进度）
kf8.py             Kindle KF8/AZW3 正文与目录解析
static/            epub.js、jszip、图标、字体子集
deploy/            systemd 单元文件
```

## 快速开始

```bash
git clone <repo> bookserver && cd bookserver
export BOOK_BASE=/srv/books/library      # 书库根目录（放你的 epub/txt/mobi/azw3/pdf）
export BOOK_PORT=8080
python3 bookserver.py
```

首次启动会在 `BOOK_BASE` 的父目录下生成 `progress.json`，索引库默认落在 `data/books.db`。

## 配置项（全部走环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BOOK_BASE` | `/home/pi/books/library` | 书库根目录 |
| `BOOK_STATIC` | `/home/pi/bookserver/static` | 静态资源目录 |
| `BOOK_PORT` | `8080` | 监听端口 |
| `BOOK_HOST` | `0.0.0.0` | 监听地址 |
| `BOOK_CODE` | 空 | 访问口令（留空则不校验） |
| `BOOK_PROGRESS` | 同 `BOOK_BASE` 同级的 `progress.json` | 阅读进度文件 |
| `BOOK_TRASH_DAYS` | `30` | 回收站保留天数 |
| `DB_READER_ENABLED` | `1` | 是否启用内置 DB 阅读器 |

## 以服务方式跑

```bash
sudo cp deploy/bookserver.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now bookserver
systemctl is-active bookserver && curl -sI http://127.0.0.1:8080/ | head -1
```

## 这个仓库里刻意不放的东西

- **不放书籍正文**：`books.db` 里是索引与抽取的章节文本，属于本地缓存；书库本体（EPUB/TXT/MOBI/AZW3/PDF）不进仓库。
- **不放进度文件**：`progress.json` 属于个人数据。
- **不放历史备份**：开发期的 `*.bak.*` 不进仓库。
- 代码中不含任何账号口令；访问口令、端口、路径一律通过环境变量注入。

## 第三方组件

`static/` 内含 epub.js、jszip（均遵循各自 MIT/BSD 许可）与霞鹜文楷子集字体（SIL OFL 1.1），版权归原作者，仅做前端渲染与阅读排版用。

## 许可

代码部分许可待作者确定（可先按 MIT 处理）。仓库不含任何受版权保护的书籍内容，请只在自己的合法资料上部署。
