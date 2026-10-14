"""The admin account, sign-in sessions, two-step codes and sign-in throttling.

Standard library only. One account, kept in ``STATE_DIR/auth.json``:

- the password is stored as an scrypt hash with its own salt;
- a session is a random 256-bit id in a cookie, known to the server only by its
  SHA-256, with its own CSRF token. A normal session ends when the browser
  closes, after 12 idle hours or after 7 days; a "keep me signed in" session
  lasts 30 days. Sessions are saved to ``STATE_DIR/sessions.json`` so a
  restart does not sign anyone out;
- two-step codes are standard TOTP (RFC 6238, SHA-1, 6 digits, 30 s), and a
  code cannot be used twice;
- failed attempts are counted per client address and lock that address out.

The account is created or replaced from the server's own shell:

    docker exec -it youtube-podcast-server python auth.py set-account

(or at /setup with SETUP_TOKEN, if that is configured). Replacing it signs every
session out, including those of a server process that is already running.
"""

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import struct
import sys
import threading
import time
from urllib.parse import quote

STATE_DIR = os.environ.get("STATE_DIR", "state")
AUTH_FILE = os.path.join(STATE_DIR, "auth.json")
SESSIONS_FILE = os.path.join(STATE_DIR, "sessions.json")

MIN_PASSWORD_LENGTH = 12
SESSION_IDLE_SECONDS = 12 * 3600
SESSION_MAX_SECONDS = 7 * 86400
REMEMBER_SECONDS = 30 * 86400  # "keep me signed in"
SESSION_SAVE_SECONDS = 300  # how stale the saved last-seen time may get
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60

# About 33 MB and ~0.1 s per guess.
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
_LOCK = threading.Lock()
_SESSIONS: dict[str, dict] = {}
_FAILURES: dict[str, list[float]] = {}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: bytes | None = None, **params) -> dict:
    params = params or _SCRYPT
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=2 ** 27, dklen=32, **params)
    return {"salt": _b64(salt), "hash": _b64(digest), **params}


def verify_password(password: str, record: dict) -> bool:
    params = {k: record[k] for k in ("n", "r", "p")}
    candidate = hash_password(password, base64.b64decode(record["salt"]), **params)
    return hmac.compare_digest(candidate["hash"], record["hash"])


# Checked when there is no account or the user name is wrong, so that a wrong
# name costs the same time as a wrong password.
_DUMMY = hash_password(secrets.token_urlsafe(16))


# ---------------------------------------------------------------------------
# Two-step codes (TOTP)
# ---------------------------------------------------------------------------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp_uri(secret: str, username: str, issuer: str = "YouTube Podcast") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}"


def _totp_code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def matching_totp_counter(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step ``code`` is valid for (current or one either side), or None."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    step = int((time.time() if now is None else now) // 30)
    found = None
    for counter in (step - 1, step, step + 1):
        if hmac.compare_digest(_totp_code(secret, counter), code):
            found = counter
    return found


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

def load_account() -> dict | None:
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_private(path: str, data) -> None:
    """Write JSON readable by the server's user only, replacing the file in one step."""
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _save_account(account: dict) -> None:
    _write_private(AUTH_FILE, account)


def password_problem(username: str, password: str) -> str:
    """Why this user name and password cannot be used, or an empty string."""
    if not username.strip():
        return "Choose a user name."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password needs at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in (username.lower(), username.lower() * 2):
        return "The password must not be the user name."
    return ""


def set_account(username: str, password: str) -> None:
    """Create or replace the account. Signs out every session and turns two-step off.

    Sessions record the account ``version`` they were created under, so a session
    from before the change stops working even in another process.
    """
    _save_account({"username": username.strip(), "password": hash_password(password), "totp": None,
                   "version": secrets.token_hex(8)})
    with _LOCK:
        _SESSIONS.clear()
        _save_sessions()


def set_totp(secret: str | None) -> None:
    account = load_account()
    if account:
        account["totp"] = {"secret": secret, "last_counter": 0} if secret else None
        _save_account(account)


def check_login(username: str, password: str, code: str = "") -> bool:
    """True only for the right user name, password and (when enabled) an unused two-step code."""
    account = load_account()
    password_ok = verify_password(password, account["password"] if account else _DUMMY)
    if account is None:
        return False
    name_ok = hmac.compare_digest(username.strip().encode("utf-8"), account["username"].encode("utf-8"))
    if not (name_ok and password_ok):
        return False
    totp = account.get("totp")
    if not totp:
        return True
    counter = matching_totp_counter(totp["secret"], code)
    if counter is None or counter <= totp.get("last_counter", 0):
        return False
    totp["last_counter"] = counter  # a code works once
    _save_account(account)
    return True


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("ascii", "ignore")).hexdigest()


def _expired(session: dict, now: float) -> bool:
    if session.get("remember"):
        return now - session["created"] > REMEMBER_SECONDS
    return now - session["seen"] > SESSION_IDLE_SECONDS or now - session["created"] > SESSION_MAX_SECONDS


_SAVED_FIELDS = ("username", "created", "seen", "csrf", "remember", "account")
_saved_at = 0.0


def _save_sessions() -> None:
    """Write the live sessions to disk. Call with _LOCK held.

    Only the SHA-256 of each session id is stored, so the file cannot be used
    to sign in.
    """
    global _saved_at
    now = time.time()
    live = {key: {f: s[f] for f in _SAVED_FIELDS if f in s} for key, s in _SESSIONS.items() if not _expired(s, now)}
    try:
        _write_private(SESSIONS_FILE, live)
        _saved_at = now
    except OSError as e:
        print(f"[auth] could not save sessions: {e}", flush=True)


def _load_sessions() -> None:
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    now = time.time()
    with _LOCK:
        for key, session in saved.items():
            if all(f in session for f in ("username", "created", "seen", "csrf")) and not _expired(session, now):
                _SESSIONS[key] = session


def _account_version() -> str | None:
    account = load_account()
    return account.get("version") if account else None


def create_session(username: str, remember: bool = False) -> str:
    session_id = secrets.token_urlsafe(32)
    now = time.time()
    version = _account_version()
    with _LOCK:
        _SESSIONS[_key(session_id)] = {
            "username": username, "created": now, "seen": now, "csrf": secrets.token_urlsafe(32),
            "remember": bool(remember), "account": version,
        }
        _save_sessions()
    return session_id


def get_session(session_id: str) -> dict | None:
    """The live session for this cookie value, refreshed; None if unknown or expired."""
    if not session_id:
        return None
    now = time.time()
    version = _account_version()
    with _LOCK:
        session = _SESSIONS.get(_key(session_id))
        if not session:
            return None
        if _expired(session, now) or session.get("account") != version:
            del _SESSIONS[_key(session_id)]
            _save_sessions()
            return None
        session["seen"] = now
        if now - _saved_at > SESSION_SAVE_SECONDS:
            _save_sessions()  # keeps the saved last-seen time fresh enough for the idle limit
        return session


def destroy_session(session_id: str) -> None:
    with _LOCK:
        if _SESSIONS.pop(_key(session_id), None) is not None:
            _save_sessions()


_load_sessions()


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

def locked_for(client: str) -> int:
    """Seconds this client still has to wait, 0 if it may try."""
    now = time.time()
    with _LOCK:
        recent = [t for t in _FAILURES.get(client, []) if now - t < LOCKOUT_SECONDS]
        if recent:
            _FAILURES[client] = recent
        else:
            _FAILURES.pop(client, None)
        if len(recent) < MAX_FAILURES:
            return 0
        return int(LOCKOUT_SECONDS - (now - recent[0])) + 1


def record_failure(client: str) -> bool:
    """Count a failed attempt. True when this one locks the client out."""
    now = time.time()
    with _LOCK:
        recent = [t for t in _FAILURES.get(client, []) if now - t < LOCKOUT_SECONDS] + [now]
        _FAILURES[client] = recent
        # keep the table from growing without bound under a spray of addresses
        if len(_FAILURES) > 10_000:
            for stale in sorted(_FAILURES, key=lambda c: _FAILURES[c][-1])[:5_000]:
                del _FAILURES[stale]
        return len(recent) == MAX_FAILURES


def clear_failures(client: str) -> None:
    with _LOCK:
        _FAILURES.pop(client, None)


# ---------------------------------------------------------------------------
# Command line (run on the server: docker exec -it youtube-podcast-server python auth.py ...)
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="auth.py", description="Manage the admin account.")
    commands = parser.add_subparsers(dest="command", required=True)
    setter = commands.add_parser("set-account", help="create or replace the admin account "
                                 "(signs every session out and turns two-step sign-in off)")
    setter.add_argument("--username", help="asked for when left out")
    setter.add_argument("--password-stdin", action="store_true",
                        help="read the password from standard input instead of asking twice")
    args = parser.parse_args(argv)

    username = args.username or input("User name: ")
    if args.password_stdin:
        password = confirm = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password (12 characters or more): ")
        confirm = getpass.getpass("New password again: ")
    problem = password_problem(username, password)
    if not problem and password != confirm:
        problem = "The two passwords are not the same."
    if problem:
        print(problem, file=sys.stderr)
        return 1
    set_account(username, password)
    import notify  # only here: the server imports auth too, and sends its own notices
    notify.send("Podcast: admin account replaced", f"Account '{username.strip()}' was set from the server's shell.",
                "high", "warning")
    time.sleep(1)  # let the notice go out before the process ends
    print(f"Account '{username.strip()}' saved. Every session is signed out and two-step sign-in is off.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
