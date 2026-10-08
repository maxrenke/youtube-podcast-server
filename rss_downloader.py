import html
import json
import os
import re
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
    is_valid_url,
    list_subscriptions,
    list_tasks,
    remove_subscription,
)

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:5757").rstrip("/")
FEED_TITLE = os.environ.get("FEED_TITLE", "YouTube Podcast")
FEED_DESC = os.environ.get(
    "FEED_DESC",
    "A personal listen-later queue: YouTube videos converted to audio, each with its "
    "original thumbnail, description, chapters and a link back to the video.",
)
FEED_AUTHOR = os.environ.get("FEED_AUTHOR", "Max Renke")
FEED_EMAIL = os.environ.get("FEED_EMAIL", "")
FEED_CATEGORY = os.environ.get("FEED_CATEGORY", "Technology")
# itunes:block asks podcast directories not to list the feed. On by default: the
# episodes are other people's videos, so this feed is for personal use only.
FEED_PRIVATE = os.environ.get("FEED_PRIVATE", "1").lower() not in ("0", "false", "no")
ARTWORK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "artwork.jpg")
PORT = int(os.environ.get("PORT", "8080"))
START_TIME = time.time()


def _read_info_json(mp3_path):
    base, _ = os.path.splitext(mp3_path)
    info_path = base + ".info.json"
    if os.path.exists(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _fmt_duration(seconds) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _fmt_upload_date(yyyymmdd: str) -> str:
    if len(yyyymmdd) == 8 and yyyymmdd.isdigit():
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return ""


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
        stem = os.path.splitext(fn)[0]
        # Prefer the square cover yt-dlp saved next to the mp3 over YouTube's 16:9 URL.
        if os.path.isfile(os.path.join(DOWNLOAD_DIR, stem + ".jpg")):
            thumbnail = f"{PUBLIC_BASE_URL}/thumb/{quote(stem + '.jpg')}"
        else:
            thumbnail = info.get("thumbnail") or ""
        items.append({
            "filename": fn,
            "title": info.get("title") or stem,
            "description": info.get("description") or "",
            "duration": info.get("duration") or 0,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            # When the episode was added; yt-dlp records it as "epoch" in the info.json.
            "added": info.get("epoch") or stat.st_mtime,
            "video_id": info.get("id") or fn,
            "thumbnail": thumbnail,
            "uploader": info.get("channel") or info.get("uploader") or "",
            "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
            "webpage_url": info.get("webpage_url") or "",
            "upload_date": _fmt_upload_date(str(info.get("upload_date") or "")),
            "chapters": [
                {"start": int(c.get("start_time") or 0), "title": c.get("title") or ""}
                for c in (info.get("chapters") or [])
            ],
        })
    items.sort(key=lambda e: e["added"], reverse=True)
    return items


def _episode_byline(ep) -> str:
    """One line of source metadata shown above the description, e.g.
    ``Professor Dave Explains - uploaded 2025-04-26 - 1:37:10``."""
    parts = [ep["uploader"]]
    if ep["upload_date"]:
        parts.append("uploaded " + ep["upload_date"])
    if ep["duration"]:
        parts.append(_fmt_duration(ep["duration"]))
    return " - ".join(p for p in parts if p)


def _description_text(ep) -> str:
    head = [line for line in (_episode_byline(ep), ep["webpage_url"]) if line]
    return "\n".join(head) + ("\n\n" if head and ep["description"] else "") + ep["description"]


def _linkify(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', escaped)


def _description_html(ep) -> str:
    """Show notes as HTML: byline, link to the video, description, chapter list."""
    out = []
    byline = html.escape(_episode_byline(ep))
    if ep["channel_url"] and ep["uploader"]:
        name = html.escape(ep["uploader"])
        link = f'<a href="{html.escape(ep["channel_url"])}">{name}</a>'
        byline = byline.replace(name, link, 1)
    if byline:
        out.append(f"<p>{byline}</p>")
    if ep["webpage_url"]:
        url = html.escape(ep["webpage_url"])
        out.append(f'<p><a href="{url}">Watch the original video</a></p>')
    if ep["description"]:
        paragraphs = re.split(r"\n\s*\n", ep["description"].strip())
        out.extend("<p>" + _linkify(p).replace("\n", "<br>") + "</p>" for p in paragraphs)
    if ep["chapters"]:
        rows = "".join(
            f"<li>{_fmt_duration(c['start'])} {html.escape(c['title'])}</li>" for c in ep["chapters"]
        )
        out.append(f"<p>Chapters</p><ul>{rows}</ul>")
    return "\n".join(out)


def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _attr(value: str) -> str:
    return sx.escape(value, {chr(34): "&quot;"})


def generate_rss():
    eps = list_episodes()
    now = formatdate(time.time(), usegmt=True)
    artwork = f"{PUBLIC_BASE_URL}/artwork.jpg"
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/"'
        ' xmlns:atom="http://www.w3.org/2005/Atom">'
    )
    out.append("<channel>")
    out.append(f"  <title>{sx.escape(FEED_TITLE)}</title>")
    out.append(f"  <link>{sx.escape(PUBLIC_BASE_URL)}</link>")
    out.append(f'  <atom:link href="{_attr(PUBLIC_BASE_URL)}/rss" rel="self" type="application/rss+xml"/>')
    out.append(f"  <description>{sx.escape(FEED_DESC)}</description>")
    out.append("  <language>en-us</language>")
    out.append(f"  <lastBuildDate>{now}</lastBuildDate>")
    out.append("  <generator>youtube-podcast-server</generator>")
    out.append("  <image>")
    out.append(f"    <url>{sx.escape(artwork)}</url>")
    out.append(f"    <title>{sx.escape(FEED_TITLE)}</title>")
    out.append(f"    <link>{sx.escape(PUBLIC_BASE_URL)}</link>")
    out.append("  </image>")
    out.append(f'  <itunes:image href="{_attr(artwork)}"/>')
    out.append(f"  <itunes:author>{sx.escape(FEED_AUTHOR)}</itunes:author>")
    out.append(f"  <itunes:summary>{sx.escape(FEED_DESC)}</itunes:summary>")
    out.append("  <itunes:owner>")
    out.append(f"    <itunes:name>{sx.escape(FEED_AUTHOR)}</itunes:name>")
    if FEED_EMAIL:
        out.append(f"    <itunes:email>{sx.escape(FEED_EMAIL)}</itunes:email>")
    out.append("  </itunes:owner>")
    out.append("  <itunes:type>episodic</itunes:type>")
    out.append("  <itunes:explicit>false</itunes:explicit>")
    out.append(f'  <itunes:category text="{_attr(FEED_CATEGORY)}"/>')
    if FEED_PRIVATE:
        out.append("  <itunes:block>Yes</itunes:block>")
    for ep in eps:
        url = f"{PUBLIC_BASE_URL}/audio/{quote(ep['filename'])}"
        # pubDate is when the episode was added, so new additions sort first in the
        # app; the video's own upload date is in the description byline.
        pub = formatdate(ep["added"], usegmt=True)
        out.append("  <item>")
        out.append(f"    <title>{sx.escape(ep['title'])}</title>")
        if ep["webpage_url"]:
            out.append(f"    <link>{sx.escape(ep['webpage_url'])}</link>")
        out.append(f"    <description>{sx.escape(_description_text(ep))}</description>")
        out.append(f"    <content:encoded>{_cdata(_description_html(ep))}</content:encoded>")
        out.append(f"    <pubDate>{pub}</pubDate>")
        out.append(f"    <guid isPermaLink=\"false\">{sx.escape(ep['video_id'])}</guid>")
        out.append(f"    <enclosure url=\"{_attr(url)}\" length=\"{ep['size']}\" type=\"audio/mpeg\"/>")
        if ep["duration"]:
            out.append(f"    <itunes:duration>{int(ep['duration'])}</itunes:duration>")
        if ep["thumbnail"]:
            out.append(f"    <itunes:image href=\"{_attr(ep['thumbnail'])}\"/>")
        if ep["uploader"]:
            out.append(f"    <itunes:author>{sx.escape(ep['uploader'])}</itunes:author>")
        out.append("    <itunes:episodeType>full</itunes:episodeType>")
        out.append("    <itunes:explicit>false</itunes:explicit>")
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
.ep{display:flex;gap:.75rem}
.ep img{width:96px;height:96px;object-fit:cover;border-radius:6px;flex:none}
.ep audio{width:100%;margin-top:.25rem}
.ep pre{white-space:pre-wrap;font:inherit;font-size:.85em;margin:.25rem 0}
.muted{color:#666;font-size:.85em}
.row{display:flex;gap:.5rem;align-items:center}
button.danger{background:#fee;border:1px solid #c66;color:#a00}
h2{margin-top:1.5rem}
</style>
<h1><img src="/artwork.jpg" alt="" style="width:48px;height:48px;border-radius:8px;vertical-align:middle"> YouTube Podcast</h1>
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
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtDur(s){ s=Math.round(s||0); const h=Math.floor(s/3600), m=Math.floor(s%3600/60); return (h?h+'h ':'')+m+'m'; }
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
    `<div class="ep">${e.thumbnail ? `<img src="${esc(e.thumbnail)}" alt="" loading="lazy">` : ''}
     <div style="flex:1;min-width:0"><b>${esc(e.title)}</b><br>
     <span class="muted">${esc(e.uploader)}${e.upload_date ? ' - uploaded '+esc(e.upload_date) : ''} - ${fmtDur(e.duration)} - ${(e.size/1048576).toFixed(1)} MB - added ${new Date(e.added*1000).toLocaleString()}${e.chapters.length ? ' - '+e.chapters.length+' chapters' : ''}${e.webpage_url ? ` - <a href="${esc(e.webpage_url)}" target="_blank" rel="noopener">source</a>` : ''}</span>
     ${e.description ? `<details><summary class="muted">Description</summary><pre>${esc(e.description)}</pre></details>` : ''}
     <audio controls preload="none" src="/audio/${encodeURIComponent(e.filename)}"></audio></div></div>`).join('') || '<p class="muted">no episodes yet</p>';

  const subs = await (await fetch('/subscriptions')).json();
  document.getElementById('subs').innerHTML = subs.map(s => {
    const last = s.last_result ? (s.last_result.ok ? `+${s.last_result.new||0} new` : `error: ${s.last_result.error}`) : '-';
    return `<div class="sub row">
      <div style="flex:1">
        <div><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a></div>
        <div class="muted">added ${fmtTs(s.added)} - last poll ${fmtTs(s.last_poll)} (${esc(last)}) - next in ${fmtNext(s.next_poll)}</div>
      </div>
      <button class="danger" onclick="del('${s.id}')">Unsubscribe</button>
    </div>`;
  }).join('') || '<p class="muted">no subscriptions</p>';

  const ts = await (await fetch('/tasks')).json();
  document.getElementById('tasks').innerHTML = ts.slice(-25).reverse().map(t =>
    `<div class="muted">[${esc(t.status)}] (${esc(t.type)}) ${esc(t.url)} ${t.error?'- '+esc(t.error):''} ${t.downloaded&&t.downloaded.length?'- pulled '+t.downloaded.length:''}</div>`).join('') || '<p class="muted">no tasks</p>';
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
  document.getElementById('dlMsg').textContent = r.ok ? 'queued' : 'error: ' + ((await r.json()).error || r.status);
  document.getElementById('dlUrl').value='';
  refresh();
};
document.getElementById('subForm').onsubmit = async e => {
  e.preventDefault();
  const u = document.getElementById('subUrl').value;
  const r = await fetch('/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})});
  document.getElementById('subMsg').textContent = r.ok ? 'subscribed; first poll starting' : 'error: ' + ((await r.json()).error || r.status);
  document.getElementById('subUrl').value='';
  refresh();
};
refresh(); setInterval(refresh, 5000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    _head_only = False  # set by do_HEAD: send headers, skip the body

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not self._head_only:
            self.wfile.write(body)

    def _text(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not self._head_only:
            self.wfile.write(body)

    def _serve_file(self, full, ctype):
        """Send a file with single-range support (podcast players seek with Range)."""
        if not os.path.isfile(full):
            self._text(404, "not found")
            return
        size = os.path.getsize(full)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            # Single range only: "bytes=start-end", "bytes=start-", "bytes=-suffix"
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if not m or (m.group(1) == "" and m.group(2) == ""):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if m.group(1) == "":
                start = max(0, size - int(m.group(2)))
            else:
                start = int(m.group(1))
                if m.group(2) != "":
                    end = min(int(m.group(2)), size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self._head_only:
            return
        with open(full, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_download(self, filename, ext, ctype):
        """Serve ``filename`` from DOWNLOAD_DIR if it is a plain name with extension ``ext``."""
        if "/" in filename or "\\" in filename or ".." in filename or not filename.lower().endswith(ext):
            self._text(400, "bad path")
            return
        self._serve_file(os.path.join(DOWNLOAD_DIR, filename), ctype)

    def do_HEAD(self):
        self._head_only = True
        self.do_GET()

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
            self._serve_download(unquote(path[len("/audio/"):]), ".mp3", "audio/mpeg")
        elif path.startswith("/thumb/"):
            self._serve_download(unquote(path[len("/thumb/"):]), ".jpg", "image/jpeg")
        elif path == "/artwork.jpg":
            self._serve_file(ARTWORK_PATH, "image/jpeg")
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
            if not is_valid_url(url):
                self._json(400, {"error": "url must start with http:// or https://"})
                return
            task_id = enqueue_download(url)
            self._json(202, {"task_id": task_id})
        elif self.path == "/subscriptions":
            data = self._read_json()
            if data is None:
                return
            url = (data.get("url") or "").strip()
            if not is_valid_url(url):
                self._json(400, {"error": "url must start with http:// or https://"})
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
