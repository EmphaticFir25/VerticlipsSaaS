"""Live service status: real checks of this server, recent provider results, operator incidents and 30-day history.

This page is served by the app itself, so if the whole server is down the page is down too. Pair it with an
external uptime monitor (see docs/LAUNCH_CHECKLIST.md) that watches /healthz from outside.
"""
from __future__ import annotations

import shutil
import threading
import time
from collections import deque

from . import config, db, mailer, worker

LEVELS = {"operational": 0, "maintenance": 1, "degraded": 2, "outage": 3}
NAMES = {v: k for k, v in LEVELS.items()}
SAMPLE_EVERY = 300
_recent: dict[str, deque] = {}
_lock = threading.Lock()


def record(provider: str, ok: bool) -> None:
    """Called after each call to a provider (transcription, scoring, email) with whether it worked."""
    with _lock:
        _recent.setdefault(provider, deque(maxlen=40)).append((time.time(), bool(ok)))


def _provider_state(provider: str, now: float, window: float = 1800) -> tuple[str, str]:
    with _lock:
        calls = [ok for ts, ok in _recent.get(provider, ()) if now - ts < window]
    if len(calls) < 3:
        return "operational", "No problems in the last 30 minutes"
    failed = sum(1 for ok in calls if not ok)
    if failed == len(calls):
        return "outage", "Recent requests are failing"
    if failed / len(calls) >= 0.4:
        return "degraded", "Some recent requests are failing"
    return "operational", "Working normally"


def _components(now: float) -> list[dict]:
    out = []

    def add(name: str, state: str, detail: str) -> None:
        out.append({"name": name, "state": state, "detail": detail})

    add("Website and API", "operational", "Responding")

    try:
        t = time.perf_counter()
        with db.conn() as c:
            c.execute("SELECT 1").fetchone()
        ms = (time.perf_counter() - t) * 1000
        add("Database", "degraded" if ms > 1500 else "operational", "Slow responses" if ms > 1500 else "Working normally")
    except Exception:
        add("Database", "outage", "Not responding")

    workers = sum(1 for t in threading.enumerate() if t.name.startswith("clipper-"))
    try:
        waiting, age = worker._projects.qsize(), db.oldest_queued_age(now)
    except Exception:
        waiting, age = 0, 0.0
    if workers == 0:
        add("Clip processing", "outage", "Workers are not running")
    elif age > 1800:
        add("Clip processing", "degraded", f"Busy: the longest wait is about {int(age // 60)} minutes")
    else:
        add("Clip processing", "operational", f"{waiting} waiting" if waiting else "Idle, ready for new videos")

    tools = all(shutil.which(x) for x in ("ffmpeg", "ffprobe"))
    try:
        import yt_dlp  # noqa: F401
        have_dl = True
    except ImportError:
        have_dl = False
    add("Rendering and downloads", "operational" if tools and have_dl else "outage",
        "Working normally" if tools and have_dl else "A required tool is missing on the server")

    if not config.TRANSCRIBE_API_KEY:
        add("Transcription", "outage", "Not configured")
    else:
        add("Transcription", *_provider_state("transcription", now))
    if not config.ANTHROPIC_API_KEY:
        add("Clip scoring", "outage", "Not configured")
    else:
        add("Clip scoring", *_provider_state("scoring", now))

    if mailer.configured():
        add("Email", *_provider_state("email", now))
    else:
        add("Email", "degraded", "Not configured: confirmation and reset emails are not being delivered")

    try:
        usage = shutil.disk_usage(config.DATA_DIR if config.DATA_DIR.exists() else config.DATA_DIR.parent)
        free = usage.free / usage.total
        add("Storage", "outage" if free < 0.03 else "degraded" if free < 0.10 else "operational",
            "Almost full" if free < 0.10 else "Plenty of space")
    except OSError:
        add("Storage", "degraded", "Could not check")
    return out


def snapshot(now: float | None = None) -> dict:
    now = now or time.time()
    components = _components(now)
    incidents = db.list_incidents(now - 14 * 86400)
    worst = max([LEVELS[c["state"]] for c in components] + [LEVELS.get(i["level"], 2) for i in incidents if i["resolved"] is None] + [0])
    days = _history(now)
    return {"overall": NAMES[worst], "components": components, "incidents": incidents, "history": days, "checked_at": now,
            "uptime_30d": _uptime(days)}


def _history(now: float, days: int = 30) -> list[dict]:
    start = now - days * 86400
    by_day: dict[int, int] = {}
    for ts, level in db.status_samples(start):
        d = int(ts // 86400)
        by_day[d] = max(by_day.get(d, 0), level)
    today = int(now // 86400)
    return [{"day": d * 86400, "level": NAMES[by_day[d]] if d in by_day else "nodata"} for d in range(today - days + 1, today + 1)]


def _uptime(history: list[dict]) -> float | None:
    known = [h for h in history if h["level"] != "nodata"]
    return round(100 * sum(1 for h in known if h["level"] in ("operational", "maintenance")) / len(known), 2) if known else None


def sample_once(now: float | None = None) -> int:
    now = now or time.time()
    snap = snapshot(now)
    level = LEVELS[snap["overall"]]
    db.add_status_sample(now, level)
    return level


def _loop() -> None:
    while True:
        try:
            sample_once()
        except Exception as exc:
            print(f"[status] sample failed: {type(exc).__name__}: {exc}")
        time.sleep(SAMPLE_EVERY)


def start() -> None:
    threading.Thread(target=_loop, name="status-sampler", daemon=True).start()
