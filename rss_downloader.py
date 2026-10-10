import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import time
import xml.sax.saxutils as sx
from email.utils import formatdate
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

import auth
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
# Everything needs a sign-in except what a podcast app fetches with no login:
# the feeds, audio, covers and chapters (see _is_public). Two ways in:
# - scripts send "Authorization: Bearer <API_TOKEN>";
# - a person signs in at /login with the account created at /setup (auth.py),
#   and the browser then carries a session cookie.
API_TOKEN = os.environ.get("API_TOKEN", "")
_ORIGIN = urlsplit(PUBLIC_BASE_URL)
PUBLIC_ORIGIN = f"{_ORIGIN.scheme}://{_ORIGIN.netloc}"
SECURE = _ORIGIN.scheme == "https"
# The __Host- prefix makes browsers refuse the cookie unless it is Secure, host-only and Path=/.
COOKIE_NAME = "__Host-ytps" if SECURE else "ytps"
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
    escaped = html.escape(text, quote=True)
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


# One dark theme for every page. Colours are named by role; spacing steps are 4/8/12/16/24/32 px.
_THEME_CSS = """
:root{color-scheme:dark;--bg:#0f1117;--surface:#171a23;--raised:#1e222d;--border:#2a2f3c;--text:#e8eaf0;
--muted:#9aa1b2;--link:#8ab4ff;--accent:#d93036;--accent-hover:#e5484d;--ok:#3dd68c;--warn:#f5a524;--danger:#ff6369;
--radius:10px;--radius-sm:6px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
h1,h2,h3,p{margin:0}
:focus-visible{outline:2px solid var(--text);outline-offset:2px}
input{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);
padding:8px 12px;width:100%;min-width:0}
input::placeholder{color:var(--muted)}
input:focus-visible{outline-offset:0;border-color:var(--text)}
.btn{font:inherit;font-weight:600;color:var(--text);background:var(--raised);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:8px 16px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--muted);text-decoration:none}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.primary:hover{background:var(--accent-hover);border-color:var(--accent-hover)}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted);padding:4px 8px;font-weight:500;font-size:13px}
.btn.ghost:hover{color:var(--text);background:var(--raised)}
.btn.ghost.danger:hover{color:var(--danger)}
.btn.danger-solid{background:transparent;border-color:var(--danger);color:var(--danger)}
.btn.danger-solid:hover{background:var(--danger);color:#14161f}
.muted{color:var(--muted)}
.small{font-size:13px}
.msg{padding:8px 12px;border-radius:var(--radius-sm);border:1px solid var(--danger);color:var(--danger);margin-bottom:16px}
.msg.ok{border-color:var(--ok);color:var(--ok)}
"""

_PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ - YouTube Podcast</title>
<style nonce="__NONCE__">__THEME__
body{display:grid;place-items:center;min-height:100vh;padding:16px}
main{width:100%;max-width:380px}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:24px;font-weight:600;font-size:17px}
.brand img{width:40px;height:40px;border-radius:var(--radius-sm)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:24px}
h1{font-size:20px;margin-bottom:16px}
label{display:block;margin:16px 0 4px;font-weight:600;font-size:13px}
form .btn{width:100%;margin-top:24px}
.card p.muted{margin-bottom:8px}
</style>
<main>
<div class="brand"><img src="/artwork.jpg" alt="" width="40" height="40">YouTube Podcast</div>
<div class="card"><h1>__TITLE__</h1>
__BODY__
</div>
</main>
</html>""".replace("__THEME__", _THEME_CSS)

_LOGIN_FORM = """<form method="post" action="/login">
<label for="u">User name</label><input id="u" name="username" autocomplete="username" required autofocus>
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<label for="c">Two-step code <span class="muted">(if turned on)</span></label>
<input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" maxlength="8">
<button class="btn primary">Sign in</button>
</form>"""

_SETUP_FORM = """<p class="muted small">Creates the admin account, or replaces it if you forgot the password.
The API token proves you control the server; it is the API_TOKEN value in the server's .env file.</p>
<form method="post" action="/setup">
<label for="t">API token</label><input id="t" name="token" type="password" autocomplete="off" required>
<label for="u">User name</label><input id="u" name="username" autocomplete="username" required>
<label for="p">New password <span class="muted">(12 characters or more)</span></label>
<input id="p" name="password" type="password" autocomplete="new-password" minlength="12" required>
<label for="p2">New password again</label>
<input id="p2" name="password2" type="password" autocomplete="new-password" minlength="12" required>
<button class="btn primary">Save</button>
</form>"""


def _message(text: str, ok: bool = False) -> str:
    return f'<p class="msg{" ok" if ok else ""}">{html.escape(text)}</p>' if text else ""


INDEX_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>YouTube Podcast</title>
<meta name="csrf" content="__CSRF__">
<style nonce="__NONCE__">__THEME__
header{position:sticky;top:0;z-index:1;background:var(--bg);border-bottom:1px solid var(--border)}
.bar{max-width:960px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;font-weight:600;font-size:17px;margin-right:auto}
.brand img{width:32px;height:32px;border-radius:var(--radius-sm)}
#account{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pill{font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;border:1px solid var(--border);color:var(--muted)}
.pill.ok,.pill.done{color:var(--ok);border-color:var(--ok)}
.pill.warn,.pill.queued,.pill.downloading{color:var(--warn);border-color:var(--warn)}
.pill.error{color:var(--danger);border-color:var(--danger)}
main{max-width:960px;margin:0 auto;padding:24px 16px 32px;display:grid;gap:24px}
section{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px}
section>h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-bottom:12px;
display:flex;justify-content:space-between;gap:8px}
section>h2 span{text-transform:none;letter-spacing:0;font-weight:500}
.add{display:flex;gap:8px;flex-wrap:wrap}
.add input{flex:1 1 280px}
.feed{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px}
.feed code{font:13px ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:4px 8px;overflow-wrap:anywhere}
ul{list-style:none;margin:0;padding:0}
.row{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-top:1px solid var(--border)}
.row:first-child{border-top:0;padding-top:0}
.row:last-child{padding-bottom:0}
.grow{flex:1;min-width:0}
.title{font-size:15px;font-weight:600;overflow-wrap:anywhere}
.meta{font-size:13px;color:var(--muted);display:flex;flex-wrap:wrap;row-gap:4px;margin-top:4px}
.meta span:not(:last-child){margin-right:8px;padding-right:8px;border-right:1px solid var(--border)}
.cover{width:72px;height:72px;border-radius:var(--radius-sm);object-fit:cover;flex:none;background:var(--raised)}
audio{width:100%;height:36px;margin-top:8px}
details{margin-top:8px}
summary{cursor:pointer;color:var(--muted);font-size:13px}
details pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;font-size:13px;color:var(--muted);margin:8px 0 0;
max-height:240px;overflow:auto;background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-sm);padding:12px}
.actions{display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex:none}
.task{align-items:center}
.task .url{overflow-wrap:anywhere}
.empty{color:var(--muted);padding:8px 0}
dialog{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:var(--radius);padding:24px;
width:min(420px,calc(100vw - 32px))}
dialog::backdrop{background:rgba(0,0,0,.6)}
dialog h2{font-size:17px;margin-bottom:8px}
dialog p{color:var(--muted);margin-bottom:12px;overflow-wrap:anywhere}
dialog code{display:block;font:15px ui-monospace,Consolas,monospace;color:var(--text);background:var(--bg);
border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px 12px;margin-bottom:12px;overflow-wrap:anywhere}
dialog .err{color:var(--danger);min-height:22px;margin:8px 0 0}
dialog .buttons{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--raised);border:1px solid var(--border);
border-radius:var(--radius-sm);padding:8px 16px;max-width:calc(100vw - 32px)}
#toast.error{border-color:var(--danger);color:var(--danger)}
#toast:empty{display:none}
@media (max-width:560px){.add input{flex-basis:100%}.add .btn{flex:1}.cover{width:56px;height:56px}.actions{flex-direction:row}.row.ep{flex-wrap:wrap}
.row.ep .actions{width:100%;justify-content:flex-end}}
@media (prefers-reduced-motion:no-preference){.btn,input{transition:border-color .12s,background-color .12s,color .12s}}
</style>
<header><div class="bar">
  <div class="brand"><img src="/artwork.jpg" alt="" width="32" height="32">YouTube Podcast</div>
  <div id="account" class="small"></div>
</div></header>
<main>
  <section aria-labelledby="addH">
    <h2 id="addH">Add</h2>
    <form id="addForm" class="add">
      <input id="addUrl" type="url" inputmode="url" required aria-label="YouTube address"
             placeholder="Paste a YouTube video, playlist or channel address">
      <button class="btn primary" name="kind" value="video">Add video</button>
      <button class="btn" name="kind" value="subscription">Subscribe</button>
    </form>
    <p class="muted small feed">Subscribing to a channel follows its Videos tab - no Shorts or live streams - whichever
      channel address you paste. Checked every hour: a channel's newest 10 videos, a playlist whole.</p>
    <div class="feed small"><span class="muted">Feed</span><code id="feedUrl"></code>
      <button class="btn ghost" id="copyFeed" type="button">Copy</button><span id="feeds" class="muted"></span></div>
  </section>
  <section aria-labelledby="actH" id="activity" hidden>
    <h2 id="actH">Activity</h2><ul id="tasks"></ul>
  </section>
  <section aria-labelledby="subH">
    <h2 id="subH">Subscriptions</h2><ul id="subs" aria-busy="true"><li class="empty">Loading...</li></ul>
  </section>
  <section aria-labelledby="epH">
    <h2 id="epH">Episodes <span id="epCount"></span></h2><ul id="eps" aria-busy="true"><li class="empty">Loading...</li></ul>
  </section>
</main>
<div id="toast" role="status" aria-live="polite"></div>

<dialog id="confirmDlg"><form method="dialog">
  <h2 id="confirmTitle"></h2><p id="confirmText"></p>
  <div class="buttons"><button class="btn" value="no">Cancel</button>
    <button class="btn danger-solid" value="yes" id="confirmOk"></button></div>
</form></dialog>

<dialog id="totpDlg"><form id="totpForm">
  <h2>Turn on two-step sign-in</h2>
  <p>Add this key to an authenticator app, then enter the 6-digit code it shows.</p>
  <code id="totpSecret"></code>
  <p class="small" id="totpUri"></p>
  <input id="totpCode" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="123456"
         aria-label="6-digit code" required>
  <p class="err small" id="totpErr" role="alert"></p>
  <div class="buttons"><button class="btn" type="button" id="totpCancel">Cancel</button>
    <button class="btn primary" id="totpConfirm">Confirm</button></div>
</form></dialog>

<dialog id="pwDlg"><form id="pwForm">
  <h2>Turn off two-step sign-in</h2>
  <p>Enter your password to confirm.</p>
  <input id="pwInput" type="password" autocomplete="current-password" aria-label="Password" required>
  <p class="err small" id="pwErr" role="alert"></p>
  <div class="buttons"><button class="btn" type="button" id="pwCancel">Cancel</button>
    <button class="btn danger-solid">Turn off</button></div>
</form></dialog>

<script nonce="__NONCE__">
const $ = id => document.getElementById(id);
function esc(v){ return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtDur(s){ s=Math.round(s||0); const h=Math.floor(s/3600), m=Math.floor(s%3600/60); return (h?h+'h ':'')+m+'m'; }
function fmtDay(t){ return new Date(t).toLocaleDateString(undefined, {month:'short', day:'numeric'}); }
function fmtWhen(s){ return s ? new Date(s).toLocaleString(undefined, {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}) : 'never'; }
function fmtNext(t){
  const m = Math.round((t*1000 - Date.now())/60000);
  return !t || m <= 0 ? 'soon' : m < 60 ? 'in '+m+'m' : 'in '+Math.round(m/60)+'h';
}
function fmtSize(bytes){ const mb = bytes/1048576; return mb >= 1024 ? (mb/1024).toFixed(1)+' GB' : mb.toFixed(1)+' MB'; }

let toastTimer;
function toast(text, isError){
  const t = $('toast');
  t.textContent = text; t.className = isError ? 'error' : '';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.textContent = ''; }, isError ? 8000 : 3500);
}

// The session cookie travels on its own; writes also carry the session's CSRF token.
const CSRF = document.querySelector('meta[name=csrf]').content;
function api(method, path, body){
  return fetch(path, {method, headers: {'Content-Type':'application/json', 'X-CSRF-Token': CSRF}, body: body ? JSON.stringify(body) : undefined});
}
async function getJson(path){
  const r = await fetch(path);
  if(r.status === 401){ location.href = '/login'; throw new Error('signed out'); }
  return r.json();
}
async function errorOf(r){ try { return (await r.json()).error || 'HTTP '+r.status; } catch(e) { return 'HTTP '+r.status; } }

// Re-render a list only when its content changed, so a playing episode or an
// open description is not reset by the periodic refresh.
const shown = {};
function render(id, html){
  if(shown[id] === html) return;
  shown[id] = html;
  $(id).innerHTML = html;
  $(id).removeAttribute('aria-busy');
}

function ask(title, text, okLabel){
  $('confirmTitle').textContent = title; $('confirmText').textContent = text; $('confirmOk').textContent = okLabel;
  const dlg = $('confirmDlg');
  dlg.returnValue = 'no';
  dlg.showModal();
  return new Promise(resolve => dlg.addEventListener('close', () => resolve(dlg.returnValue === 'yes'), {once: true}));
}

function el(tag, cls, text){ const e = document.createElement(tag); if(cls) e.className = cls; if(text) e.textContent = text; return e; }
async function account(){
  const a = await getJson('/account'), box = $('account');
  box.textContent = '';
  if(!a.username){ box.append(el('span', 'muted', 'Using the API token')); return; }
  box.append(el('span', '', a.username), el('span', 'pill ' + (a.two_step ? 'ok' : 'warn'), 'Two-step ' + (a.two_step ? 'on' : 'off')));
  const toggle = el('button', 'btn ghost', a.two_step ? 'Turn off' : 'Turn on');
  toggle.onclick = a.two_step ? () => { $('pwInput').value = ''; $('pwErr').textContent = ''; $('pwDlg').showModal(); } : startTwoStep;
  const out = el('button', 'btn ghost', 'Log out');
  out.onclick = async () => { await api('POST', '/logout'); location.href = '/login'; };
  box.append(toggle, out);
}
async function startTwoStep(){
  const r = await api('POST', '/account/totp', {action: 'start'});
  if(!r.ok){ toast(await errorOf(r), true); return; }
  const data = await r.json();
  $('totpSecret').textContent = data.secret; $('totpUri').textContent = data.uri;
  $('totpCode').value = ''; $('totpErr').textContent = '';
  $('totpDlg').showModal();
}
$('totpCancel').onclick = () => $('totpDlg').close();
$('pwCancel').onclick = () => $('pwDlg').close();
$('totpForm').onsubmit = async e => {
  e.preventDefault();
  const r = await api('POST', '/account/totp', {action: 'confirm', code: $('totpCode').value});
  if(!r.ok){ $('totpErr').textContent = await errorOf(r); return; }
  $('totpDlg').close(); toast('Two-step sign-in is on'); account();
};
$('pwForm').onsubmit = async e => {
  e.preventDefault();
  const r = await api('POST', '/account/totp', {action: 'disable', password: $('pwInput').value});
  if(!r.ok){ $('pwErr').textContent = await errorOf(r); return; }
  $('pwDlg').close(); toast('Two-step sign-in is off'); account();
};

async function refresh(){
  const [feeds, eps, subs, tasks] = await Promise.all(['/feeds', '/episodes', '/subscriptions', '/tasks'].map(getJson));
  render('feeds', feeds.length ? 'Per channel: ' + feeds.map(f =>
    `<a href="${esc(f.url)}">${esc(f.title)}</a> (${f.episodes})`).join(', ') : '');

  $('epCount').textContent = eps.length ? eps.length + (eps.length === 1 ? ' episode, ' : ' episodes, ') + fmtSize(eps.reduce((n, e) => n + e.size, 0)) : '';
  render('eps', eps.map(e => `<li class="row ep">
    ${e.thumbnail ? `<img class="cover" src="${esc(e.thumbnail)}" alt="" loading="lazy" width="72" height="72">` : '<div class="cover"></div>'}
    <div class="grow">
      <h3 class="title">${esc(e.title)}</h3>
      <div class="meta"><span>${esc(e.uploader || 'Unknown channel')}</span><span>${fmtDur(e.duration)}</span><span>${fmtSize(e.size)}</span>
        <span>added ${fmtDay(e.added*1000)}</span>${e.chapters.length ? `<span>${e.chapters.length} chapters</span>` : ''}</div>
      <audio controls preload="none" src="/audio/${encodeURIComponent(e.filename)}"></audio>
      ${e.description ? `<details><summary>Description</summary><pre>${esc(e.description)}</pre></details>` : ''}
    </div>
    <div class="actions">
      ${e.webpage_url ? `<a class="btn ghost" href="${esc(e.webpage_url)}" target="_blank" rel="noopener">YouTube</a>` : ''}
      <button class="btn ghost danger" data-del-episode="${esc(e.video_id)}" data-title="${esc(e.title)}">Delete</button>
    </div></li>`).join('') || '<li class="empty">No episodes yet. Paste a YouTube address above.</li>');

  render('subs', subs.map(s => {
    const r = s.last_result;
    const last = !r ? '<span>first check pending</span>'
      : r.ok ? `<span>last check ${fmtWhen(s.last_poll)}: ${r.new || 0} new</span>`
      : `<span class="pill error">failed</span><span>${esc(r.error).slice(0, 200)}</span>`;
    return `<li class="row"><div class="grow">
        <a class="title" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url.split('//').pop().replace('www.', ''))}</a>
        <div class="meta"><span>${s.max_items ? 'newest '+s.max_items+' per check' : 'whole list'}</span>${last}<span>next ${fmtNext(s.next_poll)}</span></div>
      </div>
      <div class="actions"><button class="btn ghost danger" data-del-sub="${esc(s.id)}" data-title="${esc(s.url)}">Unsubscribe</button></div></li>`;
  }).join('') || '<li class="empty">No subscriptions.</li>');

  $('activity').hidden = !tasks.length;
  render('tasks', tasks.slice(-8).reverse().map(t => `<li class="row task">
    <span class="pill ${esc(t.status)}">${esc(t.status)}</span>
    <div class="grow small"><span class="url">${esc(t.filename || t.url)}</span>
      ${t.downloaded && t.downloaded.length ? `<span class="muted"> - ${t.downloaded.length} new</span>` : ''}
      ${t.error ? `<div class="muted">${esc(t.error).slice(0, 300)}</div>` : ''}</div></li>`).join(''));
}
async function tick(){
  try { await refresh(); }
  catch(e) { if(e.message !== 'signed out') toast('Cannot reach the server', true); }
}

// Ids travel in data attributes, never inside inline handlers.
document.addEventListener('click', async e => {
  const d = e.target.dataset || {};
  let r;
  if(d.delSub){
    if(!await ask('Unsubscribe?', d.title, 'Unsubscribe')) return;
    r = await api('DELETE', '/subscriptions/' + encodeURIComponent(d.delSub));
  } else if(d.delEpisode){
    if(!await ask('Delete this episode?', d.title, 'Delete')) return;
    r = await api('DELETE', '/episodes/' + encodeURIComponent(d.delEpisode));
  } else return;
  if(r.ok) toast('Removed'); else toast(await errorOf(r), true);
  tick();
});
$('addForm').onsubmit = async e => {
  e.preventDefault();
  const subscribe = e.submitter && e.submitter.value === 'subscription';
  const r = await api('POST', subscribe ? '/subscriptions' : '/download', {url: $('addUrl').value.trim()});
  if(r.ok){
    $('addUrl').value = '';
    toast(subscribe ? 'Subscribed to ' + (await r.json()).url.split('//').pop().replace('www.', '') : 'Queued');
  }
  else toast(await errorOf(r), true);
  tick();
};
$('feedUrl').textContent = location.origin + '/feed';
$('copyFeed').onclick = async () => {
  try { await navigator.clipboard.writeText(location.origin + '/feed'); toast('Feed address copied'); }
  catch(e) { toast('Could not copy - select the address instead', true); }
};
account(); tick(); setInterval(tick, 5000);
</script>
</html>
""".replace("__THEME__", _THEME_CSS)


def _is_public(path: str) -> bool:
    """Routes a podcast app needs; everything else is behind the token."""
    return (
        path in ("/rss", "/feed", "/artwork.jpg", "/ping")
        or path.startswith(("/rss/", "/audio/", "/thumb/"))
        or (path.startswith("/chapters/") and path.endswith(".json"))
    )


def auth_enabled() -> bool:
    return bool(API_TOKEN) or auth.load_account() is not None


class Handler(BaseHTTPRequestHandler):
    timeout = 60  # seconds a connection may sit idle
    server_version = "ytps"  # no Python or library versions in the Server header
    sys_version = ""
    _head_only = False  # set by do_HEAD: send headers, skip the body
    _private = False  # set when the response depends on who is asking

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # same-origin (not no-referrer) so our own form posts keep their Origin header
        self.send_header("Referrer-Policy", "same-origin")
        if SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if self._private:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _client(self) -> str:
        """The caller's address. Behind the tunnel the peer is cloudflared, which
        passes the real one in CF-Connecting-IP; a direct peer cannot set it."""
        peer = self.client_address[0]
        try:
            via_proxy = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
        except ValueError:
            via_proxy = False
        forwarded = self.headers.get("CF-Connecting-IP", "").strip()
        return forwarded if via_proxy and forwarded else peer

    def _html(self, code, page, extra_headers=()):
        """Send a page with a per-response script nonce and a strict content policy."""
        nonce = secrets.token_urlsafe(16)
        body = page.replace("__NONCE__", nonce).encode("utf-8")
        self._private = True
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self'; "
            "media-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if not self._head_only:
            self.wfile.write(body)

    def _form_page(self, code, title, body, extra_headers=()):
        self._html(code, _PAGE.replace("__TITLE__", title).replace("__BODY__", body), extra_headers)

    def _redirect(self, location, cookie=None):
        self._private = True
        self.send_response(303)
        self.send_header("Location", location)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie(self, session_id, max_age):
        secure = "; Secure" if SECURE else ""
        return f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def _session_id(self) -> str:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE_NAME)
        except CookieError:
            return ""
        return morsel.value if morsel else ""

    def _insecure(self) -> bool:
        """Refuse plain http when the site is https: redirect a read, reject anything else."""
        if not SECURE or self.headers.get("X-Forwarded-Proto", "").lower() != "http":
            return False
        if self.command in ("GET", "HEAD"):
            self.send_response(308)
            self.send_header("Location", PUBLIC_BASE_URL + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(400, {"error": "use https"})
        return True

    def _refuse(self, code, error, retry_after=0) -> bool:
        self._private = True
        body = json.dumps({"error": error}).encode()
        self.send_response(code)
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not self._head_only:
            self.wfile.write(body)
        return False

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
        if self._insecure():
            return
        path, _, query = self.path.partition("?")
        if path == "/login":
            note = parse_qs(query, keep_blank_values=True)
            if auth.load_account() is None:
                body = _message("No admin account yet.") + '<p><a href="/setup">Create it</a></p>'
            else:
                body = (_message("Wrong user name, password or code." if "e" in note else "")
                        + _message("Account saved. Sign in." if "s" in note else "", ok=True) + _LOGIN_FORM)
            self._form_page(200, "Sign in", body)
            return
        if path == "/setup":
            self._form_page(200, "Set up the admin account", _SETUP_FORM)
            return
        if not _is_public(path) and not self._authorized():
            return
        if path == "/" or path == "/index.html":
            session = auth.get_session(self._session_id())
            self._html(200, INDEX_HTML.replace("__CSRF__", session["csrf"] if session else ""))
        elif path == "/account":
            session = auth.get_session(self._session_id())
            account = auth.load_account() or {}
            self._json(200, {
                "username": session["username"] if session else "",
                "two_step": bool(account.get("totp")),
            })
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

    def _authorized(self, write: bool = False) -> bool:
        """True if the request may proceed; otherwise the refusal has been sent.

        A Bearer token is accepted as is. A session cookie is enough to read;
        to write it must also come from our own page (matching Origin, the
        session's CSRF token, a JSON body), because a browser attaches cookies
        to requests that other sites trigger.
        """
        if not auth_enabled():
            return True
        self._private = True
        client = self._client()
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer" and API_TOKEN:
            wait = auth.locked_for(client)
            if wait:
                return self._refuse(429, f"too many failed attempts; try again in {wait} s", wait)
            if hmac.compare_digest(value.strip().encode(), API_TOKEN.encode()):
                return True
            auth.record_failure(client)
            print(f"[auth] wrong API token from {client}", flush=True)
            return self._refuse(401, "missing or wrong API token")
        session = auth.get_session(self._session_id())
        if session:
            if not write:
                return True
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if (self.headers.get("Origin") == PUBLIC_ORIGIN
                    and hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"])
                    and content_type == "application/json"):
                return True
            print(f"[auth] cross-site write refused from {client} (Origin {self.headers.get('Origin')})", flush=True)
            return self._refuse(403, "cross-site request refused")
        if self.command == "GET" and "text/html" in self.headers.get("Accept", ""):
            self._redirect("/login")
            return False
        return self._refuse(401, "sign in, or send the API token")

    def _read_form(self) -> dict | None:
        """A urlencoded form body as a dict, or None after answering with an error."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._form_page(413, "Too large", "")
            return None
        # A browser always names the page a form was posted from; if it is not ours, stop.
        origin = self.headers.get("Origin")
        if origin is not None and origin != PUBLIC_ORIGIN:
            self._form_page(403, "Refused", _message("This form was not sent from this site."))
            return None
        fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
        return {name: values[0] for name, values in fields.items()}

    def _locked_page(self, client) -> bool:
        wait = auth.locked_for(client)
        if wait:
            self._form_page(429, "Too many attempts",
                            _message(f"Try again in {wait // 60 + 1} minutes."), [("Retry-After", str(wait))])
        return bool(wait)

    def _login(self):
        client = self._client()
        form = self._read_form()
        if form is None or self._locked_page(client):
            return
        username = form.get("username", "")
        if auth.check_login(username, form.get("password", ""), form.get("code", "")):
            auth.clear_failures(client)
            print(f"[auth] sign-in from {client}", flush=True)
            self._redirect("/", self._cookie(auth.create_session(username.strip()), auth.SESSION_MAX_SECONDS))
        else:
            auth.record_failure(client)
            print(f"[auth] failed sign-in from {client}", flush=True)
            self._redirect("/login?e")

    def _setup(self):
        client = self._client()
        form = self._read_form()
        if form is None or self._locked_page(client):
            return
        if not API_TOKEN:
            self._form_page(403, "Set up the admin account", _message("Set API_TOKEN on the server first."))
            return
        if not hmac.compare_digest(form.get("token", "").strip().encode(), API_TOKEN.encode()):
            auth.record_failure(client)
            print(f"[auth] failed account setup from {client}", flush=True)
            self._form_page(403, "Set up the admin account", _message("Wrong API token.") + _SETUP_FORM)
            return
        username, password = form.get("username", ""), form.get("password", "")
        problem = auth.password_problem(username, password)
        if not problem and password != form.get("password2", ""):
            problem = "The two passwords are not the same."
        if problem:
            self._form_page(400, "Set up the admin account", _message(problem) + _SETUP_FORM)
            return
        auth.set_account(username, password)
        print(f"[auth] admin account set from {client}", flush=True)
        self._redirect("/login?s")

    def _two_step(self, data):
        session = auth.get_session(self._session_id())
        account = auth.load_account()
        if not session or not account:
            self._json(400, {"error": "sign in with the browser to change two-step sign-in"})
            return
        action = data.get("action")
        if action == "start":
            session["pending_totp"] = auth.new_totp_secret()
            self._json(200, {"secret": session["pending_totp"],
                             "uri": auth.totp_uri(session["pending_totp"], account["username"])})
        elif action == "confirm":
            secret = session.get("pending_totp")
            if not secret or auth.matching_totp_counter(secret, str(data.get("code", ""))) is None:
                self._json(400, {"error": "that code is not right; check the key and the phone's clock"})
                return
            auth.set_totp(secret)
            session.pop("pending_totp", None)
            self._json(200, {"two_step": True})
        elif action == "disable":
            if not auth.verify_password(str(data.get("password", "")), account["password"]):
                auth.record_failure(self._client())
                self._json(403, {"error": "wrong password"})
                return
            auth.set_totp(None)
            self._json(200, {"two_step": False})
        else:
            self._json(400, {"error": "unknown action"})

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
        if self._insecure():
            return
        if self.path == "/login":
            self._login()
            return
        if self.path == "/setup":
            self._setup()
            return
        if not self._authorized(write=True):
            return
        if self.path == "/logout":
            auth.destroy_session(self._session_id())
            self.send_response(200)
            self.send_header("Set-Cookie", self._cookie("", 0))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
        elif self.path == "/account/totp":
            data = self._read_json()
            if data is not None:
                self._two_step(data)
        elif self.path == "/download":
            data = self._read_json()
            if data is None:
                return
            url = (data.get("url") or "").strip()
            if not is_valid_url(url):
                self._json(400, {"error": tasks.URL_RULE})
                return
            task_id = enqueue_download(url)
            self._json(202, {"task_id": task_id})
        elif self.path == "/subscriptions":
            data = self._read_json()
            if data is None:
                return
            url = (data.get("url") or "").strip()
            if not is_valid_url(url):
                self._json(400, {"error": tasks.URL_RULE})
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
        if self._insecure() or not self._authorized(write=True):
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
    if not auth_enabled():
        print("WARNING: no API_TOKEN and no admin account - the admin page and API are open to anyone who can reach this server", flush=True)
    elif auth.load_account() is None:
        print(f"No admin account yet: create it at {PUBLIC_BASE_URL}/setup", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
