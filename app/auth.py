"""Passwords, sessions, login throttling and encryption of stored OAuth tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from typing import Optional

from . import config, db

SESSION_COOKIE = "vc_session"
SESSION_TTL = 30 * 86400
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
MIN_PASSWORD = 8

# scrypt parameters: ~16 MB, fast enough for login, slow for offline guessing.
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(test.hex(), digest)


# A real hash to compare against when the email is unknown, so timing does not reveal accounts.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email)) and len(email) <= 254


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    db.create_session(_token_hash(token), user_id, SESSION_TTL)
    return token


def user_for_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    user_id = db.session_user(_token_hash(token))
    return db.get_user(user_id) if user_id else None


def end_session(token: Optional[str]) -> None:
    if token:
        db.delete_session(_token_hash(token))


def token_hash(token: str) -> str:
    return _token_hash(token)


# ---------- login throttling ----------

class Throttle:
    """At most `limit` failures per key in `window` seconds. In memory, per process."""

    def __init__(self, limit: int = 8, window: float = 900):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._fails.get(key, []) if now - t < self.window]
            self._fails[key] = hits
            return len(hits) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


login_throttle = Throttle()
signup_throttle = Throttle(limit=10, window=3600)


# ---------- token encryption ----------

def _fernet():
    from cryptography.fernet import Fernet
    if not config.SECRET_KEY:
        raise RuntimeError("Set SECRET_KEY before connecting social accounts.")
    key = base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest())
    return Fernet(key)


def seal(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def unseal(blob: str) -> dict:
    return json.loads(_fernet().decrypt(blob.encode()).decode())
