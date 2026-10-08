# YouTube Podcast - Firefox extension

Sends the video, playlist or channel you are looking at to your
youtube-podcast-server.

## What it does

- **Toolbar button** - opens a small panel for the current tab: *Add this
  video*, *Subscribe to this playlist or channel*, the server's most recent
  tasks, and links to the server page and the options.
- **Right-click menu** - on a link: *Add linked video to podcast* /
  *Subscribe podcast to linked playlist or channel*. On a page: the same for
  the page itself. Handy on YouTube's home page and search results, where you
  can queue a video without opening it.
- **Notification** when a queued video is in the feed (or failed), and a badge
  on the button counting downloads still running.

It talks to the same HTTP API as everything else (`POST /download`,
`POST /subscriptions`, `GET /tasks`, `GET /episodes`) and sends the API token
as `Authorization: Bearer`.

## Set-up

1. Open the extension's options (panel -> *Options*, or `about:addons`).
2. *Server address* defaults to `https://podcast.maxrenke.com`. If you change
   it, Firefox asks for permission to reach that host.
3. *API token*: the server's `API_TOKEN`.
4. *Test connection* should answer `Connected (N episodes). Token accepted.`

The token is kept in this Firefox profile's extension storage and is only sent
to the server address you configured.

## Installing

Release Firefox only keeps extensions that Mozilla has signed.

**Try it now (until Firefox restarts):** open `about:debugging#/runtime/this-firefox`,
click *Load Temporary Add-on...* and pick `manifest.json` in this folder.

**Keep it (signed, private):** sign it as an unlisted add-on. It is not
published or reviewed for listing; you get a signed `.xpi` back.

1. Create API credentials at <https://addons.mozilla.org/developers/addon/api/key/>
   (needs a Mozilla account).
2. From this folder:

   ```bash
   npx web-ext sign --channel=unlisted --api-key=<JWT issuer> --api-secret=<JWT secret>
   ```

3. Open the `.xpi` that lands in `web-ext-artifacts/` with Firefox.

Without the command line: zip the *contents* of this folder and upload the zip
at <https://addons.mozilla.org/developers/addon/submit/distribution>, choosing
*On your own*.

## Files

| File                          | Purpose                                                         |
|-------------------------------|-----------------------------------------------------------------|
| `manifest.json`               | Manifest V2; add-on id `youtube-podcast@maxrenke.com`           |
| `common.js`                   | Settings and the API call shared by all pages                   |
| `background.js`               | Menus, queueing, watching downloads, notifications, badge       |
| `popup.html`, `popup.js`      | Toolbar panel                                                   |
| `options.html`, `options.js`  | Server address and token                                        |
| `icons/`                      | Toolbar and add-on icons                                        |

The manifest declares `browsingActivity` under data collection because the
address of a page you choose to add is sent to your server. Nothing is sent
until you click a button or menu item.
