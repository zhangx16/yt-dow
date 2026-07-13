FROM golang:1.22.2-alpine AS builder

WORKDIR /src
COPY backend/go.mod backend/main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/yt-dlp-lite .

FROM python:3.13-alpine

RUN apk add --no-cache ca-certificates ffmpeg \
    && pip install --no-cache-dir "yt-dlp[default]==2026.6.9"

COPY --from=builder /out/yt-dlp-lite /usr/local/bin/yt-dlp-lite

ENV PORT=3033 \
    DOWNLOAD_DIR=/downloads \
    YTDLP_PATH=/usr/local/bin/yt-dlp

VOLUME ["/downloads"]
EXPOSE 3033
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O - http://127.0.0.1:3033/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/yt-dlp-lite"]
