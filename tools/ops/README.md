# 一次性运维脚本（历史归档）

这些脚本是**针对某次具体故障/清理**写的，参数与判定都绑定了当时的库状态，
**不建议直接跑**。留在这里是为了保留"当时怎么处理的"的线索。

通用能力请用上一层的脚本（`fetch_pending.py` / `quick_scan.py` /
`import_album_index.py` / `secure_index.py` / `refresh_thumb_urls.py` 等）。

⚠️ 其中 `fix_library.py` 里的目录 ID 已替换成占位符 `ROOT_CID_PLACEHOLDER`，
用之前必须改成你自己的目录 id。
