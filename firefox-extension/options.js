const $ = (id) => document.getElementById(id);

function show(text, kind) {
  $("result").textContent = text;
  $("result").className = kind || "";
}

async function save() {
  let url;
  try {
    url = new URL($("server").value.trim());
  } catch (e) {
    show("That is not a valid address.", "error");
    return false;
  }
  const origin = url.origin;
  // Must be asked for straight from the click, before anything is awaited.
  // The built-in server address is already covered by the manifest. A match
  // pattern names the host only: Firefox does not accept a port in it.
  const granted = await browser.permissions.request({ origins: [url.protocol + "//" + url.hostname + "/*"] });
  if (!granted) {
    show("Firefox needs permission for " + origin + " to talk to the server.", "error");
    return false;
  }
  await browser.storage.local.set({ server: origin, token: $("token").value.trim() });
  $("server").value = origin;
  show("Saved.", "ok");
  return true;
}

async function test() {
  if (!(await save())) return;
  show("Testing...");
  try {
    // /ping is public, /health needs the token: the two tell "cannot reach the
    // server" apart from "wrong token".
    await api("GET", "/ping");
    const health = await api("GET", "/health");
    show("Connected (" + health.downloads + " episodes). " +
      (health.auth_required ? "Token accepted." : "This server does not ask for a token."), "ok");
  } catch (e) {
    show(e.message, "error");
  }
}

$("save").addEventListener("click", save);
$("test").addEventListener("click", test);
getSettings().then((s) => { $("server").value = s.server; $("token").value = s.token; });
