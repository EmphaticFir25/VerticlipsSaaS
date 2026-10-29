"""Trend finder: which videos from big YouTube channels are taking off right now.

Uses the official YouTube Data API (videos.list chart=mostPopular plus channels.list for subscriber counts), refreshed on a
timer and cached, so a page view never costs API quota. Videos are ranked by views per hour, which favours what is rising
now over what is merely big. Nothing here downloads or copies a video.
"""
from __future__ import annotations

import calendar
import re
import threading
import time
from typing import Optional

import requests

from . import config

API = "https://www.googleapis.com/youtube/v3"
MIN_DURATION = 120          # shorter videos give nothing to cut down
_cache: dict[str, list[dict]] = {}
_state = {"updated": 0.0, "error": ""}
_lock = threading.Lock()


class TrendError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.YOUTUBE_API_KEY)


def _get(path: str, params: dict) -> dict:
    try:
        r = requests.get(f"{API}/{path}", params={**params, "key": config.YOUTUBE_API_KEY}, timeout=15)
    except requests.RequestException as exc:
        raise TrendError(f"YouTube could not be reached ({type(exc).__name__}).") from None
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise TrendError(f"YouTube returned HTTP {r.status_code}: {msg[:200]}")    # the key is never part of this message
    return r.json()


def _seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _published(iso: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0


def build(region: str, now: Optional[float] = None) -> list[dict]:
    """Rising videos for one region, from channels above the subscriber floor, ranked by views per hour."""
    now = now or time.time()
    videos = _get("videos", {"part": "snippet,statistics,contentDetails", "chart": "mostPopular", "regionCode": region,
                             "maxResults": 50}).get("items", [])
    channel_ids = sorted({v["snippet"]["channelId"] for v in videos})
    subs: dict[str, Optional[int]] = {}
    for i in range(0, len(channel_ids), 50):
        data = _get("channels", {"part": "statistics", "id": ",".join(channel_ids[i:i + 50]), "maxResults": 50})
        for ch in data.get("items", []):
            st = ch.get("statistics", {})
            subs[ch["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    out = []
    for v in videos:
        sn, st, cd = v["snippet"], v.get("statistics", {}), v.get("contentDetails", {})
        if sn.get("liveBroadcastContent", "none") != "none":
            continue
        duration = _seconds(cd.get("duration", ""))
        n_subs = subs.get(sn["channelId"])
        if duration < MIN_DURATION or (n_subs is not None and n_subs < config.TRENDS_MIN_SUBSCRIBERS):
            continue
        views, likes, comments = (int(st.get(k, 0) or 0) for k in ("viewCount", "likeCount", "commentCount"))
        published = _published(sn.get("publishedAt", ""))
        hours = max(1.0, (now - published) / 3600) if published else 24.0
        thumbs = sn.get("thumbnails", {})
        out.append({
            "id": v["id"], "url": f"https://www.youtube.com/watch?v={v['id']}", "title": sn.get("title", "")[:150],
            "channel": sn.get("channelTitle", ""), "channel_id": sn["channelId"], "subscribers": n_subs,
            "thumbnail": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""),
            "views": views, "likes": likes, "comments": comments, "duration": duration, "published": published,
            "age_hours": round(hours, 1), "views_per_hour": round(views / hours),
            "engagement": round((likes + comments) / views, 4) if views else 0.0,
        })
    out.sort(key=lambda x: x["views_per_hour"], reverse=True)
    return out


def refresh() -> None:
    """Re-read every region. A failed region keeps its previous results, and the error is shown on the status page."""
    errors = []
    for region in config.TRENDS_REGIONS:
        try:
            items = build(region)
            with _lock:
                _cache[region] = items
        except TrendError as exc:
            errors.append(f"{region}: {exc}")
    with _lock:
        if len(errors) < len(config.TRENDS_REGIONS):
            _state["updated"] = time.time()
        _state["error"] = "; ".join(errors)


def for_plan(plan: dict, region: str, owned_channels: set[str], link_policy: str) -> dict:
    """What one customer sees: their plan's list size, their region (Studio can switch), and which items they may clip."""
    regions = config.TRENDS_REGIONS if plan.get("trend_items", 0) >= 50 else config.TRENDS_REGIONS[:1]
    region = region if region in regions else regions[0]
    with _lock:
        items = list(_cache.get(region, []))
        updated = _state["updated"]
    shown = items[: plan.get("trend_items", 5)]
    shown = [{**it, "clippable": link_policy != "owned" or it["channel_id"] in owned_channels} for it in shown]
    return {"configured": configured(), "region": region, "regions": regions, "updated": updated, "items": shown,
            "limit": plan.get("trend_items", 5), "has_more": len(items) > len(shown)}


def health(now: Optional[float] = None) -> tuple[str, str]:
    now = now or time.time()
    if not configured():
        return "maintenance", "Not set up on this server"
    with _lock:
        updated, error = _state["updated"], _state["error"]
    if not updated:
        return ("degraded", "No data yet") if error else ("operational", "Starting up")
    if now - updated > 4 * config.TRENDS_REFRESH_MINUTES * 60:
        return "degraded", "Data is out of date"
    return ("degraded", "Some regions failed to refresh") if error else ("operational", "Up to date")


def _loop() -> None:
    while True:
        try:
            refresh()
        except Exception as exc:
            print(f"[trends] refresh failed: {type(exc).__name__}: {exc}")
        time.sleep(config.TRENDS_REFRESH_MINUTES * 60)


def start() -> None:
    if configured():
        threading.Thread(target=_loop, name="trends", daemon=True).start()
