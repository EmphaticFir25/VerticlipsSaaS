"""One project, start to finish: source -> transcript -> scored moments -> rendered vertical clips."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import ai, billing, config, db, media

DEFAULT_SETTINGS = {
    "min_len": 20, "max_len": 60, "max_clips": 8,
    "caption_style": "karaoke", "layout": "fill", "hook_title": True,
    "highlight": "#FFE14D",
}
LENGTH_PRESETS = {"short": (15, 30), "medium": (30, 60), "long": (60, 90), "auto": (20, 75)}


class Cancelled(Exception):
    pass


def project_dir(project_id: str) -> Path:
    return config.MEDIA_DIR / project_id


def clean_settings(raw: dict, plan: dict, brand: dict) -> dict:
    """Validate customer settings against the plan; fill gaps from their brand kit."""
    s = dict(DEFAULT_SETTINGS)
    s.update({k: brand[k] for k in ("caption_style", "layout", "highlight", "hook_title") if k in brand})
    raw = raw or {}
    preset = raw.get("length")
    if preset in LENGTH_PRESETS:
        s["min_len"], s["max_len"] = LENGTH_PRESETS[preset]
        s["length"] = preset
    try:
        s["max_clips"] = max(1, min(int(raw.get("max_clips", s["max_clips"])), plan["max_clips"]))
    except (TypeError, ValueError):
        s["max_clips"] = min(s["max_clips"], plan["max_clips"])
    if raw.get("caption_style") in (*media.CAPTION_STYLES, "none"):
        s["caption_style"] = raw["caption_style"]
    if raw.get("layout") in media.LAYOUTS:
        s["layout"] = raw["layout"]
    if isinstance(raw.get("hook_title"), bool):
        s["hook_title"] = raw["hook_title"]
    return s


def run_project(project_id: str,
                transcribe: Callable[[Path, float], list[dict]] = ai.transcribe_file,
                score: Optional[Callable] = None) -> None:
    project = db.get_project(project_id)
    if project is None or project["status"] != "queued":
        return  # deleted, cancelled while waiting, or already picked up
    user = db.get_user(project["user_id"])
    if user is None:
        db.update_project(project_id, status="error", message="Account no longer exists.")
        return
    work = project_dir(project_id)
    work.mkdir(parents=True, exist_ok=True)
    charged = 0.0
    s = project["settings"]

    def step(stage: str, progress: int, message: str) -> None:
        current = db.get_project(project_id)
        if current is None or current["status"] == "cancelled":
            raise Cancelled()
        db.update_project(project_id, status="running", stage=stage, progress=progress, message=message)

    try:
        db.clear_clips(project_id)
        # 1. Source
        source = work / "source.mp4"
        if project["source_kind"] == "url":
            step("download", 4, "Downloading your video…")
            if not source.exists():
                media.download(project["source_url"], work)
        elif not source.exists():
            raise media.MediaError("The uploaded file is missing. Upload it again.")

        info = media.probe(source)
        if not info["has_audio"]:
            raise media.MediaError("This video has no audio track, so there is nothing to transcribe.")
        if not info["has_video"]:
            raise media.MediaError("This file has no video track.")

        # 2. Allowance: charged once the length is known, refunded if anything later fails.
        user = db.get_user(user["id"])
        need = billing.check_allowance(user, info["duration"])
        db.add_minutes(user["id"], need)
        charged = need
        db.update_project(project_id, duration=info["duration"], minutes_charged=need)
        if media.thumbnail(source, work / "thumb.jpg", info["duration"] * 0.1):
            db.update_project(project_id, has_thumb=1)

        # 3. Transcript
        step("transcribe", 18, "Transcribing with Whisper…")
        chunks = media.audio_chunks(source, work / "audio")
        words: list[dict] = []
        for i, (path, offset) in enumerate(chunks, 1):
            step("transcribe", 18 + int(30 * (i - 1) / len(chunks)), f"Transcribing part {i} of {len(chunks)}…")
            words += transcribe(path, offset)
        shutil.rmtree(work / "audio", ignore_errors=True)
        (work / "transcript.json").write_text(json.dumps(words))
        if len(words) < 20:
            raise media.MediaError("Not enough speech was found in this video to make clips.")

        # 4. Moments
        step("score", 50, "Finding viral moments with Claude…")
        scorer = score or ai.find_moments
        moments = scorer(words, s["min_len"], s["max_len"], s["max_clips"])
        if not moments:
            raise media.MediaError("No clip-worthy moments were found. Try a longer video or a different length.")

        # 5. Render
        plan = billing.plan_of(db.get_user(user["id"]) or user)
        brand = (db.get_user(user["id"]) or user).get("brand", {})
        logo = None
        if plan["brand_logo"] and brand.get("logo"):
            candidate = config.MEDIA_DIR / "brand" / Path(brand["logo"]).name
            logo = candidate if candidate.is_file() else None
        for i, m in enumerate(moments, 1):
            step("render", 60 + int(38 * (i - 1) / len(moments)), f"Rendering clip {i} of {len(moments)}…")
            render_one(project_id, user["id"], work, source, i, m, words, s, logo, plan["watermark"])

        db.update_project(project_id, status="done", stage="done", progress=100,
                          message=f"{len(moments)} clips ready")
    except Cancelled:
        if charged:
            db.add_minutes(user["id"], -charged)
            db.update_project(project_id, minutes_charged=0)
    except Exception as exc:  # every failure is shown to the customer on the project page
        if charged:
            db.add_minutes(user["id"], -charged)
            db.update_project(project_id, minutes_charged=0)
        msg = str(exc) if isinstance(exc, (media.MediaError, ai.AIError, billing.BillingError)) \
            else "Something went wrong while processing this video."
        print(f"[project {project_id}] failed: {type(exc).__name__}: {exc}")
        db.update_project(project_id, status="error", message=msg)


def render_one(project_id: str, user_id: str, work: Path, source: Path, idx: int, m: dict,
               words: list[dict], s: dict, logo: Optional[Path], watermark: bool) -> dict:
    out = work / f"clip_{idx:02d}.mp4"
    ass_name = None
    if s["caption_style"] != "none":
        ass_name = f"clip_{idx:02d}.ass"
        hook = m.get("hook", "") if s.get("hook_title") else ""
        (work / ass_name).write_text(
            media.build_ass(words, m["start"], m["end"], s["caption_style"], s.get("highlight", "#FFE14D"), hook),
            encoding="utf-8")
    media.render_clip(source, out, m["start"], m["end"], layout=s["layout"], logo=logo,
                      watermark=watermark, ass_name=ass_name)
    media.thumbnail(out, work / f"clip_{idx:02d}.jpg", min(1.0, (m["end"] - m["start"]) / 3), width=360)
    transcript = " ".join(w["word"] for w in words if m["start"] <= w["start"] < m["end"])
    clip = {
        "id": uuid.uuid4().hex[:12], "project_id": project_id, "user_id": user_id, "idx": idx,
        "file": out.name, "thumb": f"clip_{idx:02d}.jpg",
        "start": m["start"], "end": m["end"], "duration": round(m["end"] - m["start"], 1),
        "score": m.get("virality", 0), "scores": m.get("scores", {}),
        "title": m.get("title", f"Clip {idx}"), "hook": m.get("hook", ""),
        "reason": m.get("reason", ""), "description": m.get("description", ""),
        "hashtags": m.get("hashtags", []), "transcript": transcript[:2000],
    }
    db.add_clip(clip)
    return clip
