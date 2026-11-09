# 运维手册

面向把这套书库部署在自己机器上的人：怎么装、怎么升级、出问题从哪查、怎么备份。所有命令都以 Debian 系 Linux、`systemd`、Python 3 为前提。

> 本文中的地址、端口、口令一律用占位符（`<host>`、`<port>`、`<访问码>`），请替换成你自己的值。仓库里不存在任何真实凭据。

## 1. 目标环境与资源约束

这个项目最初为一台 **Raspberry Pi 2B**（ARMv6、单核、约 430MB 可用内存、512MB swap、SD 卡）写成，因此有几条硬约束贯穿全代码：

- 只用 Python 3 标准库，不引入任何第三方包——armv6 上 `pip` 编译 wheel 既慢又容易失败。
- 不使用多进程/线程池重活，解析走单后台 worker 串行排。
- 常驻内存目标 ≤ 180MB（systemd `MemoryMax=180M`）。超了会被 OOM kill，表现为页面突然 502/连接重置。
- SD 卡写入寿命有限：进度落盘做了限流（12 秒一次），回收站 30 天自动清理。

在 x86 服务器或 NAS 上跑没有这些限制，可以直接放宽 `MemoryMax`。

## 2. 首次部署

```bash
sudo apt install -y python3          # 其余什么都不用装
git clone <仓库地址> /home/pi/bookserver
cd /home/pi/bookserver

export BOOK_BASE=/home/pi/books/library   # 你的书库根目录
export BOOK_PORT=8080
python3 bookserver.py
```

书库根目录要与 `BOOK_BASE` 一致，目录下按分类建子目录即可（子目录名会成为分类）。首次启动会自动建 `data/books.db` 索引并扫描入库。

### 挂成服务

```bash
sudo cp deploy/bookserver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bookserver
```

单元里的 `Environment=` 行按你的实际路径改，或改用 `EnvironmentFile=/etc/default/bookserver`（单元已预留 `-` 前缀，文件不存在也不报错）。

### 验收三件套

```bash
systemctl is-active bookserver                     # active
journalctl -u bookserver -n 30 --no-pager          # 无 Traceback
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/   # 302
```

**不要用 `ss -ltn` 判断服务是否起来**：这台机器上实测有时查不到 8080，但服务其实是好的。`is-active` + `curl` 才是可靠口径。

## 3. 升级流程

不要直接覆盖运行中的 `bookserver.py`——单文件应用语法错一个字符就是整站 500。按这个顺序：

```bash
python3 -m py_compile /home/pi/bookserver/bookserver.py.new      # 1. 先编译校验
cp bookserver.py bookserver.py.bak.$(date +%Y%m%d-%H%M)          # 2. 备份
mv bookserver.py.new bookserver.py                               # 3. 替换
sudo systemctl restart bookserver                                # 4. 重启
systemctl is-active bookserver && curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/
```

`data/books.db` 不要动，代码升级不需要重建索引。备份文件记得定期清（`*.bak.*` 已在 `.gitignore` 里）。

验证数据一致时优先做“抽样比对”而不是全量哈希——SD 卡与 SSH 在高负载下算 700MB 的 MD5 很容易超时，五点抽样（首、25%、50%、75%、尾各 8MiB）已经足够发现截断。

## 4. 目录与状态文件

```text
bookserver.py            主程序（HTTP 服务与全部页面）
bookdb.py                SQLite 索引层（章节、分类、进度、回收站）
kf8.py                   KF8/AZW3 解析
static/                  epub.js / jszip / 图标 / 字体子集
deploy/bookserver.service
data/books.db            索引库（含从书上抽出的章节文本，勿公开）
<BOOK_BASE>/             你的书库（epub/txt/mobi/azw3/pdf）
<BOOK_BASE 同级>/progress.json   阅读进度与阅读记录
<BOOK_BASE 同级>/daily.json      每日阅读时长
<BOOK_BASE>/.covers/     封面缓存（可删，会重建）
<BOOK_BASE>/.trash/      回收站（BOOK_TRASH_DAYS 天后清理）
```

`data/`、`progress.json`、书库本体都不进版本库（见 `.gitignore`），它们既有体积也有版权/隐私问题。

## 5. 日常命令

```bash
systemctl status bookserver
sudo systemctl restart bookserver
journalctl -u bookserver -f
journalctl -u bookserver -n 50 --no-pager
free -m && uptime
```

## 6. 故障排查

| 现象 | 排查方向 |
|---|---|
| 8080 打不开 | `is-active` + `curl` 确认服务；看 journal 是否有 Traceback；确认端口没被别的进程占 |
| 服务被反复 kill | `MemoryMax` 太小，`journalctl -k \| grep -i oom` 看 OOM 记录；大书解析期内存峰值最高 |
| 页面能开、正文空白 | 先无痕窗口排除缓存；再看 Console 是否 PNA（Private Network Access）拦截——公网页面加载私网子资源会被 Chrome 拦，封面在列表页直连所以正常、正文在 iframe 里所以消失。解法：用 HTTPS 域名访问（如 `tailscale serve`），不要用裸 IP |
| 正文空白但无报错 | 检查该 EPUB 的 OPF `spine` 里 `href` 是否缺扩展名：epub.js 靠扩展名判类型，缺失会渲染成空页 |
| 字体点了没变 | 大概率是本机缺字形而非代码问题；也可能是字体加载晚于分页测量，导致度量用了回退字体。本项目已改成字体就绪后再分页 |
| TXT 页数异常 / 整章塌成一页 | 确认用的是“整页容器 + 按可视高度测量”的方案；不要回退到 CSS 多列分页（有亚像素漂移） |
| 进度被“拉回旧位置” | 多设备竞态。服务端以单调时间戳裁决：客户端上报带 `base`，服务端 `old_t > base` 且位置没有明显前进时判旧页面回写，只累计时长不动位置，返回 `stale:true`。排查先看 `/api/progress` 返回的 `t` 是否偏小 |
| MOBI 中段零星乱码 | PalmDOC 每条记录 4096 字节正文外还有 1–20 字节填充，整段拼接会把跨记录边界的多字节字符冲散。修法是按记录头 `textlen` 逐条截断拼接，不是回引复制的问题 |
| 新增书籍不出现 | 目录监听 20 秒轮询一次；确认文件落在 `BOOK_BASE` 内、扩展名受支持；必要时看 journal 里的入库报错 |
| 上传的 EPUB 打不开 | 先怀疑文件被截断（弱网/弱 CPU 下 gzip 传输常见）：核对字节数与源一致，再验 zip 完整性 |

## 7. 备份与恢复

日常用增量 tar，重大变更前做整卡镜像。

```bash
#!/bin/bash
BK_DIR="$HOME/backup"
DATE=$(date +%Y%m%d_%H%M)
mkdir -p "$BK_DIR"
tar -zcf "$BK_DIR/library_$DATE.tar.gz" "$BOOK_BASE"          # 书库本体
tar -zcf "$BK_DIR/state_$DATE.tar.gz"  "$HOME/bookserver/data" "$BOOK_BASE/../progress.json"
tar -zcf "$BK_DIR/conf_$DATE.tar.gz" \
  /etc/systemd/system/bookserver.service /etc/default/bookserver
# 只保留最近 10 份
ls -t "$BK_DIR"/library_*.tar.gz | tail -n +11 | xargs -r rm -f
ls -t "$BK_DIR"/state_*.tar.gz   | tail -n +11 | xargs -r rm -f
```

```bash
crontab -e
# 0 2 * * * /home/pi/backup.sh
```

**整卡镜像只能在另一台机器上用读卡器做**，不要在运行中的设备上对系统卡 `dd`：

```bash
sudo dd if=/dev/sdX of=pi_backup.img bs=4M status=progress conv=fsync
# 恢复前务必确认设备名，写错会清空整块盘
sudo dd if=pi_backup.img of=/dev/sdX bs=4M status=progress conv=fsync
```

## 8. 新增服务时的约束

如果你要在这台机器上再加服务，请遵守这几条（都是踩过坑总结出来的）：

- **端口不冲突**：避开 22、8080（书库），以及你自己已占用的下载/共享端口。
- **不入侵书库**：不往 `bookserver.py` 里塞别的服务入口，书库保持独立部署、独立鉴权。
- **鉴权独立**：新服务自带鉴权，不复用书库访问码。
- **轻量优先**：ARMv6 + 400MB 内存下避开 Docker、transmission 这类重负载方案，优先纯 Python 或单二进制程序。
- **加限额**：常驻服务配 `MemoryMax`，下载类服务配限速，避免单核被跑满。

## 9. 安全

- `BOOK_CODE` 只挡随手访问，不是加密；**不要把 8080 暴露到公网**，需要外网就用 Tailscale/WireGuard 之类的隧道。
- 访问码、下载器 RPC 密钥、共享账号口令都通过环境变量注入，不写进代码、不提交到仓库。
- `data/books.db` 里含抽出的章节正文，按书籍版权同等对待，不要公开、不要上传网盘。
- 仓库里生成的备份、日志、进度文件都已被 `.gitignore` 排除，提交前用 `git status` 复核一遍。
