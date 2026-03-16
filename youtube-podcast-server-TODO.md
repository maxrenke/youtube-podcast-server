# youtube-podcast-server-TODO.md

## 🎯 Project Goal
Transform the current synchronous `rss_downloader.py` script into a **robust, asynchronous, queued, observable podcast server** with:

- async downloads  
- task queue + status endpoints  
- health + ping  
- a simple built‑in UI for managing episodes  
- RSS feed generation  
- audio file hosting  
- eventual migration to a full **FastAPI** service  
- future extensibility (playlists, categories, cleanup, etc.)

---

# 🚨 High‑Priority Tasks (Do These First)

## 1. Add a Simple Web UI for Managing Podcasts
Create a minimal HTML interface served by the server itself.

### UI Features:
- List all downloaded episodes  
- Show:
  - title  
  - description  
  - thumbnail  
  - file size  
  - duration (if available)  
  - creation date  
- Buttons:
  - **Delete episode**
  - **View metadata**
  - **Download MP3**
- A simple form to submit a new YouTube URL (POST → `/download`)
- A “Tasks” page showing queued/downloading/completed tasks

### Implementation Notes:
- Serve static HTML from `/ui` or `/`
- Use lightweight JS fetch calls to:
  - GET `/tasks`
  - GET `/episodes`
  - DELETE `/episodes/<id>`
- No frameworks needed — pure HTML/CSS/JS is fine

---

# 🧵 Phase 1 — Async Downloading + Task Queue

## 2. Introduce a Global Task Queue
Use `queue.Queue()` or `collections.deque()`.

Each task should include:
- `id` (UUID)
- `url`
- `status` (`queued`, `downloading`, `done`, `error`)
- timestamps
- output filename
- error message (optional)

## 3. Add a Background Worker Thread
- Runs forever  
- Pulls tasks from queue  
- Calls `download_audio(url)`  
- Updates task status  

## 4. Modify `/download` POST Endpoint
- Do **not** call `download_audio` directly  
- Instead:
  - create a task  
  - push it into the queue  
  - return `{ "task_id": "<uuid>" }` immediately  

## 5. Add Task Endpoints
### `GET /tasks`
Return all tasks.

### `GET /tasks/<id>`
Return status of a single task.

---

# 🩺 Phase 2 — Health & Ping Endpoints

## 6. Add `/ping`
Return:
```json
{ "message": "pong" }
```

## 7. Add `/health`
Return:
```json
{
  "status": "ok",
  "queue_length": <int>,
  "tasks_total": <int>,
  "uptime_seconds": <int>
}
```

---

# 📡 Phase 3 — Episode Management Endpoints

## 8. Add `/episodes` GET
Return metadata for all downloaded episodes:
- filename  
- title  
- description  
- thumbnail URL  
- size  
- duration  
- created_at  

## 9. Add `/episodes/<id>` GET
Return metadata for a single episode.

## 10. Add `/episodes/<id>` DELETE
Delete:
- MP3  
- description file  
- thumbnail  

Update RSS feed accordingly.

---

# 📻 Phase 4 — RSS Improvements

## 11. Add Metadata to RSS Items
- duration (via ffprobe)  
- file size  
- thumbnail URL  
- episode GUID  

## 12. Add RSS Caching
Cache RSS XML for 30 seconds to reduce CPU load.

---

# ⚙️ Phase 5 — Convert to FastAPI

## 13. Create `server_fastapi.py`
Replace the current `SimpleHTTPRequestHandler` with FastAPI.

### Required Routes:
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve UI |
| GET | `/ping` | Ping |
| GET | `/health` | Health |
| POST | `/download` | Queue download |
| GET | `/tasks` | List tasks |
| GET | `/tasks/{id}` | Task status |
| GET | `/episodes` | List episodes |
| GET | `/episodes/{id}` | Episode metadata |
| DELETE | `/episodes/{id}` | Delete episode |
| GET | `/rss` | RSS feed |
| GET | `/audio/{filename}` | Serve audio |

## 14. Add CORS Support
Allow browser UI to call API endpoints.

## 15. Add Graceful Shutdown
- Stop worker threads  
- Flush queue  

---

# 🔮 Phase 6 — Future Features to Consider

## 16. Persistent Task Storage
Options:
- SQLite  
- TinyDB  
- JSON file  

## 17. Authentication
- API key  
- Basic auth  
- Token header  

## 18. Concurrency Limits
- Max N parallel downloads  
- Queue the rest  

## 19. WebSocket Support
Real‑time task updates in UI.

## 20. Automatic Cleanup
- Delete old episodes after X days  
- Keep RSS entries for Y days  

## 21. Playlist Support
- Accept playlist URLs  
- Queue each video as a separate task  

## 22. Multiple Podcast Feeds
- `/rss/music`  
- `/rss/interviews`  
- `/rss/tech`  

## 23. Config File
YAML or JSON:
- download directory  
- feed title  
- hostname  
- port  
- max tasks  
- logging level  

---

# 🧩 Phase 7 — Codebase Cleanup & Modularization

## 24. Split Code Into Modules
- `downloader.py`  
- `rss.py`  
- `tasks.py`  
- `server.py`  
- `ui/` (HTML/JS/CSS)  

## 25. Add Logging
- structured logs  
- timestamps  
- task lifecycle events  

---

# 🏁 Summary
This TODO file gives Claude a clear, sequential roadmap to transform your script into a **full podcast server with UI, queueing, async downloads, FastAPI, and future extensibility**.
