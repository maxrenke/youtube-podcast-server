const $ = (id) => document.getElementById(id);
let pageUrl = "";

function say(text, isError) {
  $("message").textContent = text;
  $("message").className = isError ? "error" : "";
}

async function send(type) {
  $("add").disabled = $("subscribe").disabled = true;
  say("Sending...");
  try {
    say(await browser.runtime.sendMessage({ type, url: pageUrl }));
    refreshTasks();
  } catch (e) {
    say(e.message, true);
  }
  $("add").disabled = $("subscribe").disabled = false;
}

async function refreshTasks() {
  let tasks;
  try {
    tasks = await api("GET", "/tasks");
  } catch (e) {
    $("tasks").textContent = "";
    say(e.message, true);
    return;
  }
  const list = $("tasks");
  list.textContent = "";
  for (const task of tasks.slice(-6).reverse()) {
    const item = document.createElement("li");
    const status = document.createElement("span");
    status.className = "status " + task.status;
    status.textContent = task.status;
    item.append(status, " ", task.filename || task.url);
    if (task.error) item.append(" - " + task.error.slice(0, 120));
    list.append(item);
  }
  if (!tasks.length) list.textContent = "Nothing since the server started.";
}

async function init() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  pageUrl = (tab && tab.url) || "";
  const title = document.createElement("b");
  title.textContent = (tab && tab.title) || "";
  $("page").append(title, pageUrl);
  if (!isHttpUrl(pageUrl)) {
    $("add").disabled = $("subscribe").disabled = true;
    say("Open a video, playlist or channel page first.");
  }
  const { server, token } = await getSettings();
  $("server").href = server + "/";
  if (!token) say("No API token set yet - open Options.", true);
  $("add").addEventListener("click", () => send("add"));
  $("subscribe").addEventListener("click", () => send("subscribe"));
  $("options").addEventListener("click", (e) => { e.preventDefault(); browser.runtime.openOptionsPage(); });
  $("server").addEventListener("click", (e) => { e.preventDefault(); browser.tabs.create({ url: server + "/" }); });
  refreshTasks();
  setInterval(refreshTasks, 4000);
}

init();
