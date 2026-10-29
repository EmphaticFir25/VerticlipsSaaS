"""Two-step login with an authenticator app."""
import time

import requests

from app import auth
from test_api import H, signup


def current_code(secret):
    return auth._totp(secret, int(time.time() // 30))


def test_totp_matches_rfc_vector():
    # RFC 6238 test secret "12345678901234567890" at T=59 gives 94287082 (8 digits), so 6 digits end in 287082
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert auth._totp(secret, 59 // 30) == "287082"


def test_replayed_codes_are_refused():
    secret = auth.new_totp_secret()
    step = auth.check_totp(secret, current_code(secret))
    assert step
    assert auth.check_totp(secret, current_code(secret), last_used=step) is None


def test_two_step_flow(server):
    s, _ = signup(server, "twofa@example.com", "twofa-pass-123")
    setup = s.post(f"{server}/api/me/2fa/setup", headers=H).json()
    assert setup["qr_svg"].startswith("<svg") and "otpauth://" in setup["uri"]
    bad = s.post(f"{server}/api/me/2fa/enable", json={"code": "000000"}, headers=H)
    assert bad.status_code == 400
    codes = s.post(f"{server}/api/me/2fa/enable", json={"code": current_code(setup["secret"])}, headers=H).json()["recovery_codes"]
    assert len(codes) == 8
    assert s.get(f"{server}/api/me").json()["user"]["two_factor"] is True

    creds = {"email": "twofa@example.com", "password": "twofa-pass-123"}
    need = requests.post(f"{server}/api/auth/login", json=creds, headers=H)
    assert need.status_code == 401 and need.json()["two_factor"] is True
    wrong = requests.post(f"{server}/api/auth/login", json={**creds, "code": "123456"}, headers=H)
    assert wrong.status_code == 401
    # a recovery code works once
    ok = requests.post(f"{server}/api/auth/login", json={**creds, "code": codes[0]}, headers=H)
    assert ok.status_code == 200
    again = requests.post(f"{server}/api/auth/login", json={**creds, "code": codes[0]}, headers=H)
    assert again.status_code == 401

    # turning it off needs the password and a code
    off_bad = ok_session = requests.Session()
    ok_session.cookies.update(ok.cookies)
    no_code = ok_session.post(f"{server}/api/me/2fa/disable", json={"password": "twofa-pass-123", "code": "000000"}, headers=H)
    assert no_code.status_code == 400
    off = ok_session.post(f"{server}/api/me/2fa/disable", json={"password": "twofa-pass-123", "code": codes[1]}, headers=H)
    assert off.status_code == 200
    assert requests.post(f"{server}/api/auth/login", json=creds, headers=H).status_code == 200
