# 115emby（缩略图 C 方案）

> 在 `115-album` 基础上做的**缩略图子系统**：懒抓 + 永久保存 + 静态加密 + 夜间定时预热 + 断点续传。
> 页面/播放仍由 `115-album` 提供，本目录只解决"缩略图从哪来、存哪、怎么加密、什么时候抓"。

## 一句话设计

```
用户第一次看到某张图 ──► 抓一次（走 API_MIN_INTERVAL 节流）
                        └─► 384px/q78 重编码 ──► AES-256-GCM ──► thumbs.db(加密 BLOB)
之后所有人再看这张图 ──► 命中本地库（LRU/磁盘），永不再打上游
夜里 02:00-06:00    ──► 按预算补抓没人看过的（到点自动收工，次日续跑）
```

- **不做**视频字节中转：播放仍是 302 直连 115 CDN，服务器 0 带宽。
- **不做**文件名入库：本库只存 115 的 `file_id:pickcode`（不透明 id）与加密后的图。

## 目录

```
app/secretbox.py      信封加密（AES-256-GCM；缺 cryptography 时回退 HMAC-CTR + HMAC 标签）
app/thumb_store.py    加密缩略图库：thumbs / thumb_src / kv 三表，WAL，连接即用即关
app/thumb_fetch.py    懒抓、编码、占位图识别、节流闸门、时间窗预热、断点续传
app/throttle.py       与 115-album 同名同义的节流器（最小间隔 + 并发 + 节奏休息）
app/window.py         时间窗解析（支持 02:00-06:00 / 跨零点 23:00-05:00 / 相同=全天）
app/thumbs_router.py  FastAPI 路由：/thumb/{id}（ETag/304）、/admin/thumb/*
app/upstream_adapter.py 与 115-album 对接的契约（PhotoLite / make_fetch_bytes / enqueue_from_rows）
tools/thumb_prewarm.py CLI：seed / now / window / daemon / pause / resume / reset / status
tests/test_thumb_c.py  82 项离线自测（不联网、不需要 115 凭证）
```

## 快速验证（不碰 115）

```bash
pip install -r requirements.txt

python tests/test_thumb_c.py                       # 82 项全离线自测
python tools/thumb_prewarm.py seed --count 30 --with-video
python tools/thumb_prewarm.py now --limit 5 --interval 0
python tools/thumb_prewarm.py window --until-empty --window 00:00-23:59 --interval 0
python tools/thumb_prewarm.py status
python tools/thumb_prewarm.py pause / resume / reset --state skipped
python tools/thumb_prewarm.py daemon --window 02:00-06:00      # 常驻，到点自动跑
```

`--interval 0` 只是 demo 去掉等待；接真实数据时**必须**用默认节流。

## 配置

节流（**与 `115-album` 同名，避免同账号两套服务给矛盾预算**）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_MIN_INTERVAL` | `1.0` | 两次上游请求最小间隔（秒），顺序/并发都绕不过 |
| `API_CONCURRENCY` | `2` | 同时在飞请求数 |
| `PREFETCH_REST_EVERY` | `200` | 每 N 次调用整体休息 |
| `PREFETCH_REST_SECONDS` | `5` | 每次休息秒数 |

缩略图：

| 变量 | 默认 | 说明 |
|---|---|---|
| `THUMB_DB` | `./data/thumbs.db` | 加密库路径 |
| `THUMB_SIZE` | `384` | 长边像素（实测 384/q78 比 480/q82 省约 40%） |
| `THUMB_QUALITY` | `78` | JPEG 质量 |
| `THUMB_MAX_BYTES` | `307200` | 单张上游缩略图上限，超过直接跳过（防拉原图） |
| `THUMB_LRU_MB` | `32` | 进程内解密缓存，热图 <1ms |
| `THUMB_BROWSER_CACHE` | `86400` | 浏览器缓存秒数（靠 ETag 重新校验） |

夜间预热：

| 变量 | 默认 | 说明 |
|---|---|---|
| `THUMB_PREWARM_ENABLED` | `0` | 默认只懒抓；`1` 才开夜间预热 |
| `THUMB_PREWARM_WINDOW` | `02:00-06:00` | 支持跨零点 `23:00-05:00`；起止相同=全天 |
| `THUMB_PREWARM_MAX_PER_NIGHT` | `0` | 0 = 按窗口分钟 ÷ 间隔自动推算 |
| `THUMB_PREWARM_RETRY_HOURS` | `6` | 失败项多久后可重试 |
| `THUMB_PREWARM_MAX_ATTEMPTS` | `3` | 最大重试次数 |

密钥（三选一，**优先级从上到下**）：

```ini
THUMB_ENC_KEY=            # 32 字节 hex/base64，最推荐（可用 KMS/密码管理器保存）
THUMB_ENC_PASSPHRASE=     # 口令 + 盐文件做 PBKDF2-HMAC-SHA256（60 万轮）
# 都不配 → 本机随机 data/.thumbkey（会打警告：密钥与密文同盘，只防"文件被单独拷走"）
```

## 关键行为（都有测试锁定）

| 行为 | 说明 |
|---|---|
| 一次抓取、永久命中 | 第二次起走本地库；重启进程、换设备都是 HIT |
| 视频无封面不发请求 | `photo.is_video && !photo.thumb` → 直接 skipped（115 不给视频封面，省掉几千次无用请求） |
| 占位图识别 | 115 的"图片已过期"是 3122 字节固定内容；按**内容重复**判定（不按体积，正常缩略图中位数才 3998B） |
| 超限跳过 | 单张 > `THUMB_MAX_BYTES` 或不是图片（HTML 错误页）→ skipped，绝不当缩略图存 |
| ETag / 304 | ETag = 明文 sha256 前 32 位；`If-None-Match` 命中直接 304，省流量 |
| 失败不缓存 | 占位图响应 `no-store`（115-album 踩过"长缓存把失败固化"） |
| 时间窗 | 窗外一次请求都不发；跨零点正确归属"这一夜" |
| 断点续传 | 队列状态 + `updated` 升序取批 + `prewarm_cursor`，进程重启从最久未动处继续 |
| 夜间配额 | `kv.prewarm_night / prewarm_night_done`，过零点自动归零 |
| 暂停 | `pause` 写库，重启后仍生效 |

## 20 万文件的实际预算

接口调用只有一种：目录列表（建库时一次性）。抓图属于"静态小文件下载"，按节流跑：

| `API_MIN_INTERVAL` | 每小时 | 4 小时窗口/夜 | 20 万张需要 |
|---|---|---|---|
| 1.0 s（默认） | 3,600 | 14,400 | ≈ **14 夜** |
| 0.5 s | 7,200 | 28,800 | ≈ 7 夜 |
| 0.4 s | 9,000 | 36,000 | ≈ 5.5 夜 |

磁盘（384 px / q78，按实测 12.9 KB/张口径）：

| 规模 | 全量 | 只有被看过的（懒抓，一年 1–3 万张） |
|---|---|---|
| 20 万 | ≈ 1.6–2.5 GB | ≈ 200–400 MB |
| 50 万 | ≈ 4–6 GB | ≈ 0.5–1 GB |

## 集成到 115-album（4 步）

1. 把 `app/{secretbox,thumb_store,thumb_fetch,throttle,window,thumbs_router}.py` 复制进 `115-album/app/`。
2. `115-album/app/index_store.py` 补一个按 id 取行：

```python
def get(pid: str):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None
```

3. 在 `main.py` 挂路由（`get_photo` 用上面那个 + `upstream_adapter.PhotoLite`）：

```python
from .thumb_store import ThumbStore
from .thumb_fetch import ThumbFetcher
from .thumbs_router import build_router
from .upstream_adapter import PhotoLite, make_fetch_bytes

store = ThumbStore(config.DATA_DIR / "thumbs.db")
fetcher = ThumbFetcher(store, Settings.from_env(),
                       fetch_bytes=make_fetch_bytes(thumbs.fetch_upstream_thumb, get_source()))
async def get_photo(pid: str):
    row = index_store.get(pid)
    return PhotoLite(id=row["id"], name=row["name"], thumb=row["thumb"] or "",
                     size=row["size"] or 0, ctime=row["ctime"] or 0,
                     ext=row["ext"] or "", type=row["type"] or 0) if row else None
app.include_router(build_router(store, get_photo, fetcher))
```

4. 建库时把新条目入队，然后跑 `python tools/thumb_prewarm.py --host ../115相册/115-album daemon`。


## 视频缩略图（已接入，不是"跳过"）

115 的列表接口对视频**不给**缩略图（`u` 为空），所以视频封面只能自己抓一帧：

```
downurl 拿直链（1 次接口调用，30 分钟租约内复用）
  └─ ffmpeg 只读头部 3 MB（-probesize / -rw_timeout 限制）→ 第 1 秒一帧 mjpeg
       └─ 重编码 384px/q78 → 加密入库（与图片同一条管道、同一份节流预算）
```

| 配置 | 默认 | 说明 |
|---|---|---|
| `VIDEO_THUMB_ENABLED` | `1` | 关掉就退化为"视频只显示占位图" |
| `VIDEO_THUMB_BYTES` | `3145728` | ffmpeg 允许读的字节上限（绝不下载整段视频） |
| `VIDEO_THUMB_SEEK` | `1.0` | 取第几秒（避开片头黑屏） |
| `VIDEO_THUMB_TIMEOUT` | `45` | 抓帧超时（秒） |
| `FFMPEG_BIN` | `ffmpeg` | 可执行文件路径 |
| `VIDEO_THUMB_THREADS` | `2` | 解码线程数（2C 机器别给大） |
| `VIDEO_THUMB_SIZE` | `384` | 输出长边，与缩略图库一致 |
| `VIDEO_THUMB_SKIP_EXTS` | `m3u8` | 永不抓帧（HLS 会拉多分片，流量不可控） |
| `VIDEO_THUMB_LOWPRIORITY_EXTS` | `ts,m2ts,mpg,rm,rmvb` | 低优先（无全局索引，可能多读） |
| `VIDEO_THUMB_ALLOW_LOWPRIORITY` | `0` | 1 才尝试上面的格式 |

**抓帧方式与 115-album 完全一致**：把直链交给 ffmpeg 自己读（**不是**读一段字节喂管道），
因为 MP4/MOV 的 `moov` 常在文件尾部，管道不可 seek 就永远解不开；
给 URL 时 ffmpeg 会用 HTTP Range 跳读 moov，**moov 在前在后都能抓**，
且只读它需要的那几段，不会把整片拉下来。

**格式矩阵**：

| 容器 | 抓帧 | 说明 |
|---|---|---|
| mp4 / **mov** / m4v | ✅ | 同一 demuxer；iPhone 的 MOV 多为 moov 在尾部，靠 Range 跳读 |
| mkv / webm | ✅ | Matroska 无 moov，读头部 + 定位关键帧 |
| avi / wmv / flv | ✅ | 索引可在尾部，ffmpeg 会 seek |
| ts / m2ts / mpg | ⚠️ 默认关 | 无全局索引，`-ss` 可能向前扫描，读得多 |
| rmvb / rm | ⚠️ 默认关 | RealMedia 老容器，gyan build 带 rv30/40 解码，多数可解 |
| m3u8 (HLS) | ❌ | 按需拉多个分片，流量不可控 |

**大文件（几十 GB）**：抓帧成本**与文件大小基本无关**，只与 moov 位置和关键帧距离有关，
40 GB 的 mp4 抓一帧通常仍是几 MB 级。真实风险是「CDN 单请求慢 → 超时」和
「4K HEVC 解码吃 CPU」，所以有 `-threads` 限流、`VIDEO_THUMB_TIMEOUT` 超时、
以及**失败记 `failed` 走退避重试**（不是终态跳过，大文件偶发慢，重试往往就好）。

**一处要纠正的认知**：`VIDEO_THUMB_BYTES`（默认 3 MB）是*探测读取量*，
**不是流量硬上限**。moov 在尾部的文件会额外读一小段尾部，总量通常仍 ≤ 5–8 MB。

## 115 第三方登录（开放平台 PKCE）

`app/auth_115.py` 实现了与你 fnOS 那份逆向报告一致的设备码流程：

```
POST  passportapi.115.com/open/authDeviceCode    client_id + code_challenge(S256) + scope
  └─ 返回 uid / qrcode / sign / time
轮询  qrcodeapi.115.com/get/status/             0 待扫码 → 1 已扫码 → 2 已确认
POST  passportapi.115.com/open/deviceCodeToToken uid + code_verifier → access/refresh token
```

- **好处**：不碰网页 Cookie，不会被网页端踢下线；token 有效期 48h，自动 refresh。
- **前提**：`APP_ID_115` 必须是**你自己在 115 开放平台注册的应用**（公开客户端用 PKCE，
  不需要 client_secret；但不能复用别人的 client_id）。
- **零门槛兜底**：网页扫码（115-album 已有 `v115_qr.py`）不需要注册应用，直接拿 Cookie。
- **安全**：token 用 `secretbox` 加密后 base64 存进 `kv`，`thumbs.db` 被拷走也拿不到明文；
  refresh 失败要退避（115 侧对重复刷新会去重甚至熔断）。

快速用法：

```python
from app.auth_115 import LoginSession, TokenVault, ensure_token
vault = TokenVault(store)                       # store = ThumbStore(...)
s = LoginSession(vault)                         # 需要 APP_ID_115
code = await s.start()                          # code.qrcode 给前端渲染二维码
status = await s.wait(timeout=300)              # 2 = 已确认
token = await s.finish()                        # 自动加密入库
tok = await ensure_token(vault)                 # 之后每次取用（自动续期）
```




## 直链与播放（真机实测结论）

`/api/play/{id}` → **302 到 115 CDN**，服务端不中转任何视频字节。三个实测结论：

| 事实 | 证据 |
|---|---|
| `proapi.115.com/app/chrome/downurl` **必须跟随 302** | 之前禁跳转被判失败；`follow_redirects=True` 后 200 + 解密成功 |
| `appapi.115.com` **DNS 已失效** | `Name or service not known` |
| `webapi.115.com/app/chrome/downurl` 返回 404 错误页 | `<title>哎呀，出错了</title>` |
| **直链与签发 UA 严格绑定** | 同 UA → 206 + `RIFF...AVI`；Chrome/Safari/AppleCoreMedia/VLC/空 UA → **全部 403** |
| **带 115 登录 Cookie 取流反而 403** | `{"message":"no cookie value"}` |

所以 `play` 接口用**调用方自己的 UA** 去签发（浏览器/播放器是什么 UA 就用什么），
缓存也按 UA 校验，换 UA 自动重签。响应头 `X-Upstream-UA` 就是必须用来取流的 UA。

实测（Linux 服务器 + Chrome UA 模拟浏览器）：

```
播放接口 HTTP 302 | 签发 UA == 浏览器 UA: True
同 UA 取流:  HTTP 206 | 4096 字节 | 头部 52494646ba69ad0041564920  ("RIFF....AVI ")
换 UA 取流:  HTTP 403   ← 证明绑定确实存在
```

## 数据模型（115emby 自己的，不是相册）

> 曾经为了验证抓图链路，临时复用 115-album 的 `index.db`（`photos` 表）。
> **那不是 115emby 的模型**；已改为下面的新库，`photos`/`pending_links`/EXIF/GPS/时间轴一律不出现。

| 表 | 作用 | 关键点 |
|---|---|---|
| `folders` | 目录树 | `parent_id` 层级，`name_enc` 密文，`name_rank` 同层排序 |
| `items` | 媒体项（`kind=video/image/audio`） | **没有明文 name 列**；没有 taken/camera/lat/lon；`vcodec/acodec` 决定能否直连播放 |
| `links` | 直链缓存 | 每个 item 一行，`url_enc` 密文（URL 含 UID/签名），带 `expires` 与 `ua` |
| `progress` | 播放进度 | 续播与「继续观看」的核心 |

接口（`app/server.py`）：

```
GET  /api/browse?parent=<id>     同层：子文件夹 + 媒体项（名字已解密）
GET  /api/item/{id}              单项 + 同层 prev/next（看图翻页）+ 进度
GET  /api/thumb/{id}             缩略图（加密库出图，ETag/304）
GET  /api/image/{id}             原图 → 302 直连 CDN
GET  /api/play/{id}              视频 → 302 直连 CDN（带 X-Upstream-UA）
POST /api/progress               上报进度
GET  /api/continue               继续观看
GET  /api/health                 自检
```

不做的事（和标准 Emby / 相册的区别）：不刮削 TMDB、不转码、不做剧集季集、不做时间轴/EXIF/GPS/地图。

## 索引名字 + 直链加密（`tools/secure_index.py`）

缩略图库只管图片字节；**文件名 / 文件夹名 / 路径 / 直链**在主索引里，由这一层负责。

```
主密钥(THUMB_ENC_KEY)
 ├─ HMAC(master, "enc|idx:name:v1")  → 名字加解密
 ├─ HMAC(master, "idx|idx:name:v1")  → 名字盲索引/前缀桶
 └─ HMAC(master, "enc|idx:link:v1")  → 直链加解密     ← 三条用途互相隔离
```

| 列 | 内容 |
|---|---|
| `name_enc` / `folder_enc` / `path_enc` | 文件名 / 文件夹名 / 完整路径密文（AES-256-GCM） |
| `name_idx` / `folder_idx` | 盲索引（HMAC，**精确查找**用） |
| `name_bucket` / `folder_bucket` | 前 3 字符盲索引（**前缀搜索**先收敛再解密过滤） |
| `name_rank` | 迁移时按解密后的名字排好序的整数秩 → `ORDER BY name_rank` 即按名字排序 |
| `pending_links.url_enc` | 直链密文（URL 里含 `UID`/签名） |

用法：

```bash
python tools/secure_index.py report  --db data/index.db     # 基线：多少行、哪些列缺
python tools/secure_index.py migrate --db data/index.db     # 加列 + 回填 + 排序秩 + 直链加密
python tools/secure_index.py migrate --db data/index.db --purge   # 抹明文 + VACUUM
python tools/secure_index.py verify  --db data/index.db     # 零残留 + 抽样解密
python tools/secure_index.py search  --db data/index.db --q video_     # 前缀搜索
python tools/secure_index.py list    --db data/index.db --order name   # 按名字排序
```

**实测（生产库副本，14,380 行 / 510 条直链）**

```
enc_rows 14380/14380   links_enc 510/510   plain_rows 0
residue_total 0        （库文件字节级扫不到任何文件名/路径/直链）
search "video_" → 1962 桶候选 → 1956 命中，41.8 ms
```

两个坑（已在代码里处理）：**① `UPDATE` 抹值后必须 `VACUUM`**，否则旧页仍在 freelist 里能被字节扫到；
**② 迁移必须幂等** —— 已 purge 的库再跑一次 migrate 时，无明文就不该覆盖密文（否则数据被写成空）。

代价：库从 7.5 MB 涨到 9.6 MB（+27%），名字不能再用 SQL 直接 `LIKE`/`ORDER BY`（见上表替代方案）。

## 加密能挡住什么

| 场景 | 挡住？ |
|---|---|
| 盘/快照/备份/Docker 层被拷走 | ✅（BLOB 是密文，库里没有文件名、没有 JPEG 头） |
| 服务器被 root | ❌ 原理性上限（进程内存里必有明文与密钥） |
| 115 那边 | ❌ 永远（文件真名在你的 115 账号里是明文） |
| 登录后的使用者截图/另存 | ❌ 不在加密能解决的范围 |
| Cloudflare 等 TLS 终结方 | ❌ 边缘节点可见 URL 与响应（要避免就用自有域名直连） |

## 已验证

- `python tests/test_thumb_c.py` → **270 项通过 / 0 失败**（加密/篡改/换钥、库内无明文、队列与游标、懒抓 HIT、视频抓帧路径、ffmpeg 命令构造与缺失降级、占位图、超限、节流间隔、时间窗含跨零点、预算、暂停与续传、ETag/304/占位图路由、格式策略矩阵（mov 放行 / m3u8 拦截 / ts 低优先）、抓帧失败走 failed 重试、索引名字/直链加密与迁移幂等、
  PKCE 挑战、设备码/轮询/换 token、token 加密入库与自动续期）
- demo 实跑：入队 31 → `now --limit 5` 抓 5 → `window --until-empty` 抓 25（1 个视频跳过）→ `status` 显示 `thumbs=30, enc=AES-256-GCM, window=02:00-06:00(240 分钟)`
- 守护进程实跑：`daemon --window 01:00-06:00` 在 15:33 正确输出「不在时间窗内，下次开门 10-20 01:00」并挂起等待
