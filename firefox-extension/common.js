// Shared by the background script, the popup and the options page.

const DEFAULTS = { server: "https://podcast.maxrenke.com", token: "" };

async function getSettings() {
  const stored = await browser.storage.local.get(DEFAULTS);
  stored.server = (stored.server || DEFAULTS.server).replace(/\/+$/, "");
  return stored;
}

function isHttpUrl(url) {
  return /^https?:\/\/\S+$/i.test(url || "");
}

// Calls the server's JSON API. Only the feed itself is public, so every request carries the API token.
async function api(method, path, body) {
  const { server, token } = await getSettings();
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  let response;
  try {
    response = await fetch(server + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new Error("Cannot reach " + server + " (" + e.message + ")");
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    throw new Error("The server rejected the API token. Set it in the extension's options.");
  }
  if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
  return data;
}
