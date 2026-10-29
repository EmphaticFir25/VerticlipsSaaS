"""Background work: a small pool for clip projects and another for uploads to social platforms.

Projects survive restarts: anything queued or mid-run when the server stopped is run again.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import config, db

_projects: "queue.Queue[str]" = queue.Queue()
_uploads = ThreadPoolExecutor(max_workers=3, thread_name_prefix="upload")
_started = False
_start_lock = threading.Lock()


def _loop(run: Callable[[str], None]) -> None:
    while True:
        project_id = _projects.get()
        try:
            run(project_id)
        except Exception as exc:  # run_project handles its own errors; this is a last resort
            print(f"[worker] project {project_id} crashed: {exc}")
            db.update_project(project_id, status="error", message="Processing stopped unexpectedly.")
        finally:
            _projects.task_done()


def start(run: Callable[[str], None]) -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        for p in db.projects_with_status("running"):
            db.update_project(p["id"], status="queued", message="Restarting after a server restart…", progress=0)
        for p in db.projects_with_status("queued"):
            _projects.put(p["id"])
        for pub in db.publishes_with_status("queued", "uploading"):
            db.set_publish(pub["id"], pub["user_id"], pub["clip_id"], pub["account_id"], pub["platform"],
                           "failed", "Interrupted by a server restart. Publish again.")
        for i in range(config.WORKERS):
            threading.Thread(target=_loop, args=(run,), name=f"clipper-{i}", daemon=True).start()


def submit(project_id: str) -> None:
    _projects.put(project_id)


def submit_upload(fn: Callable, *args) -> None:
    _uploads.submit(fn, *args)


def queue_position(project_id: str) -> int:
    with _projects.mutex:
        items = list(_projects.queue)
    return items.index(project_id) + 1 if project_id in items else 0
