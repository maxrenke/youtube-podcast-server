# YouTube Podcast Server

Turns YouTube videos, playlists, and channels into a self-hosted podcast RSS
feed. Single-file downloads run on demand; playlist/channel subscriptions are
re-polled on a schedule so new uploads get pulled automatically.

```
[ you ] --POST /download--+              +--> downloads/ (mp3 + .info.json)
                          |              |
[ you ] --POST /subscrip-+--> task queue --+--> /rss <-- podcast app subscribes
[ scheduler tick ] ------+              |
                                        +--> /audio/<file>.mp3 served to player
```

## Features

- **Single-video downloads** - POST a video URL, get an mp3 with embedded
  thumbnail and metadata.
- **Playlist and channel subscriptions** - POST a playlist or channel URL once,
  server polls it every hour (configurable) and only pulls IDs it hasn't seen
  before (`yt-dlp --download-archive`).
- **First poll is immediate** - a brand-new subscription enqueues a download
  the moment it's added, so you don't wait an hour for the back-catalog.
- **Persistent state** - subscriptions and the dedup archive are JSON/text
  files in a mounted volume; container restarts don't re-download anything
  or forget your subs.
- **RSS feed** with iTunes namespace tags so it imports cleanly into
  PocketCasts, Overcast, Apple Podcasts, AntennaPod, etc.
- **Tiny built-in UI** at `/` for submitting URLs, browsing episodes,
  managing subscriptions, and watching task status.
- **No external dependencies inside Python** - stdlib only; yt-dlp and
  ffmpeg are system binaries.

## Quick start (Docker)

```bash
docker compose up -d --build
```

Then:

- UI: <http://localhost:5757/>
- RSS: <http://localhost:5757/rss>

## Configuration

All via env vars. Defaults shown.

| Variable                  | Default                          | Purpose                                                                              |
|---------------------------|----------------------------------|--------------------------------------------------------------------------------------|
| `PORT`                    | `8080` (container) / `5757` host | Listen port inside the container.                                                    |
| `PUBLIC_BASE_URL`         | `http://localhost:5757`          | Used in RSS `<enclosure>` URLs. Must be reachable from your podcast player.          |
| `DOWNLOAD_DIR`            | `/app/downloads`                 | Where mp3s and `.info.json` sidecars are written.                                    |
| `STATE_DIR`               | `/app/state`                     | Where `subscriptions.json` and yt-dlp's `archive.txt` live. Persist this on a volume.|
| `POLL_INTERVAL_SECONDS`   | `3600`                           | How often each subscription is re-polled.                                            |
| `SCHEDULER_TICK_SECONDS`  | `60`                             | How often the scheduler checks for due subscriptions.                                |
| `FEED_TITLE`              | `YouTube Podcast`                | `<channel><title>` in the RSS.                                                       |
| `FEED_DESC`               | `Personal YouTube-to-podcast...` | `<channel><description>`.                                                            |
| `FEED_AUTHOR`             | `Max Renke`                      | `<itunes:author>`.                                                                   |
| `TZ`                      | unset                            | Timezone for the container's logs.                                                   |

**`PUBLIC_BASE_URL` matters.** If your phone is on a different network than
the server, `localhost`/LAN URLs in the RSS feed won't resolve. Set this to
the public HTTPS URL you expose (Cloudflare Tunnel, Tailscale Funnel, ngrok,
nginx + Let's Encrypt - whatever).

## HTTP API

### `GET /` - UI

HTML page with forms for submitting URLs and managing subscriptions.

### `GET /ping` - liveness

```json
{"message": "pong"}
```

### `GET /health` - status snapshot

```json
{
  "status": "ok",
  "queue_length": 0,
  "tasks_total": 12,
  "uptime_seconds": 3421,
  "downloads": 7,
  "public_base_url": "https://example.com"
}
```

### `GET /rss` - podcast feed

`application/rss+xml`. One `<item>` per mp3 in `DOWNLOAD_DIR`. The list is
derived from the filesystem on every request, so deleting an mp3 manually
just makes it disappear from the feed.

### `GET /episodes` - JSON list

Same data the RSS is built from, as JSON.

### `POST /download` - one-shot video download

```bash
curl -X POST http://localhost:5757/download \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}'
```

Response:

```json
{"task_id": "..."}
```

Uses `yt-dlp --no-playlist`, so if you paste a video URL that happens to
have a `&list=` parameter, only that single video is pulled.

### `POST /subscriptions` - subscribe to a playlist or channel

```bash
curl -X POST http://localhost:5757/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/playlist?list=PLAYLIST_ID"}'
```

Supports any URL that yt-dlp accepts as a multi-video source:

- Playlist URL: `https://www.youtube.com/playlist?list=PLAYLIST_ID`
- Channel handle: `https://www.youtube.com/@CHANNEL_HANDLE`
- Channel URL: `https://www.youtube.com/channel/CHANNEL_ID`
- "Uploads" tab: `https://www.youtube.com/@CHANNEL_HANDLE/videos`

Optional body field `"interval_seconds": <int>` overrides the global poll
interval for this one subscription.

Response (HTTP 201):

```json
{
  "id": "...",
  "url": "...",
  "interval_seconds": 3600,
  "added": "2026-01-01T00:00:00Z",
  "last_poll": null,
  "last_result": null,
  "next_poll": 0.0
}
```

`next_poll: 0.0` means "poll immediately". The scheduler's next tick (default
within 60 seconds) will enqueue the first poll, which downloads everything
currently in the playlist/channel that isn't already in
`STATE_DIR/archive.txt`.

### `GET /subscriptions` - list

Returns an array of subscription objects (same shape as the POST response,
with `last_poll` / `last_result` populated after the first poll).

### `GET /subscriptions/<id>` - single subscription

### `DELETE /subscriptions/<id>` - unsubscribe

Removes the subscription. Does **not** delete already-downloaded episodes
and does **not** clear the dedup archive (so re-subscribing won't re-download
the same videos - delete `STATE_DIR/archive.txt` if you want a clean slate).

### `GET /tasks` and `GET /tasks/<id>` - task history

Every download (single-video or subscription poll) creates a task. Tasks are
kept in memory only - they're for live status, not long-term audit.

### `GET /audio/<filename>` - download an mp3

Streams the file with `Accept-Ranges: bytes` so podcast apps can do partial
GETs.

## How the polling works

```
add_subscription(url)
   -> sub.next_poll = 0      # poll immediately
   -> persisted to STATE_DIR/subscriptions.json

scheduler thread (every SCHEDULER_TICK_SECONDS)
   for each sub where sub.next_poll <= now():
       sub.next_poll = now + sub.interval_seconds   # bump first
       enqueue poll task

worker thread
   for each poll task:
       yt-dlp -x --download-archive STATE_DIR/archive.txt --yes-playlist <sub.url>
       record results on the sub (last_poll, last_result.new = count)
```

`--download-archive` writes one line per downloaded `<extractor> <id>` pair.
yt-dlp consults that file before each item and skips known IDs. The archive
is shared across all subscriptions, so overlapping playlists don't cause
duplicate downloads.

## Persistent files

| Path                          | What it is                                                        |
|-------------------------------|-------------------------------------------------------------------|
| `DOWNLOAD_DIR/*.mp3`          | The audio files served at `/audio/<filename>`.                    |
| `DOWNLOAD_DIR/*.info.json`    | yt-dlp metadata sidecar; powers RSS titles, durations, thumbnails.|
| `STATE_DIR/subscriptions.json`| All registered subscriptions and their schedules.                 |
| `STATE_DIR/archive.txt`       | yt-dlp dedup log (`youtube VIDEO_ID` per line).                   |

In the provided `docker-compose.yml` both directories are bind-mounted to
`/DATA/AppData/youtube-podcast-server/` on the host.

## Local development (no Docker)

Prereqs: Python 3.11+, `yt-dlp` on PATH, `ffmpeg` on PATH.

```bash
python rss_downloader.py
# Listens on 0.0.0.0:8080 by default
```

Override anything via env vars:

```bash
PORT=9000 POLL_INTERVAL_SECONDS=900 \
    PUBLIC_BASE_URL=https://example.com python rss_downloader.py
```

## Subscribing in a podcast app

1. Make sure `PUBLIC_BASE_URL` is set to the URL your phone can actually reach
   over the internet. Re-check the RSS:
   ```bash
   curl https://your.public.url/rss | grep enclosure
   ```
   The `<enclosure url="...">` values should be your public URL, not
   `localhost` or a LAN IP.
2. In the app's "Add by URL" / "Add custom URL" flow, paste
   `https://your.public.url/rss`.
3. PocketCasts/Overcast/etc. will poll the feed every ~hour. New episodes
   appear shortly after each poll cycle. To pull instantly, force a refresh
   in the app.

## Troubleshooting

- **`/rss` works but no audio plays in the podcast app** - the enclosure URL
  isn't reachable from the phone. Confirm `PUBLIC_BASE_URL` and that the
  domain serves `/audio/<filename>.mp3` publicly.
- **Subscription added but nothing downloads** - check `GET /subscriptions`.
  `next_poll` should be a number; if it stays at `0.0` for more than a
  minute, the scheduler isn't running - look at container logs for
  `[scheduler]` errors.
- **Same videos keep getting redownloaded** - `STATE_DIR` isn't persisting.
  Verify the volume mount and that `STATE_DIR/archive.txt` is growing.
- **yt-dlp errors on specific videos (e.g. members-only, region-locked)** -
  pass cookies through. Mount a cookies file into the container and add
  `--cookies /path/in/container` to the yt-dlp invocations in `tasks.py`.
- **Disk filling up** - nothing deletes episodes automatically. Either
  delete files from `DOWNLOAD_DIR` (they vanish from `/rss` on next request)
  or add your own retention cronjob.

## Security

There is no authentication. Anyone who can reach the server can queue
downloads. If you're exposing this publicly:

- Put it behind Cloudflare Access (Zero Trust), an OAuth proxy
  (oauth2-proxy), or basic auth via your reverse proxy.
- Or expose only `/rss` and `/audio/*` publicly and keep `/download` and
  `/subscriptions` LAN-only.
