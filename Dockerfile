FROM python:3.9-slim

WORKDIR /app

# Install ffmpeg + curl + file (optional but useful for debugging)
RUN apt-get update && apt-get install -y ffmpeg curl file && \
    rm -rf /var/lib/apt/lists/*

# Remove ANY existing yt-dlp (pip or directory)
RUN pip uninstall -y yt-dlp || true && \
    rm -rf /usr/local/bin/yt-dlp

# Install the correct standalone Linux binary (ELF)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux \
        -o /usr/local/bin/yt-dlp && \
    chmod +x /usr/local/bin/yt-dlp

# Copy Python requirements (WITHOUT yt-dlp)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your app
COPY rss_downloader.py .

EXPOSE 8080

CMD ["python", "rss_downloader.py", "--server"]
