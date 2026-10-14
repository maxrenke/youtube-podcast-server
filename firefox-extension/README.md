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

## Installing and updating

Release Firefox only runs extensions Mozilla has signed. This one is signed as a
self-distributed ("unlisted") add-on - it is not listed on addons.mozilla.org - and
published as a GitHub release of this repo.

**Install:** open the newest `youtube-podcast-<version>.xpi` from the
[releases page](https://github.com/maxrenke/youtube-podcast-server/releases) in
Firefox and click **Add**.

**Updates are automatic.** The manifest's `update_url` points at
`firefox-extension-updates.json` in this repo. Firefox reads it about once a day
and installs any newer signed version on its own; about:addons > gear menu >
**Check for Updates** does it at once. The options page shows the installed
version.

**Releasing a new version** (from the repo root):

```powershell
.\Publish-Extension.ps1
```

It raises the version, lints, has Mozilla sign it, creates the GitHub release
`extension-v<version>` with the signed `.xpi`, and adds it to the update file.
It needs addons.mozilla.org API credentials - create them at
<https://addons.mozilla.org/en-US/developers/addon/api/key/> and save the JWT
issuer and JWT secret as two lines in `~\.amo-api-key` - plus node (`npx`) and a
signed-in `gh`. `-WhatIf` shows what it would do; `-Version 1.2.0` picks the
version.

**Just trying a change** without releasing: `about:debugging#/runtime/this-firefox`
> Load Temporary Add-on > `manifest.json` (gone after a restart).

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
