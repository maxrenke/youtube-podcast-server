"""Tests for the HTTP server and the on-disk episode handling. No network, no yt-dlp."""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
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
    yield


@pytest.fixture(scope="module")
def base_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _request(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


AUTH = {"Authorization": "Bearer secret-token", "Content-Type": "application/json"}


# --- URL validation -----------------------------------------------------------

@pytest.mark.parametrize("url", ["https://youtu.be/abc", "http://example.com/x?y=1"])
def test_valid_urls(url):
    assert tasks.is_valid_url(url)


@pytest.mark.parametrize("url", ["", "--exec=touch /tmp/x", "-x", "ftp://host/x", "https://a b", "javascript:1"])
def test_urls_that_could_be_options_are_rejected(url):
    assert not tasks.is_valid_url(url)


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
    # reads stay open for podcast apps
    assert _request(f"{base_url}/rss")[0] == 200
    assert _request(f"{base_url}/episodes")[0] == 200


def test_bad_requests_are_rejected_before_anything_is_queued(base_url):
    before = len(tasks.list_tasks())
    assert _request(f"{base_url}/download", "POST", {"url": "--exec=id"}, AUTH)[0] == 400
    assert _request(f"{base_url}/download", "POST", b"[1,2]", AUTH)[0] == 400
    assert _request(f"{base_url}/download", "POST", b"not json", AUTH)[0] == 400
    big = b'{"url":"https://youtu.be/' + b"a" * (70 * 1024) + b'"}'
    assert _request(f"{base_url}/download", "POST", big, AUTH)[0] == 413
    assert _request(f"{base_url}/subscriptions", "POST", {"url": "https://x.test/@c", "max_items": "many"}, AUTH)[0] == 400
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
