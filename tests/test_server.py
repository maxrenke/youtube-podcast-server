"""Tests for the HTTP server and the on-disk episode handling. No network, no yt-dlp."""

import json
import os
import re
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
    SCHEDULER_TICK_SECONDS="3600",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth
import rss_downloader as server
import tasks

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom": "http://www.w3.org/2005/Atom",
    "podcast": "https://podcastindex.org/namespace/1.0",
}


def _add_episode(video_id, title, channel, added, chapters=(), cover=True):
    # like yt-dlp, keep characters a filesystem rejects out of the file name only
    stem = f"{title} [{video_id}]".replace("<", "").replace(">", "")
    base = os.path.join(tasks.DOWNLOAD_DIR, stem)
    with open(base + ".mp3", "wb") as f:
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
    if os.path.exists(auth.AUTH_FILE):
        os.remove(auth.AUTH_FILE)
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
    good = {"token": "secret-token", "username": "max", "password": PASSWORD, "password2": PASSWORD}
    assert _form(base_url + "/setup", {**good, "token": "nope"})[0] == 403
    assert _form(base_url + "/setup", {**good, "password": "short", "password2": "short"})[0] == 400
    assert _form(base_url + "/setup", {**good, "password2": PASSWORD + "x"})[0] == 400
    assert _form(base_url + "/setup", good, {"Origin": "https://evil.example"})[0] == 403
    assert auth.load_account() is None
    status, headers, _ = _form(base_url + "/setup", good)
    assert (status, headers["Location"]) == (303, "/login?s")
    stored = auth.load_account()
    assert stored["username"] == "max" and PASSWORD not in json.dumps(stored)


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

def test_subscription_defaults_bound_channels_but_not_playlists():
    channel = tasks.add_subscription("https://www.youtube.com/@SomeChannel")
    playlist = tasks.add_subscription("https://www.youtube.com/playlist?list=PL123")
    explicit = tasks.add_subscription("https://www.youtube.com/@Other", max_items=3)
    try:
        assert (channel["max_items"], playlist["max_items"], explicit["max_items"]) == (10, 0, 3)
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
