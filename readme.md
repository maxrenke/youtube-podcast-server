# YouTube Podcast Server

Turns YouTube videos, playlists, and channels into a self-hosted podcast RSS
feed. Single-file downloads run on demand; playlist/channel subscriptions are
re-polled on a schedule so new uploads get pulled automatically.

```
[ you ] --POST /download--+              +--> downloads/ (mp3 + .jpg + .info.json)
                          |              |
[ you ] --POST /subscrip-+--> task queue --+--> /rss <-- podcast app subscribes
[ scheduler tick ] ------+              |
                                        +--> /audio/<file>.mp3 served to player
```

## Features

- **Single-video downloads** - POST a video URL, get an mp3 with embedded
  cover art, ID3 tags and chapters.
- **Episodes carry the video's metadata** - title, channel, upload date,
  full description, chapter list, a square version of the thumbnail and a link
  back to the video, both inside the mp3 and in the RSS item. See
  [Episode metadata](#episode-metadata).
- **Feed artwork and description** - `artwork.jpg` is served as the podcast
  cover; title, description, author and category come from env vars.
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
- **Tiny built-in UI** at `/` for submitting URLs, browsing and deleting
  episodes, managing subscriptions, and watching task status.
- **Private admin page and API** - only the feeds, audio, covers and chapters
  are reachable without signing in. People use a password (scrypt-hashed)
  with optional two-step codes; scripts use an API token.
- **A feed per channel** - `/rss/<channel-slug>` next to the all-in-one `/rss`.
- **Firefox extension** - add the video or subscribe to the channel you are
  looking at from the toolbar or the right-click menu. See
  [firefox-extension/README.md](firefox-extension/README.md).
- **Survives restarts** - queued and running downloads are saved and re-queued;
  a failed single download is retried once.
- **Optional retention** - `KEEP_DAYS` / `KEEP_COUNT`.
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
| `FEED_DESC`               | `A personal listen-later queue..`| `<channel><description>` and `<itunes:summary>`.                                     |
| `FEED_AUTHOR`             | `Max Renke`                      | `<itunes:author>` and `<itunes:owner>` name.                                         |
| `FEED_EMAIL`              | unset                            | `<itunes:owner>` email. Left out of the feed when unset.                             |
| `FEED_CATEGORY`           | `Technology`                     | `<itunes:category>`. Must be one of Apple's category names.                          |
| `FEED_PRIVATE`            | `1`                              | Emits `<itunes:block>Yes</itunes:block>` so directories do not list the feed. `0` to drop it. |
| `API_TOKEN`               | unset                            | Secret for POST and DELETE requests. Unset = no authentication (a warning is logged). |
| `AUDIO_QUALITY`           | `5`                              | LAME VBR quality, 0 (largest) to 9. 5 is about 130 kbps.                             |
| `SUB_MAX_ITEMS`           | `10`                             | Entries from the top of a channel looked at per poll. 0 = all. Playlists default to all. |
| `KEEP_DAYS`               | `0`                              | Delete episodes added more than this many days ago. 0 = off.                         |
| `KEEP_COUNT`              | `0`                              | Keep only this many newest episodes. 0 = off.                                        |
| `RETRY_DELAY_SECONDS`     | `60`                             | Wait before the one retry of a failed single download.                               |
| `ALLOWED_HOSTS`           | `youtube.com,youtu.be,youtube-nocookie.com` | Sites a download or subscription may point at (host or any subdomain). `*` = any. |
| `TZ`                      | unset                            | Timezone for the container's logs.                                                   |

**`PUBLIC_BASE_URL` matters.** If your phone is on a different network than
the server, `localhost`/LAN URLs in the RSS feed won't resolve. Set this to
the public HTTPS URL you expose (Cloudflare Tunnel, Tailscale Funnel, ngrok,
nginx + Let's Encrypt - whatever).

## HTTP API

**Authentication.** Everything needs a sign-in except what a podcast app
fetches without one:

| Public                                                        | Needs a sign-in                                      |
|---------------------------------------------------------------|------------------------------------------------------|
| `/rss`, `/feed`, `/rss/<slug>`                                | `/` (the admin page)                                 |
| `/audio/*`, `/thumb/*`, `/artwork.jpg`                        | `/episodes`, `/tasks`, `/subscriptions`, `/feeds`, `/health`, `/account` |
| `/chapters/<id>.json`, `/ping`, `/login`, `/setup`            | every `POST` and `DELETE`                            |

There are two ways in:

- **Scripts** (the PowerShell client, the Firefox extension, curl) send
  `Authorization: Bearer <API_TOKEN>`. The curl examples below omit the header
  for brevity.
- **People** sign in at `/login` with a user name and password, plus a
  two-step code if that is turned on. See [Signing in](#signing-in).

Anyone who has the feed address can read the feed and download the audio -
that is what lets a podcast app work - so treat the address itself as
private.

Request bodies are limited to 64 KB (`413` beyond that).

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

`application/rss+xml`. One `<item>` per mp3 in the top level of
`DOWNLOAD_DIR`, newest addition first. The list is derived from the
filesystem on every request, so removing an mp3 makes it disappear from the
feed (see [Removing episodes](#removing-episodes)).

### `GET /feed` - the same feed at a second address

Identical to `/rss` apart from its self link. Podcast apps identify a podcast
by its URL, and Pocket Casts reads the cover only when it first imports a
feed: if an app is stuck with stale artwork, follow the other address and
unfollow the old one.

### `GET /rss/<channel-slug>` - one channel's feed

Only the episodes from that channel, titled with the channel's name and
wearing its newest episode's cover. The slug is the channel name in lower
case with non-alphanumerics turned into `-` (`Professor Dave Explains` ->
`professor-dave-explains`). `404` if no episode matches.

### `GET /feeds` - available channel feeds

```json
[{"slug": "professor-dave-explains", "title": "Professor Dave Explains",
  "url": "https://example.com/rss/professor-dave-explains", "episodes": 5}]
```

### `GET /chapters/<video_id>.json` - chapters

Podcasting 2.0 chapters document, referenced from the item's
`<podcast:chapters>` tag. `404` for episodes without chapters.

### `DELETE /episodes/<video_id>` - delete an episode

Removes the mp3, cover and info.json. `404` if there is no such episode.

### `GET /episodes` - JSON list

Same data the RSS is built from, as JSON: `filename`, `title`, `description`,
`duration`, `size`, `added`, `video_id`, `thumbnail`, `uploader`,
`channel_url`, `webpage_url`, `upload_date`, `chapters`.

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

The URL must start with `http://` or `https://`; anything else gets
`400 {"error": "url must start with http:// or https://"}`. The same rule
applies to `POST /subscriptions`.

### `POST /subscriptions` - subscribe to a playlist or channel

```bash
curl -X POST http://localhost:5757/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/playlist?list=PLAYLIST_ID"}'
```

A channel address in any form is stored as that channel's **Videos tab**, so a
subscription follows uploads only - no Shorts, no live streams. The response
shows the address that was stored. Subscribing to something already
subscribed returns the existing entry instead of adding a second one. Members-only
videos are skipped.

Accepted:

- Playlist URL: `https://www.youtube.com/playlist?list=PLAYLIST_ID`
- Channel, any form: `https://www.youtube.com/@CHANNEL_HANDLE`,
  `.../channel/CHANNEL_ID`, `.../c/NAME`, `.../user/NAME`, with or without a
  tab such as `/featured`, `/streams` or `/shorts` - all become `.../videos`

Optional body fields:

- `"interval_seconds": <int>` overrides the global poll interval.
- `"max_items": <int>` is how many entries from the top of the list each poll
  looks at (`--playlist-end`); `0` means all. Default: `SUB_MAX_ITEMS` for
  channels (they list newest first, so this keeps a new subscription from
  pulling the whole back catalogue) and `0` for playlist URLs (`list=`).
  Do not bound a playlist that grows at the end: new entries past the limit
  would never be seen.

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

Every download (single-video or subscription poll) creates a task. Tasks that
are queued or running are saved to `STATE_DIR/tasks.json` and re-queued when
the server starts; finished ones are kept in memory only (last 200). A failed
single download is retried once after `RETRY_DELAY_SECONDS`; a failed poll
runs again at the subscription's next interval. Single downloads and polls
have separate workers, so a long poll does not hold up a single video.

### `GET /audio/<filename>` - download an mp3

Streams the file with `Accept-Ranges: bytes` so podcast apps can do partial
GETs.

### `GET /thumb/<filename>.jpg` - episode cover

The square cover saved next to each mp3. Referenced by the item's
`<itunes:image>`.

### `GET /artwork.jpg` - podcast cover

The feed-level image (`artwork.jpg` in the repo, 1400x1400). Replace the file
and rebuild to change it.

### `HEAD` on any GET route

Returns the same status and headers without the body. Podcast clients and
feed validators probe enclosures this way.

## Episode metadata

Every download runs yt-dlp with one shared option list
(`_ytdlp_common_args` in `tasks.py`) and leaves three files with the same
stem in `DOWNLOAD_DIR`:

| File                    | Contents                                                                 |
|-------------------------|--------------------------------------------------------------------------|
| `Title [id].mp3`        | Audio plus ID3: title, artist (channel), date, description, source URL, album (`FEED_TITLE`), genre `Podcast`, chapters, and the square cover. |
| `Title [id].jpg`        | 1400x1400 cover: the 16:9 thumbnail centred over a blurred fill of itself, so nothing is cropped. |
| `Title [id].info.json`  | yt-dlp's full metadata dump; the RSS item is built from it.              |

What each RSS `<item>` carries:

| Tag                     | Source                                                                   |
|-------------------------|--------------------------------------------------------------------------|
| `<title>`               | Video title.                                                             |
| `<link>`                | The video's URL.                                                         |
| `<description>`         | Plain text: `Channel - uploaded YYYY-MM-DD - 1h 37m`, the video URL, then the full video description. |
| `<content:encoded>`     | The same as HTML show notes: channel link, "Watch the original video", description with clickable links, chapter list. |
| `<pubDate>`             | When the episode was **added** (yt-dlp's `epoch`), not the upload date, so a newly queued old video sorts to the top in the app. The upload date is in the description. |
| `<itunes:image>`        | `/thumb/<stem>.jpg`; falls back to YouTube's thumbnail URL for files downloaded before covers were saved. |
| `<itunes:author>`       | Channel name.                                                            |
| `<itunes:duration>`     | Seconds.                                                                 |
| `<guid>`                | Video id.                                                                |

Chapters are provided twice: inside the mp3 (ID3 `CHAP` frames) and as a
`<podcast:chapters>` JSON document, so both kinds of player show them.

When an episode is published, its info.json is cut down to the fields the
feed uses (a few KB instead of several hundred).

Feed-level tags: `<image>`, `<itunes:image>`, `<itunes:summary>`,
`<itunes:owner>`, `<itunes:type>`, `<itunes:category>`, `<itunes:block>`,
`<atom:link rel="self">`.

Podcast apps cache feeds on their own servers. After a change here, expect a
delay (Pocket Casts: up to an hour or a manual refresh) before the app shows
it, and already-downloaded episodes keep the file they have.

## Removing episodes

Use the Delete button in the UI, `DELETE /episodes/<video_id>`, or set
`KEEP_DAYS` / `KEEP_COUNT` for automatic clean-up. By hand, remove (or move
out of the top level of `DOWNLOAD_DIR`) the three files for an episode:

```bash
cd /DATA/AppData/youtube-podcast-server/downloads
sudo rm "Title [id].mp3" "Title [id].jpg" "Title [id].info.json"
```

Files inside a subfolder are ignored by the feed, so `mkdir _hold && mv ...`
also works. If the video came from a subscription, its id stays in
`STATE_DIR/archive.txt` and it will not be downloaded again.

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
| `DOWNLOAD_DIR/*.jpg`          | Square episode cover served at `/thumb/<filename>`.               |
| `DOWNLOAD_DIR/.incoming/<id>/`| Staging folder per task (`sub-<id>` for a subscription). Finished episodes are moved up into `DOWNLOAD_DIR` (mp3 last), so the feed never lists a half-written file. Leftovers are from interrupted downloads and are reused on retry. |
| `STATE_DIR/tasks.json`        | Queued and running tasks, re-queued on start.                     |
| `DOWNLOAD_DIR/*.info.json`    | yt-dlp metadata sidecar; powers RSS titles, show notes, durations.|
| `STATE_DIR/subscriptions.json`| All registered subscriptions and their schedules.                 |
| `STATE_DIR/archive.txt`       | yt-dlp dedup log (`youtube VIDEO_ID` per line).                   |

In the provided `docker-compose.yml` both directories are bind-mounted to
`/DATA/AppData/youtube-podcast-server/` on the host.

## Local development (no Docker)

Prereqs: Python 3.11+, `yt-dlp` on PATH, `ffmpeg` on PATH. Lint and type
checks and the tests run as pre-commit hooks (`ruff check`, `mypy`,
`python -m pytest -q tests`); all must pass to commit.

```bash
python rss_downloader.py
# Listens on 0.0.0.0:8080 by default
```

Override anything via env vars:

```bash
PORT=9000 POLL_INTERVAL_SECONDS=900 \
    PUBLIC_BASE_URL=https://example.com python rss_downloader.py
```

## Deploying

```powershell
.\deploy.ps1                      # push master, pull + rebuild on the box, print /health
.\deploy.ps1 -Message "fix bug"   # commit everything first
```

The box (`ssh casaos`, repo at `~/youtube-podcast-server`) runs
`git pull --ff-only && docker compose up -d --build`. The container also runs
`yt-dlp -U` every time it starts, so `docker restart youtube-podcast-server`
is the usual fix when YouTube downloads start failing.

The API token lives in `~/youtube-podcast-server/.env` on the box
(`API_TOKEN=...`, git-ignored); compose passes it into the container.

The live instance is published at `https://podcast.maxrenke.com` through a
Cloudflare Tunnel (`casaos`) to `http://172.17.0.1:5757`.

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

## Signing in

**First time, or after forgetting the password:** open `/setup`, enter the API
token (the `API_TOKEN` value in the server's `.env`), a user name and a
password of 12 characters or more. Doing this again replaces the account,
signs out every session and turns two-step sign-in off.

**Two-step sign-in:** on the admin page choose *Turn on*, add the key to an
authenticator app, and confirm with the code it shows. From then on `/login`
needs the code as well. Turning it off asks for the password.

How it is built (`auth.py`, standard library only):

| Part            | Detail                                                                                   |
|-----------------|------------------------------------------------------------------------------------------|
| Password        | scrypt (N=2^15, r=8, p=1), random 16-byte salt, in `STATE_DIR/auth.json` (mode 600). A wrong user name costs the same time as a wrong password. |
| Session         | Random 256-bit id in a cookie: `__Host-` prefix, `Secure`, `HttpOnly`, `SameSite=Strict`. The server keeps only its SHA-256. 12 h idle limit, 7 days at most; a restart signs everyone out. |
| Cross-site requests | A cookie-authenticated write must carry our own `Origin`, the session's `X-CSRF-Token` and a JSON content type. The sign-in and set-up forms refuse a foreign `Origin`. |
| Two-step codes  | TOTP (RFC 6238, SHA-1, 6 digits, 30 s), one step of clock drift allowed, each code accepted once. |
| Throttling      | 5 failures (password, code, API token or set-up token) lock that client address out for 15 minutes (`429` with `Retry-After`). The address comes from `CF-Connecting-IP` when the request arrives through the tunnel. |
| Transport       | Plain http through the tunnel is redirected (reads) or refused (everything else); `Strict-Transport-Security` on every response. |
| Headers         | `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`; pages carry a Content-Security-Policy with a per-response script nonce; signed-in responses are `Cache-Control: no-store`. |
| Logging         | Sign-ins, failures and refused cross-site writes are logged with the client address, never with a secret. |

## Security

- The server only fetches from the sites in `ALLOWED_HOSTS` (YouTube by
  default; `*` for any). Without that limit a request could make the box
  fetch from inside your network. URLs are also passed to yt-dlp after `--`,
  so a request cannot inject yt-dlp options.
- The container's port is published on the docker bridge address only
  (`172.17.0.1:5757`). It runs as uid 1000 with all capabilities dropped and
  `no-new-privileges`. Nothing on the LAN can reach it over plain http. The
  two data folders must belong to uid 1000
  (`sudo chown -R 1000:1000 /DATA/AppData/youtube-podcast-server`).
- The feed and audio are public to anyone who knows the address.

For more than this:

- Put it behind Cloudflare Access (Zero Trust), an OAuth proxy
  (oauth2-proxy), or basic auth via your reverse proxy.
- Or expose only `/rss` and `/audio/*` publicly and keep `/download` and
  `/subscriptions` LAN-only.
