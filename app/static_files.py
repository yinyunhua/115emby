"""把 /static 挂成真正的静态目录（QierPlayer 等 vendor 资源需要）。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles


def install(app) -> None:
    d = os.getenv("STATIC_DIR") or str(Path(__file__).resolve().parent / "static")
    if not os.path.isdir(d):
        return
    try:
        app.mount("/static", StaticFiles(directory=d), name="static")
    except Exception:
        pass
