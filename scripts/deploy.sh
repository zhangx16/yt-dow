#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WEB_ROOT=${WEB_ROOT:-/var/www/yt-dlp-simple}

cd "$PROJECT_DIR"

if [ ! -f .env ]; then
    echo "Missing .env. Copy .env.example and set WEBUI_PASSWORD first." >&2
    exit 1
fi

docker compose up -d --build
install -d -m 755 "$WEB_ROOT"
install -m 644 \
    frontend/index.html \
    frontend/styles.css \
    frontend/app.js \
    frontend/favicon.svg \
    "$WEB_ROOT/"

if command -v nginx >/dev/null 2>&1; then
    nginx -t
    if command -v systemctl >/dev/null 2>&1; then
        systemctl reload nginx
    fi
fi

echo "yt-dow deployed successfully."
