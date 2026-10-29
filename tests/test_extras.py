"""Write rate limits, customer quotes, link ownership and the status page."""
import asyncio
import json
import uuid

import pytest
import requests

from app import auth, config, db, main, status
from test_api import H, signup


# ---------- write-rate limit ----------

def asgi(method, path, ip, headers=None):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "scheme": "http", "server": ("test", 80), "client": ("1.1.1.1", 1),
             "headers": [(k.lower().encode(), v.encode()) for k, v in {"x-forwarded-for": ip, "content-type": "application/json",
                                                                       **(headers or {})}.items()]}
    sent, out = [], {}

    async def receive():
        if not sent:
            sent.append(1)
            return {"type": "http.request", "body": b"{}", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            out["status"] = message["status"]

    asyncio.run(main.app(scope, receive, send))
    return out["status"]


def test_write_rate_limit_blocks_bursts_per_address(monkeypatch):
    monkeypatch.setattr(main, "api_throttle", auth.Throttle(limit=3, window=60))
    codes = [asgi("POST", "/api/auth/login", "7.7.7.7", {"X-Verticlip": "1"}) for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
    assert asgi("POST", "/api/auth/login", "8.8.8.8", {"X-Verticlip": "1"}) == 401      # another address is unaffected
    assert asgi("GET", "/api/plans", "7.7.7.7") == 200                                   # reads are not limited
    assert asgi("POST", "/api/billing/webhook", "7.7.7.7") == 400                        # Stripe is never throttled


# ---------- customer quotes ----------

def make_eligible(user_id):
    db.create_project(uuid.uuid4().hex[:12], user_id, title="t", status="done")


def test_quotes_need_a_finished_project_consent_and_approval(server):
    s, user = signup(server, "quote@example.com")
    body = {"name": "Sam", "role": "podcaster", "quote": "It turned my hour-long episode into a week of posts.", "consent": True}
    assert s.post(f"{server}/api/testimonial", json=body, headers=H).status_code == 403          # no clips yet
    make_eligible(user["id"])
    assert s.post(f"{server}/api/testimonial", json={**body, "consent": False}, headers=H).status_code == 400
    assert s.post(f"{server}/api/testimonial", json={**body, "quote": "too short"}, headers=H).status_code == 400
    assert s.post(f"{server}/api/testimonial", json=body, headers=H).status_code == 200

    quotes = requests.get(f"{server}/api/testimonials").json()["testimonials"]
    assert not any(q["name"] == "Sam" and "hour-long" in q["quote"] for q in quotes)             # pending is never public
    pending = db.list_testimonials("pending")
    mine = next(t for t in pending if "hour-long" in t["quote"])
    db.set_testimonial_status(mine["id"], "approved")
    quotes = requests.get(f"{server}/api/testimonials").json()["testimonials"]
    assert any("hour-long" in q["quote"] for q in quotes)
    assert set(quotes[0]) == {"name", "role", "quote"}                                           # no email or ids leak

    assert "hour-long" in json.dumps(s.get(f"{server}/api/me/export").json()["testimonials"])    # part of their data export
    s.delete(f"{server}/api/testimonial", headers=H)                                              # consent can be withdrawn
    assert not any("hour-long" in q["quote"] for q in requests.get(f"{server}/api/testimonials").json()["testimonials"])


def test_editing_an_approved_quote_sends_it_back_for_review(server):
    s, user = signup(server, "edit-quote@example.com")
    make_eligible(user["id"])
    body = {"name": "Alex", "quote": "Clean captions and the scores were honest about which clips were weaker.", "consent": True}
    s.post(f"{server}/api/testimonial", json=body, headers=H)
    db.set_testimonial_status(db.get_testimonial(user["id"])["id"], "approved")
    s.post(f"{server}/api/testimonial", json={**body, "quote": "Edited: the scores were honest and the captions were clean."}, headers=H)
    assert db.get_testimonial(user["id"])["status"] == "pending"


def test_deleting_an_account_removes_the_quote(server):
    s, user = signup(server, "gone-quote@example.com", "gone-quote-pass-1")
    make_eligible(user["id"])
    s.post(f"{server}/api/testimonial", headers=H, json={"name": "Z", "quote": "This quote should vanish with the account.", "consent": True})
    assert db.get_testimonial(user["id"])
    s.delete(f"{server}/api/me", json={"password": "gone-quote-pass-1"}, headers=H)
    assert db.get_testimonial(user["id"]) is None


# ---------- link ownership ----------

def test_owned_mode_only_allows_videos_from_connected_youtube_channels(monkeypatch):
    from starlette.exceptions import HTTPException
    uid = uuid.uuid4().hex[:16]
    db.create_user(uid, f"{uid}@example.com", "", "x")
    db.upsert_account(uuid.uuid4().hex[:12], uid, "youtube", "UC_mine", "My channel", "tokens")
    user = db.get_user(uid)
    mine = {"extractor": "Youtube", "channel_id": "UC_mine"}
    monkeypatch.setattr(config, "LINK_POLICY", "attest")
    main.enforce_link_policy(user, {"extractor": "Twitch", "channel_id": ""})                    # default mode: anything goes
    monkeypatch.setattr(config, "LINK_POLICY", "owned")
    main.enforce_link_policy(user, mine)
    for info in ({"extractor": "Youtube", "channel_id": "UC_someone_else"}, {"extractor": "Twitch", "channel_id": "x"},
                 {"extractor": "Youtube", "channel_id": ""}):
        with pytest.raises(HTTPException) as e:
            main.enforce_link_policy(user, info)
        assert e.value.status_code == 403
    db.delete_user_everything(uid)


# ---------- status page ----------

def test_status_snapshot_reports_real_components(server):
    snap = requests.get(f"{server}/api/status").json()
    names = {c["name"] for c in snap["components"]}
    assert {"Database", "Clip processing", "Transcription", "Clip scoring", "Email", "Storage"} <= names
    assert snap["overall"] in ("operational", "degraded", "outage", "maintenance") and len(snap["history"]) == 30
    assert "@" not in json.dumps(snap)                                                           # no customer data in the payload
    assert requests.get(f"{server}/status").status_code == 200


def test_failing_provider_calls_degrade_the_status():
    status._recent.clear()
    for _ in range(5):
        status.record("scoring", False)
    comp = {c["name"]: c for c in status.snapshot()["components"]}
    assert comp["Clip scoring"]["state"] == "outage"
    for _ in range(40):
        status.record("scoring", True)
    assert {c["name"]: c for c in status.snapshot()["components"]}["Clip scoring"]["state"] == "operational"
    status._recent.clear()


def test_incidents_show_up_and_clear():
    iid = db.open_incident("outage", "Uploads are failing", "We are looking into it.")
    snap = status.snapshot()
    assert snap["overall"] == "outage" and any(i["id"] == iid for i in snap["incidents"])
    assert db.resolve_incident(iid)
    after = status.snapshot()
    assert any(i["id"] == iid and i["resolved"] for i in after["incidents"])                      # kept in the recent list
    assert after["overall"] != "outage" or any(c["state"] == "outage" for c in after["components"])


def test_history_records_samples():
    import time
    now = time.time()
    db.add_status_sample(now - 3600, 0)
    db.add_status_sample(now - 7200, 2)
    h = status._history(now)
    today = h[-1]
    assert today["level"] in ("degraded", "operational", "outage") and status._uptime(h) is not None
