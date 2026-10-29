"""Verticlip SaaS — web app and JSON API.

Run locally:  uvicorn app.main:app --port 33214
"""
from __future__ import annotations

import contextlib
import html
import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import ai, auth, billing, config, db, janitor, mailer, media, pipeline, social, worker

PARALLEL = {"free": 1, "pro": 3, "studio": 5}
FILE_NAME = re.compile(r"^(clip_\d{2}\.(mp4|jpg)|thumb\.jpg)$")
ID = re.compile(r"^[0-9a-f]{8,32}$")
CSRF_HEADER = "x-verticlip"
APP_PAGES = {"dashboard": "dashboard.html", "brand": "brand.html", "accounts": "accounts.html",
             "billing": "billing.html", "settings": "settings.html"}


# ---------- helpers ----------

def err(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


async def body_json(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise err(400, "Send a JSON body.")
    if not isinstance(data, dict):
        raise err(400, "Send a JSON object.")
    return data


def current_user(request: Request) -> Optional[dict]:
    return auth.user_for_token(request.cookies.get(auth.SESSION_COOKIE))


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise err(401, "Please log in.")
    if user.get("suspended"):
        raise err(403, "This account is suspended. Contact support if you think this is a mistake.")
    return user


def require_verified(user: dict) -> None:
    if config.REQUIRE_EMAIL_VERIFICATION and not user.get("email_verified"):
        raise err(403, "Confirm your email first. Use the link we sent, or ask for a new one from your dashboard.")


def require_rights(value) -> None:
    if value is not True and str(value).lower() != "true":
        raise err(400, "Confirm that you own this video or have permission to clip it.")


def set_session(resp: Response, token: str) -> None:
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL, httponly=True,
                    secure=config.SECURE_COOKIES, samesite="lax", path="/")


def client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip") if config.TRUST_CLOUDFLARE else None
    fwd = cf or request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def public_user(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user["name"], "plan": user["plan"],
            "has_subscription": bool(user.get("stripe_subscription")), "created": user["created"],
            "email_verified": bool(user.get("email_verified")), "notify_email": bool(user.get("notify_email", 1)),
            "verification_required": config.REQUIRE_EMAIL_VERIFICATION and not user.get("email_verified")}


def public_project(p: dict) -> dict:
    out = {k: p.get(k) for k in ("id", "title", "source_url", "source_kind", "status", "stage", "progress",
                                 "message", "settings", "duration", "minutes_charged", "created", "updated",
                                 "clip_count", "top_score")}
    out["thumb"] = f"/files/{p['id']}/thumb.jpg" if p.get("has_thumb") else None
    return out


def public_clip(c: dict) -> dict:
    base = f"/files/{c['project_id']}"
    out = {k: v for k, v in c.items() if k not in ("user_id", "file", "thumb")}
    out["video"] = f"{base}/{c['file']}"
    out["download"] = f"{base}/{c['file']}?dl=1"
    out["poster"] = f"{base}/{c['thumb']}"
    return out


def _tokens() -> dict:
    return {"LEGAL_NAME": config.LEGAL_NAME, "LEGAL_ADDRESS": config.LEGAL_ADDRESS or "(address to be added)",
            "CONTACT_EMAIL": config.CONTACT_EMAIL, "PRIVACY_EMAIL": config.PRIVACY_EMAIL,
            "ABUSE_EMAIL": config.ABUSE_EMAIL, "SECURITY_EMAIL": config.SECURITY_EMAIL,
            "JURISDICTION": config.JURISDICTION, "MIN_AGE": str(config.MIN_AGE),
            "SOURCE_DAYS": str(config.SOURCE_RETENTION_DAYS), "PROJECT_DAYS": str(config.PROJECT_RETENTION_DAYS),
            "PUBLIC_BASE": config.PUBLIC_BASE}


def page(name: str, status: int = 200) -> Response:
    text = (config.WEB_DIR / name).read_text(encoding="utf-8")
    for key, value in _tokens().items():
        text = text.replace("{{" + key + "}}", html.escape(value))
    return Response(text, status_code=status, media_type="text/html", headers={"Cache-Control": "no-cache"})


# ---------- middleware ----------

class Guard(BaseHTTPMiddleware):
    """Security headers on every response; a custom header on every state-changing API call so
    other sites cannot submit forms or scripts against a logged-in customer's session."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/")
                and path != "/api/billing/webhook" and request.headers.get(CSRF_HEADER) != "1"):
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
        resp = await call_next(request)
        if path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if config.SECURE_COOKIES:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp


async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/") or exc.status_code not in (401, 404):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)
    return page("404.html", 404)


async def server_error(request: Request, exc: Exception):
    print(f"[error] {request.method} {request.url.path}: {type(exc).__name__}: {exc}")
    return JSONResponse({"detail": "Something went wrong on our side."}, status_code=500)


# ---------- pages ----------

async def landing(request: Request):
    return page("index.html")


async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return page("login.html")


async def signup_page(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return page("signup.html")


async def plain_page(request: Request):
    return page(request.url.path.strip("/") + ".html")


async def app_page(request: Request):
    name = request.path_params["name"]
    if name not in APP_PAGES:
        raise err(404, "Not found")
    require_user(request)
    return page(APP_PAGES[name])


async def project_page(request: Request):
    require_user(request)
    return page("project.html")


async def legal_page(request: Request):
    return page("terms.html" if request.url.path == "/terms" else "privacy.html")


async def healthz(request: Request):
    return JSONResponse({"ok": True})


# ---------- auth API ----------

async def signup(request: Request):
    if not config.ALLOW_SIGNUPS:
        raise err(403, "Sign-ups are closed right now.")
    data = await body_json(request)
    ip = client_ip(request)
    if auth.signup_throttle.blocked(ip):
        raise err(429, "Too many sign-ups from this network. Try again later.")
    email = auth.normalize_email(data.get("email", ""))
    password = str(data.get("password", ""))
    name = str(data.get("name", "")).strip()[:80]
    if not auth.valid_email(email):
        raise err(400, "Enter a valid email address.")
    if len(password) < auth.MIN_PASSWORD or len(password) > 200:
        raise err(400, f"Use a password of at least {auth.MIN_PASSWORD} characters.")
    if db.get_user_by_email(email):
        raise err(409, "An account with that email already exists. Log in instead.")
    if data.get("agree") is not True:
        raise err(400, f"Confirm that you are at least {config.MIN_AGE} years old and accept the Terms and Privacy Policy.")
    pw_hash = await run_in_threadpool(auth.hash_password, password)
    user = db.create_user(uuid.uuid4().hex[:16], email, name, pw_hash, consented_at=time.time())
    auth.signup_throttle.fail(ip)
    await send_verification(user)
    resp = JSONResponse({"user": public_user(user)})
    set_session(resp, auth.new_session(user["id"]))
    return resp


async def send_verification(user: dict) -> None:
    link = f"{config.PUBLIC_BASE}/verify?token={auth.new_email_token(user['id'], 'verify')}"
    try:
        await run_in_threadpool(mailer.send, user["email"], *mailer.verification_email(link))
    except Exception as exc:   # a mail outage must not block sign-up; the customer can ask for a new link
        print(f"[mail] could not send verification to {user['email']}: {exc}")


async def resend_verification(request: Request):
    user = require_user(request)
    if user.get("email_verified"):
        return JSONResponse({"ok": True})
    key = f"verify|{user['id']}"
    if auth.mail_throttle.blocked(key):
        raise err(429, "We already sent several emails. Check your inbox and spam folder, or try again in an hour.")
    auth.mail_throttle.fail(key)
    await send_verification(user)
    return JSONResponse({"ok": True})


async def verify_email(request: Request):
    data = await body_json(request)
    user_id = auth.consume_email_token(data.get("token", ""), "verify")
    if not user_id:
        raise err(400, "This link has expired or was already used. Log in and ask for a new one.")
    db.update_user(user_id, email_verified=1)
    return JSONResponse({"ok": True})


report_throttle = auth.Throttle(limit=5, window=3600)


async def submit_report(request: Request):
    """Copyright and abuse reports. Public on purpose: rights holders are not customers."""
    data = await body_json(request)
    ip = f"report|{client_ip(request)}"
    if report_throttle.blocked(ip):
        raise err(429, "Too many reports from this network. Try again later.")
    kind = "copyright" if data.get("kind") == "copyright" else "abuse"
    url, details = str(data.get("url", "")).strip()[:500], str(data.get("details", "")).strip()[:4000]
    name, email = str(data.get("name", "")).strip()[:120], auth.normalize_email(data.get("email", ""))
    if not url or len(details) < 20:
        raise err(400, "Tell us where the content is and what the problem is (at least a couple of sentences).")
    if not auth.valid_email(email):
        raise err(400, "Enter an email address we can reach you at.")
    if kind == "copyright" and not (data.get("good_faith") is True and data.get("accurate") is True and name):
        raise err(400, "A copyright notice needs your name and both confirmations.")
    report_throttle.fail(ip)
    rid = uuid.uuid4().hex[:12]
    db.create_report(rid, kind, url, name, email, details)
    body = (f"New {kind} report {rid}\n\nFrom: {name} <{email}>\nContent: {url}\n\n{details}\n\n"
            f"Review with: python -m app.admin reports")
    try:
        await run_in_threadpool(mailer.send, config.ABUSE_EMAIL, f"[Verticlip] {kind} report {rid}", body)
    except Exception as exc:
        print(f"[mail] could not forward report {rid}: {exc}")
    return JSONResponse({"ok": True, "id": rid})


async def forgot_password(request: Request):
    data = await body_json(request)
    email = auth.normalize_email(data.get("email", ""))
    ip_key, mail_key = f"forgot|{client_ip(request)}", f"forgot|{email}"
    if auth.mail_throttle.blocked(ip_key) or auth.mail_throttle.blocked(mail_key):
        raise err(429, "Too many requests. Try again in an hour.")
    auth.mail_throttle.fail(ip_key)
    user = db.get_user_by_email(email) if auth.valid_email(email) else None
    if user:
        auth.mail_throttle.fail(mail_key)
        link = f"{config.PUBLIC_BASE}/reset?token={auth.new_email_token(user['id'], 'reset')}"
        try:
            await run_in_threadpool(mailer.send, user["email"], *mailer.reset_email(link))
        except Exception as exc:
            print(f"[mail] could not send reset to {user['email']}: {exc}")
    # The same answer whether or not the address has an account, so this can't be used to find customers.
    return JSONResponse({"ok": True})


async def reset_password(request: Request):
    data = await body_json(request)
    password = str(data.get("password", ""))
    if len(password) < auth.MIN_PASSWORD or len(password) > 200:
        raise err(400, f"Use a password of at least {auth.MIN_PASSWORD} characters.")
    user_id = auth.consume_email_token(data.get("token", ""), "reset")
    if not user_id:
        raise err(400, "This link has expired or was already used. Ask for a new reset link.")
    pw_hash = await run_in_threadpool(auth.hash_password, password)
    db.update_user(user_id, pw_hash=pw_hash, email_verified=1)   # they proved they own the inbox
    db.delete_user_sessions(user_id)                             # sign out every device
    return JSONResponse({"ok": True})


async def login(request: Request):
    data = await body_json(request)
    email = auth.normalize_email(data.get("email", ""))
    password = str(data.get("password", ""))
    key = f"{client_ip(request)}|{email}"
    if auth.login_throttle.blocked(key):
        raise err(429, "Too many attempts. Wait 15 minutes and try again.")
    user = db.get_user_by_email(email)
    ok = await run_in_threadpool(auth.verify_password, password, user["pw_hash"] if user else auth._DUMMY_HASH)
    if not user or not ok:
        auth.login_throttle.fail(key)
        raise err(401, "Wrong email or password.")
    auth.login_throttle.clear(key)
    if user.get("suspended"):
        raise err(403, "This account is suspended. Contact support if you think this is a mistake.")
    resp = JSONResponse({"user": public_user(user)})
    set_session(resp, auth.new_session(user["id"]))
    return resp


async def logout(request: Request):
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


async def me(request: Request):
    user = require_user(request)
    return JSONResponse({
        "user": public_user(user), "usage": billing.usage(user), "brand": user.get("brand", {}),
        "ai": ai.ai_status(), "social": social.available(), "billing": billing.stripe_enabled(),
        "plans": config.PLANS,
    })


async def delete_me(request: Request):
    user = require_user(request)
    data = await body_json(request)
    ok = await run_in_threadpool(auth.verify_password, str(data.get("password", "")), user["pw_hash"])
    if not ok:
        raise err(403, "That password is not right.")
    if db.active_project_count(user["id"]):
        raise err(409, "Cancel your running projects before deleting your account.")
    if user.get("stripe_subscription"):
        try:
            await run_in_threadpool(billing.cancel_subscription, user["stripe_subscription"])
        except billing.BillingError as exc:
            raise err(502, str(exc))
    logo = (user.get("brand") or {}).get("logo")
    for pid in db.delete_user_everything(user["id"]):
        shutil.rmtree(pipeline.project_dir(pid), ignore_errors=True)
    if logo:
        (config.MEDIA_DIR / "brand" / Path(logo).name).unlink(missing_ok=True)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


export_throttle = auth.Throttle(limit=5, window=3600)


async def export_me(request: Request):
    user = require_user(request)
    if export_throttle.blocked(user["id"]):
        raise err(429, "You can download your data a few times an hour. Try again later.")
    export_throttle.fail(user["id"])
    data = db.export_user(user["id"])
    for p in data["projects"]:
        t = pipeline.project_dir(p["id"]) / "transcript.json"
        if t.is_file():
            try:
                p["transcript"] = " ".join(w.get("word", "") for w in json.loads(t.read_text()))
            except (ValueError, OSError):
                pass
    data["exported_at"] = time.time()
    data["note"] = "Video files are not included here. Download clips from each project page while they are available."
    return Response(json.dumps(data, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="verticlip-data.json"'})


async def update_me(request: Request):
    user = require_user(request)
    data = await body_json(request)
    if "name" in data:
        db.update_user(user["id"], name=str(data["name"]).strip()[:80])
    if isinstance(data.get("notify_email"), bool):
        db.update_user(user["id"], notify_email=int(data["notify_email"]))
    return JSONResponse({"user": public_user(db.get_user(user["id"]))})


async def change_password(request: Request):
    user = require_user(request)
    data = await body_json(request)
    if not await run_in_threadpool(auth.verify_password, str(data.get("current", "")), user["pw_hash"]):
        raise err(400, "Your current password is wrong.")
    new = str(data.get("new", ""))
    if len(new) < auth.MIN_PASSWORD or len(new) > 200:
        raise err(400, f"Use a password of at least {auth.MIN_PASSWORD} characters.")
    db.update_user(user["id"], pw_hash=await run_in_threadpool(auth.hash_password, new))
    token = request.cookies.get(auth.SESSION_COOKIE, "")
    db.delete_user_sessions(user["id"], keep=auth.token_hash(token))
    return JSONResponse({"ok": True})


# ---------- projects ----------

def _check_ai() -> None:
    status = ai.ai_status()
    if not (status["scoring"] and status["transcription"]):
        raise err(503, "Clipping isn't available right now. The server's AI keys aren't set up yet.")


def _check_parallel(user: dict) -> None:
    limit = PARALLEL.get(user["plan"], 1)
    if db.active_project_count(user["id"]) >= limit:
        raise err(429, f"Your plan runs {limit} video{'s' if limit > 1 else ''} at a time. "
                       "Wait for the current one to finish.")


async def list_projects(request: Request):
    user = require_user(request)
    return JSONResponse({"projects": [public_project(p) for p in db.list_projects(user["id"])]})


async def create_project(request: Request):
    user = require_user(request)
    require_verified(user)
    data = await body_json(request)
    require_rights(data.get("rights"))
    _check_ai()
    _check_parallel(user)
    try:
        url = await run_in_threadpool(media.validate_url, str(data.get("url", "")))
        info = await run_in_threadpool(media.remote_info, url)
    except media.MediaError as exc:
        raise err(400, str(exc))
    if info["is_live"]:
        raise err(400, "Live streams can be clipped once they have ended.")
    if info["duration"]:
        try:
            billing.check_allowance(user, info["duration"])
        except billing.BillingError as exc:
            raise err(402, str(exc))
    plan = billing.plan_of(user)
    settings = pipeline.clean_settings(data.get("settings") or {}, plan, user.get("brand", {}))
    title = str(data.get("title") or info["title"] or url).strip()[:200]
    project = db.create_project(uuid.uuid4().hex[:12], user["id"], title=title, source_url=url,
                                source_kind="url", status="queued", message="Waiting to start…",
                                settings=settings, duration=info["duration"])
    worker.submit(project["id"])
    return JSONResponse({"project": public_project(project)}, status_code=201)


async def upload_project(request: Request):
    user = require_user(request)
    require_verified(user)
    _check_ai()
    _check_parallel(user)
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    if int(request.headers.get("content-length") or 0) > limit + 1024 * 1024:
        raise err(413, f"Files can be up to {config.MAX_UPLOAD_MB} MB.")
    form = await request.form(max_files=1, max_fields=6, max_part_size=64 * 1024)
    require_rights(form.get("rights"))
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise err(400, "Choose a video file.")
    try:
        raw_settings = json.loads(str(form.get("settings") or "{}"))
    except ValueError:
        raw_settings = {}
    project_id = uuid.uuid4().hex[:12]
    work = pipeline.project_dir(project_id)
    work.mkdir(parents=True, exist_ok=True)
    dest = work / "source.mp4"
    size = 0
    with open(dest, "wb") as out:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                shutil.rmtree(work, ignore_errors=True)
                raise err(413, f"Files can be up to {config.MAX_UPLOAD_MB} MB.")
            out.write(chunk)
    try:
        info = await run_in_threadpool(media.probe, dest)
        if not info["has_video"]:
            raise media.MediaError("That file is not a video.")
        billing.check_allowance(user, info["duration"])
    except (media.MediaError, billing.BillingError) as exc:
        shutil.rmtree(work, ignore_errors=True)
        status = 402 if isinstance(exc, billing.BillingError) else 400
        raise err(status, str(exc) if isinstance(exc, billing.BillingError) else "That file could not be read as a video.")
    plan = billing.plan_of(user)
    title = Path(upload.filename or "Upload").stem[:200] or "Upload"
    project = db.create_project(project_id, user["id"], title=title, source_kind="upload", status="queued",
                                message="Waiting to start…", duration=info["duration"],
                                settings=pipeline.clean_settings(raw_settings, plan, user.get("brand", {})))
    worker.submit(project_id)
    return JSONResponse({"project": public_project(project)}, status_code=201)


def _owned_project(request: Request, user: dict) -> dict:
    pid = request.path_params["pid"]
    project = db.get_project(pid, user["id"]) if ID.match(pid) else None
    if project is None:
        raise err(404, "That project doesn't exist.")
    return project


async def get_project(request: Request):
    user = require_user(request)
    project = _owned_project(request, user)
    clips = [public_clip(c) for c in db.list_clips(project["id"], user["id"])]
    return JSONResponse({"project": public_project(project), "clips": clips,
                         "queue_position": worker.queue_position(project["id"]),
                         "accounts": db.list_accounts(user["id"]), "youtube_privacy": config.YOUTUBE_PRIVACY})


async def cancel_project(request: Request):
    user = require_user(request)
    project = _owned_project(request, user)
    if project["status"] not in ("queued", "running"):
        raise err(409, "This project is not running.")
    db.update_project(project["id"], status="cancelled", message="Cancelled")
    return JSONResponse({"ok": True})


async def retry_project(request: Request):
    user = require_user(request)
    project = _owned_project(request, user)
    if project["status"] not in ("error", "cancelled"):
        raise err(409, "Only failed or cancelled projects can be retried.")
    _check_ai()
    _check_parallel(user)
    db.update_project(project["id"], status="queued", stage="", progress=0, message="Waiting to start…")
    worker.submit(project["id"])
    return JSONResponse({"ok": True})


async def delete_project(request: Request):
    user = require_user(request)
    project = _owned_project(request, user)
    if project["status"] in ("queued", "running"):
        raise err(409, "Cancel this project before deleting it.")
    db.delete_project(project["id"], user["id"])
    shutil.rmtree(pipeline.project_dir(project["id"]), ignore_errors=True)
    return JSONResponse({"ok": True})


async def update_clip(request: Request):
    user = require_user(request)
    data = await body_json(request)
    fields: dict[str, Any] = {}
    if "title" in data:
        fields["title"] = str(data["title"]).strip()[:100] or "Untitled clip"
    if "description" in data:
        fields["description"] = str(data["description"]).strip()[:2000]
    if "hashtags" in data and isinstance(data["hashtags"], list):
        fields["hashtags"] = [("#" + str(h).strip().lstrip("#").replace(" ", ""))[:40]
                              for h in data["hashtags"] if str(h).strip()][:10]
    clip = db.update_clip(request.path_params["cid"], user["id"], **fields)
    if clip is None:
        raise err(404, "That clip doesn't exist.")
    return JSONResponse({"clip": public_clip(clip)})


async def serve_file(request: Request):
    user = require_user(request)
    pid, name = request.path_params["pid"], request.path_params["name"]
    if not ID.match(pid) or not FILE_NAME.match(name) or db.get_project(pid, user["id"]) is None:
        raise err(404, "Not found")
    path = pipeline.project_dir(pid) / name
    if not path.is_file():
        raise err(404, "Not found")
    headers = {"Cache-Control": "private, max-age=3600"}
    if request.query_params.get("dl") and name.endswith(".mp4"):
        project = db.get_project(pid, user["id"])
        stem = re.sub(r"[^A-Za-z0-9 _-]+", "", project["title"])[:60].strip() or "verticlip"
        headers["Content-Disposition"] = f'attachment; filename="{stem} - {name}"'
    return FileResponse(path, headers=headers)


# ---------- brand kit ----------

async def get_brand(request: Request):
    user = require_user(request)
    return JSONResponse({"brand": user.get("brand", {}), "usage": billing.usage(user)})


async def put_brand(request: Request):
    user = require_user(request)
    data = await body_json(request)
    brand = dict(user.get("brand", {}))
    if data.get("caption_style") in (*media.CAPTION_STYLES, "none"):
        brand["caption_style"] = data["caption_style"]
    if data.get("layout") in media.LAYOUTS:
        brand["layout"] = data["layout"]
    if isinstance(data.get("hook_title"), bool):
        brand["hook_title"] = data["hook_title"]
    if isinstance(data.get("highlight"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", data["highlight"]):
        brand["highlight"] = data["highlight"].upper()
    db.update_user(user["id"], brand=brand)
    return JSONResponse({"brand": brand})


async def upload_logo(request: Request):
    user = require_user(request)
    if int(request.headers.get("content-length") or 0) > 3 * 1024 * 1024:
        raise err(413, "Logos can be up to 2 MB.")
    form = await request.form(max_files=1, max_fields=2)
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise err(400, "Choose an image.")
    raw = await upload.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise err(413, "Logos can be up to 2 MB.")
    ext = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"RIFF": ".webp"}
    suffix = next((e for sig, e in ext.items() if raw.startswith(sig)), None)
    if suffix is None:
        raise err(400, "Use a PNG, JPG or WebP image.")
    folder = config.MEDIA_DIR / "brand"
    folder.mkdir(parents=True, exist_ok=True)
    brand = dict(user.get("brand", {}))
    if brand.get("logo"):
        (folder / Path(brand["logo"]).name).unlink(missing_ok=True)
    name = f"{user['id']}_{uuid.uuid4().hex[:6]}{suffix}"
    (folder / name).write_bytes(raw)
    brand["logo"] = name
    db.update_user(user["id"], brand=brand)
    return JSONResponse({"brand": brand})


async def delete_logo(request: Request):
    user = require_user(request)
    brand = dict(user.get("brand", {}))
    if brand.pop("logo", None):
        (config.MEDIA_DIR / "brand" / Path(user["brand"]["logo"]).name).unlink(missing_ok=True)
    db.update_user(user["id"], brand=brand)
    return JSONResponse({"brand": brand})


async def brand_logo(request: Request):
    user = require_user(request)
    name = user.get("brand", {}).get("logo")
    path = config.MEDIA_DIR / "brand" / Path(name).name if name else None
    if not path or not path.is_file():
        raise err(404, "Not found")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})


# ---------- social accounts & publishing ----------

async def list_accounts(request: Request):
    user = require_user(request)
    return JSONResponse({"accounts": db.list_accounts(user["id"]), "available": social.available()})


async def remove_account(request: Request):
    user = require_user(request)
    if not db.delete_account(request.path_params["aid"], user["id"]):
        raise err(404, "That account isn't connected.")
    return JSONResponse({"ok": True})


async def oauth_start(request: Request):
    user = require_user(request)
    platform = request.path_params["platform"]
    try:
        url = social.youtube_start(user["id"]) if platform == "youtube" else \
            social.tiktok_start(user["id"]) if platform == "tiktok" else None
    except social.SocialError as exc:
        return RedirectResponse(f"/accounts?error={quote(str(exc))}", status_code=303)
    if url is None:
        raise err(404, "Not found")
    return RedirectResponse(url, status_code=303)


async def oauth_callback(request: Request):
    platform = request.path_params["platform"]
    if platform not in ("youtube", "tiktok"):
        raise err(404, "Not found")
    q = request.query_params
    if q.get("error"):
        return RedirectResponse(f"/accounts?error={quote('Connection was cancelled.')}", status_code=303)
    user_id = db.pop_oauth_state(q.get("state", ""), platform)
    viewer = current_user(request)
    if not user_id or (viewer and viewer["id"] != user_id):
        return RedirectResponse(f"/accounts?error={quote('That sign-in link expired. Try connecting again.')}",
                                status_code=303)
    try:
        handler = social.youtube_callback if platform == "youtube" else social.tiktok_callback
        await run_in_threadpool(handler, user_id, q.get("code", ""))
    except social.SocialError as exc:
        return RedirectResponse(f"/accounts?error={quote(str(exc))}", status_code=303)
    except Exception as exc:
        print(f"[oauth] {platform} callback failed: {exc}")
        return RedirectResponse(f"/accounts?error={quote('Could not finish connecting. Try again.')}",
                                status_code=303)
    return RedirectResponse(f"/accounts?connected={platform}", status_code=303)


def _do_upload(pub_id: str, user_id: str, clip_id: str, account_id: str) -> None:
    clip = db.get_clip(clip_id, user_id)
    account = db.get_account(account_id, user_id)
    if not clip or not account:
        return
    db.set_publish(pub_id, user_id, clip_id, account_id, account["platform"], "uploading")
    try:
        path = pipeline.project_dir(clip["project_id"]) / clip["file"]
        fn = social.upload_youtube if account["platform"] == "youtube" else social.upload_tiktok
        detail = fn(account, clip, path)
        db.set_publish(pub_id, user_id, clip_id, account_id, account["platform"], "done", detail)
    except Exception as exc:
        msg = str(exc) if isinstance(exc, social.SocialError) else "Upload failed. Try again."
        print(f"[publish] {clip_id} -> {account['platform']}: {exc}")
        db.set_publish(pub_id, user_id, clip_id, account_id, account["platform"], "failed", msg[:300])


async def publish(request: Request):
    user = require_user(request)
    data = await body_json(request)
    clip_ids = [str(c) for c in (data.get("clip_ids") or [])][:50]
    account_ids = [str(a) for a in (data.get("account_ids") or [])][:20]
    clips = [c for c in (db.get_clip(cid, user["id"]) for cid in clip_ids) if c]
    accounts = [a for a in (db.get_account(aid, user["id"]) for aid in account_ids) if a]
    if not clips:
        raise err(400, "Pick at least one clip.")
    if not accounts:
        raise err(400, "Pick at least one connected account.")
    for clip in clips:
        for acct in accounts:
            pub_id = uuid.uuid4().hex[:12]
            db.set_publish(pub_id, user["id"], clip["id"], acct["id"], acct["platform"], "queued")
            worker.submit_upload(_do_upload, pub_id, user["id"], clip["id"], acct["id"])
    return JSONResponse({"queued": len(clips) * len(accounts)})


# ---------- billing ----------

async def plans(request: Request):
    return JSONResponse({"plans": config.PLANS, "billing": billing.stripe_enabled()})


async def checkout(request: Request):
    user = require_user(request)
    data = await body_json(request)
    try:
        url = await run_in_threadpool(billing.checkout_url, user, str(data.get("plan", "")))
    except billing.BillingError as exc:
        raise err(400, str(exc))
    return JSONResponse({"url": url})


async def portal(request: Request):
    user = require_user(request)
    try:
        url = await run_in_threadpool(billing.portal_url, user)
    except billing.BillingError as exc:
        raise err(400, str(exc))
    return JSONResponse({"url": url})


async def stripe_webhook(request: Request):
    payload = await request.body()
    if not billing.verify_signature(payload, request.headers.get("stripe-signature", ""),
                                    config.STRIPE_WEBHOOK_SECRET):
        raise err(400, "Bad signature.")
    note = billing.handle_event(billing.parse_event(payload))
    print(f"[stripe] {note}")
    return JSONResponse({"received": True})


# ---------- app ----------

routes = [
    Route("/", landing),
    Route("/login", login_page),
    Route("/signup", signup_page),
    Route("/copyright", plain_page),
    Route("/report", plain_page),
    Route("/refunds", plain_page),
    Route("/support", plain_page),
    Route("/forgot", plain_page),
    Route("/reset", plain_page),
    Route("/verify", plain_page),
    Route("/terms", legal_page),
    Route("/privacy", legal_page),
    Route("/healthz", healthz),
    Route("/project/{pid:str}", project_page),
    Route("/api/auth/signup", signup, methods=["POST"]),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/auth/logout", logout, methods=["POST"]),
    Route("/api/auth/forgot", forgot_password, methods=["POST"]),
    Route("/api/auth/reset", reset_password, methods=["POST"]),
    Route("/api/auth/verify", verify_email, methods=["POST"]),
    Route("/api/auth/resend", resend_verification, methods=["POST"]),
    Route("/api/me", me, methods=["GET"]),
    Route("/api/me", update_me, methods=["PATCH"]),
    Route("/api/me", delete_me, methods=["DELETE"]),
    Route("/api/me/export", export_me, methods=["GET"]),
    Route("/api/me/password", change_password, methods=["POST"]),
    Route("/api/report", submit_report, methods=["POST"]),
    Route("/api/projects", list_projects, methods=["GET"]),
    Route("/api/projects", create_project, methods=["POST"]),
    Route("/api/projects/upload", upload_project, methods=["POST"]),
    Route("/api/projects/{pid:str}", get_project, methods=["GET"]),
    Route("/api/projects/{pid:str}", delete_project, methods=["DELETE"]),
    Route("/api/projects/{pid:str}/cancel", cancel_project, methods=["POST"]),
    Route("/api/projects/{pid:str}/retry", retry_project, methods=["POST"]),
    Route("/api/clips/{cid:str}", update_clip, methods=["PATCH"]),
    Route("/files/{pid:str}/{name:str}", serve_file),
    Route("/api/brand", get_brand, methods=["GET"]),
    Route("/api/brand", put_brand, methods=["PUT"]),
    Route("/api/brand/logo", upload_logo, methods=["POST"]),
    Route("/api/brand/logo", delete_logo, methods=["DELETE"]),
    Route("/brand/logo", brand_logo),
    Route("/api/accounts", list_accounts, methods=["GET"]),
    Route("/api/accounts/{aid:str}", remove_account, methods=["DELETE"]),
    Route("/auth/{platform:str}/start", oauth_start),
    Route("/auth/{platform:str}/callback", oauth_callback),
    Route("/api/publish", publish, methods=["POST"]),
    Route("/api/plans", plans),
    Route("/api/billing/checkout", checkout, methods=["POST"]),
    Route("/api/billing/portal", portal, methods=["POST"]),
    Route("/api/billing/webhook", stripe_webhook, methods=["POST"]),
    Mount("/static", StaticFiles(directory=config.WEB_DIR / "static"), name="static"),
    Mount("/assets", StaticFiles(directory=config.ASSETS_DIR), name="assets"),
    Route("/{name:str}", app_page),
]


@contextlib.asynccontextmanager
async def lifespan(app):
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if config.SECURE_COOKIES and not config.SECRET_KEY:
        raise RuntimeError("Set SECRET_KEY before running on an https PUBLIC_BASE.")
    if config.SECURE_COOKIES and not (config.LEGAL_ADDRESS and config.CONTACT_EMAIL != "support@localhost"):
        raise RuntimeError("Set LEGAL_NAME, LEGAL_ADDRESS and CONTACT_EMAIL (and PRIVACY_EMAIL, ABUSE_EMAIL if different) "
                           "before running on an https PUBLIC_BASE. The legal pages and takedown process need them.")
    if config.SECURE_COOKIES and not mailer.configured():
        raise RuntimeError("Set SMTP_HOST (and the other SMTP_* settings) before running on an https PUBLIC_BASE. "
                           "Customers need email to confirm their address and reset a password.")
    if not config.SECRET_KEY:
        print("[warn] SECRET_KEY is not set. Social account connections are disabled until it is.")
    worker.start(pipeline.run_project)
    janitor.start()
    yield


app = Starlette(
    routes=routes,
    middleware=[Middleware(Guard)],
    exception_handlers={HTTPException: http_error, Exception: server_error},
    lifespan=lifespan,
)
