FROM python:3.9-slim

WORKDIR /app

# Install ffmpeg for audio extraction
RUN apt-get update && apt-get install -y ffmpeg

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rss_downloader.py .

EXPOSE 8080

CMD ["python", "rss_downloader.py", "--server"]
