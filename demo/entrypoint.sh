#!/bin/sh
# 容器入口：api 启动恢复服务；demo 启动本地演示下载进程。
set -eu

case "${1:-api}" in
  api)
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  demo)
    shift || true
    exec python -I /srv/demo/holder.py "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
