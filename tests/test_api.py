"""End-to-end API tests against a live server with fake AI endpoints."""
import hashlib
import hmac
import json
import time

import requests

from conftest import WEBHOOK_SECRET

H = {"X-Verticlip": "1"}


def signup(base, email, password="password123"):
    s = requests.Session()
    r = s.post(f"{base}/api/auth/signup", json={"email": email, "password": password, "name": "T"}, headers=H)
    assert r.status_code == 200, r.text
    return s, r.json()["user"]


def wait_done(s, base, pid, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        p = s.get(f"{base}/api/projects/{pid}").json()
        if p["project"]["status"] in ("done", "error", "cancelled"):
            return p
        time.sleep(1)
    raise AssertionError("project did not finish")


def test_writes_need_the_header(server):
    r = requests.post(f"{server}/api/auth/signup", json={"email": "x@y.co", "password": "password123"})
    assert r.status_code == 403


def test_signup_login_logout(server):
    s, user = signup(server, "flow@example.com")
    assert user["plan"] == "free"
    assert s.get(f"{server}/api/me").status_code == 200
    dup = requests.post(f"{server}/api/auth/signup", json={"email": "FLOW@example.com", "password": "password123"},
                        headers=H)
    assert dup.status_code == 409
    bad = requests.post(f"{server}/api/auth/login", json={"email": "flow@example.com", "password": "nope-nope"},
                        headers=H)
    assert bad.status_code == 401
    s.post(f"{server}/api/auth/logout", headers=H)
    assert s.get(f"{server}/api/me").status_code == 401
    s2 = requests.Session()
    assert s2.post(f"{server}/api/auth/login", json={"email": "flow@example.com", "password": "password123"},
                   headers=H).status_code == 200
    assert s2.get(f"{server}/api/me").json()["user"]["email"] == "flow@example.com"


def test_signup_validation(server):
    r = requests.post(f"{server}/api/auth/signup", json={"email": "bad", "password": "password123"}, headers=H)
    assert r.status_code == 400
    r = requests.post(f"{server}/api/auth/signup", json={"email": "short@example.com", "password": "123"}, headers=H)
    assert r.status_code == 400


def test_pages_require_login(server):
    r = requests.get(f"{server}/dashboard?url=abc", allow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/dashboard%3Furl%3Dabc"
    assert requests.get(f"{server}/").status_code == 200
    assert requests.get(f"{server}/terms").status_code == 200


def test_upload_to_clips_and_isolation(server, sample_video):
    s, user = signup(server, "owner@example.com")
    with open(sample_video, "rb") as f:
        r = s.post(f"{server}/api/projects/upload", headers=H,
                   files={"file": ("my talk.mp4", f, "video/mp4")},
                   data={"settings": json.dumps({"length": "short", "max_clips": 50, "caption_style": "karaoke"})})
    assert r.status_code == 201, r.text
    project = r.json()["project"]
    assert project["settings"]["max_clips"] == 6  # capped by the free plan
    data = wait_done(s, server, project["id"])
    assert data["project"]["status"] == "done", data["project"]["message"]
    clips = data["clips"]
    assert len(clips) == 2
    assert clips[0]["score"] == 91 and clips[0]["hashtags"] == ["#startups", "#founder"]
    for c in clips:
        assert 8 <= c["duration"] <= 30.5
        v = s.get(server + c["video"])
        assert v.status_code == 200 and v.headers["content-type"] == "video/mp4"
    dl = s.get(server + clips[0]["download"])
    assert "attachment" in dl.headers.get("content-disposition", "")
    # minutes were charged for a 40 s video
    assert s.get(f"{server}/api/me").json()["usage"]["minutes_used"] == 1

    # rename a clip
    r = s.patch(f"{server}/api/clips/{clips[0]['id']}", json={"title": "  New name  "}, headers=H)
    assert r.json()["clip"]["title"] == "New name"

    # another customer sees nothing
    other, _ = signup(server, "intruder@example.com")
    assert other.get(f"{server}/api/projects/{project['id']}").status_code == 404
    assert other.get(server + clips[0]["video"]).status_code == 404
    assert other.patch(f"{server}/api/clips/{clips[0]['id']}", json={"title": "x"}, headers=H).status_code == 404
    assert other.get(f"{server}/api/projects").json()["projects"] == []
    assert requests.get(server + clips[0]["video"], allow_redirects=False).status_code in (303, 401)

    # publishing needs a connected account
    r = s.post(f"{server}/api/publish", json={"clip_ids": [clips[0]["id"]], "account_ids": []}, headers=H)
    assert r.status_code == 400

    # delete cleans up
    assert s.delete(f"{server}/api/projects/{project['id']}", headers=H).status_code == 200
    assert s.get(f"{server}/api/projects/{project['id']}").status_code == 404


def test_upload_rejects_non_video(server):
    s, _ = signup(server, "notvideo@example.com")
    r = s.post(f"{server}/api/projects/upload", headers=H, files={"file": ("x.mp4", b"hello", "video/mp4")})
    assert r.status_code == 400


def test_private_links_are_refused(server):
    s, _ = signup(server, "ssrf@example.com")
    for url in ("http://127.0.0.1/video.mp4", "http://localhost:8080/x", "file:///etc/passwd", "http://10.0.0.5/a"):
        r = s.post(f"{server}/api/projects", json={"url": url}, headers=H)
        assert r.status_code == 400, url


def _signed(payload: bytes, secret=WEBHOOK_SECRET):
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}


def test_stripe_webhook_upgrades_and_downgrades(server):
    s, user = signup(server, "payer@example.com")
    event = {"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {
        "client_reference_id": user["id"], "customer": "cus_1", "subscription": "sub_1",
        "metadata": {"plan": "pro", "user_id": user["id"]}}}}
    payload = json.dumps(event).encode()
    assert requests.post(f"{server}/api/billing/webhook", data=payload,
                         headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
    assert requests.post(f"{server}/api/billing/webhook", data=payload, headers=_signed(payload)).status_code == 200
    me = s.get(f"{server}/api/me").json()
    assert me["user"]["plan"] == "pro" and me["usage"]["max_clips"] == 15 and not me["usage"]["watermark"]
    # replaying the same event is a no-op; cancellation drops back to free
    requests.post(f"{server}/api/billing/webhook", data=payload, headers=_signed(payload))
    gone = json.dumps({"id": "evt_2", "type": "customer.subscription.deleted",
                       "data": {"object": {"customer": "cus_1"}}}).encode()
    assert requests.post(f"{server}/api/billing/webhook", data=gone, headers=_signed(gone)).status_code == 200
    assert s.get(f"{server}/api/me").json()["user"]["plan"] == "free"


def test_oauth_callback_rejects_unknown_state(server):
    s, _ = signup(server, "oauth@example.com")
    r = s.get(f"{server}/auth/tiktok/callback?code=abc&state=forged", allow_redirects=False)
    assert r.status_code == 303 and "/accounts?error=" in r.headers["location"]


def test_brand_kit(server):
    s, _ = signup(server, "brand@example.com")
    r = s.put(f"{server}/api/brand", json={"caption_style": "bold", "highlight": "#00ff88", "layout": "nope"}, headers=H)
    assert r.json()["brand"] == {"caption_style": "bold", "highlight": "#00FF88"}
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = s.post(f"{server}/api/brand/logo", files={"file": ("l.png", png, "image/png")}, headers=H)
    assert r.status_code == 200 and r.json()["brand"]["logo"].endswith(".png")
    assert s.get(f"{server}/brand/logo").status_code == 200
    r = s.post(f"{server}/api/brand/logo", files={"file": ("l.svg", b"<svg/>", "image/svg+xml")}, headers=H)
    assert r.status_code == 400
