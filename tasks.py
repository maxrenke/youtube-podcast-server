"""Task queues, workers, subscriptions and retention.

Two task types, each with its own queue and worker so a long subscription poll
never blocks a single-video request:

- ``video``        - one-shot single-video download (``--no-playlist``).
- ``subscription`` - poll a playlist or channel URL with ``--download-archive``
                     so only new uploads get pulled.

State on disk (``STATE_DIR``, keep it on a mounted volume):

- ``subscriptions.json`` - registered subscriptions and their schedules.
- ``archive.txt``        - yt-dlp's dedup log for subscriptions.
- ``tasks.json``         - tasks that are queued or running; re-queued on start.

A daemon thread (``_scheduler``) wakes every ``SCHEDULER_TICK_SECONDS``,
enqueues a poll for every subscription whose ``next_poll`` has passed, and
applies the retention limits (``KEEP_DAYS`` / ``KEEP_COUNT``).
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from queue import Queue
from urllib.parse import urlsplit

import notify

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
STATE_DIR = os.environ.get("STATE_DIR", "state")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", str(60 * 60)))
SCHEDULER_TICK_SECONDS = int(os.environ.get("SCHEDULER_TICK_SECONDS", "60"))
# LAME VBR quality, 0 (largest) to 9. 5 is about 130 kbps, plenty for speech.
AUDIO_QUALITY = os.environ.get("AUDIO_QUALITY", "5")
# What an episode's audio is:
#   m4a        YouTube's own 128 kbps AAC stream, kept as it is. Nothing is
#              re-encoded, so adding takes seconds and no quality is lost.
#   m4a-small  YouTube's 48 kbps AAC stream, also kept as it is: about a third
#              of the size, fine for speech.
#   mp3        re-encode to mp3 at AUDIO_QUALITY (minutes for a long video).
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "m4a").lower()
AUDIO_EXTS = (".m4a", ".mp3")
# How many entries from the top of a channel are looked at on each poll (0 = all).
SUB_MAX_ITEMS = int(os.environ.get("SUB_MAX_ITEMS", "10"))
# Retention, both off by default: drop episodes older than KEEP_DAYS, and keep
# only the newest KEEP_COUNT.
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "0"))
KEEP_COUNT = int(os.environ.get("KEEP_COUNT", "0"))
RETRY_DELAY_SECONDS = int(os.environ.get("RETRY_DELAY_SECONDS", "60"))
# Sites a download or subscription may point at (the host or any subdomain). "*" = any site.
ALLOWED_HOSTS = [
    h.strip().lower()
    for h in os.environ.get("ALLOWED_HOSTS", "youtube.com,youtu.be,youtube-nocookie.com").split(",")
    if h.strip()
]
URL_RULE = "url must be an http(s) address on an allowed site (" + ", ".join(ALLOWED_HOSTS) + ")"
MAX_ATTEMPTS = 2
TASK_HISTORY = 200

# ID3 album tag written into every mp3; rss_downloader uses the same value as the feed title.
FEED_TITLE = os.environ.get("FEED_TITLE", "YouTube Podcast")

# yt-dlp works in a per-task folder under INCOMING_DIR; finished episodes are moved
# up into DOWNLOAD_DIR. The feed lists every mp3 in DOWNLOAD_DIR, so a file must
# never be there half-written.
INCOMING_DIR = os.path.join(DOWNLOAD_DIR, ".incoming")

SUBSCRIPTIONS_FILE = os.path.join(STATE_DIR, "subscriptions.json")
ARCHIVE_FILE = os.path.join(STATE_DIR, "archive.txt")
TASKS_FILE = os.path.join(STATE_DIR, "tasks.json")

VIDEO_QUEUE: "Queue[dict]" = Queue()
SUB_QUEUE: "Queue[dict]" = Queue()
TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()
# What yt-dlp printed for each task, newest LOG_LINES lines. Kept apart from the
# task itself so the task list stays small; the admin page fetches it on demand.
TASK_LOGS: dict[str, deque] = {}
LOG_LINES = 400
# The yt-dlp process of each running task, so a task can be stopped.
PROCS: dict[str, subprocess.Popen] = {}

SUBSCRIPTIONS: dict[str, dict] = {}
SUBS_LOCK = threading.Lock()

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
_PENDING = (STATUS_QUEUED, STATUS_DOWNLOADING)

TYPE_VIDEO = "video"
TYPE_SUBSCRIPTION = "subscription"

# Fields of yt-dlp's info.json the feed uses; the rest (formats, heatmaps,
# automatic captions - hundreds of KB) is dropped when an episode is published.
_INFO_KEYS = (
    "id", "title", "description", "duration", "epoch", "timestamp", "upload_date",
    "channel", "uploader", "channel_url", "uploader_url", "webpage_url", "thumbnail",
)


def _now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"


def is_valid_url(url: str) -> bool:
    """Only http(s) URLs on an allowed site are handed to yt-dlp.

    Anything not starting with http(s):// could be read as an option, and an
    unrestricted host would let a request make this box fetch from inside the
    home network.
    """
    if not re.match(r"https?://[^\s]+$", url, flags=re.IGNORECASE):
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if "*" in ALLOWED_HOSTS:
        return bool(host)
    return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_HOSTS)


_CHANNEL_PATH = re.compile(r"^/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/[^/]*)?/?$")


def normalize_subscription_url(url: str) -> str:
    """A YouTube channel address in any form becomes that channel's Videos tab.

    Given the bare channel address, yt-dlp walks the Videos, Live and Shorts
    tabs. A subscription follows uploads only, so every channel address
    (bare, /featured, /streams, /shorts, ...) is pointed at /videos. Playlists
    and anything that is not a channel are returned unchanged.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    match = _CHANNEL_PATH.match(parts.path)
    if not match or "list=" in parts.query or not (host == "youtube.com" or host.endswith(".youtube.com")):
        return url
    return f"https://www.youtube.com/{match.group(1)}/videos"


def download_problem(url: str) -> str:
    """Why this address cannot be added as a single video, or an empty string.

    A channel or playlist address handed to the single-video path makes yt-dlp
    fetch everything behind it - a whole channel as one task.
    """
    if not is_valid_url(url):
        return URL_RULE
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    on_youtube = host == "youtube.com" or host.endswith(".youtube.com")
    if on_youtube and (_CHANNEL_PATH.match(parts.path) or parts.path.rstrip("/") == "/playlist"):
        return "that is a channel or playlist address - use Subscribe for it"
    return ""


def _ensure_dirs() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(INCOMING_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Task tracking
# ---------------------------------------------------------------------------

def queue_length() -> int:
    return VIDEO_QUEUE.qsize() + SUB_QUEUE.qsize()


def _queue_for(task: dict) -> "Queue[dict]":
    return VIDEO_QUEUE if task["type"] == TYPE_VIDEO else SUB_QUEUE


def _save_tasks() -> None:
    """Persist tasks that still have work to do, so a restart does not drop them."""
    with TASKS_LOCK:
        pending = [dict(t) for t in TASKS.values() if t["status"] in _PENDING]
    try:
        _write_json(TASKS_FILE, pending)
    except OSError as e:
        print(f"[tasks] failed to save {TASKS_FILE}: {e}", flush=True)


def _load_tasks() -> None:
    """Re-queue whatever was queued or mid-download when the process last stopped."""
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    for task in pending:
        if task["type"] == TYPE_SUBSCRIPTION:
            # The subscription may have been removed or re-pointed since the poll was queued.
            sub = SUBSCRIPTIONS.get(task.get("subscription_id", ""))
            if not sub:
                continue
            task["url"], task["max_items"] = sub["url"], sub.get("max_items", 0)
        elif download_problem(task["url"]):
            # Queued before channel and playlist addresses were refused as single videos.
            print(f"[tasks] dropped {task['url']}: {download_problem(task['url'])}", flush=True)
            shutil.rmtree(os.path.join(INCOMING_DIR, task["id"]), ignore_errors=True)
            continue
        task["status"] = STATUS_QUEUED
        with TASKS_LOCK:
            TASKS[task["id"]] = task
        _queue_for(task).put(task)
    if pending:
        print(f"[tasks] re-queued {len(pending)} task(s) from {TASKS_FILE}", flush=True)


def _set(task: dict, **fields) -> None:
    with TASKS_LOCK:
        task.update(fields)
    _save_tasks()


def _record_task(task: dict) -> None:
    with TASKS_LOCK:
        TASKS[task["id"]] = task
        finished = [t for t in TASKS.values() if t["status"] not in _PENDING]
        for old in finished[:-TASK_HISTORY]:
            del TASKS[old["id"]]
            TASK_LOGS.pop(old["id"], None)
    _queue_for(task).put(task)
    _save_tasks()


def enqueue_download(url: str) -> str:
    """Queue a single-video download. Returns the new task id."""
    task = {
        "id": str(uuid.uuid4()),
        "type": TYPE_VIDEO,
        "url": url,
        "status": STATUS_QUEUED,
        "created": _now_iso(),
        "attempts": 0,
        "filename": None,
        "error": None,
    }
    _record_task(task)
    return str(task["id"])


def _view(task: dict) -> dict:
    """A copy of the task for the API, with how long its current step has been running."""
    view = dict(task)
    converting = view.pop("converting", None)
    if view["status"] == STATUS_DOWNLOADING and view.get("progress_at"):
        view["progress_age"] = max(0, int(time.time() - view["progress_at"]))
        # ffmpeg prints nothing while it converts; the growing mp3 is the progress.
        if converting:
            try:
                view["converted_bytes"] = os.path.getsize(converting)
            except OSError:
                pass
    return view


def get_task(task_id: str) -> dict | None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        return _view(task) if task else None


def list_tasks() -> list[dict]:
    with TASKS_LOCK:
        return [_view(t) for t in TASKS.values()]


def cancel_task(task_id: str) -> str | None:
    """Stop a queued or running task. Returns its status afterwards, None if unknown."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        if task["status"] not in _PENDING:
            return task["status"]
        task["cancelled"] = True
        proc = PROCS.get(task_id)
        if task["status"] == STATUS_QUEUED:
            # nothing is running yet; the worker skips it when its turn comes
            task["status"], task["ended"] = STATUS_CANCELLED, _now_iso()
    if proc:
        proc.kill()  # the worker sees yt-dlp end and marks the task cancelled
    _save_tasks()
    return STATUS_CANCELLED


def get_task_log(task_id: str) -> dict | None:
    """The command yt-dlp was started with and the lines it has printed so far."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return None
        return {**_view(task), "lines": list(TASK_LOGS.get(task_id, ()))}


# ---------------------------------------------------------------------------
# Subscriptions (playlists/channels polled on a schedule)
# ---------------------------------------------------------------------------

def _load_subscriptions() -> None:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        repointed = False
        with SUBS_LOCK:
            SUBSCRIPTIONS.clear()
            for sub in data:
                videos_url = normalize_subscription_url(sub["url"])
                if videos_url != sub["url"]:
                    # Saved before channel addresses were pointed at the Videos tab:
                    # fix it, drop what the old address left in staging, poll again now.
                    print(f"[subs] {sub['url']} -> {videos_url}", flush=True)
                    sub["url"], sub["next_poll"] = videos_url, 0.0
                    shutil.rmtree(os.path.join(INCOMING_DIR, "sub-" + sub["id"]), ignore_errors=True)
                    repointed = True
                SUBSCRIPTIONS[sub["id"]] = sub
    except (OSError, json.JSONDecodeError) as e:
        print(f"[subs] failed to load {SUBSCRIPTIONS_FILE}: {e}", flush=True)
        return
    if repointed:
        _save_subscriptions()


def _save_subscriptions() -> None:
    _ensure_dirs()
    with SUBS_LOCK:
        data = list(SUBSCRIPTIONS.values())
    _write_json(SUBSCRIPTIONS_FILE, data)


def list_subscriptions() -> list[dict]:
    with SUBS_LOCK:
        return [dict(s) for s in SUBSCRIPTIONS.values()]


def get_subscription(sub_id: str) -> dict | None:
    with SUBS_LOCK:
        sub = SUBSCRIPTIONS.get(sub_id)
        return dict(sub) if sub else None


def add_subscription(
    url: str, interval_seconds: int | None = None, max_items: int | None = None
) -> dict:
    """Register a playlist/channel URL for periodic polling.

    The first poll is scheduled immediately (``next_poll`` = now). The
    background scheduler will pick it up within ``SCHEDULER_TICK_SECONDS``.

    A channel address is stored as that channel's Videos tab (see
    ``normalize_subscription_url``), so Shorts and live streams are left out.
    Subscribing to something already subscribed returns the existing entry.

    ``max_items`` is how many entries from the top of the list each poll looks
    at (0 = all). Channels list newest first, so the default keeps a new
    subscription from pulling the whole back catalogue. Playlists are finite
    and usually grow at the end, so they default to all.
    """
    url = normalize_subscription_url(url)
    with SUBS_LOCK:
        existing = next((dict(s) for s in SUBSCRIPTIONS.values() if s["url"] == url), None)
    if existing:
        return existing  # already subscribed: nothing to add
    if max_items is None:
        max_items = 0 if "list=" in url else SUB_MAX_ITEMS
    sub = {
        "id": str(uuid.uuid4()),
        "url": url,
        "interval_seconds": int(interval_seconds or POLL_INTERVAL_SECONDS),
        "max_items": max(0, int(max_items)),
        "added": _now_iso(),
        "last_poll": None,
        "last_result": None,
        "last_task_id": None,
        "next_poll": 0.0,  # epoch seconds; 0 = poll immediately
    }
    sub_id = str(sub["id"])
    with SUBS_LOCK:
        SUBSCRIPTIONS[sub_id] = sub
    _save_subscriptions()
    return dict(sub)


def remove_subscription(sub_id: str) -> bool:
    with SUBS_LOCK:
        if sub_id not in SUBSCRIPTIONS:
            return False
        del SUBSCRIPTIONS[sub_id]
    _save_subscriptions()
    shutil.rmtree(os.path.join(INCOMING_DIR, "sub-" + sub_id), ignore_errors=True)
    return True


def set_subscription_items(sub_id: str, max_items: int) -> dict | None:
    """Change how many entries from the top of the list each poll looks at (0 = all)."""
    with SUBS_LOCK:
        sub = SUBSCRIPTIONS.get(sub_id)
        if not sub:
            return None
        sub["max_items"] = max(0, int(max_items))
    _save_subscriptions()
    return get_subscription(sub_id)


def check_subscription_now(sub_id: str) -> str | None:
    """Poll a subscription now instead of at its next interval. Returns the poll's task id.

    If a poll of it is already queued or running, that one is returned.
    """
    sub = get_subscription(sub_id)
    if not sub:
        return None
    with TASKS_LOCK:
        pending = next((t["id"] for t in TASKS.values()
                        if t.get("subscription_id") == sub_id and t["status"] in _PENDING), None)
    if pending:
        return str(pending)
    _update_subscription(sub_id, next_poll=time.time() + sub["interval_seconds"])
    return _enqueue_subscription_poll(sub)


def _update_subscription(sub_id: str, **fields) -> None:
    with SUBS_LOCK:
        sub = SUBSCRIPTIONS.get(sub_id)
        if not sub:
            return
        sub.update(fields)
    _save_subscriptions()


def _enqueue_subscription_poll(sub: dict) -> str:
    task = {
        "id": str(uuid.uuid4()),
        "type": TYPE_SUBSCRIPTION,
        "subscription_id": sub["id"],
        "url": sub["url"],
        "max_items": sub.get("max_items", 0),
        "status": STATUS_QUEUED,
        "created": _now_iso(),
        "attempts": 0,
        "downloaded": [],
        "error": None,
    }
    _record_task(task)
    return str(task["id"])


# ---------------------------------------------------------------------------
# Episodes on disk: publishing, deleting, retention
# ---------------------------------------------------------------------------

_ARCHIVE_CACHE: dict = {"mtime": None, "ids": frozenset()}


def subscription_video_ids() -> frozenset:
    """Ids of the videos that arrived through a subscription (yt-dlp's archive file)."""
    try:
        mtime = os.stat(ARCHIVE_FILE).st_mtime
    except OSError:
        return frozenset()
    if _ARCHIVE_CACHE["mtime"] != mtime:
        with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
            ids = frozenset(line.split()[-1] for line in f if line.strip())
        _ARCHIVE_CACHE.update(mtime=mtime, ids=ids)
    return _ARCHIVE_CACHE["ids"]


def _trim_info(info_path: str) -> None:
    """Rewrite yt-dlp's info.json keeping only what the feed needs."""
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    slim = {k: info[k] for k in _INFO_KEYS if info.get(k) is not None}
    slim["chapters"] = [
        {"start_time": c.get("start_time") or 0, "title": c.get("title") or ""}
        for c in (info.get("chapters") or [])
    ]
    _write_json(info_path, slim)


def _publish(incoming_audio: str) -> str | None:
    """Move a finished episode from its staging folder into DOWNLOAD_DIR.

    Sidecars go first and the audio last, so the feed never shows an episode
    without its metadata. Returns the published audio path.
    """
    stem, audio_ext = os.path.splitext(os.path.basename(incoming_audio))
    audio_ext = audio_ext.lower()
    if audio_ext not in AUDIO_EXTS or not os.path.exists(incoming_audio):
        return None
    workdir = os.path.dirname(incoming_audio)
    _trim_info(os.path.join(workdir, stem + ".info.json"))
    for ext in (".info.json", ".jpg", audio_ext):
        src = os.path.join(workdir, stem + ext)
        if os.path.exists(src):
            os.replace(src, os.path.join(DOWNLOAD_DIR, stem + ext))
    # the same video fetched again in the other format replaces the old file
    for other in AUDIO_EXTS:
        if other != audio_ext:
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, stem + other))
            except FileNotFoundError:
                pass
    return os.path.join(DOWNLOAD_DIR, stem + audio_ext)


def _episodes_on_disk() -> list[dict]:
    """Every published episode as ``{"stem", "video_id", "added"}``, newest first."""
    if not os.path.isdir(DOWNLOAD_DIR):
        return []
    found = []
    for fn in os.listdir(DOWNLOAD_DIR):
        if not fn.lower().endswith(AUDIO_EXTS):
            continue
        stem = os.path.splitext(fn)[0]
        info: dict = {}
        try:
            with open(os.path.join(DOWNLOAD_DIR, stem + ".info.json"), "r", encoding="utf-8") as f:
                info = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
        try:
            mtime = os.path.getmtime(os.path.join(DOWNLOAD_DIR, fn))
        except OSError:
            continue
        found.append({
            "stem": stem,
            "video_id": str(info.get("id") or fn),
            "added": info.get("epoch") or mtime,
        })
    found.sort(key=lambda e: e["added"], reverse=True)
    return found


def _remove_files(stem: str) -> None:
    for ext in (*AUDIO_EXTS, ".jpg", ".info.json"):
        try:
            os.remove(os.path.join(DOWNLOAD_DIR, stem + ext))
        except FileNotFoundError:
            pass


def delete_episode(video_id: str) -> bool:
    """Remove an episode's mp3, cover and info.json. Returns False if there is no such episode."""
    stems = [e["stem"] for e in _episodes_on_disk() if e["video_id"] == video_id]
    for stem in stems:
        _remove_files(stem)
    return bool(stems)


def prune() -> list[str]:
    """Apply KEEP_DAYS / KEEP_COUNT. Returns the stems that were removed."""
    if KEEP_DAYS <= 0 and KEEP_COUNT <= 0:
        return []
    episodes = _episodes_on_disk()
    doomed = episodes[KEEP_COUNT:] if KEEP_COUNT > 0 else []
    if KEEP_DAYS > 0:
        cutoff = time.time() - KEEP_DAYS * 86400
        doomed += [e for e in episodes if e["added"] < cutoff and e not in doomed]
    for ep in doomed:
        _remove_files(ep["stem"])
        print(f"[retention] removed {ep['stem']}", flush=True)
    return [e["stem"] for e in doomed]


# ---------------------------------------------------------------------------
# yt-dlp invocations
# ---------------------------------------------------------------------------

# Square, blurred-fill version of the 16:9 YouTube thumbnail: podcast apps expect square art.
_SQUARE_THUMB = (
    "ThumbnailsConvertor+ffmpeg_o:-filter_complex "
    "[0:v]split[a][b];"
    "[a]scale=1400:1400:force_original_aspect_ratio=increase,crop=1400:1400,boxblur=40:5[bg];"
    "[b]scale=1400:-2[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2 -q:v 3"
)


def _audio_args() -> list[str]:
    """Which stream to take and what to turn it into (see AUDIO_FORMAT)."""
    if AUDIO_FORMAT == "mp3":
        return ["-x", "--audio-format", "mp3", "--audio-quality", AUDIO_QUALITY]
    # Among same-quality streams yt-dlp prefers the original language over dubs.
    small = "bestaudio[ext=m4a][abr<=64]/" if AUDIO_FORMAT == "m4a-small" else ""
    return [
        "-f", small + "bestaudio[ext=m4a]/bestaudio",
        # A no-op when the stream already is m4a; converts only if YouTube had none.
        "-x", "--audio-format", "m4a",
        # Index at the front of the file, so a player can start before it has all of it.
        "--ppa", "Metadata+ffmpeg_o:-movflags +faststart",
    ]


def _ytdlp_common_args(workdir: str) -> list[str]:
    """Options shared by single and playlist downloads.

    Each episode ends up as three files with the same stem: the audio (``.m4a``
    or ``.mp3``, with tags, chapters and cover art embedded), ``.jpg`` (the same
    square cover, served at ``/thumb/``) and ``.info.json`` (source of the RSS
    item metadata).
    """
    # "%(xx|literal)s" is yt-dlp's way to set a metadata field to a fixed string.
    album = FEED_TITLE.replace("%", "").replace(")", "").replace("|", "")
    return [
        *_audio_args(),
        "--embed-metadata",
        "--embed-chapters",
        "--embed-thumbnail",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--ppa", _SQUARE_THUMB,
        "--parse-metadata", f"%(xx|{album})s:%(meta_album)s",
        "--parse-metadata", "%(xx|Podcast)s:%(meta_genre)s",
        "--write-info-json",
        "--no-write-playlist-metafiles",  # no info.json/cover for the playlist or channel itself
        "-o", os.path.join(workdir, "%(title)s [%(id)s].%(ext)s"),
        "--print", "after_move:filepath",
        # --print makes yt-dlp quiet; turn its normal output back on, with one
        # progress line per update, so the admin page can show what it is doing.
        "--no-quiet", "--progress", "--newline",
        "--encoding", "utf-8",  # what _run_ytdlp decodes, whatever the platform's default is
    ]


def _prepare_workdir(workdir: str) -> None:
    """Create the staging folder and drop mp3s left there by an interrupted run.

    yt-dlp treats an existing mp3 as a finished conversion and would hand back
    the truncated file. Source downloads (.webm, .part) are kept so a retry
    can resume.
    """
    _ensure_dirs()
    os.makedirs(workdir, exist_ok=True)
    for fn in os.listdir(workdir):
        if fn.lower().endswith(".mp3"):
            os.remove(os.path.join(workdir, fn))


def _run_ytdlp(cmd: list[str], task: dict, timeout: int, workdir: str = "") -> tuple[list[str], str]:
    """Run yt-dlp, keeping its command line and live output on the task.

    Every line it prints goes into the task's log, and the newest line is the
    task's ``progress`` (with the staging folder cut out of it, to keep it
    readable). A line that is the path of a finished mp3 is published
    right away, so a playlist's episodes appear one by one instead of all at
    the end. Returns the published paths and, if yt-dlp exited non-zero, its
    error text.
    """
    log: deque = deque(maxlen=LOG_LINES)
    with TASKS_LOCK:
        TASK_LOGS[task["id"]] = log
        task["command"] = shlex.join(cmd)
        task["progress"], task["progress_at"] = "starting yt-dlp", time.time()
    paths: list[str] = []
    errors: list[str] = []
    # stderr is merged into stdout: one stream in the order things happened, and
    # no second pipe that could fill up and stall yt-dlp.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    with TASKS_LOCK:
        PROCS[task["id"]] = proc
    try:
        for raw in proc.stdout or []:
            line = raw.rstrip()
            if not line:
                continue
            published = _publish(line)
            if published:
                paths.append(published)
                line = "published " + os.path.basename(published)
            elif line.startswith("ERROR:"):
                errors.append(line)
            marker = "[ExtractAudio] Destination: "
            with TASKS_LOCK:
                log.append(line)
                task["converting"] = line[len(marker):] if line.startswith(marker) else None
                short = line.replace(workdir + os.sep, "") if workdir else line
                task["progress"], task["progress_at"] = short[:300], time.time()
        proc.wait()
    finally:
        killer.cancel()
        with TASKS_LOCK:
            PROCS.pop(task["id"], None)
    if task.get("cancelled"):
        raise RuntimeError("stopped")
    error = ""
    if proc.returncode != 0:
        error = "\n".join(errors)[-2000:] or "\n".join(list(log)[-5:]) or "yt-dlp failed"
    return paths, error


def _ytdlp_single(task: dict, workdir: str) -> str | None:
    """Download one video; returns the published mp3 path or None."""
    _prepare_workdir(workdir)
    # --playlist-items 1 is a backstop: whatever the address turns out to be, one item at most.
    cmd = ["yt-dlp", *_ytdlp_common_args(workdir), "--no-playlist", "--playlist-items", "1", "--", task["url"]]
    paths, error = _run_ytdlp(cmd, task, 60 * 60, workdir)
    if error and not paths:
        raise RuntimeError(error)
    return paths[-1] if paths else None


def _ytdlp_playlist(task: dict, workdir: str) -> list[str]:
    """Download every new item in a playlist/channel. Returns paths of newly published mp3s.

    Uses ``--download-archive`` so previously-downloaded video IDs are skipped.
    """
    _prepare_workdir(workdir)
    cmd = [
        "yt-dlp",
        *_ytdlp_common_args(workdir),
        "--ignore-errors",
        "--yes-playlist",
        "--download-archive", ARCHIVE_FILE,
        # Members-only videos cannot be fetched; skip them quietly instead of
        # failing the poll every hour.
        "--match-filter", "availability!=subscriber_only",
    ]
    max_items = int(task.get("max_items") or 0)
    if max_items > 0:
        cmd += ["--playlist-end", str(max_items)]
    cmd += ["--", task["url"]]
    paths, error = _run_ytdlp(cmd, task, 60 * 60 * 6, workdir)
    # With --ignore-errors yt-dlp may exit non-zero even with partial success;
    # report the error only if nothing landed.
    if error and not paths:
        raise RuntimeError(error)
    return paths


# ---------------------------------------------------------------------------
# Workers + scheduler
# ---------------------------------------------------------------------------

def _handle_video_task(task: dict) -> None:
    workdir = os.path.join(INCOMING_DIR, task["id"])
    final_path = _ytdlp_single(task, workdir)
    if not final_path:
        raise RuntimeError("No mp3 produced")
    task["filename"] = os.path.basename(final_path)
    shutil.rmtree(workdir, ignore_errors=True)


def _handle_subscription_task(task: dict) -> None:
    sub_id = task["subscription_id"]
    workdir = os.path.join(INCOMING_DIR, "sub-" + sub_id)
    try:
        paths = _ytdlp_playlist(task, workdir)
        task["downloaded"] = [os.path.basename(p) for p in paths]
        result = {"ok": True, "new": len(paths), "at": _now_iso()}
    except Exception as e:
        result = {"ok": False, "error": str(e)[:2000], "at": _now_iso()}
        raise
    finally:
        with SUBS_LOCK:
            sub = SUBSCRIPTIONS.get(sub_id)
            interval = sub["interval_seconds"] if sub else POLL_INTERVAL_SECONDS
            was_ok = not sub or not sub.get("last_result") or sub["last_result"].get("ok")
        # tell once when a subscription starts failing, not on every hourly retry
        if was_ok and not result["ok"] and not task.get("cancelled"):
            notify.send("Podcast: subscription check failed",
                        f"{task['url']}\n{str(result['error']).strip().splitlines()[-1][:300]}", "default", "x")
        _update_subscription(
            sub_id,
            last_poll=_now_iso(),
            last_result=result,
            last_task_id=task["id"],
            next_poll=time.time() + interval,
        )


def _run_task(task: dict) -> None:
    if task["type"] == TYPE_VIDEO:
        _handle_video_task(task)
    elif task["type"] == TYPE_SUBSCRIPTION:
        _handle_subscription_task(task)
    else:
        raise RuntimeError(f"unknown task type: {task['type']}")


def _worker(q: "Queue[dict]") -> None:
    _ensure_dirs()
    while True:
        task = q.get()
        try:
            if task.get("cancelled"):
                continue  # stopped while it was still waiting
            _set(task, status=STATUS_DOWNLOADING, started=_now_iso(),
                 attempts=int(task.get("attempts") or 0) + 1)
            _run_task(task)
            _set(task, status=STATUS_DONE, ended=_now_iso(), error=None)
        except Exception as e:  # noqa: BLE001 - one bad task must not kill the worker
            if task.get("cancelled"):
                _set(task, status=STATUS_CANCELLED, ended=_now_iso(), error=None)
                if task["type"] == TYPE_VIDEO:
                    shutil.rmtree(os.path.join(INCOMING_DIR, task["id"]), ignore_errors=True)
                continue
            error = str(e)[:2000]
            # A failed single download gets one more try; a failed poll simply
            # runs again at the subscription's next interval.
            if task["type"] == TYPE_VIDEO and task["attempts"] < MAX_ATTEMPTS:
                _set(task, status=STATUS_QUEUED, error=error)
                retry = threading.Timer(RETRY_DELAY_SECONDS, q.put, args=(task,))
                retry.daemon = True
                retry.start()
            else:
                _set(task, status=STATUS_ERROR, error=error, ended=_now_iso())
                if task["type"] == TYPE_VIDEO:
                    shutil.rmtree(os.path.join(INCOMING_DIR, task["id"]), ignore_errors=True)
                    last_line = error.strip().splitlines()[-1] if error.strip() else "unknown error"
                    notify.send("Podcast: download failed", f"{task['url']}\n{last_line[:300]}", "default", "x")
        finally:
            q.task_done()


def _scheduler() -> None:
    """Wake on a fixed tick: enqueue polls for due subscriptions, apply retention."""
    while True:
        try:
            now = time.time()
            due: list[dict] = []
            with SUBS_LOCK:
                for sub in SUBSCRIPTIONS.values():
                    if sub.get("next_poll", 0) <= now:
                        due.append(dict(sub))
            for sub in due:
                # Push next_poll forward immediately so we don't double-enqueue
                # if the worker is slow.
                _update_subscription(sub["id"], next_poll=now + sub["interval_seconds"])
                _enqueue_subscription_poll(sub)
            prune()
        except Exception as e:  # noqa: BLE001 - keep the scheduler alive
            print(f"[scheduler] tick error: {e}", flush=True)
        time.sleep(SCHEDULER_TICK_SECONDS)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

_ensure_dirs()
_load_subscriptions()
_load_tasks()

for _name, _queue in (("ytps-video", VIDEO_QUEUE), ("ytps-subs", SUB_QUEUE)):
    threading.Thread(target=_worker, args=(_queue,), daemon=True, name=_name).start()

threading.Thread(target=_scheduler, daemon=True, name="ytps-scheduler").start()
