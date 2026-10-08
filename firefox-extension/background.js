// Queues downloads and subscriptions on the server, then watches each download
// and reports the result with a notification. The toolbar badge shows how many
// downloads are still running.

const POLL_MS = 5000;
const GIVE_UP_MS = 90 * 60 * 1000;
const watching = new Map(); // task id -> url

function notify(title, message) {
  browser.notifications.create({
    type: "basic",
    iconUrl: browser.runtime.getURL("icons/icon-96.png"),
    title,
    message,
  });
}

function updateBadge() {
  browser.browserAction.setBadgeBackgroundColor({ color: "#e52d2d" });
  browser.browserAction.setBadgeText({ text: watching.size ? String(watching.size) : "" });
}

async function watchTask(taskId, url) {
  watching.set(taskId, url);
  updateBadge();
  const started = Date.now();
  try {
    while (Date.now() - started < GIVE_UP_MS) {
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
      const task = await api("GET", "/tasks/" + encodeURIComponent(taskId));
      if (task.status === "done") {
        const episodes = await api("GET", "/episodes");
        const episode = episodes.find((e) => e.filename === task.filename);
        notify("Added to podcast", episode ? episode.title + " - " + episode.uploader : task.filename);
        return;
      }
      if (task.status === "error") {
        notify("Podcast download failed", (task.error || "unknown error").slice(0, 300));
        return;
      }
    }
    notify("Podcast download", "Still not finished after 90 minutes: " + url);
  } catch (e) {
    notify("Podcast download", e.message);
  } finally {
    watching.delete(taskId);
    updateBadge();
  }
}

async function addVideo(url) {
  if (!isHttpUrl(url)) throw new Error("This page has no http(s) address to send.");
  const { task_id: taskId } = await api("POST", "/download", { url });
  watchTask(taskId, url);
  return "Queued. A notification follows when it is in the feed.";
}

async function subscribe(url) {
  if (!isHttpUrl(url)) throw new Error("This page has no http(s) address to send.");
  const sub = await api("POST", "/subscriptions", { url });
  return sub.max_items
    ? "Subscribed: the newest " + sub.max_items + " uploads are checked every poll."
    : "Subscribed: the whole list is pulled.";
}

// Runs an action started from a menu, where a notification is the only place to answer.
async function fromMenu(action, url) {
  try {
    const message = await action(url);
    if (action === subscribe) notify("Podcast subscription", message);
  } catch (e) {
    notify("YouTube Podcast", e.message);
  }
}

browser.menus.create({ id: "add-link", contexts: ["link"], title: "Add linked video to podcast" });
browser.menus.create({ id: "add-page", contexts: ["page", "video"], title: "Add this video to podcast" });
browser.menus.create({ id: "sub-link", contexts: ["link"], title: "Subscribe podcast to linked playlist or channel" });
browser.menus.create({ id: "sub-page", contexts: ["page"], title: "Subscribe podcast to this playlist or channel" });

browser.menus.onClicked.addListener((info, tab) => {
  const url = info.menuItemId.endsWith("-link") ? info.linkUrl : info.pageUrl || (tab && tab.url);
  fromMenu(info.menuItemId.startsWith("add-") ? addVideo : subscribe, url);
});

// The popup asks the background script to do the work so that watching a
// download carries on after the popup closes.
browser.runtime.onMessage.addListener((message) => {
  if (message.type === "add") return addVideo(message.url);
  if (message.type === "subscribe") return subscribe(message.url);
  return undefined;
});
