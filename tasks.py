"""Task queue, worker, and subscription scheduler.

Two task types share one queue:

- ``video``        - one-shot single-video download (``--no-playlist``).
- ``subscription`` - poll a playlist or channel URL with ``--download-archive``
                     so only new uploads get pulled.

Subscriptions live on disk in ``STATE_DIR/subscriptions.json`` and yt-dlp's
deduplication archive lives in ``STATE_DIR/archive.txt``. Both survive
container restarts as long as ``STATE_DIR`` is on a mounted volume.

A daemon thread (``_scheduler``) wakes every ``POLL_INTERVAL_SECONDS`` and
enqueues a poll task for every subscription whose ``next_poll`` has passed.
On ``add_subscription`` the new subscription's ``next_poll`` is set to
*now*, so the first poll runs immediately.
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from queue import Queue

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
STATE_DIR = os.environ.get("STATE_DIR", "state")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", str(60 * 60)))
SCHEDULER_TICK_SECONDS = int(os.environ.get("SCHEDULER_TICK_SECONDS", "60"))

SUBSCRIPTIONS_FILE = os.path.join(STATE_DIR, "subscriptions.json")
ARCHIVE_FILE = os.path.join(STATE_DIR, "archive.txt")

TASK_QUEUE: "Queue[dict]" = Queue()
TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()

SUBSCRIPTIONS: dict[str, dict] = {}
SUBS_LOCK = threading.Lock()

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_ERROR = "error"

TYPE_VIDEO = "video"
TYPE_SUBSCRIPTION = "subscription"


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _sanitize(name: str) -> str:
    name = re.sub(r"[^\w\s.-]", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name).strip("._")
    return name[:120] or "untitled"


def _ensure_dirs() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Task tracking
# ---------------------------------------------------------------------------

def _record_task(task: dict) -> None:
    with TASKS_LOCK:
        TASKS[task["id"]] = task
    TASK_QUEUE.put(task)


def enqueue_download(url: str) -> str:
    """Queue a single-video download. Returns the new task id."""
    task = {
        "id": str(uuid.uuid4()),
        "type": TYPE_VIDEO,
        "url": url,
        "status": STATUS_QUEUED,
        "created": _now_iso(),
        "filename": None,
        "error": None,
    }
    _record_task(task)
    return str(task["id"])


def get_task(task_id: str) -> dict | None:
    with TASKS_LOCK:
        return TASKS.get(task_id)


def list_tasks() -> list[dict]:
    with TASKS_LOCK:
        return list(TASKS.values())


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
    tmp = SUBSCRIPTIONS_FILE + ".tmp"
    with SUBS_LOCK:
        data = list(SUBSCRIPTIONS.values())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SUBSCRIPTIONS_FILE)


def list_subscriptions() -> list[dict]:
    with SUBS_LOCK:
        return [dict(s) for s in SUBSCRIPTIONS.values()]


def get_subscription(sub_id: str) -> dict | None:
    with SUBS_LOCK:
        sub = SUBSCRIPTIONS.get(sub_id)
        return dict(sub) if sub else None


def add_subscription(url: str, interval_seconds: int | None = None) -> dict:
    """Register a playlist/channel URL for periodic polling.

    The first poll is scheduled immediately (``next_poll`` = now). The
    background scheduler will pick it up within ``SCHEDULER_TICK_SECONDS``.
    """
    sub = {
        "id": str(uuid.uuid4()),
        "url": url,
        "interval_seconds": int(interval_seconds or POLL_INTERVAL_SECONDS),
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
        "status": STATUS_QUEUED,
        "created": _now_iso(),
        "downloaded": [],
        "error": None,
    }
    _record_task(task)
    return str(task["id"])


# ---------------------------------------------------------------------------
# yt-dlp invocations
# ---------------------------------------------------------------------------

def _ytdlp_single(url: str, out_template: str) -> str | None:
    """Download one video; returns the final mp3 path or None."""
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--add-metadata",
        "--write-info-json",
        "--no-playlist",
        "-o", out_template,
        "--print", "after_move:filepath",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "yt-dlp failed")
    out = proc.stdout.strip().splitlines()
    return out[-1] if out else None


def _ytdlp_playlist(url: str, out_template: str) -> list[str]:
    """Download every new item in a playlist/channel. Returns paths of newly downloaded mp3s.

    Uses ``--download-archive`` so previously-downloaded video IDs are skipped.
    """
    _ensure_dirs()
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--embed-thumbnail",
        "--add-metadata",
        "--write-info-json",
        "--ignore-errors",
        "--yes-playlist",
        "--download-archive", ARCHIVE_FILE,
        "-o", out_template,
        "--print", "after_move:filepath",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60 * 6)
    # With --ignore-errors yt-dlp may exit non-zero even with partial success.
    # Capture printed filepaths regardless; surface stderr only if nothing landed.
    paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    paths = [p for p in paths if p.lower().endswith(".mp3") and os.path.exists(p)]
    if proc.returncode != 0 and not paths:
        raise RuntimeError(proc.stderr.strip()[-2000:] or "yt-dlp failed")
    return paths


# ---------------------------------------------------------------------------
# Worker + scheduler
# ---------------------------------------------------------------------------

def _handle_video_task(task: dict) -> None:
    out_template = os.path.join(DOWNLOAD_DIR, "%(title)s [%(id)s].%(ext)s")
    final_path = _ytdlp_single(task["url"], out_template)
    if final_path and os.path.exists(final_path):
        task["filename"] = os.path.basename(final_path)
    else:
        mp3s = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".mp3")]
        if not mp3s:
            raise RuntimeError("No mp3 produced")
        mp3s.sort(key=lambda f: os.path.getmtime(os.path.join(DOWNLOAD_DIR, f)), reverse=True)
        task["filename"] = mp3s[0]


def _handle_subscription_task(task: dict) -> None:
    sub_id = task["subscription_id"]
    out_template = os.path.join(DOWNLOAD_DIR, "%(title)s [%(id)s].%(ext)s")
    try:
        paths = _ytdlp_playlist(task["url"], out_template)
        task["downloaded"] = [os.path.basename(p) for p in paths]
        result = {"ok": True, "new": len(paths), "at": _now_iso()}
    except Exception as e:
        task["error"] = str(e)[:2000]
        result = {"ok": False, "error": task["error"], "at": _now_iso()}
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


def _worker() -> None:
    _ensure_dirs()
    while True:
        task = TASK_QUEUE.get()
        if task is None:
            break
        try:
            with TASKS_LOCK:
                task["status"] = STATUS_DOWNLOADING
                task["started"] = _now_iso()
            if task["type"] == TYPE_VIDEO:
                _handle_video_task(task)
            elif task["type"] == TYPE_SUBSCRIPTION:
                _handle_subscription_task(task)
            else:
                raise RuntimeError(f"unknown task type: {task['type']}")
            with TASKS_LOCK:
                task["status"] = STATUS_DONE
                task["ended"] = _now_iso()
        except Exception as e:
            with TASKS_LOCK:
                task["status"] = STATUS_ERROR
                task["error"] = (task.get("error") or str(e))[:2000]
                task["ended"] = _now_iso()
        finally:
            TASK_QUEUE.task_done()


def _scheduler() -> None:
    """Wake on a fixed tick and enqueue polls for any due subscription."""
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
        except Exception as e:
            print(f"[scheduler] tick error: {e}", flush=True)
        time.sleep(SCHEDULER_TICK_SECONDS)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

_ensure_dirs()
_load_subscriptions()

_worker_thread = threading.Thread(target=_worker, daemon=True, name="ytps-worker")
_worker_thread.start()

_scheduler_thread = threading.Thread(target=_scheduler, daemon=True, name="ytps-scheduler")
_scheduler_thread.start()
