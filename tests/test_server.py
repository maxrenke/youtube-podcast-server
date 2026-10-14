"""Tests for the HTTP server and the on-disk episode handling. No network, no yt-dlp."""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer

import pytest

# The modules read their configuration at import time.
_ROOT = tempfile.mkdtemp(prefix="ytps_tests_")
os.environ.update(
    DOWNLOAD_DIR=os.path.join(_ROOT, "downloads"),
    STATE_DIR=os.path.join(_ROOT, "state"),
    PUBLIC_BASE_URL="https://pod.example",
    API_TOKEN="secret-token",
    SETUP_TOKEN="setup-secret",
    NTFY_URL="",
    SCHEDULER_TICK_SECONDS="3600",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import notify
import rss_downloader as server
import tasks

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "podcast": "https://podcastindex.org/namespace/1.0",
}


def _add_episode(video_id, title, channel, added, chapters=(), cover=True, ext=".mp3"):
    # like yt-dlp, keep characters a filesystem rejects out of the file name only
    stem = f"{title} [{video_id}]".replace("<", "").replace(">", "")
    base = os.path.join(tasks.DOWNLOAD_DIR, stem)
    with open(base + ext, "wb") as f:
        f.write(bytes(range(256)) * 4)
    if cover:
        with open(base + ".jpg", "wb") as f:
            f.write(b"\xff\xd8\xff")
    info = {
        "id": video_id, "title": title, "channel": channel, "epoch": added,
        "description": "Line one\nhttps://example.com/a?b=1&c=2\n\n<script>alert(1)</script>",
        "duration": 5830, "upload_date": "20250426",
        "channel_url": "https://www.youtube.com/@" + channel.replace(" ", ""),
        "webpage_url": "https://www.youtube.com/watch?v=" + video_id,
        "chapters": [{"start_time": s, "title": t} for s, t in chapters],
    }
    with open(base + ".info.json", "w", encoding="utf-8") as f:
        json.dump(info, f)
    return stem


@pytest.fixture(autouse=True)
def clean_downloads():
    for fn in os.listdir(tasks.DOWNLOAD_DIR):
        path = os.path.join(tasks.DOWNLOAD_DIR, fn)
        if os.path.isfile(path):
            os.remove(path)
    server._EPISODE_CACHE.update(key=None, items=[])
    for path in (auth.AUTH_FILE, auth.SESSIONS_FILE, tasks.ARCHIVE_FILE):
        if os.path.exists(path):
            os.remove(path)
    auth._SESSIONS.clear()
    auth._FAILURES.clear()
    yield


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # hand 3xx answers back instead of following them


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _form(url, fields, headers=None):
    head = {"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN, **(headers or {})}
    return _request(url, "POST", urllib.parse.urlencode(fields).encode(), head)


ORIGIN = "https://pod.example"
PASSWORD = "correct horse battery staple"


def _sign_in(base_url, client="198.51.100.1"):
    """Create the account, sign in, and return the headers a browser would then send."""
    auth.set_account("max", PASSWORD)
    status, headers, _ = _form(base_url + "/login", {"username": "max", "password": PASSWORD},
                               {"CF-Connecting-IP": client})
    assert (status, headers["Location"]) == (303, "/")
    cookie = headers["Set-Cookie"]
    session = {"Cookie": cookie.split(";")[0], "CF-Connecting-IP": client}
    page = _request(base_url + "/", headers=session)[2].decode()
    csrf = re.search(r'<meta name="csrf" content="([^"]+)">', page).group(1)
    return cookie, session, {**session, "Origin": ORIGIN, "X-CSRF-Token": csrf, "Content-Type": "application/json"}


AUTH = {"Authorization": "Bearer secret-token", "Content-Type": "application/json"}


# --- URL validation -----------------------------------------------------------

@pytest.mark.parametrize("url", ["https://youtu.be/abc", "https://www.youtube.com/watch?v=1", "http://m.youtube.com/x"])
def test_valid_urls(url):
    assert tasks.is_valid_url(url)


@pytest.mark.parametrize("url", ["", "--exec=touch /tmp/x", "-x", "ftp://host/x", "https://a b", "javascript:1"])
def test_urls_that_could_be_options_are_rejected(url):
    assert not tasks.is_valid_url(url)


@pytest.mark.parametrize("url", [
    "http://192.168.1.1/admin", "http://localhost:8080/x", "https://example.com/v.mp4",
    "https://youtube.com.evil.example/x", "https://notyoutube.com/x", "https://youtu.be@evil.example/x",
])
def test_only_allowed_sites_are_fetched(url, monkeypatch):
    assert not tasks.is_valid_url(url)
    monkeypatch.setattr(tasks, "ALLOWED_HOSTS", ["*"])
    assert tasks.is_valid_url(url)


# --- feed ---------------------------------------------------------------------

def test_feed_is_well_formed_and_carries_episode_metadata():
    _add_episode("vid00000001", "Old one", "Channel A", 1000)
    _add_episode("vid00000002", "New & <shiny>", "Channel B", 2000, chapters=[(0, "Intro"), (65, "Part 2")])
    channel = ET.fromstring(server.generate_rss()).find("channel")
    assert channel.find("itunes:image", NS).get("href") == "https://pod.example/artwork.jpg"
    assert channel.find("atom:link", NS).get("href") == "https://pod.example/rss"
    items = channel.findall("item")
    assert [i.findtext("title") for i in items] == ["New & <shiny>", "Old one"]  # newest addition first
    item = items[0]
    assert item.findtext("guid") == "vid00000002"
    assert item.findtext("link") == "https://www.youtube.com/watch?v=vid00000002"
    assert item.find("itunes:image", NS).get("href").startswith("https://pod.example/thumb/")
    assert item.findtext("itunes:duration", namespaces=NS) == "5830"
    assert item.findtext("description").startswith("Channel B - uploaded 2025-04-26 - 1h 37m\n")
    notes = item.findtext("content:encoded", namespaces=NS)
    assert '<a href="https://example.com/a?b=1&amp;c=2">' in notes
    assert "<script>" not in notes and "&lt;script&gt;" in notes
    # a quote in a described URL must not be able to close the href attribute
    assert server._linkify('https://x.test/"onmouseover="alert(1)') == (
        '<a href="https://x.test/&quot;onmouseover=&quot;alert(1)">https://x.test/&quot;onmouseover=&quot;alert(1)</a>')
    assert "<li>1:05 Part 2</li>" in notes
    assert item.find("podcast:chapters", NS).get("url") == "https://pod.example/chapters/vid00000002.json"
    assert items[1].find("podcast:chapters", NS) is None


def test_subscription_episodes_are_ordered_by_upload_and_hand_added_ones_by_arrival():
    day = 86400
    now = 1_800_000_000
    # a poll fetched three uploads newest first, so the oldest arrived last
    for n, (vid, uploaded) in enumerate([("sub00000003", now - 1 * day), ("sub00000002", now - 40 * day), ("sub00000001", now - 90 * day)]):
        stem = _add_episode(vid, f"upload {vid[-1]}", "Channel A", now + n * 60)
        path = os.path.join(tasks.DOWNLOAD_DIR, stem + ".info.json")
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
        info["timestamp"] = uploaded
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f)
    with open(tasks.ARCHIVE_FILE, "w", encoding="utf-8") as f:
        f.write("youtube sub00000001\nyoutube sub00000002\nyoutube sub00000003\n")
    # an old video added by hand, two days before the poll
    _add_episode("hand0000001", "added by hand", "Channel B", now - 2 * day)
    server._EPISODE_CACHE.update(key=None, items=[])
    assert [e["title"] for e in server.list_episodes()] == ["upload 3", "added by hand", "upload 2", "upload 1"]
    items = ET.fromstring(server.generate_rss()).find("channel").findall("item")
    assert [i.findtext("title") for i in items] == ["upload 3", "added by hand", "upload 2", "upload 1"]
    from email.utils import parsedate_to_datetime
    assert parsedate_to_datetime(items[2].findtext("pubDate")).timestamp() == now - 40 * day


def test_m4a_episodes_are_listed_served_and_deleted_like_mp3(base_url):
    stem = _add_episode("vid00000007", "Kept as it came", "Channel A", 2000, ext=".m4a")
    _add_episode("vid00000008", "Re-encoded", "Channel A", 1000)
    items = ET.fromstring(server.generate_rss()).find("channel").findall("item")
    kinds = {i.findtext("title"): (i.find("enclosure").get("type"), i.find("enclosure").get("url")[-4:]) for i in items}
    assert kinds == {"Kept as it came": ("audio/x-m4a", ".m4a"), "Re-encoded": ("audio/mpeg", ".mp3")}
    status, headers, body = _request(f"{base_url}/audio/{urllib.request.quote(stem)}.m4a", headers={"Range": "bytes=0-3"})
    assert (status, headers["Content-Type"], body) == (206, "audio/mp4", bytes(range(4)))
    assert _request(f"{base_url}/audio/{urllib.request.quote(stem)}.jpg")[0] == 400
    assert _request(f"{base_url}/episodes/vid00000007", "DELETE", headers=AUTH)[0] == 200
    assert not [f for f in os.listdir(tasks.DOWNLOAD_DIR) if f.startswith(stem)]


def test_fetching_a_video_again_in_the_other_format_replaces_it():
    _add_episode("vid00000009", "Twice", "Channel A", 1000)  # the mp3 from before
    workdir = os.path.join(tasks.INCOMING_DIR, "task-m4a")
    os.makedirs(workdir, exist_ok=True)
    base = os.path.join(workdir, "Twice [vid00000009]")
    with open(base + ".m4a", "wb") as f:
        f.write(b"aac")
    with open(base + ".info.json", "w", encoding="utf-8") as f:
        json.dump({"id": "vid00000009", "title": "Twice", "epoch": 5}, f)
    assert tasks._publish(base + ".m4a") == os.path.join(tasks.DOWNLOAD_DIR, "Twice [vid00000009].m4a")
    audio = sorted(f for f in os.listdir(tasks.DOWNLOAD_DIR) if f.startswith("Twice") and not f.endswith((".json", ".jpg")))
    assert audio == ["Twice [vid00000009].m4a"]
    server._EPISODE_CACHE.update(key=None, items=[])
    assert [e["video_id"] for e in server.list_episodes()] == ["vid00000009"]


def test_audio_format_picks_the_stream(monkeypatch):
    monkeypatch.setattr(tasks, "AUDIO_FORMAT", "m4a")
    args = tasks._audio_args()
    assert args[:2] == ["-f", "bestaudio[ext=m4a]/bestaudio"] and "--audio-quality" not in args
    monkeypatch.setattr(tasks, "AUDIO_FORMAT", "m4a-small")
    assert tasks._audio_args()[1].startswith("bestaudio[ext=m4a][abr<=64]/")
    monkeypatch.setattr(tasks, "AUDIO_FORMAT", "mp3")
    assert tasks._audio_args() == ["-x", "--audio-format", "mp3", "--audio-quality", tasks.AUDIO_QUALITY]


def test_subscription_controls(base_url, monkeypatch):
    monkeypatch.setattr(tasks, "SUB_QUEUE", tasks.Queue())  # no worker drains this one
    sub = tasks.add_subscription("https://www.youtube.com/@SomeChannel")
    try:
        target = f"{base_url}/subscriptions/{sub['id']}"
        assert _request(target, "PATCH", {"max_items": 3}, {"Content-Type": "application/json"})[0] == 401
        status, _, body = _request(target, "PATCH", {"max_items": 3}, AUTH)
        assert (status, json.loads(body)["max_items"], tasks.get_subscription(sub["id"])["max_items"]) == (200, 3, 3)
        for bad in ({"max_items": -1}, {"max_items": "3"}, {"max_items": True}, {}):
            assert _request(target, "PATCH", bad, AUTH)[0] == 400
        assert _request(f"{base_url}/subscriptions/nope", "PATCH", {"max_items": 3}, AUTH)[0] == 404
        with open(tasks.SUBSCRIPTIONS_FILE, encoding="utf-8") as f:
            assert json.load(f)[0]["max_items"] == 3  # saved, not just in memory

        assert _request(target + "/check", "POST", b"{}", {"Content-Type": "application/json"})[0] == 401
        status, _, body = _request(target + "/check", "POST", b"{}", AUTH)
        first = json.loads(body)["task_id"]
        assert status == 202 and tasks.get_task(first)["max_items"] == 3
        # asking again while that check is still waiting does not queue a second one
        assert json.loads(_request(target + "/check", "POST", b"{}", AUTH)[2])["task_id"] == first
        assert tasks.SUB_QUEUE.qsize() == 1
        assert tasks.get_subscription(sub["id"])["next_poll"] > time.time()
        assert _request(f"{base_url}/subscriptions/nope/check", "POST", b"{}", AUTH)[0] == 404
    finally:
        tasks.remove_subscription(sub["id"])
        for task in tasks.list_tasks():
            if task.get("subscription_id") == sub["id"]:
                tasks.TASKS.pop(task["id"], None)
        tasks._save_tasks()


def test_channel_feed_holds_only_that_channel():
    _add_episode("vid00000001", "A1", "Channel A", 1000)
    _add_episode("vid00000002", "B1", "Channel B", 2000)
    assert [f["slug"] for f in server.list_feeds()] == ["channel-a", "channel-b"]
    channel = ET.fromstring(server.generate_rss("channel-a")).find("channel")
    assert channel.findtext("title") == "Channel A"
    assert channel.find("atom:link", NS).get("href") == "https://pod.example/rss/channel-a"
    assert [i.findtext("title") for i in channel.findall("item")] == ["A1"]
    assert server.generate_rss("nobody") is None


def test_chapters_document():
    _add_episode("vid00000001", "A1", "Channel A", 1000, chapters=[(0, "Intro"), (65, "Part 2")])
    assert server.chapters_json("vid00000001") == {
        "version": "1.2.0",
        "chapters": [{"startTime": 0, "title": "Intro"}, {"startTime": 65, "title": "Part 2"}],
    }
    assert server.chapters_json("missing") is None


# --- HTTP ---------------------------------------------------------------------

def test_range_head_and_path_checks(base_url):
    stem = _add_episode("vid00000001", "A1", "Channel A", 1000)
    audio = f"{base_url}/audio/{urllib.request.quote(stem)}.mp3"
    status, headers, body = _request(audio, headers={"Range": "bytes=10-19"})
    assert (status, headers["Content-Range"], body) == (206, "bytes 10-19/1024", bytes(range(10, 20)))
    status, headers, body = _request(audio, headers={"Range": "bytes=-4"})
    assert (status, body) == (206, bytes(range(252, 256)))
    assert _request(audio, headers={"Range": "bytes=5000-"})[0] == 416
    status, headers, body = _request(audio, method="HEAD")
    assert (status, headers["Content-Length"], body) == (200, "1024", b"")
    assert _request(f"{base_url}/audio/..%2Fstate%2Fx.mp3")[0] == 400
    assert _request(f"{base_url}/thumb/{urllib.request.quote(stem)}.info.json")[0] == 400
    assert _request(f"{base_url}/rss", method="HEAD")[0] == 200
    assert b'href="https://pod.example/feed" rel="self"' in _request(f"{base_url}/feed")[2]
    assert _request(f"{base_url}/rss/channel-a")[0] == 200
    assert _request(f"{base_url}/rss/nobody")[0] == 404
    assert _request(f"{base_url}/chapters/vid00000001.json")[0] == 404  # no chapters on this one
    assert _request(f"{base_url}/rss/nobody")[0] == 404


def test_write_endpoints_need_the_token(base_url):
    _add_episode("vid00000001", "A1", "Channel A", 1000)
    body = {"url": "https://youtu.be/abc"}
    json_only = {"Content-Type": "application/json"}
    assert _request(f"{base_url}/download", "POST", body, json_only)[0] == 401
    assert _request(f"{base_url}/subscriptions", "POST", body, json_only)[0] == 401
    assert _request(f"{base_url}/episodes/vid00000001", "DELETE")[0] == 401
    wrong = {"Authorization": "Bearer nope", "Content-Type": "application/json"}
    assert _request(f"{base_url}/episodes/vid00000001", "DELETE", headers=wrong)[0] == 401
    assert len(server.list_episodes()) == 1


def test_only_what_a_podcast_app_needs_is_public(base_url):
    stem = _add_episode("vid00000001", "A1", "Channel A", 1000, chapters=[(0, "Intro")])
    name = urllib.request.quote(stem)
    for path in ("/rss", "/feed", "/rss/channel-a", "/artwork.jpg", "/ping",
                 f"/audio/{name}.mp3", f"/thumb/{name}.jpg", "/chapters/vid00000001.json"):
        assert _request(base_url + path)[0] == 200, path
    for path in ("/", "/index.html", "/episodes", "/tasks", "/tasks/x", "/subscriptions",
                 "/subscriptions/x", "/feeds", "/health", "/anything-else"):
        assert _request(base_url + path)[0] == 401, path
        assert _request(base_url + path, method="HEAD")[0] == 401, path
    # a page load is sent to the sign-in page; a script is just refused
    status, headers, _ = _request(base_url + "/", headers={"Accept": "text/html,*/*"})
    assert (status, headers["Location"]) == (303, "/login")
    assert "WWW-Authenticate" not in _request(base_url + "/episodes")[1]
    assert _request(base_url + "/episodes", headers=AUTH)[0] == 200
    # the token is not a password: HTTP Basic is not accepted
    assert _request(base_url + "/", headers={"Authorization": "Basic OnNlY3JldC10b2tlbg=="})[0] == 401


def test_security_headers_and_https_only(base_url):
    stem = _add_episode("vid00000001", "A1", "Channel A", 1000)
    public = _request(base_url + "/feed")[1]
    assert public["X-Content-Type-Options"] == "nosniff"
    assert public["X-Frame-Options"] == "DENY"
    assert public["Strict-Transport-Security"] == "max-age=31536000"
    assert "Python" not in public["Server"]
    assert "Cache-Control" not in _request(f"{base_url}/audio/{urllib.request.quote(stem)}.mp3")[1]
    assert _request(base_url + "/episodes", headers=AUTH)[1]["Cache-Control"] == "no-store"
    login = _request(base_url + "/login")[1]
    assert login["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in login["Content-Security-Policy"]
    assert "script-src 'nonce-" in login["Content-Security-Policy"]
    # plain http behind the tunnel: reads are redirected, anything else is refused unprocessed
    plain = {"X-Forwarded-Proto": "http"}
    status, headers, _ = _request(base_url + "/feed", headers=plain)
    assert (status, headers["Location"]) == (308, "https://pod.example/feed")
    assert _form(base_url + "/login", {"username": "max", "password": PASSWORD}, plain)[0] == 400
    assert _request(base_url + "/download", "POST", {"url": "https://youtu.be/abc"}, {**AUTH, **plain})[0] == 400


# --- sign-in --------------------------------------------------------------------

def test_account_setup_needs_the_token_and_a_real_password(base_url):
    good = {"token": "setup-secret", "username": "max", "password": PASSWORD, "password2": PASSWORD}
    assert _form(base_url + "/setup", {**good, "token": "nope"})[0] == 403
    assert _form(base_url + "/setup", {**good, "token": "secret-token"})[0] == 403  # the API token cannot do it
    assert _form(base_url + "/setup", {**good, "password": "short", "password2": "short"})[0] == 400
    assert _form(base_url + "/setup", {**good, "password2": PASSWORD + "x"})[0] == 400
    assert _form(base_url + "/setup", good, {"Origin": "https://evil.example"})[0] == 403
    assert auth.load_account() is None
    status, headers, _ = _form(base_url + "/setup", good)
    assert (status, headers["Location"]) == (303, "/login?s")
    stored = auth.load_account()
    assert stored["username"] == "max" and PASSWORD not in json.dumps(stored)


def test_web_setup_is_off_without_a_setup_token(base_url, monkeypatch):
    monkeypatch.setattr(server, "SETUP_TOKEN", "")
    page = _request(base_url + "/setup")[2].decode()
    assert "python auth.py set-account" in page and "<form" not in page
    good = {"token": "", "username": "max", "password": PASSWORD, "password2": PASSWORD}
    for token in ("", "secret-token"):
        assert _form(base_url + "/setup", {**good, "token": token})[0] == 403
    assert auth.load_account() is None


def test_account_set_from_the_shell_signs_out_sessions_of_the_running_server(base_url):
    _, session, _ = _sign_in(base_url)
    assert _request(base_url + "/episodes", headers=session)[0] == 200
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run = subprocess.run(
        [sys.executable, "auth.py", "set-account", "--username", "max", "--password-stdin"],
        input=PASSWORD + "2\n", capture_output=True, text=True, cwd=repo, timeout=60, check=False,
        env={**os.environ, "STATE_DIR": tasks.STATE_DIR, "NTFY_URL": ""},
    )
    assert run.returncode == 0, run.stderr
    assert "saved" in run.stdout
    # a different process replaced the account; this server's session no longer counts
    assert _request(base_url + "/episodes", headers=session)[0] == 401
    assert auth.check_login("max", PASSWORD + "2") and not auth.check_login("max", PASSWORD)
    weak = subprocess.run([sys.executable, "auth.py", "set-account", "--username", "max", "--password-stdin"],
                          input="short\n", capture_output=True, text=True, cwd=repo, timeout=60, check=False,
                          env={**os.environ, "STATE_DIR": tasks.STATE_DIR, "NTFY_URL": ""})
    assert weak.returncode == 1 and "12 characters" in weak.stderr


@pytest.fixture
def ntfy(monkeypatch):
    """A stand-in ntfy server; yields the list of notices it received."""
    from http.server import BaseHTTPRequestHandler
    received = []

    class Topic(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            received.append({"title": self.headers["Title"], "priority": self.headers["Priority"],
                             "tags": self.headers.get("Tags", ""), "body": body})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Topic)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(notify, "NTFY_URL", f"http://127.0.0.1:{httpd.server_address[1]}/topic")
    yield received
    httpd.shutdown()


def _wait_for(received, count):
    for _ in range(100):
        if len(received) >= count:
            break
        time.sleep(0.05)
    time.sleep(0.2)  # nothing more should arrive
    return [n["title"] for n in received]


def test_notices_for_sign_in_lockout_two_step_and_setup(base_url, ntfy):
    _, _, write = _sign_in(base_url, client="198.51.100.20")
    assert _wait_for(ntfy, 1) == ["Podcast: signed in"]
    assert ntfy[0]["body"] == "max from 198.51.100.20" and ntfy[0]["tags"] == "key"

    attacker = {"CF-Connecting-IP": "203.0.113.50"}
    for _ in range(auth.MAX_FAILURES + 2):  # more than enough: only the lock itself is reported
        _form(base_url + "/login", {"username": "max", "password": "guess guess guess"}, attacker)
    titles = _wait_for(ntfy, 2)
    assert titles.count("Podcast: sign-in locked") == 1
    locked = next(n for n in ntfy if n["title"] == "Podcast: sign-in locked")
    assert locked["priority"] == "high" and "203.0.113.50" in locked["body"]

    started = json.loads(_request(base_url + "/account/totp", "POST", {"action": "start"}, write)[2])
    code = auth._totp_code(started["secret"], int(time.time() // 30))
    _request(base_url + "/account/totp", "POST", {"action": "confirm", "code": code}, write)
    _request(base_url + "/account/totp", "POST", {"action": "disable", "password": PASSWORD}, write)
    titles = _wait_for(ntfy, 4)
    assert titles[-2:] == ["Podcast: two-step sign-in turned on", "Podcast: two-step sign-in turned OFF"]
    assert ntfy[-1]["priority"] == "high"

    _form(base_url + "/setup", {"token": "setup-secret", "username": "max", "password": PASSWORD, "password2": PASSWORD})
    assert _wait_for(ntfy, 5)[-1] == "Podcast: admin account replaced"


def test_notices_for_a_failed_download_and_a_subscription_that_starts_failing(ntfy, monkeypatch):
    def broken(task, workdir):
        raise RuntimeError("WARNING: something\nERROR: [youtube] abc: Video unavailable")

    monkeypatch.setattr(tasks, "_ytdlp_single", broken)
    queue = tasks.Queue()
    threading.Thread(target=tasks._worker, args=(queue,), daemon=True).start()
    task = {"id": "fail1", "type": "video", "url": "https://youtu.be/abc", "status": "queued", "attempts": 1}
    tasks.TASKS["fail1"] = task
    queue.put(task)  # its second and last try
    try:
        assert _wait_for(ntfy, 1) == ["Podcast: download failed"]
        assert ntfy[0]["body"] == "https://youtu.be/abc\nERROR: [youtube] abc: Video unavailable"
        assert task["status"] == "error"
    finally:
        tasks.TASKS.pop("fail1", None)

    monkeypatch.setattr(tasks, "_ytdlp_playlist", lambda task, workdir: (_ for _ in ()).throw(RuntimeError("ERROR: blocked")))
    monkeypatch.setattr(tasks, "SUB_QUEUE", tasks.Queue())
    sub = tasks.add_subscription("https://www.youtube.com/@SomeChannel")
    try:
        poll = {"id": "p1", "subscription_id": sub["id"], "url": sub["url"], "max_items": 10}
        for _ in range(3):  # fails three hours running: one notice
            with pytest.raises(RuntimeError):
                tasks._handle_subscription_task(dict(poll))
        assert _wait_for(ntfy, 2)[1:] == ["Podcast: subscription check failed"]
        assert ntfy[1]["body"] == sub["url"] + "\nERROR: blocked"
    finally:
        tasks.remove_subscription(sub["id"])


def test_sign_in_sets_a_locked_down_cookie_and_wrong_details_do_not(base_url):
    auth.set_account("max", PASSWORD)
    for fields in ({"username": "max", "password": "wrong password!"}, {"username": "mallory", "password": PASSWORD}):
        status, headers, _ = _form(base_url + "/login", fields, {"CF-Connecting-IP": "198.51.100.7"})
        assert (status, headers["Location"]) == (303, "/login?e")
        assert "Set-Cookie" not in headers
    assert _form(base_url + "/login", {"username": "max", "password": PASSWORD},
                 {"Origin": "https://evil.example"})[0] == 403
    assert b"Wrong user name, password or code." in _request(base_url + "/login?e")[2]
    assert b"Wrong user name" not in _request(base_url + "/login")[2]
    cookie, session, _ = _sign_in(base_url)
    assert cookie.startswith("__Host-ytps=")
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
        assert flag in cookie
    assert _request(base_url + "/episodes", headers=session)[0] == 200
    assert json.loads(_request(base_url + "/account", headers=session)[2]) == {"username": "max", "two_step": False}


def test_a_session_cookie_alone_cannot_write(base_url):
    _add_episode("vid00000001", "A1", "Channel A", 1000)
    _, session, write = _sign_in(base_url)
    target = base_url + "/episodes/vid00000001"
    assert _request(target, "DELETE", headers=session)[0] == 403  # what a forged cross-site request looks like
    assert _request(target, "DELETE", headers={**write, "Origin": "https://evil.example"})[0] == 403
    assert _request(target, "DELETE", headers={**write, "X-CSRF-Token": "guess"})[0] == 403
    assert _request(target, "DELETE", headers={**write, "Content-Type": "text/plain"})[0] == 403
    assert len(server.list_episodes()) == 1
    assert _request(target, "DELETE", headers=write)[0] == 200
    # signing out ends the session on the server, not just in the browser
    status, headers, _ = _request(base_url + "/logout", "POST", b"{}", write)
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]
    assert _request(base_url + "/episodes", headers=session)[0] == 401


def test_sessions_expire(base_url):
    _, session, _ = _sign_in(base_url)
    record = next(iter(auth._SESSIONS.values()))
    record["seen"] -= auth.SESSION_IDLE_SECONDS + 1
    assert _request(base_url + "/episodes", headers=session)[0] == 401
    _, session, _ = _sign_in(base_url)
    record = next(iter(auth._SESSIONS.values()))
    record["created"] -= auth.SESSION_MAX_SECONDS + 1
    assert _request(base_url + "/episodes", headers=session)[0] == 401


def test_keep_me_signed_in(base_url):
    auth.set_account("max", PASSWORD)
    plain = _form(base_url + "/login", {"username": "max", "password": PASSWORD})[1]["Set-Cookie"]
    kept = _form(base_url + "/login", {"username": "max", "password": PASSWORD, "remember": "1"})[1]["Set-Cookie"]
    assert "Max-Age" not in plain  # gone when the browser closes
    assert "Max-Age=2592000" in kept and "HttpOnly" in kept and "SameSite=Strict" in kept
    plain_cookie, kept_cookie = ({"Cookie": c.split(";")[0]} for c in (plain, kept))
    sessions = {s["remember"]: s for s in auth._SESSIONS.values()}
    # 13 idle hours and 8 days old: past both limits of a normal session, fine for a kept one
    for session in sessions.values():
        session["seen"] -= 13 * 3600
        session["created"] -= 8 * 86400
    assert _request(base_url + "/episodes", headers=plain_cookie)[0] == 401
    assert _request(base_url + "/episodes", headers=kept_cookie)[0] == 200
    sessions[True]["created"] -= 23 * 86400  # now 31 days old
    assert _request(base_url + "/episodes", headers=kept_cookie)[0] == 401


def test_sessions_survive_a_restart_and_only_their_hash_is_stored(base_url):
    cookie, session, write = _sign_in(base_url)
    session_id = cookie.split(";")[0].split("=", 1)[1]
    with open(auth.SESSIONS_FILE, encoding="utf-8") as f:
        stored = f.read()
    assert session_id not in stored and len(json.loads(stored)) == 1
    auth._SESSIONS.clear()  # what a restart does to memory
    assert _request(base_url + "/episodes", headers=session)[0] == 401
    auth._load_sessions()
    assert _request(base_url + "/episodes", headers=session)[0] == 200
    # still the same session: its CSRF token works, and signing out removes it from the file
    assert _request(base_url + "/logout", "POST", b"{}", write)[0] == 200
    with open(auth.SESSIONS_FILE, encoding="utf-8") as f:
        assert json.load(f) == {}
    auth._SESSIONS.clear()
    auth._load_sessions()
    assert _request(base_url + "/episodes", headers=session)[0] == 401
    # replacing the account signs every saved session out too
    _, session, _ = _sign_in(base_url)
    auth.set_account("max", PASSWORD)
    auth._SESSIONS.clear()
    auth._load_sessions()
    assert _request(base_url + "/episodes", headers=session)[0] == 401


def test_repeated_failures_lock_the_client_out(base_url):
    auth.set_account("max", PASSWORD)
    attacker, other = {"CF-Connecting-IP": "203.0.113.9"}, {"CF-Connecting-IP": "203.0.113.10"}
    for _ in range(auth.MAX_FAILURES):
        assert _form(base_url + "/login", {"username": "max", "password": "guess guess guess"}, attacker)[0] == 303
    status, headers, _ = _form(base_url + "/login", {"username": "max", "password": PASSWORD}, attacker)
    assert status == 429 and int(headers["Retry-After"]) > 0  # even the right password has to wait
    assert _request(base_url + "/episodes", headers={**AUTH, **attacker})[0] == 429
    assert _form(base_url + "/login", {"username": "max", "password": PASSWORD}, other)[1]["Location"] == "/"
    # the API token is throttled the same way
    scanner = {"CF-Connecting-IP": "203.0.113.11"}
    for _ in range(auth.MAX_FAILURES):
        assert _request(base_url + "/episodes", headers={"Authorization": "Bearer nope", **scanner})[0] == 401
    assert _request(base_url + "/episodes", headers={**AUTH, **scanner})[0] == 429


def test_two_step_codes(base_url):
    # RFC 6238 test vector: SHA-1, time 59 -> 94287082, i.e. 6 digits 287082
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth._totp_code(secret, 59 // 30) == "287082"
    assert auth.matching_totp_counter(secret, "287082", now=59) == 1
    assert auth.matching_totp_counter(secret, "287 082", now=89) == 1  # one step of clock drift
    assert auth.matching_totp_counter(secret, "287082", now=200) is None

    _, _, write = _sign_in(base_url)
    started = json.loads(_request(base_url + "/account/totp", "POST", {"action": "start"}, write)[2])
    assert started["uri"].startswith("otpauth://totp/") and started["secret"] in started["uri"]
    assert _request(base_url + "/account/totp", "POST", {"action": "confirm", "code": "000000"}, write)[0] == 400
    assert auth.load_account()["totp"] is None
    code = auth._totp_code(started["secret"], int(time.time() // 30))
    assert _request(base_url + "/account/totp", "POST", {"action": "confirm", "code": code}, write)[0] == 200

    assert not auth.check_login("max", PASSWORD)  # password alone is no longer enough
    assert not auth.check_login("max", PASSWORD, "000000")
    assert auth.check_login("max", PASSWORD, code)
    assert not auth.check_login("max", PASSWORD, code)  # a code works once
    assert _request(base_url + "/account/totp", "POST", {"action": "disable", "password": "nope"}, write)[0] == 403
    assert _request(base_url + "/account/totp", "POST", {"action": "disable", "password": PASSWORD}, write)[0] == 200
    assert auth.check_login("max", PASSWORD)


def test_bad_requests_are_rejected_before_anything_is_queued(base_url):
    before = len(tasks.list_tasks())
    assert _request(f"{base_url}/download", "POST", {"url": "--exec=id"}, AUTH)[0] == 400
    assert _request(f"{base_url}/download", "POST", b"[1,2]", AUTH)[0] == 400
    assert _request(f"{base_url}/download", "POST", b"not json", AUTH)[0] == 400
    big = b'{"url":"https://youtu.be/' + b"a" * (70 * 1024) + b'"}'
    assert _request(f"{base_url}/download", "POST", big, AUTH)[0] == 413
    assert _request(f"{base_url}/subscriptions", "POST", {"url": "https://www.youtube.com/@c", "max_items": "many"}, AUTH)[0] == 400
    assert _request(f"{base_url}/download", "POST", {"url": "http://192.168.1.1/admin"}, AUTH)[0] == 400
    assert len(tasks.list_tasks()) == before


def test_delete_episode_removes_all_three_files(base_url):
    stem = _add_episode("vid00000001", "A1", "Channel A", 1000)
    _add_episode("vid00000002", "B1", "Channel B", 2000)
    assert _request(f"{base_url}/episodes/vid00000001", "DELETE", headers=AUTH)[0] == 200
    left = os.listdir(tasks.DOWNLOAD_DIR)
    assert not [f for f in left if f.startswith(stem)]
    assert len([f for f in left if f.endswith(".mp3")]) == 1
    assert _request(f"{base_url}/episodes/vid00000001", "DELETE", headers=AUTH)[0] == 404


# --- subscriptions, retention, staging, queue persistence -------------------------

@pytest.mark.parametrize("given", [
    "https://www.youtube.com/@FlintDibble",
    "https://www.youtube.com/@FlintDibble/",
    "https://youtube.com/@FlintDibble/featured",
    "https://www.youtube.com/@FlintDibble/streams",
    "https://www.youtube.com/@FlintDibble/shorts",
    "https://m.youtube.com/@FlintDibble/videos?view=0",
])
def test_any_channel_address_becomes_its_videos_tab(given):
    assert tasks.normalize_subscription_url(given) == "https://www.youtube.com/@FlintDibble/videos"


@pytest.mark.parametrize("given, expected", [
    ("https://www.youtube.com/channel/UC27vDmUZpQjuJFFkUz8ujtg", "https://www.youtube.com/channel/UC27vDmUZpQjuJFFkUz8ujtg/videos"),
    ("https://www.youtube.com/c/SomeName/live", "https://www.youtube.com/c/SomeName/videos"),
    ("https://www.youtube.com/user/SomeName", "https://www.youtube.com/user/SomeName/videos"),
    # not channels: left exactly as given
    ("https://www.youtube.com/playlist?list=PL123", "https://www.youtube.com/playlist?list=PL123"),
    ("https://www.youtube.com/watch?v=abc&list=PL123", "https://www.youtube.com/watch?v=abc&list=PL123"),
    ("https://youtu.be/abc", "https://youtu.be/abc"),
    ("https://example.com/@FlintDibble", "https://example.com/@FlintDibble"),
])
def test_other_addresses(given, expected):
    assert tasks.normalize_subscription_url(given) == expected


def test_subscribing_stores_the_videos_tab_and_does_not_duplicate(base_url):
    status, _, body = _request(f"{base_url}/subscriptions", "POST", {"url": "https://www.youtube.com/@FlintDibble"}, AUTH)
    first = json.loads(body)
    try:
        assert (status, first["url"], first["max_items"]) == (201, "https://www.youtube.com/@FlintDibble/videos", 10)
        again = json.loads(_request(f"{base_url}/subscriptions", "POST",
                                    {"url": "https://www.youtube.com/@FlintDibble/streams"}, AUTH)[2])
        assert again["id"] == first["id"]
        assert len(tasks.list_subscriptions()) == 1
    finally:
        tasks.remove_subscription(first["id"])


def test_saved_channel_subscriptions_are_repointed_on_start(monkeypatch):
    stale = os.path.join(tasks.INCOMING_DIR, "sub-old1")
    os.makedirs(stale, exist_ok=True)
    saved = [
        {"id": "old1", "url": "https://www.youtube.com/@FlintDibble", "interval_seconds": 3600, "max_items": 10, "next_poll": 9e9},
        {"id": "ok1", "url": "https://www.youtube.com/playlist?list=PL1", "interval_seconds": 3600, "max_items": 0, "next_poll": 9e9},
    ]
    with open(tasks.SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(saved, f)
    with open(tasks.TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump([
            {"id": "t1", "type": "subscription", "subscription_id": "old1", "url": "https://www.youtube.com/@FlintDibble",
             "max_items": 10, "status": "downloading"},
            {"id": "t2", "type": "subscription", "subscription_id": "gone", "url": "https://www.youtube.com/@Gone", "status": "queued"},
        ], f)
    monkeypatch.setattr(tasks, "SUBSCRIPTIONS", {})
    monkeypatch.setattr(tasks, "TASKS", {})
    monkeypatch.setattr(tasks, "SUB_QUEUE", tasks.Queue())  # no worker drains this one
    try:
        tasks._load_subscriptions()
        tasks._load_tasks()
        assert tasks.SUBSCRIPTIONS["old1"]["url"] == "https://www.youtube.com/@FlintDibble/videos"
        assert tasks.SUBSCRIPTIONS["old1"]["next_poll"] == 0.0  # poll again straight away
        assert tasks.SUBSCRIPTIONS["ok1"]["next_poll"] == 9e9
        assert not os.path.exists(stale)  # what the old address left in staging is gone
        with open(tasks.SUBSCRIPTIONS_FILE, encoding="utf-8") as f:
            assert json.load(f)[0]["url"].endswith("/videos")
        # the interrupted poll follows the fixed address; the poll of a removed subscription is dropped
        assert list(tasks.TASKS) == ["t1"]
        assert tasks.TASKS["t1"]["url"] == "https://www.youtube.com/@FlintDibble/videos"
    finally:
        os.remove(tasks.SUBSCRIPTIONS_FILE)
        os.remove(tasks.TASKS_FILE)


def test_subscription_defaults_bound_channels_but_not_playlists():
    channel = tasks.add_subscription("https://www.youtube.com/@SomeChannel")
    playlist = tasks.add_subscription("https://www.youtube.com/playlist?list=PL123")
    explicit = tasks.add_subscription("https://www.youtube.com/@Other", max_items=3)
    try:
        assert (channel["max_items"], playlist["max_items"], explicit["max_items"]) == (10, 0, 3)
        assert channel["url"] == "https://www.youtube.com/@SomeChannel/videos"
    finally:
        for sub in (channel, playlist, explicit):
            tasks.remove_subscription(sub["id"])


def test_retention_by_count_and_age(monkeypatch):
    now = time.time()
    _add_episode("vid00000001", "ancient", "C", now - 40 * 86400)
    _add_episode("vid00000002", "old", "C", now - 5 * 86400)
    _add_episode("vid00000003", "new", "C", now - 60)
    assert tasks.prune() == []  # both limits off by default
    monkeypatch.setattr(tasks, "KEEP_DAYS", 30)
    assert tasks.prune() == ["ancient [vid00000001]"]
    monkeypatch.setattr(tasks, "KEEP_DAYS", 0)
    monkeypatch.setattr(tasks, "KEEP_COUNT", 1)
    assert tasks.prune() == ["old [vid00000002]"]
    assert [e["video_id"] for e in tasks._episodes_on_disk()] == ["vid00000003"]


def test_publish_trims_info_and_stale_audio_is_cleared():
    workdir = os.path.join(tasks.INCOMING_DIR, "task-x")
    os.makedirs(workdir, exist_ok=True)
    stale = os.path.join(workdir, "half done [vid00000009].mp3")
    with open(stale, "wb") as f:
        f.write(b"truncated")
    with open(os.path.join(workdir, "half done [vid00000009].webm"), "wb") as f:
        f.write(b"source")
    tasks._prepare_workdir(workdir)
    assert os.listdir(workdir) == ["half done [vid00000009].webm"]  # source kept for the retry

    base = os.path.join(workdir, "T [vid00000009]")
    with open(base + ".mp3", "wb") as f:
        f.write(b"audio")
    with open(base + ".info.json", "w", encoding="utf-8") as f:
        json.dump({"id": "vid00000009", "title": "T", "formats": ["x"] * 500, "epoch": 5,
                   "chapters": [{"start_time": 1.5, "end_time": 9, "title": "c"}]}, f)
    published = tasks._publish(base + ".mp3")
    assert published == os.path.join(tasks.DOWNLOAD_DIR, "T [vid00000009].mp3")
    with open(os.path.join(tasks.DOWNLOAD_DIR, "T [vid00000009].info.json"), encoding="utf-8") as f:
        info = json.load(f)
    assert info == {"id": "vid00000009", "title": "T", "epoch": 5,
                    "chapters": [{"start_time": 1.5, "title": "c"}]}


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/@qnaline", "https://www.youtube.com/@qnaline/videos", "https://www.youtube.com/channel/UC123",
    "https://www.youtube.com/playlist?list=PL123", "https://m.youtube.com/c/Name/streams",
])
def test_a_channel_or_playlist_is_not_a_single_video(url, base_url):
    assert "use Subscribe" in tasks.download_problem(url)
    before = len(tasks.list_tasks())
    status, _, body = _request(f"{base_url}/download", "POST", {"url": url}, AUTH)
    assert status == 400 and "use Subscribe" in json.loads(body)["error"]
    assert len(tasks.list_tasks()) == before


@pytest.mark.parametrize("url", ["https://youtu.be/abc", "https://www.youtube.com/watch?v=abc&list=PL123",
                                 "https://www.youtube.com/shorts/abc", "https://www.youtube.com/live/abc"])
def test_single_video_addresses_are_accepted(url):
    assert tasks.download_problem(url) == ""


def test_a_queued_channel_download_is_dropped_on_start(monkeypatch):
    with open(tasks.TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump([
            {"id": "bad", "type": "video", "url": "https://www.youtube.com/@qnaline", "status": "downloading"},
            {"id": "good", "type": "video", "url": "https://youtu.be/abc", "status": "queued"},
        ], f)
    monkeypatch.setattr(tasks, "TASKS", {})
    monkeypatch.setattr(tasks, "VIDEO_QUEUE", tasks.Queue())  # no worker drains this one
    try:
        tasks._load_tasks()
        assert list(tasks.TASKS) == ["good"]
    finally:
        os.remove(tasks.TASKS_FILE)


def test_a_running_task_can_be_stopped(base_url):
    task = {"id": "long1", "type": "video", "url": "https://youtu.be/abc", "status": "downloading", "attempts": 1}
    tasks.TASKS["long1"] = task
    outcome = {}

    def run():
        try:
            tasks._run_ytdlp([sys.executable, "-u", "-c", "import time; print('working'); time.sleep(60)"], task, 120)
        except RuntimeError as e:
            outcome["error"] = str(e)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        for _ in range(100):
            if "long1" in tasks.PROCS and tasks.TASK_LOGS.get("long1"):
                break
            time.sleep(0.05)
        assert _request(f"{base_url}/tasks/long1/cancel", "POST", b"{}")[0] == 401
        started = time.time()
        status, _, body = _request(f"{base_url}/tasks/long1/cancel", "POST", b"{}", AUTH)
        worker.join(10)
        assert (status, json.loads(body)) == (200, {"status": "cancelled"})
        assert not worker.is_alive() and time.time() - started < 10  # yt-dlp was killed, not waited for
        assert outcome == {"error": "stopped"} and "long1" not in tasks.PROCS
        # a queued task is marked straight away; a finished one cannot be stopped
        tasks.TASKS["q1"] = {"id": "q1", "type": "video", "url": "https://youtu.be/abc", "status": "queued"}
        assert json.loads(_request(f"{base_url}/tasks/q1/cancel", "POST", b"{}", AUTH)[2]) == {"status": "cancelled"}
        assert tasks.TASKS["q1"]["status"] == "cancelled"
        tasks.TASKS["d1"] = {"id": "d1", "type": "video", "url": "https://youtu.be/abc", "status": "done"}
        assert _request(f"{base_url}/tasks/d1/cancel", "POST", b"{}", AUTH)[0] == 409
        assert _request(f"{base_url}/tasks/nope/cancel", "POST", b"{}", AUTH)[0] == 404
    finally:
        for task_id in ("long1", "q1", "d1"):
            tasks.TASKS.pop(task_id, None)
            tasks.TASK_LOGS.pop(task_id, None)
        tasks._save_tasks()


def test_a_run_keeps_its_command_and_live_output(base_url):
    workdir = os.path.join(tasks.INCOMING_DIR, "run-test")
    os.makedirs(workdir, exist_ok=True)
    # stands in for yt-dlp: progress lines, one finished episode, one failed item, non-zero exit
    script = (
        "import os, sys\n"
        "print('[download]  50.0% of 10.00MiB at 1.00MiB/s ETA 00:05')\n"
        "path = os.path.join(sys.argv[1], 'T [vid00000042].mp3')\n"
        "open(path, 'wb').write(b'audio')\n"
        "print('[ExtractAudio] Destination: ' + path)\n"
        "print(path)\n"
        "print('ERROR: [youtube] abc: Video unavailable')\n"
        "sys.exit(1)\n"
    )
    task = {"id": "run1", "type": "video", "url": "https://youtu.be/abc", "status": "downloading"}
    tasks.TASKS["run1"] = task
    try:
        paths, error = tasks._run_ytdlp([sys.executable, "-u", "-c", script, workdir], task, 30, workdir)
        assert [os.path.basename(p) for p in paths] == ["T [vid00000042].mp3"]
        assert os.path.exists(os.path.join(tasks.DOWNLOAD_DIR, "T [vid00000042].mp3"))  # published as soon as it was printed
        assert error == "ERROR: [youtube] abc: Video unavailable"
        assert task["progress"] == "ERROR: [youtube] abc: Video unavailable"

        assert _request(f"{base_url}/tasks/run1/log")[0] == 401
        status, _, body = _request(f"{base_url}/tasks/run1/log", headers=AUTH)
        log = json.loads(body)
        assert status == 200 and " -u -c " in log["command"] and "run-test" in log["command"]
        assert log["lines"] == [
            "[download]  50.0% of 10.00MiB at 1.00MiB/s ETA 00:05",
            "[ExtractAudio] Destination: " + os.path.join(workdir, "T [vid00000042].mp3"),
            "published T [vid00000042].mp3",
            "ERROR: [youtube] abc: Video unavailable",
        ]
        assert log["progress_age"] >= 0 and "converting" not in log
        listed = next(t for t in json.loads(_request(f"{base_url}/tasks", headers=AUTH)[2]) if t["id"] == "run1")
        assert "lines" not in listed and listed["command"] == log["command"]
        assert _request(f"{base_url}/tasks/nope/log", headers=AUTH)[0] == 404
    finally:
        tasks.TASKS.pop("run1", None)
        tasks.TASK_LOGS.pop("run1", None)


def test_pending_tasks_survive_a_restart(monkeypatch):
    monkeypatch.setattr(tasks, "TASKS", {})
    monkeypatch.setattr(tasks, "VIDEO_QUEUE", tasks.Queue())  # no worker drains this one
    task_id = tasks.enqueue_download("https://youtu.be/abc")
    with open(tasks.TASKS_FILE, encoding="utf-8") as f:
        assert [t["id"] for t in json.load(f)] == [task_id]
    monkeypatch.setattr(tasks, "TASKS", {})
    monkeypatch.setattr(tasks, "VIDEO_QUEUE", tasks.Queue())
    tasks._load_tasks()
    assert tasks.get_task(task_id)["status"] == "queued"
    assert tasks.VIDEO_QUEUE.qsize() == 1
    tasks._set(tasks.TASKS[task_id], status="done")
    with open(tasks.TASKS_FILE, encoding="utf-8") as f:
        assert json.load(f) == []
