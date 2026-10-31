# 115emby

把 **115 网盘**当成一个能流畅浏览的私人媒体库：目录树、缩略图墙、随机发现、
在线播放（含 HLS）、搜索、收藏，以及**给访客发的受限账户**。

> 索引与名字在本地加密，缩略图自己抓并自己存，115 只当"文件存放处"。
> 面向小内存机器（2 核 / 1GB 级别）优化过。

---

## 一、功能

| 模块 | 说明 |
|---|---|
| **随机发现** | 从全库随机抽目录展示（每个目录 1 张、一次 80 个目录），「换一批」换一批新的 |
| **目录浏览** | 目录树 + 缩略图墙 + 列表模式；横向子目录带**可拖动的细滑动条**（电脑与手机都有） |
| **缩略图** | 抓 115 缩略图；视频用 ffmpeg 抓帧；目录自己没有媒体时**自动向下级目录取封面** |
| **封面** | 每个目录 1~4 张预览，按张数**精确切分**（1 张铺满 / 2 张左右 / 3~4 张九宫格），不留半格空白 |
| **卡片发光** | 按图片边缘像素取色、向四周发散（距离与强度都可通过 CSS 变量调） |
| **长名字** | 侧边栏与卡片名字太长时**滚动显示**，不截断成省略号 |
| **播放** | 图片灯箱、视频直链播放、HLS 代理、外部播放器 handoff |
| **搜索** | 全库名字搜索（支持中文），内存索引，命中的目录与条目分开列 |
| **收藏** | 条目级收藏，跨目录聚合 |
| **账户管理** | 给访客发独立口令（**只凭口令登录，不用记账户名**），**严格限定可见目录**；账户列表右侧显示可访问目录名 |
| **媒体概览** | 待生成 / 已生成 / 已排除 的真实数量，失败原因归类 |
| **后台** | 扫描管理、系统设置、缩略图队列、账户管理、访问安全 |
| **首次部署** | 第一次打开网页会强制设置管理口令（可选部署口令 `SETUP_TOKEN` 防止被人抢先） |

---

## 二、快速开始

### 1. 准备

* 一个 115 账号
* Docker 与 Docker Compose
* 抓视频封面需要 ffmpeg —— **镜像里已装好**

### 2. 起来

```bash
git clone <仓库地址> 115emby
cd 115emby
cp .env.example .env
# 至少填 COOKIE_115（或 ACCESS_TOKEN_115 / APP_ID_115）
$EDITOR .env

mkdir -p data
docker compose up -d --build
docker compose logs -f          # 看到 Application startup complete 即可
```

默认只监听 `127.0.0.1:17867`。要对外用，建议前面挂一层 Caddy / Nginx 做 TLS，
**不要**直接把 8000 暴露到公网。

### 3. 首次启动：设置管理口令 ⚠️

打开 `http://127.0.0.1:17867/` → 自动跳到登录页，并且页面**直接变成「首次部署」表单**：

1. 输入新口令（**至少 8 位**）+ 再输一次确认
2. 点「设置并进入」
3. 口令存到 `DATA_DIR/site_auth.json`（PBKDF2-HMAC-SHA256，20 万轮），设置完直接进站

设置过一次之后这个入口**永久关闭**。

> **公网部署务必先设 `SETUP_TOKEN`**。不设的话，谁先打开页面谁就能设口令。
> 设了之后首次表单会多一个「部署口令」输入框。

忘了口令可以在服务器上改：

```bash
docker compose exec app python -m app.site_auth setpw <新口令>
```

### 4. 添加扫描目录

后台 `/admin` → **扫描管理** → 「扫描目录」里添加 115 的目录 id（或点目录树选），
然后点「开始扫描」。首次扫描会走一遍全库，**很慢**（受 `API_MIN_INTERVAL` 节流），
中途可以关页面，扫描在服务端继续。

### 5. 生成缩略图

扫描完成后，后台「媒体概览」能看到「待生成」数量。

* 想慢慢来：什么都不用做，用户翻到哪张抓哪张（懒抓）
* 想提前抓：把 `THUMB_PREWARM_ENABLED=1`，或后台点「立即生成」

> ⚠️ 缩略图抓取会占用你 115 账号的请求额度。如果这个账号**同时在跑别的应用**，
> 一定把 `API_MIN_INTERVAL` 调大（≥1.0），别把风控额度一次打满。

---

## 三、各平台怎么浏览与播放

页面本身是纯 HTML/CSS/JS，**没有构建步骤**，任何现代浏览器都能直接打开。

### 电脑（Windows / macOS / Linux）

| 浏览器 | 浏览 | 播放 |
|---|---|---|
| **Chrome / Edge / Firefox** | ✅ 全部功能 | 图片 ✅ · MP4 ✅ · **AVI/MKV/RMVB/TS 需装 HLS 插件** |
| **Safari（macOS）** | ✅ | 图片 ✅ · MP4 ✅ · HLS ✅ 原生支持 |

**桌面浏览器为什么要装 HLS 插件**：
115 对非 MP4 格式的转码流是 HLS（`.m3u8`），而 Chrome / Edge / Firefox
**不支持原生 HLS**，页面会退回 `hls.js` 拉流 —— 但 115 的转码 CDN 只允许
`https://115.com` 跨域，`hls.js` 的请求会被 CORS 拒绝。

**解决办法：装一个 userscript**（仓库里已附带）：

```
app/static/115emby-hls-desktop.user.js
```

安装步骤：

1. 浏览器装 **Tampermonkey**（Chrome / Edge / Firefox 都有）
2. 打开 Tampermonkey → 「添加新脚本」→ 把 `115emby-hls-desktop.user.js` 全文粘进去 → 保存
   （或者把该文件拖进浏览器，Tampermonkey 会自动识别）
3. 回到 115emby 播放页刷新即可

脚本原理：用 `GM_xmlhttpRequest`（不受页面 CORS 限制）去取 `.m3u8` 与分片，
再喂给页面的播放器。**只在你自己的站点上生效**，`@match` 是通配 `*://*/play/*`，
换成自己的域名也不用改。

### 安卓

| 场景 | 浏览 | 播放 |
|---|---|---|
| **Chrome / Edge / 夸克 / 微信内置** | ✅ | 图片 ✅ · MP4 ✅ · **AVI/MKV/RMVB 需装 HLS 插件** |
| **可装 Tampermonkey 的浏览器** | ✅ | 装上面的 userscript → 全格式 ✅ |

安卓上装 userscript 的可行方案：

* **Kiwi Browser**（基于 Chromium，**支持 Chrome 扩展**）→ 装 Tampermonkey → 导入 userscript
* **Firefox for Android** → 装 Tampermonkey → 导入 userscript
* **Edge for Android** 也支持部分扩展

如果不想装插件：MP4 可以直接播；其它格式可以在播放页点「外部播放器」
（生成一个临时链接，丢给 VLC / MX Player 打开）。

### iPhone / iPad

| 浏览器 | 浏览 | 播放 |
|---|---|---|
| **Safari** | ✅ | 图片 ✅ · MP4 ✅ · **HLS ✅（iOS 原生支持）** |
| Chrome / Edge（iOS 版） | ✅ | 同上（iOS 上它们都是 Safari 内核） |

**iOS 不需要插件** —— 系统原生支持 HLS，所以 AVI/MKV/RMVB 的转码流能直接播。
这也是为什么桌面端才需要那个 userscript。

### 一句话总结

| 平台 | 要不要 HLS 插件 |
|---|---|
| **iOS / iPadOS / macOS Safari** | ❌ 不用，原生支持 |
| **电脑 Chrome / Edge / Firefox** | ✅ 要装 `115emby-hls-desktop.user.js` |
| **安卓 Chrome / Edge / 夸克** | ✅ 要装（或换 Kiwi / Firefox 安卓版装 Tampermonkey） |
| **任何平台只看图片 / MP4** | ❌ 不用 |

---

## 四、配置

全部环境变量见 **[`.env.example`](.env.example)**（每一项都有注释）。
最常改的几个：

| 变量 | 默认 | 说明 |
|---|---|---|
| `COOKIE_115` | 空 | 115 网页版 Cookie，**最常用**的凭据方式 |
| `API_MIN_INTERVAL` | `1.0` | 上游请求最小间隔（秒）。账号共享时**调大** |
| `THUMB_PREWARM_ENABLED` | `0` | 夜间定时抓缩略图 |
| `VIDEO_THUMB_ENABLED` | `1` | 视频抓帧（需要 ffmpeg，镜像自带） |
| `SETUP_TOKEN` | 空 | 首次部署口令，公网**建议设置** |
| `MIN_IMAGE_BYTES` | `30000` | 小于此体积的图不显示（二维码、1px 占位图） |
| `HIDE_VIDEO_MAX_BYTES` | `0` | 小于此体积的视频判为广告片（0 = 不过滤） |
| `DISCOVER_WIN` | `200` | 随机发现每个目录参与抽样的窗口大小 |

密钥三选一（`THUMB_ENC_KEY` / `THUMB_ENC_PASSPHRASE`+盐 / 都不配走本机随机）。
**换密钥 = 旧索引全部解不开**，务必先把密钥备份走再迁移。

### 调界面

卡片发光的距离与强度集中在两个地方，改完刷新即生效（`browse2.html` 是 `no-cache`）：

```css
.glowcard { --gr: 1.45; }   /* 发散距离倍率：1 = 很近，2 = 更远 */
```

强度改 `.glowcard` 那四层 `box-shadow` 的 alpha 值（默认 `.98 / .72 / .42 / .20`）。
弱机降级：`body.lowfx`（自动）、`body.nofx`（全关）。

---

## 五、安全说明（请认真读）

* **索引里的文件名、目录名是加密存的**，缩略图也是加密存的。
  密钥不配的话会自动生成在 `DATA_DIR/.thumbkey`，**和密文同一块盘** ——
  这种情况下加密只能防"数据库文件被单独拷走"。
* 真要防住，密钥必须放在**机器之外**（密码管理器 / KMS / 只读挂载的密钥文件），
  并把 `DATA_DIR` 的备份与密钥**分开保存**。
* 站点口令、访客账户口令都是 PBKDF2-HMAC-SHA256，各自独立加盐。
* 访客账户**只凭口令登录**：后端按口令逐个比对（管理员优先），
  所以访客不用记账户名，但也意味着**口令要够长**（建议 ≥ 8 位，别用纯数字短口令）。
* 访客账户的可见性**逐条服务端校验**：未授权的目录 / 条目一律返回 404，
  并且新增接口默认对访客关闭（白名单制），不会因为忘了加判断而泄露。
* `DATA_DIR` 里有全部索引、缩略图、口令与密钥 —— 备份它，也别把它提交到仓库
  （`.gitignore` 已排除）。

---

## 六、目录结构

```
app/                  服务端（FastAPI）
  serve.py            装配入口（uvicorn app.serve:app）
  main.py             应用与中间件
  site_auth.py        站点登录 / 首次部署 / 会话
  accounts.py         访客账户 + 授权目录（严格可见性判定）
  perm.py             访客的默认拒绝白名单
  admin_*.py          后台接口
  frontend_api.py     前台 JSON 接口（目录树/列表/随机发现/搜索/收藏）
  server.py           出图、播放、直链、handoff
  source115.py        115 上游客户端（分页、节流、重试）
  multi_scan.py       扫描器；fast_scan.py 快扫；scanner.py 走队列
  thumb_*.py          缩略图抓取 / 存储 / 预热
  secretbox.py        索引与缩略图的加密
  static/             前端（无构建步骤，直接是 html/css/js）
    115emby-hls-desktop.user.js   ← 桌面/安卓 HLS 插件
    player.html / browse2.html / admin.html / login.html
tools/                运维脚本（批量抓图、导入索引、快扫等）
  ops/                历史归档：一次性故障处理脚本，别直接跑
tests/                C 方案缩略图的测试
docs/                 设计文档
data/                 运行期数据（**不入库**）
```

---

## 七、已知限制

* **115 只给图片缩略图**，视频封面必须自己抓帧 —— 没装 ffmpeg 时视频是空封面。
* 115 对"生成不了缩略图"的文件会返回一张**固定的灰色占位图**，
  本项目识别并跳过（详见 `docs/DESIGN-缩略图方案.md`）。
* 抓帧会真实消耗上游流量，`VIDEO_THUMB_BYTES` 限制的是**读取上限**，不是流量上限。
* 全库扫描很慢，这是上游节流的必然结果，不是 bug。
* **单个目录列取失败不会终止整个扫描任务**（会记进 `scan_failed` 并跳过），
  连续 5 个失败才停（多半是 Cookie 失效）；被跳过的目录用后台单目录补扫补回来。
* 前端不做构建，改 `app/static/*.html` 刷新即生效；
  `admin.js` / `admin.css` 有版本号（`?v=`），改完要把版本号加一。
* 桌面端播放非 MP4 格式**必须装 HLS userscript**，这是 115 CDN 的 CORS 限制决定的，
  不是本项目的 bug。

---

## 八、License

[MIT](LICENSE)
