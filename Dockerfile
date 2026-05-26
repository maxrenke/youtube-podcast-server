FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates atomicparsley \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux \
        -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

COPY rss_downloader.py tasks.py ./

ENV PORT=8080 \
    DOWNLOAD_DIR=/app/downloads \
    STATE_DIR=/app/state \
    POLL_INTERVAL_SECONDS=3600 \
    PUBLIC_BASE_URL=http://localhost:5757

EXPOSE 8080

CMD ["python", "rss_downloader.py"]
