"""Retention: removes customer data once it is no longer needed. Runs at start-up and every few hours."""
from __future__ import annotations

import shutil
import threading
import time

from . import config, db

DAY = 86400
INTERVAL = 6 * 3600


def _project_dir(pid: str):
    return config.MEDIA_DIR / pid


def run_once(now: float | None = None) -> dict:
    now = now or time.time()
    done = {"sources": 0, "projects": 0, "accounts": 0}
    db.purge_expired()
    if config.SOURCE_RETENTION_DAYS > 0:
        for p in db.finished_projects_older_than(now - config.SOURCE_RETENTION_DAYS * DAY):
            d = _project_dir(p["id"])
            for leftover in (d / "source.mp4", d / "audio"):
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                elif leftover.exists():
                    leftover.unlink(missing_ok=True)
                    done["sources"] += 1
    if config.PROJECT_RETENTION_DAYS > 0:
        for p in db.projects_older_than(now - config.PROJECT_RETENTION_DAYS * DAY):
            db.delete_project(p["id"], p["user_id"])
            shutil.rmtree(_project_dir(p["id"]), ignore_errors=True)
            done["projects"] += 1
    if config.REQUIRE_EMAIL_VERIFICATION and config.UNVERIFIED_ACCOUNT_DAYS > 0:
        for uid in db.stale_unverified_users(now - config.UNVERIFIED_ACCOUNT_DAYS * DAY):
            for pid in db.delete_user_everything(uid):
                shutil.rmtree(_project_dir(pid), ignore_errors=True)
            done["accounts"] += 1
    return done


def _loop() -> None:
    while True:
        try:
            result = run_once()
            if any(result.values()):
                print(f"[retention] removed {result}")
        except Exception as exc:   # never let a cleanup problem take the app down
            print(f"[retention] failed: {type(exc).__name__}: {exc}")
        time.sleep(INTERVAL)


def start() -> None:
    threading.Thread(target=_loop, name="retention", daemon=True).start()
