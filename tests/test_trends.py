"""Pricing and the trend finder."""
import time

import pytest
import requests

from app import config, trends
from test_api import H, signup

NOW = 1_800_000_000.0


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def video(vid, channel, views, hours_old, dur="PT12M30S", live="none", likes=1000, comments=100):
    return {"id": vid, "snippet": {"title": f"Title {vid}", "channelId": channel, "channelTitle": channel.upper(),
                                   "publishedAt": iso(NOW - hours_old * 3600), "liveBroadcastContent": live,
                                   "thumbnails": {"medium": {"url": f"https://i.ytimg.com/vi/{vid}/mq.jpg"}}},
            "statistics": {"viewCount": str(views), "likeCount": str(likes), "commentCount": str(comments)},
            "contentDetails": {"duration": dur}}


class FakeYouTube:
    def __init__(self):
        self.calls = []

    def __call__(self, url, params=None, timeout=0):
        self.calls.append(url)
        r = requests.Response()
        r.status_code = 200
        if url.endswith("/videos"):
            body = {"items": [
                video("big_old", "ucbig", 50_000_000, 400),                      # huge but slow: 125K/h
                video("rising", "ucbig", 3_000_000, 10),                         # 300K/h  -> should rank first
                video("short", "ucbig", 9_000_000, 5, dur="PT45S"),              # too short to cut down
                video("live", "ucbig", 9_000_000, 5, live="live"),               # live stream
                video("small_channel", "ucsmall", 9_000_000, 5),                 # not mainstream
                video("hidden_subs", "uchidden", 1_000_000, 20),                 # subscriber count hidden -> kept
            ]}
        else:
            body = {"items": [{"id": "ucbig", "statistics": {"subscriberCount": "4000000"}},
                              {"id": "ucsmall", "statistics": {"subscriberCount": "12000"}},
                              {"id": "uchidden", "statistics": {"hiddenSubscriberCount": True}}]}
        import json
        r._content = json.dumps(body).encode()
        return r


def test_studio_is_the_best_value_per_minute():
    p = config.PLANS
    per_min = {k: p[k]["price"] / p[k]["minutes"] for k in ("pro", "studio")}
    assert per_min["studio"] < per_min["pro"] * 0.75          # clearly cheaper, not marginally
    assert p["studio"]["minutes"] > p["pro"]["minutes"] and p["studio"]["max_clips"] >= p["pro"]["max_clips"]
    assert p["free"]["trend_items"] < p["pro"]["trend_items"] < p["studio"]["trend_items"]


def test_pricing_pages_match_the_plans(server):
    home = requests.get(server + "/pricing").text
    assert f"{config.PLANS['studio']['minutes']:,} minutes a month" in home and "Best value" in home


def test_build_ranks_by_velocity_and_filters(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(config, "TRENDS_MIN_SUBSCRIBERS", 500_000)
    monkeypatch.setattr(requests, "get", FakeYouTube())
    items = trends.build("US", NOW)
    ids = [i["id"] for i in items]
    assert ids == ["rising", "big_old", "hidden_subs"] or ids == ["rising", "hidden_subs", "big_old"]
    assert ids[0] == "rising" and items[0]["views_per_hour"] == 300_000 and items[0]["subscribers"] == 4_000_000
    assert not {"short", "live", "small_channel"} & set(ids)
    assert items[0]["url"] == "https://www.youtube.com/watch?v=rising" and items[0]["engagement"] > 0


def test_errors_never_contain_the_api_key(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "SUPER-SECRET-KEY")

    def boom(url, params=None, timeout=0):
        raise requests.ConnectionError(f"failed for {url}?key={params['key']}")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(trends.TrendError) as e:
        trends.build("US", NOW)
    assert "SUPER-SECRET-KEY" not in str(e.value)


def seed(monkeypatch, n=60):
    items = [{"id": f"v{i}", "channel_id": "ucbig" if i % 2 == 0 else "ucother", "views_per_hour": 1000 - i} for i in range(n)]
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "k")
    monkeypatch.setattr(config, "TRENDS_REGIONS", ["US", "GB", "CA", "AU"])
    monkeypatch.setitem(trends._cache, "US", items)
    monkeypatch.setitem(trends._cache, "GB", [{"id": "gb1", "channel_id": "x", "views_per_hour": 5}])
    monkeypatch.setitem(trends._state, "updated", time.time())


def test_each_plan_sees_its_own_list_size_and_regions(monkeypatch):
    seed(monkeypatch)
    free = trends.for_plan(config.PLANS["free"], "GB", set(), "attest")
    pro = trends.for_plan(config.PLANS["pro"], "US", set(), "attest")
    studio = trends.for_plan(config.PLANS["studio"], "GB", set(), "attest")
    assert len(free["items"]) == 5 and len(pro["items"]) == 20 and len(studio["items"]) == 1
    assert free["region"] == "US" and free["regions"] == ["US"]                # regions are a Studio feature
    assert studio["region"] == "GB" and studio["regions"] == ["US", "GB", "CA", "AU"]
    assert free["has_more"] is True


def test_clip_button_follows_the_link_policy(monkeypatch):
    seed(monkeypatch)
    open_mode = trends.for_plan(config.PLANS["pro"], "US", set(), "attest")
    assert all(i["clippable"] for i in open_mode["items"])
    owned_mode = trends.for_plan(config.PLANS["pro"], "US", {"ucbig"}, "owned")
    assert {i["channel_id"] for i in owned_mode["items"] if i["clippable"]} == {"ucbig"}
    assert not all(i["clippable"] for i in owned_mode["items"])
    assert "clippable" not in trends._cache["US"][0]                           # the shared cache is never modified


def test_trends_api_needs_login_and_reports_when_unconfigured(server):
    assert requests.get(f"{server}/api/trends").status_code == 401
    s, _ = signup(server, "trendy@example.com")
    body = s.get(f"{server}/api/trends").json()
    assert body["configured"] is False and body["items"] == [] and body["limit"] == 5
    assert s.get(f"{server}/trends").status_code == 200


def test_health_reflects_freshness(monkeypatch):
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "")
    assert trends.health()[0] == "maintenance"
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "k")
    monkeypatch.setitem(trends._state, "updated", time.time())
    monkeypatch.setitem(trends._state, "error", "")
    assert trends.health()[0] == "operational"
    monkeypatch.setitem(trends._state, "updated", time.time() - 10 * 3600)
    assert trends.health()[0] == "degraded"
