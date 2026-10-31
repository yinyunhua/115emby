#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在服务器本机：先重置被误判的视频，再走应用真实 HTTP 路径抓完缩略图。
后台 nohup 运行，进度写 /tmp/fetch_pending.log，不依赖 SSH 长连接。
"""
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.getenv("BASE_URL") or "http://127.0.0.1:8080"
BASE_DIR = os.getenv("BASE_DIR") or "/app"          # 部署目录（容器里是 /app）
THUMBS = os.getenv("THUMB_DB") or (BASE_DIR + "/data/thumbs.db")
EMBY = os.getenv("EMBY_DB") or (BASE_DIR + "/data/emby.db")
LOG = "/tmp/fetch_pending.log"


def log(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def reset():
    """把"被误判成图片"的视频 + failed 视频重新排队（updated=1 → 队首）。"""
    con = sqlite3.connect(THUMBS, timeout=30)
    con.execute("ATTACH DATABASE ? AS e", (EMBY,))
    n1 = con.execute(
        "UPDATE thumb_src SET state='pending', attempts=0, last_err='', updated=1 "
        "WHERE state='skipped' AND last_err='上游占位图' "
        "AND pid IN (SELECT id FROM e.items WHERE kind='video')").rowcount
    n2 = con.execute(
        "UPDATE thumb_src SET state='pending', attempts=0, last_err='', updated=1 "
        "WHERE state='failed' "
        "AND pid IN (SELECT id FROM e.items WHERE kind='video')").rowcount
    con.commit()
    log("重置：占位图误判 %d 条 + failed %d 条" % (n1, n2))
    con.close()


def pending_list():
    con = sqlite3.connect(THUMBS, timeout=30)
    con.execute("ATTACH DATABASE ? AS e", (EMBY,))
    rows = con.execute(
        "SELECT s.pid, COALESCE(i.ext,'?') ext FROM thumb_src s "
        "LEFT JOIN e.items i ON i.id=s.pid "
        "WHERE s.state='pending' AND s.updated=1 ORDER BY s.pid").fetchall()
    con.close()
    return rows


def main():
    open(LOG, "w", encoding="utf-8").close()
    # 站点口令：优先环境变量，其次默认口令文件（可用 SITEPW_FILE 覆盖）
    pf = os.getenv("SITEPW_FILE") or (BASE_DIR + "/.sitepw")
    pw = (os.getenv("SITEPW") or "").strip() or open(pf).read().strip()
    rq = urllib.request.Request(
        BASE + "/api/login", data=json.dumps({"password": pw}).encode(),
        headers={"Content-Type": "application/json"})
    ck = urllib.request.urlopen(rq, timeout=30).headers.get("Set-Cookie", "").split(";")[0]
    log("登录成功")
    reset()
    rows = pending_list()
    total = len(rows)
    log("待抓 %d 条（视频 %d）" % (total, sum(1 for r in rows if r[1] in
        ("mp4", "ts", "mov", "avi", "rm", "rmvb", "mkv", "wmv", "flv", "3gp", "m2ts", "mpg"))))
    ok = bad = 0
    t0 = time.time()
    for n, (pid, ext) in enumerate(rows, 1):
        try:
            rq = urllib.request.Request(BASE + "/api/thumb/" + urllib.parse.quote(pid, safe=""),
                                        headers={"Cookie": ck})
            resp = urllib.request.urlopen(rq, timeout=200)
            body = resp.read()
            good = len(body) > 400 and "svg" not in (resp.headers.get("Content-Type") or "")
            ok += 1 if good else 0
            bad += 0 if good else 1
            res = ("OK %dB %s" % (len(body), resp.headers.get("X-Thumb"))) if good else "占位/跳过"
        except urllib.error.HTTPError as e:
            bad += 1
            res = "HTTP %s" % e.code
        except Exception as e:                                   # noqa: BLE001
            bad += 1
            res = str(e)[:30]
        if n % 10 == 0 or n == total:
            log("[%d/%d] 成功 %d 失败 %d | %-4s %s" % (n, total, ok, bad, ext, res))
    log("完成：成功 %d / 失败 %d，用时 %.1f 分钟" % (ok, bad, (time.time() - t0) / 60))
    log("最终状态：%s" % (dict(sqlite3.connect(THUMBS).execute(
        "SELECT state, COUNT(*) FROM thumb_src GROUP BY state").fetchall()),))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                                     # noqa: BLE001
        log("异常终止：%r" % (exc,))
