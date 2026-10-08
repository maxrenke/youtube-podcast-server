FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux \
        -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

COPY rss_downloader.py tasks.py artwork.jpg ./

ENV PORT=8080 \
    DOWNLOAD_DIR=/app/downloads \
    STATE_DIR=/app/state \
    POLL_INTERVAL_SECONDS=3600 \
    PUBLIC_BASE_URL=http://localhost:5757

EXPOSE 8080

# YouTube changes break old yt-dlp builds, so pick up the latest release on every start.
CMD ["sh", "-c", "yt-dlp -U || true; exec python rss_downloader.py"]
