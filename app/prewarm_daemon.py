"""定时读取（缩略图预热）：按时间窗自动抓，开关与时间可随时改。

取代原来的 cron：应用内自带调度，每 60 秒检查一次
* ``THUMB_PREWARM_ENABLED=1`` 且当前时间在 ``THUMB_PREWARM_WINDOW``（如 02:00-06:00）内 → 启动
* 窗口结束 / 被关闭 → ``run_window`` 自己收工或被取消
* 速度不设数量上限，完全由安全间隔（``THUMB_IMG_MIN_INTERVAL``）决定
"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from . import index_crypto as ic
from .admin_auth import make_guard

BASE_TICK = 60          # 有活干时的检查间隔
MAX_IDLE_TICK = 300     # 队列空时的最大退避（5 分钟，够压掉空转又不至于迟钝）


def _idle_delay(idle: int) -> int:
    """★ req-prewarmidle：空转越久，间隔越长（60→120→240→300 封顶）。"""
    if idle <= 0:
        return BASE_TICK
    return min(BASE_TICK * (2 ** min(idle - 1, 3)), MAX_IDLE_TICK)


def poke(app) -> None:
    """★ req-prewarmidle：立刻唤醒守护循环重新判断。

    退避之后循环可能正睡 5 分钟，若不唤醒，用户刚保存的时间窗/开关
    要等好几分钟才生效（实测还原 22:00-06:00 后 inside 仍是 True）。
    后台设置改动时调它，做到"改完立刻生效"。"""
    holder = getattr(getattr(app, "state", None), "prewarm_holder", None)
    if not holder:
        return
    ev = holder.get("wake")
    try:
        if ev is not None:
            ev.set()
    except Exception:
        pass


def _parse_window(raw: str):
    s = (raw or "02:00-06:00").strip()
    try:
        a, b = s.split("-")
        ha, ma = [int(x) for x in a.strip().split(":")]
        hb, mb = [int(x) for x in b.strip().split(":")]
        return ha * 60 + ma, hb * 60 + mb
    except Exception:
        return 120, 360


def _inside(cur: int, start: int, end: int) -> bool:
    if start == end:            # 起止相同 = 全天
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end      # 跨零点


def install(app) -> None:
    db = getattr(app.state, "db", None)
    fetcher = getattr(app.state, "fetcher", None)
    if db is None or fetcher is None:
        return

    holder = {"task": None, "started_at": 0, "idle": 0, "inside": False,
              "wake": asyncio.Event(),
              "manual": {"running": False, "limit": 0, "started": 0, "finished": 0,
                         "stopped": "", "error": "", "fetched": 0, "failed": 0,
                         "skipped": 0, "hit": 0}}

    async def _photo(pid: str):
        row = db.get_item(pid)
        if not row:
            return None
        try:
            thumb = ic.safe_unseal_link(row["thumb_enc"] or "")
        except Exception:
            thumb = ""
        kind = row["kind"] or ""
        try:
            name = ic.safe_unseal(row["name_enc"] or "")
        except Exception:
            name = ""
        return type("P", (), {
            "id": pid, "name": name, "thumb": thumb,
            "size": int(row["size"] or 0), "ctime": int(row["mtime"] or 0),
            "ext": row["ext"] or "", "type": 2 if kind == "video" else 5,
            "is_video": kind == "video",
        })()

    def _run_night():
        """启动一次窗口内预热（调用方已确认有活干）。"""
        try:
            fetcher.settings.window = raw_now[0]          # 让 run_window 用最新时间窗
            fetcher.settings.max_per_night = 0            # 不设数量上限，按安全间隔
            store = getattr(app.state, "store", None)
            if store is not None:
                store.kv_set("prewarm_paused", "0")       # 清掉历史"暂停"标记
        except Exception:
            pass
        holder["task"] = asyncio.create_task(
            fetcher.run_window(_photo, respect_window=True))
        holder["started_at"] = int(time.time())

    raw_now = ["02:00-06:00"]

    async def _loop():
        """★ req-prewarmidle：**队列没有待抓的就不起 run_window**。

        以前队列抓空后仍每分钟起一次，每次都空转（一次 kv 写 + 一行日志，
        实测累计 2383 次 queue-empty、每晚约 480 次）。现在：
          1. 起跑前先用 has_work() 探一下（一条 LIMIT 1 索引查询，约 0.4ms）；
          2. 没活干就**什么都不做**（不写库、不打日志），并按 60→600 秒退避；
          3. 一旦有新内容入队，最多 10 分钟内自动接着抓。
        """
        idle = 0
        while True:
            try:
                enabled = (os.getenv("THUMB_PREWARM_ENABLED") or "0").strip() == "1"
                raw = (os.getenv("THUMB_PREWARM_WINDOW") or "02:00-06:00").strip()
                raw_now[0] = raw
                start, end = _parse_window(raw)
                cur = time.localtime()
                cur_min = cur.tm_hour * 60 + cur.tm_min
                inside = _inside(cur_min, start, end)

                t = holder["task"]
                running = t is not None and not t.done()

                if enabled and inside and not running:
                    has = False
                    try:
                        has = bool(await asyncio.to_thread(fetcher.has_work))
                    except Exception:
                        has = False
                    if has:
                        idle = 0
                        _run_night()
                    else:
                        idle += 1          # 没活干 → 退避，不做任何写入/日志
                elif running and (not enabled or not inside):
                    t.cancel()             # 关掉，或时间窗改了 → 立刻收工
                    holder["task"] = None
                    idle = 0
                if not enabled or not inside:
                    idle = 0               # 窗口外不需要退避（本来就不动）
                holder["inside"] = bool(enabled and inside)
            except Exception:
                pass
            holder["idle"] = idle
            # ★ req-prewarmidle：用可被 poke() 打断的等待代替裸 sleep，
            #   这样"保存计划"能立刻生效，而不用等退避结束。
            ev = holder.get("wake")
            try:
                await asyncio.wait_for(ev.wait(), timeout=_idle_delay(idle))
            except asyncio.TimeoutError:
                pass
            except Exception:
                await asyncio.sleep(_idle_delay(idle))
            if ev is not None:
                ev.clear()

    async def _manual(limit: int) -> None:
        """★ req-thumbnow：立即生成（不等时间窗，也不占夜间预算）。"""
        st = holder["manual"]
        try:
            summary = await fetcher.run_window(
                _photo, respect_window=False,
                max_items=(limit if limit > 0 else 200000))
            st.update({"running": False, "finished": int(time.time()),
                       "stopped": summary.get("stopped", ""),
                       "fetched": summary.get("fetched", 0),
                       "failed": summary.get("failed", 0),
                       "skipped": summary.get("skipped", 0),
                       "hit": summary.get("hit", 0)})
        except Exception as exc:                        # noqa: BLE001
            st.update({"running": False, "finished": int(time.time()),
                       "error": "%s: %s" % (type(exc).__name__, exc)})

    guard = getattr(app.state, "admin_guard", None) or make_guard()

    @app.post("/admin/thumb/prewarm", dependencies=[Depends(guard)])
    async def prewarm_now(body: dict = Body(default_factory=dict)):
        """立即生成缩略图：{limit} 不传或 0 = 把待生成的全部抓完。"""
        st = holder["manual"]
        if st.get("running"):
            raise HTTPException(409, "立即生成已在运行")
        t = holder["task"]
        if t is not None and not t.done():
            raise HTTPException(409, "定时窗口正在生成，请稍候再试")
        try:
            limit = int((body or {}).get("limit") or 0)
        except Exception:
            limit = 0
        st.update({"running": True, "limit": limit, "started": int(time.time()),
                   "finished": 0, "stopped": "", "error": "",
                   "fetched": 0, "failed": 0, "skipped": 0, "hit": 0})
        asyncio.create_task(_manual(limit))
        return JSONResponse({"ok": True, "limit": limit, "started": st["started"]})

    @app.on_event("startup")
    async def _boot():
        asyncio.create_task(_loop())

    app.state.prewarm_holder = holder
