"""Push notifications through an ntfy server (https://ntfy.sh), when NTFY_URL is set.

NTFY_URL is the full topic address, e.g. http://172.17.0.1:8095/my-topic. Each
message is sent on its own short-lived thread, so a slow or missing ntfy server
never holds up a request or a download. Titles stay ASCII (they travel as HTTP
headers); anything from YouTube goes in the message body.
"""

import os
import threading
import urllib.request

NTFY_URL = os.environ.get("NTFY_URL", "")


def send(title: str, message: str, priority: str = "default", tags: str = "") -> None:
    if NTFY_URL:
        threading.Thread(target=_post, args=(NTFY_URL, title, message, priority, tags), daemon=True).start()


def _post(url: str, title: str, message: str, priority: str, tags: str) -> None:
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    request = urllib.request.Request(url, data=message.encode("utf-8"), headers=headers, method="POST")
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except OSError as e:
        print(f"[notify] could not reach ntfy: {e}", flush=True)
