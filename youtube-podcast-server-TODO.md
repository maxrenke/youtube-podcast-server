# youtube-podcast-server - status, audit, proposals

Last updated 2026-10-07. Supersedes the original roadmap; items from it are
folded into the tables below.

## Where the original roadmap stands

| Roadmap item                                   | Status                                                        |
|------------------------------------------------|---------------------------------------------------------------|
| Task queue + background worker                 | Done (`tasks.py`)                                             |
| `/download` returns a task id immediately      | Done                                                          |
| `/tasks`, `/tasks/<id>`                        | Done                                                          |
| `/ping`, `/health`                             | Done                                                          |
| `GET /episodes`                                | Done                                                          |
| RSS metadata (duration, size, thumbnail, guid) | Done; extended 2026-10-07 (show notes, covers, chapters)      |
| Built-in UI                                    | Done (inline in `rss_downloader.py`); no delete button yet    |
| Playlist support                               | Done as subscriptions with hourly polling                     |
| `GET/DELETE /episodes/<id>`                    | Not done - see P2                                             |
| RSS caching                                    | Not done - see P7                                             |
| Persistent task storage                        | Not done - see P4                                             |
| Authentication                                 | Not done - see P1                                             |
| Concurrency limits, cleanup, multiple feeds    | Not done - see P5, P3, P10                                    |
| FastAPI rewrite, WebSockets, config file       | Not done, not recommended: stdlib server is enough at this size |

## Audit 2026-10-07

Scope: `rss_downloader.py`, `tasks.py`, `Dockerfile`, `docker-compose.yml`,
`deploy.ps1`, the deployed container, and the feed as Pocket Casts sees it.

### Fixed in this pass

| # | Finding                                                                                                                         | Fix                                                                                 |
|---|---------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------|
| 1 | **yt-dlp option injection.** The request `url` was appended to the yt-dlp command as-is, so a body like `{"url": "--exec=..."}` was parsed as an option. With the server now public, that was remote command execution. | URLs must match `https?://...`; the URL is passed after `--`.                        |
| 2 | **Stored XSS in the UI.** Titles, URLs and error text went into `innerHTML` unescaped; a video title containing markup would run in the page. | Everything from YouTube or a request goes through `esc()`.                          |
| 3 | No `HEAD` support (501). Podcast clients and validators probe enclosures with HEAD.                                              | `do_HEAD` on every GET route.                                                       |
| 4 | No feed artwork; no summary/owner/self link.                                                                                     | `artwork.jpg`, `/artwork.jpg`, full channel tag set.                                |
| 5 | Episode image pointed at YouTube's 16:9 thumbnail URL (cropped or rejected by apps; URL can expire).                            | Square cover saved per episode, served from `/thumb/`, also embedded in the mp3.    |
| 6 | Show notes were the raw description only; no channel, upload date, source link or chapters.                                      | Byline + link + description + chapter list, plain and HTML; chapters embedded as ID3. |
| 7 | `pubDate` came from file mtime, which changes whenever a file is rewritten.                                                      | Uses yt-dlp's `epoch` (time added) from the info.json; mtime only as fallback.      |
| 8 | Two of the three old episodes had no info.json, so titles were filenames with underscores.                                       | Test episodes removed from the feed; all new downloads write info.json.             |
| 9 | `deploy.ps1` pushed to a remote named `upstream`; the repo only has `origin`.                                                    | Pushes to `origin`.                                                                 |
| 11 | **Half-written episodes in the feed.** yt-dlp converted straight into `DOWNLOAD_DIR`, so the growing mp3 was listed (wrong length, truncated audio) for the minutes a conversion takes. Playlist pulls also printed nothing until the whole run ended. | Downloads run in `DOWNLOAD_DIR/.incoming`; each finished episode is moved into place, mp3 last, as soon as it is done. |
| 10 | The pre-commit hook (ruff) failed on 10 existing findings, so nothing could be committed without bypassing it.                  | Findings fixed; hook passes.                                                        |

### Open findings

| # | Finding                                                                                                                         | Risk   |
|---|---------------------------------------------------------------------------------------------------------------------------------|--------|
| A | **No authentication on write endpoints.** `POST /download`, `POST /subscriptions` and `DELETE /subscriptions/<id>` are open to the internet at `podcast.maxrenke.com`. Anyone can queue downloads, subscribe the box to a whole channel, or remove subscriptions. | High   |
| B | No retention and no delete endpoint. Disk only grows; the data volume was at 78% on audit day.                                   | Medium |
| C | Audio is re-encoded to V0 VBR mp3 from a ~128 kbps source: larger than the source with no quality gain (roughly 1 MB/min).       | Medium |
| D | Tasks live in memory. A restart drops queued downloads silently and there is no retry.                                           | Medium |
| E | One worker. A first poll of a large channel can run for hours and blocks single-video requests behind it.                        | Medium |
| F | A new subscription pulls the entire back catalogue (hundreds of videos for a channel).                                           | Medium |
| G | yt-dlp is fixed at whatever was latest when the image was built; YouTube changes break old builds.                               | Medium |
| H | `Content-Length` is trusted without a cap, so one request can make the server read an arbitrarily large body.                    | Low    |
| I | `/rss` and `/episodes` re-read every info.json (50 KB - 1 MB each) on every request.                                             | Low    |
| J | Container runs as root; files in the data dirs are root-owned, so managing them on the host needs sudo.                          | Low    |
| K | No tests. `AGENTS.md` describes pytest usage that does not exist.                                                                | Low    |
| L | Dead files: `ui/index.html` (never served or copied into the image), `Modelfile` and `32k.ps1` (Ollama scripts unrelated to this project), `atomicparsley` in the Dockerfile (only used for m4a). | Low    |
| M | `TZ=America/New_York` in compose; log timestamps are three hours off Pacific time.                                               | Low    |
| N | Cloudflare's CDN terms restrict serving large media files through the proxy on non-Enterprise plans. One listener is unlikely to draw attention, but it is not a supported use - worth reading the current Service-Specific Terms. | Unknown |

## Proposed improvements

Ordered by value for effort.

| #   | Proposal                                                                                                                         | Addresses | Size  |
|-----|----------------------------------------------------------------------------------------------------------------------------------|-----------|-------|
| P1  | **Token on write endpoints.** `API_TOKEN` env; every non-GET request needs `Authorization: Bearer <token>`; the UI asks once and keeps it in `localStorage`. `/rss`, `/audio`, `/thumb` stay open so podcast apps keep working. Alternative with no code: two path-scoped Cloudflare Access apps. | A         | Small |
| P2  | **Delete from the UI.** `DELETE /episodes/<video_id>` removes the mp3, jpg and info.json; a button per episode. Needs P1 first.   | B         | Small |
| P3  | **Retention.** `KEEP_DAYS` / `KEEP_COUNT` env; the scheduler tick prunes the oldest episodes.                                     | B         | Small |
| P4  | **Persist the queue.** Write queued/running tasks to `STATE_DIR/tasks.json`; re-enqueue on start; one retry with backoff.         | D         | Small |
| P5  | **Two workers**, one reserved for single-video requests so a long subscription poll cannot block them.                           | E         | Small |
| P6  | **Bound subscriptions.** `--playlist-end N` (default 10) or `--dateafter now-30days` on the first poll; per-subscription override. | F         | Small |
| P7  | **Smaller audio.** `AUDIO_QUALITY` env defaulting to 5 (about half the size, fine for speech), or skip re-encoding entirely and serve the source m4a (faster downloads, smallest files; needs `audio/mp4` enclosures). | C         | Small |
| P8  | **Keep yt-dlp current.** `yt-dlp -U` at container start, or a weekly rebuild.                                                     | G         | Small |
| P9  | **Trim info.json** to the fields the feed uses after download, and cache the episode list on the directory mtime.                 | I         | Small |
| P10 | **Feeds per source.** `/rss/<slug>` filtered by channel or subscription, each with the channel's own name and avatar, so Professor Dave and everything else are separate podcasts in the app. | -         | Medium |
| P11 | **RSS chapters.** `<podcast:chapters>` JSON per episode for players that do not read ID3 chapters.                                | -         | Small |
| P12 | **Tests** for `generate_rss`, Range parsing and URL validation; run them in the pre-commit hook.                                  | K         | Small |
| P13 | Cap request bodies at 64 KB; run the container as `PUID:PGID`; delete the dead files; set `TZ` to the right zone.                 | H, J, L, M | Small |
