"""SQLite storage for users, sessions, projects, clips, social accounts and publishes.

Every query that touches user content takes a user_id, so one customer can never read
another customer's projects or clips.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from . import config

_lock = threading.RLock()
_initialized: set[str] = set()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    pw_hash TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    stripe_customer TEXT,
    stripe_subscription TEXT,
    minutes_used REAL NOT NULL DEFAULT 0,
    period_start REAL NOT NULL,
    brand TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS email_tokens_user ON email_tokens(user_id, kind);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'url',
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT '',
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}',
    duration REAL NOT NULL DEFAULT 0,
    minutes_charged REAL NOT NULL DEFAULT 0,
    has_thumb INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_user ON projects(user_id, created);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_project ON clips(project_id);
CREATE TABLE IF NOT EXISTS social_accounts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    tokens TEXT NOT NULL,
    created REAL NOT NULL,
    UNIQUE (user_id, platform, external_id)
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS publishes (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    clip_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL,
    UNIQUE (clip_id, account_id)
);
CREATE TABLE IF NOT EXISTS stripe_events (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL
);
"""

PROJECT_FIELDS = ("title", "source_url", "source_kind", "status", "stage", "progress", "message",
                  "settings", "duration", "minutes_charged", "has_thumb")


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    path = config.DB_PATH
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            if str(path) not in _initialized:
                c.executescript(SCHEMA)
                cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
                if "email_verified" not in cols:   # database from before email verification existed
                    c.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
                    c.execute("UPDATE users SET email_verified = 1")   # existing customers are grandfathered in
                _initialized.add(str(path))
            yield c
            c.commit()
        finally:
            c.close()


def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(r) if r is not None else None


# ---------- users ----------

def create_user(user_id: str, email: str, name: str, pw_hash: str) -> dict:
    now = time.time()
    with conn() as c:
        c.execute("INSERT INTO users (id, email, name, pw_hash, period_start, created) VALUES (?,?,?,?,?,?)",
                  (user_id, email, name, pw_hash, now, now))
    return get_user(user_id)


def get_user(user_id: str) -> Optional[dict]:
    with conn() as c:
        return _user(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_email(email: str) -> Optional[dict]:
    with conn() as c:
        return _user(c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone())


def get_user_by_customer(customer_id: str) -> Optional[dict]:
    with conn() as c:
        return _user(c.execute("SELECT * FROM users WHERE stripe_customer = ?", (customer_id,)).fetchone())


def _user(r) -> Optional[dict]:
    u = _row(r)
    if u:
        u["brand"] = json.loads(u["brand"] or "{}")
    return u


def update_user(user_id: str, **fields) -> None:
    allowed = {"name", "pw_hash", "plan", "stripe_customer", "stripe_subscription",
               "minutes_used", "period_start", "brand", "email_verified"}
    sets = {k: (json.dumps(v) if k == "brand" else v) for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with conn() as c:
        c.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                  (*sets.values(), user_id))


def add_minutes(user_id: str, minutes: float) -> None:
    """Atomic usage change (negative minutes refund)."""
    with conn() as c:
        c.execute("UPDATE users SET minutes_used = MAX(0, minutes_used + ?) WHERE id = ?", (minutes, user_id))


# ---------- sessions ----------

def create_session(token_hash: str, user_id: str, ttl: float) -> None:
    now = time.time()
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        c.execute("INSERT INTO sessions (token_hash, user_id, expires) VALUES (?,?,?)",
                  (token_hash, user_id, now + ttl))


def session_user(token_hash: str) -> Optional[str]:
    with conn() as c:
        r = c.execute("SELECT user_id FROM sessions WHERE token_hash = ? AND expires > ?",
                      (token_hash, time.time())).fetchone()
    return r["user_id"] if r else None


def delete_session(token_hash: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def delete_user_sessions(user_id: str, keep: str = "") -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep))


# ---------- email tokens (verification and password reset) ----------

def create_email_token(token_hash: str, user_id: str, kind: str, ttl: float) -> None:
    now = time.time()
    with conn() as c:
        c.execute("DELETE FROM email_tokens WHERE expires < ? OR (user_id = ? AND kind = ?)", (now, user_id, kind))
        c.execute("INSERT INTO email_tokens (token_hash, user_id, kind, expires) VALUES (?,?,?,?)",
                  (token_hash, user_id, kind, now + ttl))


def consume_email_token(token_hash: str, kind: str) -> Optional[str]:
    """Single use: the row is deleted as it is read."""
    with conn() as c:
        r = c.execute("DELETE FROM email_tokens WHERE token_hash = ? AND kind = ? AND expires > ? RETURNING user_id",
                      (token_hash, kind, time.time())).fetchone()
    return r["user_id"] if r else None


# ---------- projects ----------

def create_project(project_id: str, user_id: str, **fields) -> dict:
    now = time.time()
    data = {k: fields.get(k) for k in PROJECT_FIELDS if k in fields}
    if "settings" in data:
        data["settings"] = json.dumps(data["settings"])
    cols = ["id", "user_id", "created", "updated", *data.keys()]
    with conn() as c:
        c.execute(f"INSERT INTO projects ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                  (project_id, user_id, now, now, *data.values()))
    return get_project(project_id, user_id)


def update_project(project_id: str, **fields) -> None:
    sets = {k: (json.dumps(v) if k == "settings" else v) for k, v in fields.items() if k in PROJECT_FIELDS}
    if not sets:
        return
    with conn() as c:
        c.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in sets)}, updated = ? WHERE id = ?",
                  (*sets.values(), time.time(), project_id))


def _project(r) -> Optional[dict]:
    p = _row(r)
    if p:
        p["settings"] = json.loads(p["settings"] or "{}")
        p["has_thumb"] = bool(p["has_thumb"])
    return p


def get_project(project_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    """With user_id, only that user's project is returned. Without it (workers only), any project."""
    with conn() as c:
        if user_id is None:
            r = c.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        else:
            r = c.execute("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)).fetchone()
    return _project(r)


def list_projects(user_id: str, limit: int = 200) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT p.*, (SELECT COUNT(*) FROM clips WHERE project_id = p.id) AS clip_count, "
                         "(SELECT MAX(score) FROM clips WHERE project_id = p.id) AS top_score "
                         "FROM projects p WHERE user_id = ? ORDER BY created DESC LIMIT ?",
                         (user_id, limit)).fetchall()
    return [_project(r) for r in rows]


def delete_project(project_id: str, user_id: str) -> bool:
    with conn() as c:
        c.execute("DELETE FROM publishes WHERE clip_id IN (SELECT id FROM clips WHERE project_id = ? AND user_id = ?)",
                  (project_id, user_id))
        c.execute("DELETE FROM clips WHERE project_id = ? AND user_id = ?", (project_id, user_id))
        return c.execute("DELETE FROM projects WHERE id = ? AND user_id = ?",
                         (project_id, user_id)).rowcount > 0


def projects_with_status(*statuses: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(f"SELECT * FROM projects WHERE status IN ({', '.join('?' * len(statuses))}) "
                         "ORDER BY created", statuses).fetchall()
    return [_project(r) for r in rows]


def active_project_count(user_id: str) -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM projects WHERE user_id = ? AND status IN ('queued', 'running')",
                         (user_id,)).fetchone()[0]


# ---------- clips ----------

def add_clip(clip: dict) -> None:
    with conn() as c:
        c.execute("INSERT INTO clips (id, project_id, user_id, idx, score, data) VALUES (?,?,?,?,?,?)",
                  (clip["id"], clip["project_id"], clip["user_id"], clip["idx"], float(clip.get("score", 0)),
                   json.dumps(clip)))


def clear_clips(project_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM clips WHERE project_id = ?", (project_id,))


def list_clips(project_id: str, user_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT data FROM clips WHERE project_id = ? AND user_id = ? ORDER BY score DESC",
                         (project_id, user_id)).fetchall()
        pubs = c.execute("SELECT * FROM publishes WHERE user_id = ? AND clip_id IN "
                         "(SELECT id FROM clips WHERE project_id = ?)", (user_id, project_id)).fetchall()
    by_clip: dict[str, list] = {}
    for p in pubs:
        by_clip.setdefault(p["clip_id"], []).append(dict(p))
    out = []
    for r in rows:
        clip = json.loads(r["data"])
        clip["publishes"] = by_clip.get(clip["id"], [])
        out.append(clip)
    return out


def get_clip(clip_id: str, user_id: str) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT data FROM clips WHERE id = ? AND user_id = ?", (clip_id, user_id)).fetchone()
    return json.loads(r["data"]) if r else None


def update_clip(clip_id: str, user_id: str, **fields) -> Optional[dict]:
    with conn() as c:
        r = c.execute("SELECT data FROM clips WHERE id = ? AND user_id = ?", (clip_id, user_id)).fetchone()
        if r is None:
            return None
        clip = json.loads(r["data"])
        clip.update(fields)
        c.execute("UPDATE clips SET data = ? WHERE id = ?", (json.dumps(clip), clip_id))
    return clip


# ---------- social accounts ----------

def upsert_account(account_id: str, user_id: str, platform: str, external_id: str, name: str, tokens: str) -> str:
    with conn() as c:
        r = c.execute("SELECT id FROM social_accounts WHERE user_id = ? AND platform = ? AND external_id = ?",
                      (user_id, platform, external_id)).fetchone()
        if r:
            c.execute("UPDATE social_accounts SET name = ?, tokens = ? WHERE id = ?", (name, tokens, r["id"]))
            return r["id"]
        c.execute("INSERT INTO social_accounts (id, user_id, platform, external_id, name, tokens, created) "
                  "VALUES (?,?,?,?,?,?,?)", (account_id, user_id, platform, external_id, name, tokens, time.time()))
        return account_id


def list_accounts(user_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id, platform, external_id, name, created FROM social_accounts "
                         "WHERE user_id = ? ORDER BY created", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: str, user_id: str) -> Optional[dict]:
    with conn() as c:
        return _row(c.execute("SELECT * FROM social_accounts WHERE id = ? AND user_id = ?",
                              (account_id, user_id)).fetchone())


def set_account_tokens(account_id: str, tokens: str) -> None:
    with conn() as c:
        c.execute("UPDATE social_accounts SET tokens = ? WHERE id = ?", (tokens, account_id))


def delete_account(account_id: str, user_id: str) -> bool:
    with conn() as c:
        return c.execute("DELETE FROM social_accounts WHERE id = ? AND user_id = ?",
                         (account_id, user_id)).rowcount > 0


# ---------- OAuth state ----------

def save_oauth_state(state: str, user_id: str, platform: str) -> None:
    now = time.time()
    with conn() as c:
        c.execute("DELETE FROM oauth_states WHERE created < ?", (now - 1800,))
        c.execute("INSERT INTO oauth_states (state, user_id, platform, created) VALUES (?,?,?,?)",
                  (state, user_id, platform, now))


def pop_oauth_state(state: str, platform: str) -> Optional[str]:
    with conn() as c:
        r = c.execute("SELECT user_id FROM oauth_states WHERE state = ? AND platform = ? AND created > ?",
                      (state, platform, time.time() - 1800)).fetchone()
        c.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    return r["user_id"] if r else None


# ---------- publishes ----------

def set_publish(pub_id: str, user_id: str, clip_id: str, account_id: str, platform: str,
                status: str, detail: str = "") -> None:
    with conn() as c:
        c.execute("INSERT INTO publishes (id, user_id, clip_id, account_id, platform, status, detail, updated) "
                  "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(clip_id, account_id) DO UPDATE SET "
                  "status = excluded.status, detail = excluded.detail, updated = excluded.updated",
                  (pub_id, user_id, clip_id, account_id, platform, status, detail, time.time()))


def publishes_with_status(*statuses: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(f"SELECT * FROM publishes WHERE status IN ({', '.join('?' * len(statuses))})",
                         statuses).fetchall()
    return [dict(r) for r in rows]


# ---------- stripe idempotency ----------

def seen_stripe_event(event_id: str) -> bool:
    """Record a webhook event id. True if it was already processed."""
    with conn() as c:
        try:
            c.execute("INSERT INTO stripe_events (id, created) VALUES (?, ?)", (event_id, time.time()))
            return False
        except sqlite3.IntegrityError:
            return True


def reset_for_tests(path: Path) -> None:
    config.DB_PATH = path
    _initialized.discard(str(path))

