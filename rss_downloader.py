import sys
import subprocess
import os
import http.server
import socketserver
import urllib.parse
from datetime import datetime
from xml.sax.saxutils import escape
import json

DOWNLOAD_DIR = "downloads"
AUDIO_EXT = ".mp3"

def download_audio(youtube_url):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    command = [
        "yt-dlp",
        "-f", "bestaudio/best",
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-metadata",
        "--embed-thumbnail",
        "--write-description",
        "--postprocessor-args", "ffmpeg:-metadata comment='%(description)s'",
        "-o", os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        youtube_url
    ]
    try:
        subprocess.run(command, check=True)
        print("Download and extraction complete.")
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")

def generate_rss_feed():
    items = []
    for filename in os.listdir(DOWNLOAD_DIR):
        if filename.endswith(AUDIO_EXT):
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            description_filepath = os.path.splitext(filepath)[0] + ".description"
            description = ""
            if os.path.exists(description_filepath):
                with open(description_filepath, "r", encoding="utf-8") as f:
                    description = f.read()

            pub_date = datetime.utcfromtimestamp(os.path.getmtime(filepath)).strftime('%a, %d %b %Y %H:%M:%S GMT')
            url = f"http://localhost:8080/audio/{urllib.parse.quote(filename)}"
            items.append(f"""
                <item>
                    <title>{escape(filename)}</title>
                    <description>{escape(description)}</description>
                    <enclosure url="{url}" type="audio/mpeg"/>
                    <guid>{escape(filename)}</guid>
                    <pubDate>{pub_date}</pubDate>
                </item>
            """)
    rss = f"""<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0">
      <channel>
        <title>Downloaded YouTube Audio</title>
        <link>http://localhost:8080/rss</link>
        <description>All downloaded audio files</description>
        {''.join(items)}
      </channel>
    </rss>
    """
    return rss

class RSSRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/rss":
            rss = generate_rss_feed()
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(rss.encode("utf-8"))
        elif self.path.startswith("/audio/"):
            filename = urllib.parse.unquote(self.path[len("/audio/"):])
            filepath = os.path.join(DOWNLOAD_DIR, filename)
            if os.path.isfile(filepath):
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                with open(filepath, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "File not found")
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        if self.path == "/download":
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data)
                url = data.get("url")
                if url:
                    download_audio(url)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"message": "Download started."}).encode("utf-8"))
                else:
                    self.send_error(400, "Missing 'url' in request body")
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON")
        else:
            self.send_error(404, "Not found")

def run_server():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    handler = RSSRequestHandler
    with socketserver.TCPServer(("localhost", 8080), handler) as httpd:
        print("Serving RSS feed and audio files at http://localhost:8080/rss")
        httpd.serve_forever()

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--server":
        run_server()
    elif len(sys.argv) == 2:
        youtube_url = sys.argv[1]
        download_audio(youtube_url)
    else:
        print("Usage:")
        print("  python download_audio.py <YouTube_URL>")
        print("  python download_audio.py --server")
        sys.exit(1)