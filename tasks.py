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
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from queue import Queue

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
STATE_DIR = os.environ.get("STATE_DIR", "state")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", str(60 * 60)))
SCHEDULER_TICK_SECONDS = int(os.environ.get("SCHEDULER_TICK_SECONDS", "60"))
# LAME VBR quality, 0 (largest) to 9. 5 is about 130 kbps, plenty for speech.
AUDIO_QUALITY = os.environ.get("AUDIO_QUALITY", "5")
# How many entries from the top of a channel are looked at on each poll (0 = all).
SUB_MAX_ITEMS = int(os.environ.get("SUB_MAX_ITEMS", "10"))
# Retention, both off by default: drop episodes older than KEEP_DAYS, and keep
# only the newest KEEP_COUNT.
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "0"))
KEEP_COUNT = int(os.environ.get("KEEP_COUNT", "0"))
RETRY_DELAY_SECONDS = int(os.environ.get("RETRY_DELAY_SECONDS", "60"))
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

SUBSCRIPTIONS: dict[str, dict] = {}
SUBS_LOCK = threading.Lock()

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_ERROR = "error"
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
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def is_valid_url(url: str) -> bool:
    """Only http(s) URLs are handed to yt-dlp; anything else could be read as an option."""
    return bool(re.match(r"https?://[^\s]+$", url, flags=re.IGNORECASE))


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


def get_task(task_id: str) -> dict | None:
    with TASKS_LOCK:
        task = TASKS.get(task_id)
        return dict(task) if task else None


def list_tasks() -> list[dict]:
    with TASKS_LOCK:
        return [dict(t) for t in TASKS.values()]


# ---------------------------------------------------------------------------
# Subscriptions (playlists/channels polled on a schedule)
# ---------------------------------------------------------------------------

def _load_subscriptions() -> None:
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        return
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        with SUBS_LOCK:
            SUBSCRIPTIONS.clear()
            for sub in data:
                SUBSCRIPTIONS[sub["id"]] = sub
    except (OSError, json.JSONDecodeError) as e:
        print(f"[subs] failed to load {SUBSCRIPTIONS_FILE}: {e}", flush=True)


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

    ``max_items`` is how many entries from the top of the list each poll looks
    at (0 = all). Channels list newest first, so the default keeps a new
    subscription from pulling the whole back catalogue. Playlists are finite
    and usually grow at the end, so they default to all.
    """
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


def _publish(incoming_mp3: str) -> str | None:
    """Move a finished episode from its staging folder into DOWNLOAD_DIR.

    Sidecars go first and the mp3 last, so the feed never shows an episode
    without its metadata. Returns the published mp3 path.
    """
    if not incoming_mp3.lower().endswith(".mp3") or not os.path.exists(incoming_mp3):
        return None
    workdir = os.path.dirname(incoming_mp3)
    stem = os.path.splitext(os.path.basename(incoming_mp3))[0]
    _trim_info(os.path.join(workdir, stem + ".info.json"))
    for ext in (".info.json", ".jpg", ".mp3"):
        src = os.path.join(workdir, stem + ext)
        if os.path.exists(src):
            os.replace(src, os.path.join(DOWNLOAD_DIR, stem + ext))
    return os.path.join(DOWNLOAD_DIR, stem + ".mp3")


def _episodes_on_disk() -> list[dict]:
    """Every published episode as ``{"stem", "video_id", "added"}``, newest first."""
    if not os.path.isdir(DOWNLOAD_DIR):
        return []
    found = []
    for fn in os.listdir(DOWNLOAD_DIR):
        if not fn.lower().endswith(".mp3"):
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
    for ext in (".mp3", ".jpg", ".info.json"):
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


def _ytdlp_common_args(workdir: str) -> list[str]:
    """Options shared by single and playlist downloads.

    Each episode ends up as three files with the same stem: ``.mp3`` (ID3 tags,
    chapters and cover art embedded), ``.jpg`` (the same square cover, served at
    ``/thumb/``) and ``.info.json`` (source of the RSS item metadata).
    """
    # "%(xx|literal)s" is yt-dlp's way to set a metadata field to a fixed string.
    album = FEED_TITLE.replace("%", "").replace(")", "").replace("|", "")
    return [
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", AUDIO_QUALITY,
        "--embed-metadata",
        "--embed-chapters",
        "--embed-thumbnail",
        "--write-thumbnail",
        "--convert-thumbnails", "jpg",
        "--ppa", _SQUARE_THUMB,
        "--parse-metadata", f"%(xx|{album})s:%(meta_album)s",
        "--parse-metadata", "%(xx|Podcast)s:%(meta_genre)s",
        "--write-info-json",
        "-o", os.path.join(workdir, "%(title)s [%(id)s].%(ext)s"),
        "--print", "after_move:filepath",
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


def _ytdlp_single(url: str, workdir: str) -> str | None:
    """Download one video; returns the published mp3 path or None."""
    _prepare_workdir(workdir)
    cmd = ["yt-dlp", *_ytdlp_common_args(workdir), "--no-playlist", "--", url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "yt-dlp failed")
    out = proc.stdout.strip().splitlines()
    return _publish(out[-1]) if out else None


def _ytdlp_playlist(url: str, workdir: str, max_items: int = 0) -> list[str]:
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
    ]
    if max_items > 0:
        cmd += ["--playlist-end", str(max_items)]
    cmd += ["--", url]
    paths: list[str] = []
    # stderr goes to a file: a pipe nobody reads would fill up and stall yt-dlp.
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        killer = threading.Timer(60 * 60 * 6, proc.kill)
        killer.start()
        try:
            # yt-dlp prints each finished file; publish it right away instead of
            # holding every episode back until the whole poll is done.
            for line in proc.stdout or []:
                published = _publish(line.strip())
                if published:
                    paths.append(published)
            proc.wait()
        finally:
            killer.cancel()
        err.seek(0)
        stderr = err.read()
    # With --ignore-errors yt-dlp may exit non-zero even with partial success;
    # surface stderr only if nothing landed.
    if proc.returncode != 0 and not paths:
        raise RuntimeError(stderr.strip()[-2000:] or "yt-dlp failed")
    return paths


# ---------------------------------------------------------------------------
# Workers + scheduler
# ---------------------------------------------------------------------------

def _handle_video_task(task: dict) -> None:
    workdir = os.path.join(INCOMING_DIR, task["id"])
    final_path = _ytdlp_single(task["url"], workdir)
    if not final_path:
        raise RuntimeError("No mp3 produced")
    task["filename"] = os.path.basename(final_path)
    shutil.rmtree(workdir, ignore_errors=True)


def _handle_subscription_task(task: dict) -> None:
    sub_id = task["subscription_id"]
    workdir = os.path.join(INCOMING_DIR, "sub-" + sub_id)
    try:
        paths = _ytdlp_playlist(task["url"], workdir, int(task.get("max_items") or 0))
        task["downloaded"] = [os.path.basename(p) for p in paths]
        result = {"ok": True, "new": len(paths), "at": _now_iso()}
    except Exception as e:
        result = {"ok": False, "error": str(e)[:2000], "at": _now_iso()}
        raise
    finally:
        with SUBS_LOCK:
            sub = SUBSCRIPTIONS.get(sub_id)
            interval = sub["interval_seconds"] if sub else POLL_INTERVAL_SECONDS
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
            _set(task, status=STATUS_DOWNLOADING, started=_now_iso(),
                 attempts=int(task.get("attempts") or 0) + 1)
            _run_task(task)
            _set(task, status=STATUS_DONE, ended=_now_iso(), error=None)
        except Exception as e:  # noqa: BLE001 - one bad task must not kill the worker
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
