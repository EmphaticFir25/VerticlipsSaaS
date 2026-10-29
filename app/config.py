"""Settings, read once from the environment (and .env when present)."""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # optional in development
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# ---------- deployment ----------
PUBLIC_BASE = _env("PUBLIC_BASE", "http://localhost:33214").rstrip("/")
SECURE_COOKIES = PUBLIC_BASE.startswith("https://")
# Only honour CF-Connecting-IP when the app really sits behind Cloudflare; otherwise anyone can forge it.
TRUST_CLOUDFLARE = _env("TRUST_CLOUDFLARE", "false").lower() == "true"

# ---------- who is behind the service (shown on the legal pages) ----------
LEGAL_NAME = _env("LEGAL_NAME", "Verticlip")
LEGAL_ADDRESS = _env("LEGAL_ADDRESS")
CONTACT_EMAIL = _env("CONTACT_EMAIL", "support@localhost")
PRIVACY_EMAIL = _env("PRIVACY_EMAIL") or CONTACT_EMAIL
ABUSE_EMAIL = _env("ABUSE_EMAIL") or CONTACT_EMAIL        # also the designated DMCA agent address
SECURITY_EMAIL = _env("SECURITY_EMAIL") or CONTACT_EMAIL
JURISDICTION = _env("JURISDICTION", "the Commonwealth of Massachusetts, United States")
MIN_AGE = _int("MIN_AGE", 16)
SIGNUPS_PER_HOUR = _int("SIGNUPS_PER_HOUR", 10)   # per network address

# ---------- how long we keep customer data (0 turns a rule off) ----------
SOURCE_RETENTION_DAYS = _int("SOURCE_RETENTION_DAYS", 14)       # the uploaded or downloaded original video
PROJECT_RETENTION_DAYS = _int("PROJECT_RETENTION_DAYS", 90)     # clips, transcript, thumbnails
UNVERIFIED_ACCOUNT_DAYS = _int("UNVERIFIED_ACCOUNT_DAYS", 7)    # accounts that never confirmed their email

# ---------- abuse protection ----------
TURNSTILE_SITE_KEY = _env("TURNSTILE_SITE_KEY")      # Cloudflare Turnstile (free). Both keys set = bot check on sign-up
TURNSTILE_SECRET = _env("TURNSTILE_SECRET")
HIBP_CHECK = _env("HIBP_CHECK", "true").lower() == "true"   # refuse passwords found in known data breaches
BLOCK_DISPOSABLE_EMAIL = _env("BLOCK_DISPOSABLE_EMAIL", "true").lower() == "true"

API_WRITES_PER_MINUTE = _int("API_WRITES_PER_MINUTE", 120)   # per network address, across all write endpoints
SENTRY_DSN = _env("SENTRY_DSN")                              # optional error tracking; personal data is stripped before sending

# ---------- email (verification and password reset) ----------
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or 587)
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
MAIL_FROM = _env("MAIL_FROM", "Verticlip <no-reply@localhost>")
# New accounts must confirm their email before creating projects. On by default once SMTP is configured.
REQUIRE_EMAIL_VERIFICATION = _env("REQUIRE_EMAIL_VERIFICATION", "true" if SMTP_HOST else "false").lower() == "true"
SECRET_KEY = _env("SECRET_KEY")  # signs nothing public; used to encrypt stored OAuth tokens
DATA_DIR = Path(_env("DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "verticlip.db"
MEDIA_DIR = DATA_DIR / "media"
WEB_DIR = ROOT / "web"
ASSETS_DIR = ROOT / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
WORKERS = max(1, _int("WORKERS", 2))
ALLOW_SIGNUPS = _env("ALLOW_SIGNUPS", "true").lower() != "false"

# ---------- cloud AI ----------
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _env("ANTHROPIC_MODEL", "claude-haiku-5-5")
# Thinking depth for Haiku 5.5 (low|medium|high|xhigh|max). Empty sends no effort setting.
ANTHROPIC_EFFORT = _env("ANTHROPIC_EFFORT", "low")
ANTHROPIC_URL = _env("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages")

# Any OpenAI-compatible transcription endpoint with word timestamps (OpenAI whisper-1, Groq, etc.).
TRANSCRIBE_API_KEY = _env("TRANSCRIBE_API_KEY") or _env("OPENAI_API_KEY")
TRANSCRIBE_URL = _env("TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions")
TRANSCRIBE_MODEL = _env("TRANSCRIBE_MODEL", "whisper-1")

# ---------- limits ----------
MAX_SOURCE_MINUTES = _int("MAX_SOURCE_MINUTES", 180)
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 2048)

# ---------- publishing ----------
GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
GOOGLE_CLIENT_SECRETS = _env("GOOGLE_CLIENT_SECRETS")  # optional path to a downloaded client_secret.json
TIKTOK_CLIENT_KEY = _env("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = _env("TIKTOK_CLIENT_SECRET")
YOUTUBE_PRIVACY = _env("YOUTUBE_PRIVACY", "private").lower()
if YOUTUBE_PRIVACY not in ("private", "unlisted", "public"):
    YOUTUBE_PRIVACY = "private"

# ---------- billing ----------
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_TAX = _env("STRIPE_TAX", "false").lower() == "true"
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_PRO = _env("STRIPE_PRICE_PRO")
STRIPE_PRICE_STUDIO = _env("STRIPE_PRICE_STUDIO")

# Monthly allowances. Minutes are minutes of source video processed.
PLANS = {
    "free": {"name": "Free", "price": 0, "minutes": _int("FREE_MINUTES", 30),
             "max_clips": 6, "watermark": True, "brand_logo": False},
    "pro": {"name": "Pro", "price": _int("PRO_PRICE_USD", 19), "minutes": _int("PRO_MINUTES", 600),
            "max_clips": 15, "watermark": False, "brand_logo": True},
    "studio": {"name": "Studio", "price": _int("STUDIO_PRICE_USD", 49), "minutes": _int("STUDIO_MINUTES", 1200),
               "max_clips": 25, "watermark": False, "brand_logo": True},
}


def stripe_price(plan: str) -> str:
    return {"pro": STRIPE_PRICE_PRO, "studio": STRIPE_PRICE_STUDIO}.get(plan, "")


def plan_for_price(price_id: str) -> str | None:
    for plan in ("pro", "studio"):
        if price_id and stripe_price(plan) == price_id:
            return plan
    return None


def google_credentials() -> tuple[str, str]:
    """Client id/secret from env, or from a downloaded client_secret.json."""
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        return GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
    if GOOGLE_CLIENT_SECRETS and Path(GOOGLE_CLIENT_SECRETS).is_file():
        import json
        raw = json.loads(Path(GOOGLE_CLIENT_SECRETS).read_text())
        block = raw.get("web") or raw.get("installed") or {}
        return block.get("client_id", ""), block.get("client_secret", "")
    return "", ""
