import hmac
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
    delete_episode,
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
# When set, every POST and DELETE needs "Authorization: Bearer <API_TOKEN>".
# GET routes stay open: podcast apps fetch the feed, audio and covers with no login.
API_TOKEN = os.environ.get("API_TOKEN", "")
MAX_BODY_BYTES = 64 * 1024
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


def _fmt_length(seconds) -> str:
    """Episode length as ``1h 37m``. Not ``1:37:10``: players turn that into a seek link."""
    h, m = divmod(int(seconds or 0) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _fmt_upload_date(yyyymmdd: str) -> str:
    if len(yyyymmdd) == 8 and yyyymmdd.isdigit():
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
    return ""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_EPISODE_CACHE: dict = {"key": None, "items": []}


def list_episodes():
    """Episodes, newest addition first. Cached until DOWNLOAD_DIR changes."""
    try:
        mtime = os.stat(DOWNLOAD_DIR).st_mtime
    except OSError:
        return []
    # A folder touched in the last couple of seconds may still be changing.
    if _EPISODE_CACHE["key"] == mtime and time.time() - mtime > 2:
        return _EPISODE_CACHE["items"]
    items = _scan_episodes()
    _EPISODE_CACHE.update(key=mtime, items=items)
    return items


def _scan_episodes():
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
            "feed": _slug(info.get("channel") or info.get("uploader") or ""),
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
    ``Professor Dave Explains - uploaded 2025-04-26 - 1h 37m``."""
    parts = [ep["uploader"]]
    if ep["upload_date"]:
        parts.append("uploaded " + ep["upload_date"])
    if ep["duration"]:
        parts.append(_fmt_length(ep["duration"]))
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


def list_feeds():
    """One entry per channel that has episodes: ``/rss/<slug>`` serves only that channel."""
    feeds: dict[str, dict] = {}
    for ep in list_episodes():
        if not ep["feed"]:
            continue
        feed = feeds.setdefault(ep["feed"], {
            "slug": ep["feed"],
            "title": ep["uploader"],
            "url": f"{PUBLIC_BASE_URL}/rss/{ep['feed']}",
            "episodes": 0,
        })
        feed["episodes"] += 1
    return sorted(feeds.values(), key=lambda f: f["title"].lower())


def chapters_json(video_id: str):
    """Podcasting 2.0 chapters document for one episode, or None."""
    for ep in list_episodes():
        if ep["video_id"] == video_id and ep["chapters"]:
            return {
                "version": "1.2.0",
                "chapters": [{"startTime": c["start"], "title": c["title"]} for c in ep["chapters"]],
            }
    return None


def generate_rss(feed: str = "", path: str = "/rss"):
    """The whole library, or with ``feed`` (a channel slug) only that channel's episodes.

    ``path`` is the URL path the main feed was requested at; it becomes the
    feed's self link. Returns None when ``feed`` matches no episode.
    """
    eps = list_episodes()
    title, desc, author = FEED_TITLE, FEED_DESC, FEED_AUTHOR
    link, self_url = PUBLIC_BASE_URL, f"{PUBLIC_BASE_URL}{path}"
    artwork = f"{PUBLIC_BASE_URL}/artwork.jpg"
    if feed:
        eps = [e for e in eps if e["feed"] == feed]
        if not eps:
            return None
        title = author = eps[0]["uploader"]
        desc = f"{title} videos as audio, from {FEED_TITLE}."
        link = eps[0]["channel_url"] or PUBLIC_BASE_URL
        self_url = f"{PUBLIC_BASE_URL}/rss/{feed}"
        # A channel feed wears its newest episode's cover.
        artwork = eps[0]["thumbnail"] or artwork
    now = formatdate(time.time(), usegmt=True)
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'
        ' xmlns:content="http://purl.org/rss/1.0/modules/content/"'
        ' xmlns:atom="http://www.w3.org/2005/Atom"'
        ' xmlns:podcast="https://podcastindex.org/namespace/1.0">'
    )
    out.append("<channel>")
    out.append(f"  <title>{sx.escape(title)}</title>")
    out.append(f"  <link>{sx.escape(link)}</link>")
    out.append(f'  <atom:link href="{_attr(self_url)}" rel="self" type="application/rss+xml"/>')
    out.append(f"  <description>{sx.escape(desc)}</description>")
    out.append("  <language>en-us</language>")
    out.append(f"  <lastBuildDate>{now}</lastBuildDate>")
    out.append("  <generator>youtube-podcast-server</generator>")
    out.append("  <image>")
    out.append(f"    <url>{sx.escape(artwork)}</url>")
    out.append(f"    <title>{sx.escape(title)}</title>")
    out.append(f"    <link>{sx.escape(link)}</link>")
    out.append("  </image>")
    out.append(f'  <itunes:image href="{_attr(artwork)}"/>')
    out.append(f"  <itunes:author>{sx.escape(author)}</itunes:author>")
    out.append(f"  <itunes:summary>{sx.escape(desc)}</itunes:summary>")
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
        if ep["chapters"]:
            chapters_url = f"{PUBLIC_BASE_URL}/chapters/{quote(ep['video_id'])}.json"
            out.append(
                f'    <podcast:chapters url="{_attr(chapters_url)}" type="application/json+chapters"/>'
            )
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
<p>Feed: <a id="feed" href="/feed">/feed</a> <span id="feeds" class="muted"></span></p>

<h2>Download single video</h2>
<form id="dlForm"><input id="dlUrl" placeholder="https://www.youtube.com/watch?v=..." required><button>Download</button></form>
<p id="dlMsg" class="muted"></p>

<h2>Subscribe to playlist or channel</h2>
<p class="muted">Polled hourly. First pull starts immediately; videos already pulled are skipped. A channel is limited to its newest 10 uploads per poll, a playlist is taken whole.</p>
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
function token(){ try { return localStorage.getItem('token') || ''; } catch(e) { return ''; } }
// Write requests carry the API token; on 401 ask for it once and retry.
async function api(method, path, body){
  const headers = {'Content-Type':'application/json'};
  if(token()) headers['Authorization'] = 'Bearer ' + token();
  const r = await fetch(path, {method, headers, body: body ? JSON.stringify(body) : undefined});
  if(r.status !== 401) return r;
  const t = prompt('API token (API_TOKEN on the server)');
  if(!t) return r;
  try { localStorage.setItem('token', t.trim()); } catch(e) {}
  return api(method, path, body);
}
async function refresh(){
  const feeds = await (await fetch('/feeds')).json();
  document.getElementById('feeds').innerHTML = feeds.length ? ' - per channel: ' + feeds.map(f =>
    `<a href="${esc(f.url)}">${esc(f.title)}</a> (${f.episodes})`).join(', ') : '';
  const eps = await (await fetch('/episodes')).json();
  document.getElementById('eps').innerHTML = eps.map(e =>
    `<div class="ep">${e.thumbnail ? `<img src="${esc(e.thumbnail)}" alt="" loading="lazy">` : ''}
     <div style="flex:1;min-width:0"><b>${esc(e.title)}</b><br>
     <span class="muted">${esc(e.uploader)}${e.upload_date ? ' - uploaded '+esc(e.upload_date) : ''} - ${fmtDur(e.duration)} - ${(e.size/1048576).toFixed(1)} MB - added ${new Date(e.added*1000).toLocaleString()}${e.chapters.length ? ' - '+e.chapters.length+' chapters' : ''}${e.webpage_url ? ` - <a href="${esc(e.webpage_url)}" target="_blank" rel="noopener">source</a>` : ''}</span>
     ${e.description ? `<details><summary class="muted">Description</summary><pre>${esc(e.description)}</pre></details>` : ''}
     <audio controls preload="none" src="/audio/${encodeURIComponent(e.filename)}"></audio></div>
     <div><button class="danger" data-del-episode="${esc(e.video_id)}" data-title="${esc(e.title)}">Delete</button></div></div>`).join('') || '<p class="muted">no episodes yet</p>';

  const subs = await (await fetch('/subscriptions')).json();
  document.getElementById('subs').innerHTML = subs.map(s => {
    const last = s.last_result ? (s.last_result.ok ? `+${s.last_result.new||0} new` : `error: ${s.last_result.error}`) : '-';
    return `<div class="sub row">
      <div style="flex:1">
        <div><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a></div>
        <div class="muted">${s.max_items ? 'newest '+s.max_items+' per poll' : 'all items'} - added ${fmtTs(s.added)} - last poll ${fmtTs(s.last_poll)} (${esc(last)}) - next in ${fmtNext(s.next_poll)}</div>
      </div>
      <button class="danger" data-del-sub="${esc(s.id)}">Unsubscribe</button>
    </div>`;
  }).join('') || '<p class="muted">no subscriptions</p>';

  const ts = await (await fetch('/tasks')).json();
  document.getElementById('tasks').innerHTML = ts.slice(-25).reverse().map(t =>
    `<div class="muted">[${esc(t.status)}] (${esc(t.type)}) ${esc(t.url)} ${t.error?'- '+esc(t.error):''} ${t.downloaded&&t.downloaded.length?'- pulled '+t.downloaded.length:''}</div>`).join('') || '<p class="muted">no tasks</p>';
}
// Ids travel in data attributes, never inside inline handlers.
document.addEventListener('click', async e => {
  const d = e.target.dataset || {};
  if(d.delSub){
    if(!confirm('Unsubscribe?')) return;
    await api('DELETE', '/subscriptions/' + encodeURIComponent(d.delSub));
  } else if(d.delEpisode){
    if(!confirm('Delete "' + d.title + '" from the server?')) return;
    await api('DELETE', '/episodes/' + encodeURIComponent(d.delEpisode));
  } else return;
  refresh();
});
document.getElementById('dlForm').onsubmit = async e => {
  e.preventDefault();
  const u = document.getElementById('dlUrl').value;
  const r = await api('POST', '/download', {url:u});
  document.getElementById('dlMsg').textContent = r.ok ? 'queued' : 'error: ' + ((await r.json()).error || r.status);
  document.getElementById('dlUrl').value='';
  refresh();
};
document.getElementById('subForm').onsubmit = async e => {
  e.preventDefault();
  const u = document.getElementById('subUrl').value;
  const r = await api('POST', '/subscriptions', {url:u});
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
                "queue_length": tasks.queue_length(),
                "tasks_total": len(list_tasks()),
                "uptime_seconds": int(time.time() - START_TIME),
                "downloads": len(list_episodes()),
                "public_base_url": PUBLIC_BASE_URL,
                "auth_required": bool(API_TOKEN),
            })
        elif path in ("/rss", "/feed"):
            # Same feed under two addresses. Podcast apps key a podcast on its URL and
            # some only read the cover when they first import it, so a second
            # address is the way to get a clean re-import.
            self._text(200, generate_rss(path=path), "application/rss+xml; charset=utf-8")
        elif path.startswith("/rss/"):
            xml = generate_rss(unquote(path[len("/rss/"):]))
            if xml is None:
                self._text(404, "no such feed")
            else:
                self._text(200, xml, "application/rss+xml; charset=utf-8")
        elif path == "/feeds":
            self._json(200, list_feeds())
        elif path.startswith("/chapters/") and path.endswith(".json"):
            doc = chapters_json(unquote(path[len("/chapters/"):-len(".json")]))
            if doc is None:
                self._json(404, {"error": "not found"})
            else:
                self._json(200, doc)
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

    def _authorized(self) -> bool:
        """True if no token is configured or the request carries it; otherwise answers 401."""
        if not API_TOKEN:
            return True
        given = self.headers.get("Authorization", "").encode()
        if hmac.compare_digest(given, f"Bearer {API_TOKEN}".encode()):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        body = json.dumps({"error": "missing or wrong API token"}).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": "request body too large"})
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            self._json(400, {"error": "invalid json"})
            return None
        return data

    def do_POST(self):
        if not self._authorized():
            return
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
            try:
                sub = add_subscription(
                    url,
                    interval_seconds=data.get("interval_seconds"),
                    max_items=data.get("max_items"),
                )
            except (TypeError, ValueError):
                self._json(400, {"error": "interval_seconds and max_items must be numbers"})
                return
            self._json(201, sub)
        else:
            self._text(404, "not found")

    def do_DELETE(self):
        if not self._authorized():
            return
        if self.path.startswith("/episodes/"):
            video_id = unquote(self.path[len("/episodes/"):])
            if delete_episode(video_id):
                self._json(200, {"deleted": video_id})
            else:
                self._json(404, {"error": "not found"})
        elif self.path.startswith("/subscriptions/"):
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
    if not API_TOKEN:
        print("WARNING: API_TOKEN is not set - anyone who can reach this server can add and delete", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
