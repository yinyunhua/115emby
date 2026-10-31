"""115emby 启动入口。

main.py 保持上游原样，功能都在独立模块：
* admin_auth     —— /login 登录页 + HttpOnly Cookie 会话（后台不再需要口令输入框）
* admin_settings —— /admin/settings /api/root
* multi_scan     —— /admin/scan/multi（多根目录串行）+ /admin/scan/progress（层级进度）
* fast_thumbs    —— 图片缩略图独立油门 + 按需换签名 + 480px
* fast_scan      —— 目录扫描并行预读
* video_fix      —— downurl 下载专用 Cookie + 省内存 ffmpeg + 低优先格式 demuxer
"""
from __future__ import annotations

from .main import app
from .admin_auth import install as install_auth
from .site_auth import install as install_site_auth
from .perm import install as install_perm
from .static_files import install as install_static
from .hls115 import install as install_hls115
from .nocache import install as install_nocache
from .prewarm_daemon import install as install_prewarm
from .admin_settings import install as install_settings
from .admin_accounts import install as install_accounts
from .multi_scan import install as install_multi_scan
from .fast_thumbs import install as install_fast_thumbs
from .fast_scan import install as install_fast_scan
from .incremental import install as install_incremental
from .video_fix import install as install_video
from .quick_scan_api import install as install_quick_scan
from .frontend_api import install as install_frontend
from .handoff import install as install_handoff

install_static(app)
install_hls115(app)
install_nocache(app)
install_auth(app)
install_prewarm(app)
install_settings(app)
install_accounts(app)
install_multi_scan(app)
install_fast_thumbs(app)
install_fast_scan(app)
install_incremental(app)
install_video(app)
install_quick_scan(app)
install_frontend(app)
install_handoff(app)

# 站点登录：必须最后装 —— 中间件按安装顺序从外到内，
# 装最后它就在最外层，未登录的请求连后面的中间件和路由都不会进。
install_site_auth(app)

# ★ req-accounts：普通账户权限闸门（**默认拒绝**）。
# 必须**紧跟**在 site_auth 之后装：这样它在 site_auth 的内侧，
# 未登录请求先被 site_auth 拦掉，登录后的请求才轮到它判账户权限。
install_perm(app)
