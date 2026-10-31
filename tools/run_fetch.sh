#!/bin/sh
cd ${BASE_DIR:-/app} || exit 1
rm -f /tmp/fetch_pending.log
exec python3 tools/fetch_pending.py
