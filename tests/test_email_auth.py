"""Email verification, password reset and the checks around them."""
import json
import re

import requests

from conftest import TMP
from test_api import H, signup


def outbox():
    path = TMP / "data" / "outbox.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def last_token(to, kind):
    mails = [m for m in outbox() if m["to"] == to and kind in m["body"] + m["subject"].lower()]
    assert mails, f"no {kind} email for {to}"
    return re.search(r"token=([\w-]+)", mails[-1]["body"]).group(1)


def test_signup_sends_verification_and_link_verifies(server):
    s, user = signup(server, "verify@example.com")
    assert user["email_verified"] is False
    token = last_token("verify@example.com", "confirm")
    assert requests.post(f"{server}/api/auth/verify", json={"token": token}, headers=H).status_code == 200
    assert s.get(f"{server}/api/me").json()["user"]["email_verified"] is True
    again = requests.post(f"{server}/api/auth/verify", json={"token": token}, headers=H)
    assert again.status_code == 400          # single use


def test_password_reset_flow(server):
    s, _ = signup(server, "reset@example.com", "old-password-1")
    r = requests.post(f"{server}/api/auth/forgot", json={"email": "reset@example.com"}, headers=H)
    assert r.status_code == 200
    token = last_token("reset@example.com", "reset")
    short = requests.post(f"{server}/api/auth/reset", json={"token": token, "password": "short"}, headers=H)
    assert short.status_code == 400
    ok = requests.post(f"{server}/api/auth/reset", json={"token": token, "password": "brand-new-pass-2"}, headers=H)
    assert ok.status_code == 200
    assert s.get(f"{server}/api/me").status_code == 401                  # old sessions are gone
    assert requests.post(f"{server}/api/auth/login", json={"email": "reset@example.com", "password": "old-password-1"},
                         headers=H).status_code == 401
    assert requests.post(f"{server}/api/auth/login", json={"email": "reset@example.com", "password": "brand-new-pass-2"},
                         headers=H).status_code == 200
    reuse = requests.post(f"{server}/api/auth/reset", json={"token": token, "password": "another-pass-3"}, headers=H)
    assert reuse.status_code == 400                                       # single use


def test_forgot_does_not_reveal_accounts(server):
    known = requests.post(f"{server}/api/auth/forgot", json={"email": "reset@example.com"}, headers=H)
    unknown = requests.post(f"{server}/api/auth/forgot", json={"email": "nobody-here@example.com"}, headers=H)
    assert known.status_code == unknown.status_code == 200 and known.json() == unknown.json()
    assert not [m for m in outbox() if m["to"] == "nobody-here@example.com"]


def test_bad_tokens_are_rejected(server):
    for path in ("verify", "reset"):
        r = requests.post(f"{server}/api/auth/{path}", json={"token": "nope", "password": "long-enough-pw"}, headers=H)
        assert r.status_code == 400


def test_unverified_accounts_cannot_create_projects_when_required(monkeypatch):
    from app import config, main
    import pytest
    from starlette.exceptions import HTTPException
    monkeypatch.setattr(config, "REQUIRE_EMAIL_VERIFICATION", True)
    with pytest.raises(HTTPException) as e:
        main.require_verified({"email_verified": 0})
    assert e.value.status_code == 403
    main.require_verified({"email_verified": 1})


def test_cloudflare_header_is_ignored_unless_trusted(server):
    class Req:
        headers = {"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "9.9.9.9"}
        client = None
    from app import config, main
    assert main.client_ip(Req) == "9.9.9.9"
