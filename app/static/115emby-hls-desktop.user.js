// ==UserScript==
// @name         115emby 全平台 HLS 播放（绕过 CORS）
// @namespace    https://github.com/115emby/115emby
// @version      1.1.0
// @description  让「不支持原生 HLS」的浏览器播放 115emby 的 AVI/MKV/RMVB 等转码视频。适用：电脑 Chrome/Edge、安卓 Chrome/Edge/Alook/Kiwi/Firefox、以及其他 Chromium 内核浏览器。原理：GM_xmlhttpRequest 以扩展特权拉流，绕过 115 CDN 的 CORS 限制；分片直连 115 CDN，不经过服务器，不占服务器带宽。
// @author       -
// @match        *://*/play/*
// @match        *://*/view/*
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      *
// @run-at       document-start
// ==/UserScript==

/*
 * 背景
 * ────
 * 115 的 HLS 转码流（cpats01.115.com）返回：
 *     Access-Control-Allow-Origin: https://115.com
 * 且 OPTIONS 预检直接 403。Hls.js 用 fetch/XHR 拉流 → 被 CORS 拒绝。
 *
 * 谁需要这个脚本？
 * ────────────────
 *   浏览器「不支持原生 HLS」→ 页面走 Hls.js → 受 CORS 限制 → 播不了
 *     · 电脑 Chrome / Edge / Firefox / Brave ……
 *     · 安卓 Alook / Chrome / Edge / Kiwi / Firefox ……
 *   浏览器「支持原生 HLS」→ 页面走 <video src>（媒体元素不做 CORS 检查）→ 本来就能播
 *     · iOS / iPadOS 的 Safari、Alook、Chrome（都是 WebKit 内核）
 *     · macOS Safari
 *   本脚本会自动判断，不需要原生 HLS 的浏览器上完全不生效。
 *
 * 方案
 * ────
 * GM_xmlhttpRequest 以「扩展特权」发请求，不受页面 CORS 约束。
 * 给 Hls.js 换一个用 GM 请求的 loader：
 *   · m3u8 由本站 /api/hls/{id}/at 签发（服务器只出约 300 字节）
 *   · 分片由浏览器直接去 115 CDN 拉（GM 特权）→ 服务器 0 字节
 *
 * 关键约束
 * ────────
 * 115 签名绑定 UA（se=u,ua）。服务器用「请求头 UA」签发，
 * 所以 GM 请求必须带与页面完全一致的 UA，否则 invalid signature。
 */

(function () {
  'use strict';

  const W = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;

  // ────────── 0. 只在不支持原生 HLS 的浏览器上启用 ──────────
  // iOS / macOS 的 WebKit 系列支持原生 HLS，页面不会用 Hls.js，脚本无需介入。
  function supportsNativeHls() {
    try {
      const v = document.createElement('video');
      if (v.canPlayType('application/vnd.apple.mpegurl')) return true;
      // 部分浏览器返回 'maybe'
      const r = v.canPlayType('application/x-mpegURL');
      return r === 'probably' || r === 'maybe';
    } catch (e) {
      return false;
    }
  }
  if (supportsNativeHls()) return;   // 原生能播，直接退出，不干扰页面

  // ────────── 1. GM 请求封装 ──────────
  function gmGet(url, responseType, timeout) {
    return new Promise(function (resolve, reject) {
      GM_xmlhttpRequest({
        method: 'GET',
        url: url,
        responseType: responseType,
        timeout: timeout,
        headers: {
          // ★ 必须与页面 UA 完全一致：115 签名 se=u,ua 绑定 UA
          'User-Agent': navigator.userAgent,
          'Referer': location.origin + '/',
        },
        onload: function (res) {
          if (res.status >= 200 && res.status < 300) {
            resolve(res.response);
          } else {
            let extra = '';
            try {
              if (typeof res.response === 'string') {
                extra = ' — ' + res.response.slice(0, 160);
              } else if (res.response && res.response.byteLength) {
                extra = ' — ' + String.fromCharCode.apply(null,
                  new Uint8Array(res.response).slice(0, 160));
              }
            } catch (e) { /* ignore */ }
            reject(new Error('HTTP ' + res.status + extra));
          }
        },
        onerror: function () { reject(new Error('网络错误')); },
        ontimeout: function () { reject(new Error('请求超时')); },
      });
    });
  }

  // ────────── 2. 自定义 Loader ──────────
  // hls.js 1.x Loader 接口：
  //   constructor(config) ; load(context, config, callbacks) ; abort() ; destroy()
  //   this.stats = { aborted, loaded, retry, total, chunkCount, bwEstimate,
  //                  loading:{start,first,end}, parsing:{start,end}, buffering:{start,end} }
  function makeGMLoader() {
    function GMLoader(config) {
      this.config = config || {};
      this.stats = {
        aborted: false, loaded: 0, retry: 0, total: 0, chunkCount: 0, bwEstimate: 0,
        loading: { start: 0, first: 0, end: 0 },
        parsing: { start: 0, end: 0 },
        buffering: { start: 0, end: 0 },
      };
    }

    GMLoader.prototype.load = function (context, config, callbacks) {
      const self = this;
      const url = context.url;
      const sameOrigin = url.indexOf('/') === 0 || url.indexOf(location.origin) === 0;

      // 本站请求（/api/hls/.../at）同源，用原生 fetch 即可
      if (sameOrigin) {
        this.stats.loading.start = performance.now();
        fetch(url, { credentials: 'same-origin' })
          .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.text();
          })
          .then(function (text) {
            self.stats.loading.first = performance.now();
            self.stats.loading.end = performance.now();
            self.stats.loaded = self.stats.total = text.length;
            callbacks.onSuccess({ url: url, data: text }, self.stats, context, null);
          })
          .catch(function (err) {
            callbacks.onError({ code: 0, text: err.message }, context, null, self.stats);
          });
        return;
      }

      // 115 CDN 的流 → GM 特权请求，绕过 CORS
      const isPlaylist = /\.m3u8(\?|$)/i.test(url);
      this.stats.loading.start = performance.now();

      gmGet(url, isPlaylist ? 'text' : 'arraybuffer', isPlaylist ? 20000 : 45000)
        .then(function (data) {
          if (self.stats.aborted) return;
          self.stats.loading.first = performance.now();
          self.stats.loading.end = performance.now();

          if (isPlaylist) {
            const text = String(data);
            self.stats.loaded = self.stats.total = text.length;
            callbacks.onSuccess({ url: url, data: text }, self.stats, context, null);
          } else {
            let buf = data;
            if (buf && !(buf instanceof ArrayBuffer)) {
              buf = new Uint8Array(buf).buffer;
            }
            self.stats.loaded = self.stats.total = buf ? buf.byteLength : 0;
            callbacks.onSuccess({ url: url, data: buf }, self.stats, context, null);
          }
        })
        .catch(function (err) {
          if (self.stats.aborted) return;
          callbacks.onError({ code: 0, text: err.message }, context, null, self.stats);
        });
    };

    GMLoader.prototype.abort = function () {
      this.stats.aborted = true;
    };

    GMLoader.prototype.destroy = function () {
      this.abort();
    };

    return GMLoader;
  }

  // ────────── 3. 注入 ──────────
  let patched = false;

  function patchHls() {
    if (patched) return true;
    const Hls = W.Hls;
    if (!Hls || !Hls.DefaultConfig) return false;
    try {
      Hls.DefaultConfig.loader = makeGMLoader();
      // 手机内存有限，缓冲别开太大；桌面可稍宽
      const mobile = /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
      Hls.DefaultConfig.maxBufferLength = mobile ? 20 : 40;
      Hls.DefaultConfig.maxMaxBufferLength = mobile ? 40 : 80;
      Hls.DefaultConfig.manifestLoadingTimeOut = 25000;
      Hls.DefaultConfig.fragLoadingTimeOut = 45000;
      patched = true;
      console.info('[115emby-GM] Hls.js loader → GM 特权请求（绕过 CORS）');
      return true;
    } catch (e) {
      console.warn('[115emby-GM] 注入失败:', e);
      return false;
    }
  }

  // Hls.js 是页面按需动态加载的（/static/vendor/hls/hls.min.js），
  // 轮询等它出现，必须早于 new Hls() 调用
  let tries = 0;
  const timer = setInterval(function () {
    if (patchHls() || ++tries > 300) clearInterval(timer);
  }, 100);
  if (document.readyState !== 'loading') patchHls();

  // ────────── 4. 提示 ──────────
  function toast(msg) {
    if (!document.body) return;
    const d = document.createElement('div');
    d.textContent = msg;
    d.style.cssText = [
      'position:fixed', 'left:50%', 'bottom:30px', 'transform:translateX(-50%)',
      'z-index:2147483647', 'padding:9px 16px', 'border-radius:999px',
      'background:rgba(20,26,36,.92)', 'color:#e8eef8',
      'font:13px/1.5 -apple-system,BlinkMacSystemFont,sans-serif',
      'border:1px solid rgba(255,255,255,.16)',
      'box-shadow:0 8px 24px rgba(0,0,0,.5)',
      'backdrop-filter:blur(12px)', '-webkit-backdrop-filter:blur(12px)',
      'pointer-events:none', 'opacity:0', 'transition:opacity .25s',
    ].join(';');
    document.body.appendChild(d);
    requestAnimationFrame(function () { d.style.opacity = '1'; });
    setTimeout(function () {
      d.style.opacity = '0';
      setTimeout(function () { d.remove(); }, 300);
    }, 2600);
  }

  // loadSource 被调用 = 真的要用 HLS 了，此时提示一次
  let toasted = false;
  function hookLoadSource() {
    const Hls = W.Hls;
    if (!Hls || !Hls.prototype || !Hls.prototype.loadSource || Hls.prototype.__gmHooked) return;
    const orig = Hls.prototype.loadSource;
    Hls.prototype.loadSource = function (url) {
      if (patchHls() && !toasted) {
        toasted = true;
        toast('已启用油猴 HLS 加速 · 绕过 CORS');
      }
      return orig.apply(this, arguments);
    };
    Hls.prototype.__gmHooked = true;
  }

  let t2tries = 0;
  const t2 = setInterval(function () {
    hookLoadSource();
    if ((W.Hls && W.Hls.prototype && W.Hls.prototype.__gmHooked) || ++t2tries > 300) clearInterval(t2);
  }, 100);
})();
