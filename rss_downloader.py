import json
import os
import time
import xml.sax.saxutils as sx
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

import tasks
from tasks import (
    DOWNLOAD_DIR,
    add_subscription,
    enqueue_download,
    get_subscription,
    get_task,
    list_subscriptions,
    list_tasks,
    remove_subscription,
)

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5757").rstrip("/")
FEED_TITLE = os.environ.get("FEED_TITLE", "YouTube Podcast")
FEED_DESC = os.environ.get("FEED_DESC", "Personal YouTube-to-podcast feed")
FEED_AUTHOR = os.environ.get("FEED_AUTHOR", "Max Renke")
PORT = int(os.environ.get("PORT", "8080"))
START_TIME = time.time()


def _read_info_json(mp3_path):
    base, _ = os.path.splitext(mp3_path)
    info_path = base + ".info.json"
    if os.path.exists(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def list_episodes():
    if not os.path.isdir(DOWNLOAD_DIR):
        return []
    items = []
    for fn in os.listdir(DOWNLOAD_DIR):
        if not fn.lower().endswith(".mp3"):
            continue
        full = os.path.join(DOWNLOAD_DIR, fn)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        info = _read_info_json(full)
        items.append({
            "filename": fn,
            "title": info.get("title") or os.path.splitext(fn)[0],
            "description": info.get("description") or "",
            "duration": info.get("duration") or 0,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "video_id": info.get("id") or fn,
            "thumbnail": info.get("thumbnail") or "",
            "uploader": info.get("uploader") or "",
            "webpage_url": info.get("webpage_url") or "",
        })
    items.sort(key=lambda e: e["mtime"], reverse=True)
    return items


def generate_rss():
    eps = list_episodes()
    now = formatdate(time.time(), usegmt=True)
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/">')
    out.append("<channel>")
    out.append(f"  <title>{sx.escape(FEED_TITLE)}</title>")
    out.append(f"  <link>{sx.escape(PUBLIC_BASE_URL)}</link>")
    out.append(f"  <description>{sx.escape(FEED_DESC)}</description>")
    out.append("  <language>en-us</language>")
    out.append(f"  <lastBuildDate>{now}</lastBuildDate>")
    out.append(f"  <itunes:author>{sx.escape(FEED_AUTHOR)}</itunes:author>")
    out.append("  <itunes:explicit>false</itunes:explicit>")
    out.append("  <itunes:category text=\"Technology\"/>")
    for ep in eps:
        url = f"{PUBLIC_BASE_URL}/audio/{quote(ep['filename'])}"
        pub = formatdate(ep["mtime"], usegmt=True)
        out.append("  <item>")
        out.append(f"    <title>{sx.escape(ep['title'])}</title>")
        out.append(f"    <description>{sx.escape(ep['description'])}</description>")
        out.append(f"    <pubDate>{pub}</pubDate>")
        out.append(f"    <guid isPermaLink=\"false\">{sx.escape(ep['video_id'])}</guid>")
        out.append(f"    <enclosure url=\"{sx.escape(url, {chr(34): '&quot;'})}\" length=\"{ep['size']}\" type=\"audio/mpeg\"/>")
        if ep["duration"]:
            out.append(f"    <itunes:duration>{int(ep['duration'])}</itunes:duration>")
        if ep["thumbnail"]:
            out.append(f"    <itunes:image href=\"{sx.escape(ep['thumbnail'], {chr(34): '&quot;'})}\"/>")
        if ep["uploader"]:
            out.append(f"    <itunes:author>{sx.escape(ep['uploader'])}</itunes:author>")
        out.append("  </item>")
    out.append("</channel></rss>")
    return "\n".join(out)


INDEX_HTML = """<!doctype html>
<meta charset="utf-8"><title>YouTube Podcast</title>
<style>
body{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem}
input,button{font-size:1rem;padding:.5rem}
input{width:65%}
.ep,.sub{border-bottom:1px solid #ddd;padding:.5rem 0}
.muted{color:#666;font-size:.85em}
.row{display:flex;gap:.5rem;align-items:center}
button.danger{background:#fee;border:1px solid #c66;color:#a00}
h2{margin-top:1.5rem}
</style>
<h1>YouTube Podcast</h1>
<p>Feed: <a id="feed" href="/rss">/rss</a></p>

<h2>Download single video</h2>
<form id="dlForm"><input id="dlUrl" placeholder="https://www.youtube.com/watch?v=..." required><button>Download</button></form>
<p id="dlMsg" class="muted"></p>

<h2>Subscribe to playlist or channel</h2>
<p class="muted">Polled hourly. First pull starts immediately. yt-dlp <code>--download-archive</code> means already-downloaded videos are skipped.</p>
<form id="subForm"><input id="subUrl" placeholder="https://www.youtube.com/playlist?list=... or https://www.youtube.com/@channel" required><button>Subscribe</button></form>
<p id="subMsg" class="muted"></p>
<div id="subs"></div>

<h2>Episodes</h2><div id="eps"></div>
<h2>Recent tasks</h2><div id="tasks"></div>

<script>
function fmtTs(s){ return s ? new Date(s).toLocaleString() : 'never'; }
function fmtNext(ep){
  if(!ep) return 'soon';
  const ms = ep*1000 - Date.now();
  if(ms <= 0) return 'soon';
  const m = Math.round(ms/60000);
  return m < 60 ? m+'m' : Math.round(m/60)+'h';
}
async function refresh(){
  const eps = await (await fetch('/episodes')).json();
  document.getElementById('eps').innerHTML = eps.map(e =>
    `<div class="ep"><b>${e.title}</b><br>
     <span class="muted">${(e.size/1048576).toFixed(1)} MB - ${new Date(e.mtime*1000).toLocaleString()}</span><br>
     <audio controls src="/audio/${encodeURIComponent(e.filename)}"></audio></div>`).join('') || '<p class="muted">no episodes yet</p>';

  const subs = await (await fetch('/subscriptions')).json();
  document.getElementById('subs').innerHTML = subs.map(s => {
    const last = s.last_result ? (s.last_result.ok ? `+${s.last_result.new||0} new` : `error: ${s.last_result.error}`) : '-';
    return `<div class="sub row">
      <div style="flex:1">
        <div><a href="${s.url}" target="_blank">${s.url}</a></div>
        <div class="muted">added ${fmtTs(s.added)} - last poll ${fmtTs(s.last_poll)} (${last}) - next in ${fmtNext(s.next_poll)}</div>
      </div>
      <button class="danger" onclick="del('${s.id}')">Unsubscribe</button>
    </div>`;
  }).join('') || '<p class="muted">no subscriptions</p>';

  const ts = await (await fetch('/tasks')).json();
  document.getElementById('tasks').innerHTML = ts.slice(-25).reverse().map(t =>
    `<div class="muted">[${t.status}] (${t.type}) ${t.url} ${t.error?'- '+t.error:''} ${t.downloaded&&t.downloaded.length?'- pulled '+t.downloaded.length:''}</div>`).join('') || '<p class="muted">no tasks</p>';
}
async function del(id){
  if(!confirm('Unsubscribe?')) return;
  await fetch('/subscriptions/'+id, {method:'DELETE'});
  refresh();
}
document.getElementById('dlForm').onsubmit = async e => {
  e.preventDefault();
  const u = document.getElementById('dlUrl').value;
  const r = await fetch('/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})});
  document.getElementById('dlMsg').textContent = r.ok ? 'queued' : 'error';
  document.getElementById('dlUrl').value='';
  refresh();
};
document.getElementById('subForm').onsubmit = async e => {
  e.preventDefault();
  const u = document.getElementById('subUrl').value;
  const r = await fetch('/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})});
  document.getElementById('subMsg').textContent = r.ok ? 'subscribed; first poll starting' : 'error';
  document.getElementById('subUrl').value='';
  refresh();
};
refresh(); setInterval(refresh, 5000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._text(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/ping":
            self._json(200, {"message": "pong"})
        elif path == "/health":
            self._json(200, {
                "status": "ok",
                "queue_length": tasks.TASK_QUEUE.qsize(),
                "tasks_total": len(list_tasks()),
                "uptime_seconds": int(time.time() - START_TIME),
                "downloads": len(list_episodes()),
                "public_base_url": PUBLIC_BASE_URL,
            })
        elif path == "/rss":
            self._text(200, generate_rss(), "application/rss+xml; charset=utf-8")
        elif path == "/episodes":
            self._json(200, list_episodes())
        elif path == "/tasks":
            self._json(200, list_tasks())
        elif path.startswith("/tasks/"):
            t = get_task(path[len("/tasks/"):])
            if not t:
                self._json(404, {"error": "not found"})
            else:
                self._json(200, t)
        elif path == "/subscriptions":
            self._json(200, list_subscriptions())
        elif path.startswith("/subscriptions/"):
            s = get_subscription(path[len("/subscriptions/"):])
            if not s:
                self._json(404, {"error": "not found"})
            else:
                self._json(200, s)
        elif path.startswith("/audio/"):
            filename = unquote(path[len("/audio/"):])
            if "/" in filename or ".." in filename:
                self._text(400, "bad path")
                return
            full = os.path.join(DOWNLOAD_DIR, filename)
            if not os.path.isfile(full):
                self._text(404, "not found")
                return
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(full, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        else:
            self._text(404, "not found")

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return None

    def do_POST(self):
        if self.path == "/download":
            data = self._read_json()
            if data is None:
                return
            url = (data.get("url") or "").strip()
            if not url:
                self._json(400, {"error": "missing url"})
                return
            task_id = enqueue_download(url)
            self._json(202, {"task_id": task_id})
        elif self.path == "/subscriptions":
            data = self._read_json()
            if data is None:
                return
            url = (data.get("url") or "").strip()
            if not url:
                self._json(400, {"error": "missing url"})
                return
            interval = data.get("interval_seconds")
            sub = add_subscription(url, interval_seconds=interval)
            self._json(201, sub)
        else:
            self._text(404, "not found")

    def do_DELETE(self):
        if self.path.startswith("/subscriptions/"):
            sub_id = self.path[len("/subscriptions/"):]
            if remove_subscription(sub_id):
                self._json(200, {"deleted": sub_id})
            else:
                self._json(404, {"error": "not found"})
        else:
            self._text(404, "not found")


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    print(f"Serving on 0.0.0.0:{PORT} (public base: {PUBLIC_BASE_URL}, downloads: {DOWNLOAD_DIR})", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
