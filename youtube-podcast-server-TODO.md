# youtube-podcast-server - status, audit, proposals

Last updated 2026-10-08. Supersedes the original roadmap; items from it are
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
| 11 | **Half-written episodes in the feed.** yt-dlp converted straight into `DOWNLOAD_DIR`, so the growing mp3 was listed (wrong length, truncated audio) for the minutes a conversion takes. Playlist pulls also printed nothing until the whole run ended. | Downloads run in `DOWNLOAD_DIR/.incoming`; each finished episode is moved into place, mp3 last, as soon as it is done. Stale mp3s from an interrupted conversion are cleared before each run (yt-dlp would otherwise publish the truncated file). |
| 10 | The pre-commit hook (ruff) failed on 10 existing findings, so nothing could be committed without bypassing it.                  | Findings fixed; hook passes.                                                        |

### Open findings

Findings A-M from the audit were closed on 2026-10-08 (see the table below).
Still open:

| # | Finding                                                                                                                         | Risk   |
|---|---------------------------------------------------------------------------------------------------------------------------------|--------|
| J | Container runs as root; files in the data dirs are root-owned, so managing them on the host needs sudo. Not changed: the yt-dlp self-update at start writes to `/usr/local/bin` and needs root. | Low    |
| N | Cloudflare's CDN terms restrict serving large media files through the proxy on non-Enterprise plans. One listener is unlikely to draw attention, but it is not a supported use - worth reading the current Service-Specific Terms. | Unknown |

## Proposals - status

| #   | Proposal                                   | Status 2026-10-08                                                                      |
|-----|--------------------------------------------|----------------------------------------------------------------------------------------|
| P1  | Token                                      | Done: `API_TOKEN` on everything except feeds, audio, covers, chapters; browser sign-in |
| P2  | Delete from the UI                         | Done: `DELETE /episodes/<video_id>` + button                                           |
| P3  | Retention                                  | Done: `KEEP_DAYS` / `KEEP_COUNT`, off by default                                       |
| P4  | Persist the queue                          | Done: `STATE_DIR/tasks.json`, re-queue on start, one retry                             |
| P5  | Two workers                                | Done: one for single videos, one for polls; staging folder per task                    |
| P6  | Bound subscriptions                        | Done: `max_items` / `SUB_MAX_ITEMS` (channels 10, playlists all)                       |
| P7  | Smaller audio                              | Done: `AUDIO_QUALITY`, default 5. Serving the source m4a without re-encoding: not done |
| P8  | Keep yt-dlp current                        | Done: `yt-dlp -U` at container start                                                   |
| P9  | Trim info.json, cache the episode list     | Done                                                                                   |
| P10 | Feeds per source                           | Done: `/rss/<channel-slug>`, `/feeds`. Uses the newest episode cover, not the channel avatar |
| P11 | RSS chapters                               | Done: `<podcast:chapters>` + `/chapters/<video_id>.json`                               |
| P12 | Tests                                      | Done: `tests/test_server.py`, run by the pre-commit hook                               |
| P13 | Housekeeping                               | Done: 64 KB body cap, dead files removed, `TZ` fixed. Run-as-non-root not done (J)     |

## Ideas not started

- Serve the source audio (m4a/opus) without re-encoding: fastest and smallest.
- Channel avatars as the artwork of per-channel feeds.
