#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! docker info >/dev/null 2>&1; then
    echo '无法访问 Docker；请在服务器有 Docker 权限的终端执行此脚本。' >&2
    exit 1
fi
test -f .env || { echo '缺少 telegram/.env' >&2; exit 1; }
chmod 600 .env
mkdir -p work state
# Telegram API runs as uid 101 and needs to read completed videos in shared /work.
chmod 755 work
chmod 700 state
docker compose --env-file .env config --quiet
docker compose --env-file .env build video-bot
docker compose --env-file .env run --rm --no-deps --entrypoint python video-bot /app/prepare.py
docker compose --env-file .env up -d
docker compose --env-file .env ps
