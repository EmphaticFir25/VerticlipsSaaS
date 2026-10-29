"""Public pages, headers, SEO files and sign-up hygiene."""
import requests

from app import auth, config


def test_every_tab_has_its_own_indexable_page(server):
    titles = set()
    for path in ("/", "/how-it-works", "/features", "/pricing", "/faq"):
        r = requests.get(server + path)
        assert r.status_code == 200 and "x-robots-tag" not in r.headers
        assert f'<link rel="canonical" href="{config.PUBLIC_BASE}{"" if path == "/" else path}"' in r.text
        assert "{{" not in r.text
        titles.add(r.text.split("<title>")[1].split("</title>")[0])
    assert len(titles) == 5            # distinct titles, so search engines see five pages


def test_app_and_account_pages_are_not_indexed(server):
    for path in ("/login", "/signup", "/forgot"):
        assert "noindex" in requests.get(server + path).headers["x-robots-tag"]


def test_security_headers_and_csp(server):
    h = requests.get(server + "/").headers
    csp = h["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "fonts.googleapis" not in csp
    assert h["x-frame-options"] == "DENY" and "camera=()" in h["permissions-policy"]
    assert requests.get(server + "/api/plans").headers["cache-control"] == "no-store"


def test_no_page_loads_fonts_from_google():
    import glob
    for f in glob.glob(str(config.WEB_DIR / "*.html")):
        assert "fonts.googleapis.com" not in open(f).read(), f


def test_legal_pages_are_filled_in_and_list_retention(server):
    for path in ("/terms", "/privacy", "/copyright", "/refunds", "/support"):
        r = requests.get(server + path)
        assert r.status_code == 200 and "{{" not in r.text, path
    assert f"{config.PROJECT_RETENTION_DAYS} days" in requests.get(server + "/privacy").text


def test_robots_sitemap_and_security_txt(server):
    robots = requests.get(server + "/robots.txt").text
    assert "Disallow: /api/" in robots and "Sitemap:" in robots
    sitemap = requests.get(server + "/sitemap.xml").text
    assert "/pricing" in sitemap and "/dashboard" not in sitemap
    assert requests.get(server + "/.well-known/security.txt").text.startswith("Contact: mailto:")


def test_disposable_addresses_are_refused(server):
    r = requests.post(f"{server}/api/auth/signup", headers={"X-Verticlip": "1"},
                      json={"email": "x@mailinator.com", "password": "password123", "agree": True})
    assert r.status_code == 400 and "permanent" in r.json()["detail"]


def test_breached_password_check_uses_only_a_hash_prefix(monkeypatch):
    import hashlib

    class Resp:
        status_code = 200
        digest = hashlib.sha1(b"password").hexdigest().upper()
        text = f"{digest[5:]}:3861493\r\n0000000000000000000000000000000000A:2"

    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return Resp

    import requests as r
    monkeypatch.setattr(r, "get", fake_get)
    monkeypatch.setattr(config, "HIBP_CHECK", True)
    assert auth.password_is_breached("password") is True
    assert seen["url"].endswith(Resp.digest[:5]) and Resp.digest[5:] not in seen["url"]
    monkeypatch.setattr(r, "get", lambda *a, **k: (_ for _ in ()).throw(r.RequestException()))
    assert auth.password_is_breached("password") is False      # fails open when the service is down


def test_captcha_is_enforced_when_configured(monkeypatch):
    monkeypatch.setattr(config, "TURNSTILE_SECRET", "s")
    monkeypatch.setattr(config, "TURNSTILE_SITE_KEY", "k")

    class Ok:
        def json(self):
            return {"success": True}

    class No:
        def json(self):
            return {"success": False}

    import requests as r
    monkeypatch.setattr(r, "post", lambda *a, **k: Ok())
    assert auth.captcha_ok("tok", "1.1.1.1") is True
    monkeypatch.setattr(r, "post", lambda *a, **k: No())
    assert auth.captcha_ok("tok", "1.1.1.1") is False
    monkeypatch.setattr(config, "TURNSTILE_SECRET", "")
    assert auth.captcha_ok("", "1.1.1.1") is True               # off when not configured
