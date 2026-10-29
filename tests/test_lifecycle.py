"""Account deletion, data export, retention, consent, takedowns and reports."""
import time
import uuid

import requests

from conftest import TMP
from test_api import H, signup


def test_signup_requires_age_and_terms_consent(server):
    r = requests.post(f"{server}/api/auth/signup", json={"email": "young@example.com", "password": "password123"}, headers=H)
    assert r.status_code == 400 and "16" in r.json()["detail"]


def test_creating_a_project_needs_the_rights_confirmation(server):
    s, _ = signup(server, "rights@example.com")
    r = s.post(f"{server}/api/projects", json={"url": "https://example.com/v.mp4"}, headers=H)
    assert r.status_code == 400 and "permission" in r.json()["detail"]


def test_export_has_the_data_and_no_secrets(server):
    s, user = signup(server, "export@example.com")
    r = s.get(f"{server}/api/me/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    body = r.json()
    assert body["account"]["email"] == "export@example.com"
    assert "pw_hash" not in body["account"] and "totp_secret" not in body["account"]


def test_account_deletion_removes_everything(server, sample_video):
    s, user = signup(server, "goodbye@example.com", "goodbye-pass-1")
    wrong = s.delete(f"{server}/api/me", json={"password": "nope-nope-nope"}, headers=H)
    assert wrong.status_code == 403
    ok = s.delete(f"{server}/api/me", json={"password": "goodbye-pass-1"}, headers=H)
    assert ok.status_code == 200
    assert s.get(f"{server}/api/me").status_code == 401
    assert requests.post(f"{server}/api/auth/login", json={"email": "goodbye@example.com", "password": "goodbye-pass-1"},
                         headers=H).status_code == 401
    from app import db
    assert db.get_user_by_email("goodbye@example.com") is None


def test_retention_removes_old_sources_and_projects(monkeypatch):
    from app import config, db, janitor
    uid = uuid.uuid4().hex[:16]
    db.create_user(uid, f"{uid}@example.com", "", "x")
    now = time.time()
    old = db.create_project(uuid.uuid4().hex[:12], uid, title="old", status="done")
    recent = db.create_project(uuid.uuid4().hex[:12], uid, title="recent", status="done")
    for p in (old, recent):
        d = config.MEDIA_DIR / p["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.mp4").write_bytes(b"x")
        (d / "clip_01.mp4").write_bytes(b"x")
    with db.conn() as c:   # age the old project
        c.execute("UPDATE projects SET created = ?, updated = ? WHERE id = ?", (now - 200 * 86400, now - 200 * 86400, old["id"]))
        c.execute("UPDATE projects SET updated = ? WHERE id = ?", (now - 20 * 86400, recent["id"]))
    monkeypatch.setattr(config, "SOURCE_RETENTION_DAYS", 14)
    monkeypatch.setattr(config, "PROJECT_RETENTION_DAYS", 90)
    janitor.run_once(now)
    assert not (config.MEDIA_DIR / old["id"]).exists()                      # whole project gone
    assert (config.MEDIA_DIR / recent["id"] / "clip_01.mp4").exists()        # clip kept
    assert not (config.MEDIA_DIR / recent["id"] / "source.mp4").exists()     # original dropped after 14 days
    db.delete_user_everything(uid)


def test_unverified_accounts_expire_only_when_verification_is_on(monkeypatch):
    from app import config, db, janitor
    uid = uuid.uuid4().hex[:16]
    db.create_user(uid, f"{uid}@example.com", "", "x")
    with db.conn() as c:
        c.execute("UPDATE users SET created = ? WHERE id = ?", (time.time() - 30 * 86400, uid))
    monkeypatch.setattr(config, "REQUIRE_EMAIL_VERIFICATION", False)
    janitor.run_once()
    assert db.get_user(uid) is not None
    monkeypatch.setattr(config, "REQUIRE_EMAIL_VERIFICATION", True)
    janitor.run_once()
    assert db.get_user(uid) is None


def test_takedown_strikes_and_suspends():
    from app import admin, db
    uid = uuid.uuid4().hex[:16]
    db.create_user(uid, f"{uid}@example.com", "", "x")
    for i in range(3):
        p = db.create_project(uuid.uuid4().hex[:12], uid, title=f"p{i}", status="done")
        admin.takedown(p["id"])
    u = db.get_user(uid)
    assert u["strikes"] == 3 and u["suspended"] == 1
    db.delete_user_everything(uid)


def test_suspended_users_cannot_log_in(server):
    s, user = signup(server, "banned@example.com", "banned-pass-1")
    from app import db
    db.update_user(user["id"], suspended=1)
    assert s.get(f"{server}/api/me").status_code == 403
    r = requests.post(f"{server}/api/auth/login", json={"email": "banned@example.com", "password": "banned-pass-1"}, headers=H)
    assert r.status_code == 403


def test_reports_are_validated_and_stored(server):
    bad = requests.post(f"{server}/api/report", json={"kind": "copyright", "url": "x", "details": "short"}, headers=H)
    assert bad.status_code == 400
    ok = requests.post(f"{server}/api/report", headers=H, json={
        "kind": "copyright", "url": f"{server}/files/abc/clip_01.mp4", "name": "Rights Holder", "email": "rh@example.com",
        "details": "This clip reproduces my video without permission.", "good_faith": True, "accurate": True})
    assert ok.status_code == 200
    from app import db
    assert any(r["id"] == ok.json()["id"] for r in db.list_reports())
