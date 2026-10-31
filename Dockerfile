# 115emby —— 115 网盘媒体库浏览器（缩略图 / 加密索引 / 播放）
FROM python:3.12-slim

# ffmpeg：视频抓帧出缩略图；curl：HEALTHCHECK 用
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，改代码时不必重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tools ./tools

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    EMBY_DB=/app/data/emby.db \
    THUMB_DB=/app/data/thumbs.db \
    MALLOC_ARENA_MAX=2

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "uvicorn", "app.serve:app", \
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
