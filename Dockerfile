FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The server runs as an unprivileged user. yt-dlp lives in a folder that user owns
# so it can still update itself at start.
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /opt/yt-dlp /app/downloads /app/state \
    && curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux \
        -o /opt/yt-dlp/yt-dlp && chmod +x /opt/yt-dlp/yt-dlp \
    && chown -R app:app /opt/yt-dlp /app
ENV PATH=/opt/yt-dlp:$PATH

COPY rss_downloader.py tasks.py auth.py artwork.jpg ./
USER app

ENV PORT=8080 \
    DOWNLOAD_DIR=/app/downloads \
    STATE_DIR=/app/state \
    POLL_INTERVAL_SECONDS=3600 \
    PUBLIC_BASE_URL=http://localhost:5757

EXPOSE 8080

# YouTube changes break old yt-dlp builds, so pick up the latest release on every start.
CMD ["sh", "-c", "yt-dlp -U || true; exec python rss_downloader.py"]
