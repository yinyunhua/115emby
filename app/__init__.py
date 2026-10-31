"""115emby（缩略图加密 + 懒抓 + 定时预热）

本包只负责本次新增的「缩略图 C 方案」：

* ``secretbox``    信封加密（AES-256-GCM，缺库时回退 HMAC-CTR）
* ``thumb_store``  加密 BLOB 库 + 抓取队列 + 断点续传游标
* ``thumb_fetch``  懒抓、节流、夜间时间窗调度、占位图识别
* ``thumbs_router`` FastAPI 路由（/thumb/{id}，ETag/304）
* ``settings``     环境变量（与 115-album 的 API_MIN_INTERVAL 等保持同名同义）

上层页面（时间轴/灯箱/播放）仍由 ``115-album`` 提供，本包通过
``upstream_adapter`` 与之对接。
"""

__version__ = "0.1.0"
