"""The admin account, sign-in sessions, two-step codes and sign-in throttling.

Standard library only. One account, kept in ``STATE_DIR/auth.json``:

- the password is stored as an scrypt hash with its own salt;
- a session is a random 256-bit id in a cookie, known to the server only by its
  SHA-256, with an idle and an absolute lifetime and its own CSRF token;
- two-step codes are standard TOTP (RFC 6238, SHA-1, 6 digits, 30 s), and a
  code cannot be used twice;
- failed attempts are counted per client address and lock that address out.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from urllib.parse import quote

STATE_DIR = os.environ.get("STATE_DIR", "state")
AUTH_FILE = os.path.join(STATE_DIR, "auth.json")

MIN_PASSWORD_LENGTH = 12
SESSION_IDLE_SECONDS = 12 * 3600
SESSION_MAX_SECONDS = 7 * 86400
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


def _save_account(account: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(account, f, indent=2)
    os.replace(tmp, AUTH_FILE)


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
    """Create or replace the account. Signs out every session and turns two-step off."""
    _save_account({"username": username.strip(), "password": hash_password(password), "totp": None})
    with _LOCK:
        _SESSIONS.clear()


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


def create_session(username: str) -> str:
    session_id = secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK:
        _SESSIONS[_key(session_id)] = {
            "username": username, "created": now, "seen": now, "csrf": secrets.token_urlsafe(32),
        }
    return session_id


def get_session(session_id: str) -> dict | None:
    """The live session for this cookie value, refreshed; None if unknown or expired."""
    if not session_id:
        return None
    now = time.time()
    with _LOCK:
        session = _SESSIONS.get(_key(session_id))
        if not session:
            return None
        if now - session["seen"] > SESSION_IDLE_SECONDS or now - session["created"] > SESSION_MAX_SECONDS:
            del _SESSIONS[_key(session_id)]
            return None
        session["seen"] = now
        return session


def destroy_session(session_id: str) -> None:
    with _LOCK:
        _SESSIONS.pop(_key(session_id), None)


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


def record_failure(client: str) -> None:
    with _LOCK:
        _FAILURES.setdefault(client, []).append(time.time())
        # keep the table from growing without bound under a spray of addresses
        if len(_FAILURES) > 10_000:
            for stale in sorted(_FAILURES, key=lambda c: _FAILURES[c][-1])[:5_000]:
                del _FAILURES[stale]


def clear_failures(client: str) -> None:
    with _LOCK:
        _FAILURES.pop(client, None)
