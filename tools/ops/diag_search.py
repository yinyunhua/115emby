"""诊断 115 搜索接口：打印每个 base 的 HTTP 状态与响应片段。"""
import asyncio
import sys

sys.path.insert(0, "/app")
import httpx                                              # noqa: E402
from app import source115                                 # noqa: E402


async def main():
    print("WEBAPI_BASES:", source115.WEBAPI_BASES)
    h = source115._headers()
    print("请求头:", {k: (v[:18] + "…" if len(str(v)) > 20 else v) for k, v in h.items()})
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0),
                                 follow_redirects=True, verify=False) as client:
        for base in list(source115.WEBAPI_BASES) + ["https://webapi.115.com", "https://115.com"]:
            for kw in ("BT-btt", "澳门银河"):
                try:
                    r = await client.get(base + "/files/search",
                                         params={"search_value": kw, "cid": "0", "aid": "1",
                                                 "offset": 0, "limit": 20, "format": "json"},
                                         headers=h)
                    txt = r.text[:220].replace("\n", " ")
                    print("  %-28s %-8s HTTP %s  %s" % (base.replace("https://", ""), kw,
                                                        r.status_code, txt))
                except Exception as exc:                  # noqa: BLE001
                    print("  %-28s %-8s 异常 %s" % (base.replace("https://", ""), kw, str(exc)[:90]))


asyncio.run(main())
