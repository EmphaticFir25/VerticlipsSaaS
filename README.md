# Verticlip SaaS

> Proprietary and confidential. See [LICENSE](LICENSE). Keep this repository private.

Multi-user, cloud-powered version of Verticlip. Customers paste a link or upload a video, and Verticlip:

1. downloads it (yt-dlp) or takes the upload,
2. transcribes it with the **Whisper API** (word-level timestamps, long audio split into 20-minute chunks),
3. has **Claude Haiku** read the transcript and pick standalone moments, each with a 0–100 virality score
   (hook / flow / value / trend), a title, a hook line, a description and hashtags,
4. snaps each moment to whole words and sentence ends, then renders 1080×1920 clips with ffmpeg:
   fill (crop) or fit (blurred background) layout, word-highlight / bold-box / clean captions,
   an on-screen hook title, and the customer's logo (paid) or a "Made with Verticlip" mark (free),
5. publishes chosen clips to the customer's own YouTube channels and TikTok accounts.

The local single-user app stays in [`verticlip`](https://github.com/EmphaticFir25/verticlip). This repo shares no code
with it at runtime.

## What's in it

| Area | Details |
|---|---|
| Accounts | Email + password (scrypt), HTTP-only session cookies, login throttling, per-user data isolation |
| Plans | Free / Pro / Studio with monthly minute allowances, clip caps, parallel-job limits; minutes refunded on failure or cancel |
| Billing | Stripe Checkout, Customer Portal, signed and de-duplicated webhooks |
| Publishing | Per-customer Google and TikTok OAuth, tokens encrypted at rest (Fernet, keyed by `SECRET_KEY`), TikTok token refresh, chunked TikTok uploads, resumable YouTube uploads |
| Jobs | Persistent queue in SQLite, worker threads, restart-safe (in-flight projects re-run), cancel and retry |
| Safety | Anti-forgery header on every write, private-network links refused, upload size and type checks, security headers |
| UI | Landing page with pricing, dashboard, project page with ranked clips and score breakdowns, brand kit with live preview, social accounts, billing, settings |

```
app/
  main.py      routes + API (Starlette)
  pipeline.py  one project end to end
  ai.py        Whisper API + Claude tool-use scoring, snapping, ranking
  media.py     yt-dlp, ffprobe/ffmpeg, captions (ASS), rendering
  social.py    YouTube / TikTok OAuth and uploads
  billing.py   plans, allowances, Stripe
  auth.py      passwords, sessions, throttling, token encryption
  db.py        SQLite
  worker.py    background queue
web/           pages (plain HTML/CSS/JS, no build step)
assets/fonts/  Poppins Bold for captions (OFL)
```

## Run it with Docker

```bash
git clone https://github.com/EmphaticFir25/verticlipsSaaS.git
cd verticlipsSaaS
cp .env.example .env      # fill in PUBLIC_BASE, SECRET_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY
docker compose up -d --build
```

It listens on port **33215** on the host (container port 33214), so it can sit next to the local Verticlip on the same machine.
Put it behind HTTPS (Cloudflare Tunnel, Caddy, etc.) and set `PUBLIC_BASE` to that address. Google and TikTok only accept HTTPS redirect URIs.

Everything (database, uploads, clips) lives in `./data`. Back that folder up.

## Connect the outside services

All of these are optional except the two AI keys. Without them, new projects are refused with a clear message.

- **Anthropic:** `ANTHROPIC_API_KEY`. The model defaults to `claude-haiku-5-5`, and you can change it with `ANTHROPIC_MODEL`.
- **OpenAI Whisper:** `OPENAI_API_KEY`. To use another OpenAI-compatible transcription endpoint, set `TRANSCRIBE_URL`, `TRANSCRIBE_MODEL` and `TRANSCRIBE_API_KEY`.
- **YouTube:** in Google Cloud, create an OAuth client of type *Web application*, enable *YouTube Data API v3*, and add
  `https://YOUR_DOMAIN/auth/youtube/callback` as a redirect URI. Put the id and secret in `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
  While the consent screen is in *Testing*, only listed test users can connect. Public customers need Google's verification
  for the `youtube.upload` scope.
- **TikTok:** in your app's Login Kit → Web, add `https://YOUR_DOMAIN/auth/tiktok/callback`, and enable the Content Posting API.
  Uploads go to the creator's inbox. Direct posting needs TikTok's app audit.
- **Stripe:** create two monthly prices (Pro and Studio) and set `STRIPE_PRICE_PRO` / `STRIPE_PRICE_STUDIO` and
  `STRIPE_SECRET_KEY`. Add a webhook to `https://YOUR_DOMAIN/api/billing/webhook` for `checkout.session.completed`,
  `customer.subscription.updated`, `customer.subscription.deleted` and `invoice.paid`, and put its signing secret in
  `STRIPE_WEBHOOK_SECRET`. Turn on the Customer Portal in Stripe settings.

## Develop

```bash
pip install -r requirements-dev.txt     # also needs ffmpeg on PATH
cp .env.example .env
uvicorn app.main:app --reload --port 33214
pytest
```

The tests run a real server against fake Whisper and Claude endpoints, render real clips with ffmpeg, and check
login, plan limits, per-customer isolation, Stripe signatures and the brand kit.

## Keeping it private and secure

- **Repository:** keep it **private** (GitHub → Settings → General → Danger Zone → Change visibility). Only the server
  code in `app/` runs your logic; the browser only ever receives `web/` and `assets/`.
- **Keys are yours, not the customer's.** The Anthropic, OpenAI/Whisper, Google, TikTok and Stripe credentials live only in
  `.env` on your server. Customers never enter or see them, and no API route returns them.
- **Never commit `.env`** (it is git-ignored and excluded from the Docker image). Rotate any key that was ever pasted in chat,
  an issue or a commit. Set `SECRET_KEY` before going live; the app refuses to start on an https `PUBLIC_BASE` without it.
- **Network:** the container port is bound to `127.0.0.1`, so only your reverse proxy (Caddy, Cloudflare Tunnel) can reach it.
  This matters because the login and sign-up throttles trust the forwarded client address.
- **Data:** `./data` holds password hashes, uploads, and encrypted social tokens. Keep it off public storage and back it up.
- **Billing:** set a monthly spend limit on your Anthropic and OpenAI accounts so a bug or abuse can't run up a bill.

## Scaling notes

This is built to run as **one container** with SQLite and local disk, which is fine for a launch. When you outgrow it:
move clips to S3/R2 with signed URLs, swap SQLite for Postgres, and run the worker as its own process. The job
code in `pipeline.py` doesn't depend on the web layer, so it moves over unchanged. Password reset by email and admin
tooling are not built yet.
