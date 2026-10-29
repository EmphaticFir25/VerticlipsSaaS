"""YouTube and TikTok: OAuth connect flows and uploads, per customer. Tokens are stored encrypted."""
from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import requests

from . import auth, config, db

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
TT_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TT_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
TT_CHUNK_MAX = 64 * 1024 * 1024
TT_CHUNK = 10 * 1024 * 1024


class SocialError(RuntimeError):
    pass


def redirect_uri(platform: str) -> str:
    return f"{config.PUBLIC_BASE}/auth/{platform}/callback"


def available() -> dict:
    gid, gsecret = config.google_credentials()
    return {"youtube": bool(gid and gsecret and config.SECRET_KEY),
            "tiktok": bool(config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET and config.SECRET_KEY)}


# ---------- YouTube ----------

def youtube_start(user_id: str) -> str:
    client_id, _ = config.google_credentials()
    if not available()["youtube"]:
        raise SocialError("YouTube connections are not set up on this server yet.")
    state = secrets.token_urlsafe(24)
    db.save_oauth_state(state, user_id, "youtube")
    return GOOGLE_AUTH + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri("youtube"), "response_type": "code",
        "scope": YT_SCOPES, "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        "state": state,
    })


def youtube_callback(user_id: str, code: str) -> None:
    client_id, client_secret = config.google_credentials()
    tok = requests.post(GOOGLE_TOKEN, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri("youtube"), "grant_type": "authorization_code",
    }, timeout=30).json()
    if "access_token" not in tok:
        raise SocialError(f"Google sign-in failed: {tok.get('error_description') or tok.get('error')}")
    if not tok.get("refresh_token"):
        raise SocialError("Google did not grant offline access. Remove Verticlip from your Google account "
                          "permissions and connect again.")
    me = requests.get("https://www.googleapis.com/youtube/v3/channels",
                      params={"part": "snippet", "mine": "true"},
                      headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30).json()
    items = me.get("items") or []
    if not items:
        raise SocialError("That Google account has no YouTube channel. Create one first, then connect again.")
    ch = items[0]
    db.upsert_account(uuid.uuid4().hex[:12], user_id, "youtube", ch["id"], ch["snippet"]["title"],
                      auth.seal({"refresh_token": tok["refresh_token"]}))


def _google_access_token(tokens: dict) -> str:
    client_id, client_secret = config.google_credentials()
    tok = requests.post(GOOGLE_TOKEN, data={
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token",
    }, timeout=30).json()
    if "access_token" not in tok:
        raise SocialError("YouTube access was revoked. Reconnect this channel on the Accounts page.")
    return tok["access_token"]


def upload_youtube(account: dict, clip: dict, path: Path) -> str:
    access = _google_access_token(auth.unseal(account["tokens"]))
    tags = [h.lstrip("#") for h in clip.get("hashtags", [])]
    description = "\n\n".join(x for x in [clip.get("description", ""), " ".join(clip.get("hashtags", [])),
                                          "#Shorts"] if x)
    meta = {
        "snippet": {"title": clip["title"][:95] + " #Shorts" if len(clip["title"]) <= 92 else clip["title"][:100],
                    "description": description[:4900], "tags": tags[:15], "categoryId": "22"},
        "status": {"privacyStatus": config.YOUTUBE_PRIVACY, "selfDeclaredMadeForKids": False},
    }
    size = path.stat().st_size
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        json=meta, timeout=60,
        headers={"Authorization": f"Bearer {access}", "X-Upload-Content-Type": "video/mp4",
                 "X-Upload-Content-Length": str(size)})
    if init.status_code >= 400 or "Location" not in init.headers:
        raise SocialError(f"YouTube refused the upload: {_err(init)}")
    with open(path, "rb") as f:
        put = requests.put(init.headers["Location"], data=f, timeout=1800,
                           headers={"Authorization": f"Bearer {access}", "Content-Type": "video/mp4",
                                    "Content-Length": str(size)})
    if put.status_code >= 400:
        raise SocialError(f"YouTube upload failed: {_err(put)}")
    video_id = put.json().get("id", "")
    return f"https://youtube.com/shorts/{video_id} ({config.YOUTUBE_PRIVACY})"


# ---------- TikTok ----------

def tiktok_start(user_id: str) -> str:
    if not available()["tiktok"]:
        raise SocialError("TikTok connections are not set up on this server yet.")
    state = secrets.token_urlsafe(24)
    db.save_oauth_state(state, user_id, "tiktok")
    return TT_AUTH + "?" + urlencode({
        "client_key": config.TIKTOK_CLIENT_KEY, "scope": "user.info.basic,video.upload",
        "response_type": "code", "redirect_uri": redirect_uri("tiktok"), "state": state,
    })


def _tiktok_token(data: dict) -> dict:
    tok = requests.post(TT_TOKEN, data={"client_key": config.TIKTOK_CLIENT_KEY,
                                        "client_secret": config.TIKTOK_CLIENT_SECRET, **data},
                        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30).json()
    if "access_token" not in tok:
        raise SocialError(f"TikTok sign-in failed: {tok.get('error_description') or tok.get('error') or 'unknown'}")
    return {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
            "expires_at": time.time() + float(tok.get("expires_in", 0)) - 60}


def tiktok_callback(user_id: str, code: str) -> None:
    tokens = _tiktok_token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri("tiktok")})
    info = requests.get("https://open.tiktokapis.com/v2/user/info/", params={"fields": "open_id,display_name"},
                        headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=30).json()
    user = (info.get("data") or {}).get("user") or {}
    if not user.get("open_id"):
        raise SocialError("TikTok did not return the account details.")
    db.upsert_account(uuid.uuid4().hex[:12], user_id, "tiktok", user["open_id"], user.get("display_name", "TikTok"),
                      auth.seal(tokens))


def _tiktok_access(account: dict) -> str:
    tokens = auth.unseal(account["tokens"])
    if tokens.get("expires_at", 0) > time.time():
        return tokens["access_token"]
    if not tokens.get("refresh_token"):
        raise SocialError("TikTok access expired. Reconnect this account on the Accounts page.")
    fresh = _tiktok_token({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    db.set_account_tokens(account["id"], auth.seal(fresh))
    return fresh["access_token"]


def tiktok_chunks(size: int) -> tuple[int, int]:
    """(chunk_size, chunk_count) within TikTok's rules: one chunk up to 64 MB, otherwise 10 MB chunks
    with the remainder folded into the last one."""
    if size <= TT_CHUNK_MAX:
        return size, 1
    return TT_CHUNK, size // TT_CHUNK


def upload_tiktok(account: dict, clip: dict, path: Path) -> str:
    """Content Posting API, inbox mode: the video lands in the creator's TikTok inbox to finish and post.
    Posting straight to the profile needs TikTok's app audit."""
    access = _tiktok_access(account)
    size = path.stat().st_size
    chunk, count = tiktok_chunks(size)
    init = requests.post("https://open.tiktokapis.com/v2/post/publish/inbox/video/init/", timeout=60,
                         headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json; charset=UTF-8"},
                         json={"source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                               "chunk_size": chunk, "total_chunk_count": count}}).json()
    err = init.get("error", {})
    if err.get("code") not in (None, "ok"):
        raise SocialError(f"TikTok rejected the upload: {err.get('message') or err.get('code')}")
    url = init["data"]["upload_url"]
    with open(path, "rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            body = f.read(end - start + 1)
            r = requests.put(url, data=body, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(len(body))})
            if r.status_code >= 400:
                raise SocialError(f"TikTok upload failed with HTTP {r.status_code}.")
    return "Sent to your TikTok inbox. Open TikTok to add a caption and post."


def _err(r: requests.Response) -> str:
    try:
        return r.json().get("error", {}).get("message", r.text[:200])
    except ValueError:
        return r.text[:200]
