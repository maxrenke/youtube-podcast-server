import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from queue import Queue

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")

TASK_QUEUE: "Queue[dict]" = Queue()
TASKS: dict[str, dict] = {}
TASKS_LOCK = threading.Lock()

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_DONE = "done"
STATUS_ERROR = "error"


def _sanitize(name):
    name = re.sub(r"[^\w\s.-]", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name).strip("._")
    return name[:120] or "untitled"


def enqueue_download(url):
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "url": url,
        "status": STATUS_QUEUED,
        "created": datetime.utcnow().isoformat() + "Z",
        "filename": None,
        "error": None,
    }
    with TASKS_LOCK:
        TASKS[task_id] = task
    TASK_QUEUE.put(task)
    return task_id


def get_task(task_id):
    with TASKS_LOCK:
        return TASKS.get(task_id)


def list_tasks():
    with TASKS_LOCK:
        return list(TASKS.values())


def _run_ytdlp(url, out_template):
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
    final_path = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else None
    return final_path


def _worker():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    while True:
        task = TASK_QUEUE.get()
        if task is None:
            break
        try:
            with TASKS_LOCK:
                task["status"] = STATUS_DOWNLOADING
                task["started"] = datetime.utcnow().isoformat() + "Z"
            out_template = os.path.join(DOWNLOAD_DIR, "%(title)s [%(id)s].%(ext)s")
            final_path = _run_ytdlp(task["url"], out_template)
            if final_path and os.path.exists(final_path):
                task["filename"] = os.path.basename(final_path)
            else:
                # Fallback: find newest mp3
                mp3s = [f for f in os.listdir(DOWNLOAD_DIR) if f.endswith(".mp3")]
                if not mp3s:
                    raise RuntimeError("No mp3 produced")
                mp3s.sort(key=lambda f: os.path.getmtime(os.path.join(DOWNLOAD_DIR, f)), reverse=True)
                task["filename"] = mp3s[0]
            with TASKS_LOCK:
                task["status"] = STATUS_DONE
                task["ended"] = datetime.utcnow().isoformat() + "Z"
        except Exception as e:
            with TASKS_LOCK:
                task["status"] = STATUS_ERROR
                task["error"] = str(e)[:2000]
                task["ended"] = datetime.utcnow().isoformat() + "Z"
        finally:
            TASK_QUEUE.task_done()


_worker_thread = threading.Thread(target=_worker, daemon=True)
_worker_thread.start()
