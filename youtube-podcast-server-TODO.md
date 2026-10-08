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
| N | Cloudflare's CDN terms restrict serving large media files through the proxy on non-Enterprise plans. One listener is unlikely to draw attention, but it is not a supported use - worth reading the current Service-Specific Terms. | Unknown |

## Security audit 2026-10-08

Scope: authentication and exposure of the public instance. Each finding was
confirmed against the live site before the fix and re-tested after it.

| # | Severity | Finding | Status |
|---|----------|---------|--------|
| S1 | High | **CSRF.** The admin page used HTTP Basic, which a browser attaches to requests triggered by other sites. A cross-site "simple" POST (`text/plain`, foreign `Origin`) to `/download` was accepted. | Fixed: session cookie with `SameSite=Strict`; cookie-authenticated writes need our `Origin`, a CSRF token and a JSON content type |
| S2 | High | **The cloudflared WebUI (port 14333) hands the tunnel token to anyone on the LAN or tailnet** (`GET /config`, no login). With it someone can run their own connector for this tunnel and receive the site's traffic, sign-ins included. | Fixed 2026-10-08: the WebUI port is bound to loopback on the box (closed on LAN and tailnet) and the tunnel secret was rotated |
| S3 | Medium | The login was one static shared secret typed into a browser prompt: no user, no lockout, no logout, same secret as the automation. | Fixed: password account (scrypt), optional TOTP, server-side sessions, logout; the API token is for scripts only |
| S4 | Medium | No throttling: 40 wrong passwords in 5.5 s, all answered. | Fixed: 5 failures lock the client address out for 15 minutes |
| S5 | Medium | Plain http was served (`http://podcast.maxrenke.com/feed` -> 200, the sign-in prompt too). | Fixed: the app redirects or refuses http and sends HSTS; "Always Use HTTPS" is on for the zone, so the edge redirects first |
| S6 | Medium | The server fetched any http(s) URL it was given, including addresses inside the home network (SSRF, reachable through S1). | Fixed: `ALLOWED_HOSTS`, YouTube only by default |
| S7 | Medium | The origin was published on every interface over plain http (`casaos.local:5757`), so a token used there crossed the LAN in clear. | Fixed: bound to the docker bridge address only |
| S8 | Low | No security headers at all; the admin page could be framed; no content policy. | Fixed: nosniff, frame denial, referrer policy, CSP with script nonce, `no-store` on signed-in responses |
| S9 | Low | A double quote in a video description URL could close the `href` in the show notes sent to podcast apps. | Fixed |
| S10 | Low | Container ran with default capabilities. | Fixed: runs as uid 1000, `cap_drop: ALL`, `no-new-privileges` |
| S11 | Low | `Server` header named the Python version (hidden by Cloudflare, visible on the LAN). | Fixed |
| S12 | Info | The feed and audio are readable by anyone who has or guesses the address (`/feed`, `/rss`). | Open by design. A secret path (`/f/<key>/feed`) would close it at the cost of re-following the feed in the app |
| S13 | Info | A Cloudflare API token that can edit every zone on the account sits in a file on the PC and never expires. It was only needed for set-up. | Fixed 2026-10-08: token deleted in the Cloudflare dashboard |
| S14 | Info | yt-dlp is fetched unpinned at build time and self-updates at start. | Accepted: needed to keep YouTube working |

Checked and found in order: TLS 1.2 and 1.3 only at the edge; no secrets in
git history; token comparison is constant-time; path traversal on `/audio`
and `/thumb` is refused; the extension keeps its token in extension storage
and only talks to the configured server.

## Proposals - status

| #   | Proposal                                   | Status 2026-10-08                                                                      |
|-----|--------------------------------------------|----------------------------------------------------------------------------------------|
| P1  | Sign-in                                    | Done: password account with optional two-step codes for people, `API_TOKEN` for scripts |
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
| P13 | Housekeeping                               | Done: 64 KB body cap, dead files removed, `TZ` fixed, container runs as uid 1000      |

## Ideas not started

- Serve the source audio (m4a/opus) without re-encoding: fastest and smallest.
- Channel avatars as the artwork of per-channel feeds.
