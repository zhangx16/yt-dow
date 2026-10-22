FROM python:3.13-alpine
RUN apk add --no-cache ca-certificates ffmpeg nodejs rclone \
    && pip install --no-cache-dir 'yt-dlp[default]==2026.8.19' \
    && mkdir -p /etc/yt-dlp \
    && printf '%s\n' '--js-runtimes node' > /etc/yt-dlp/config
WORKDIR /app
COPY worker.py prepare.py ./
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/worker.py"]
