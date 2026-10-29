"""Cloud AI: speech-to-text (OpenAI-compatible Whisper API) and moment scoring (Claude Haiku)."""
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Callable

import requests

from . import config

log = logging.getLogger("verticlip.ai")

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class AIError(RuntimeError):
    pass


def _post_with_retry(send: Callable[[], requests.Response], label: str, attempts: int = 4) -> requests.Response:
    delay = 2.0
    for attempt in range(attempts):
        try:
            resp = send()
        except requests.RequestException as exc:
            if attempt == attempts - 1:
                raise AIError(f"{label} could not be reached ({type(exc).__name__}).") from exc
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in RETRY_STATUS or attempt == attempts - 1:
                raise AIError(f"{label} returned HTTP {resp.status_code}: {_error_text(resp)}")
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                delay = max(delay, min(float(retry_after), 60))
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise AIError(f"{label} failed.")


def _error_text(resp: requests.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error", data)
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return str(msg)[:300]
    except ValueError:
        return resp.text[:300]


# ---------- transcription ----------

def transcribe_file(path: Path, offset: float = 0.0) -> list[dict]:
    """Word-level transcript of one audio file. Timestamps are shifted by `offset` seconds."""
    if not config.TRANSCRIBE_API_KEY:
        raise AIError("Transcription is not configured. Set OPENAI_API_KEY (or TRANSCRIBE_API_KEY).")

    def send():
        with open(path, "rb") as f:
            return requests.post(
                config.TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {config.TRANSCRIBE_API_KEY}"},
                data=[("model", config.TRANSCRIBE_MODEL), ("response_format", "verbose_json"),
                      ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")],
                files={"file": (path.name, f, "audio/mpeg")},
                timeout=600,
            )

    data = _post_with_retry(send, "Transcription").json()
    return words_from_response(data, offset)


def words_from_response(data: dict, offset: float = 0.0) -> list[dict]:
    """Words with absolute timestamps. Falls back to spreading segment text evenly when an
    endpoint returns segments without word timings."""
    words = []
    for w in data.get("words") or []:
        text = str(w.get("word", "")).strip()
        if text:
            words.append({"start": float(w["start"]) + offset, "end": float(w["end"]) + offset, "word": text})
    if words:
        return words
    for seg in data.get("segments") or []:
        tokens = str(seg.get("text", "")).split()
        if not tokens:
            continue
        s, e = float(seg["start"]), float(seg["end"])
        step = max(e - s, 0.01) / len(tokens)
        for i, tok in enumerate(tokens):
            words.append({"start": s + i * step + offset, "end": s + (i + 1) * step + offset, "word": tok})
    return words


# ---------- moment scoring ----------

CLIP_TOOL = {
    "name": "report_clips",
    "description": "Report the best standalone short-form clips found in the transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number", "description": "Start time in seconds, taken from a transcript timestamp."},
                        "end": {"type": "number", "description": "End time in seconds."},
                        "title": {"type": "string", "description": "Catchy title, at most 70 characters."},
                        "hook": {"type": "string", "description": "On-screen hook text for the first seconds, 3 to 8 words."},
                        "virality": {"type": "integer", "minimum": 0, "maximum": 100},
                        "scores": {
                            "type": "object",
                            "properties": {
                                "hook": {"type": "integer", "minimum": 0, "maximum": 100},
                                "flow": {"type": "integer", "minimum": 0, "maximum": 100},
                                "value": {"type": "integer", "minimum": 0, "maximum": 100},
                                "trend": {"type": "integer", "minimum": 0, "maximum": 100},
                            },
                            "required": ["hook", "flow", "value", "trend"],
                        },
                        "reason": {"type": "string", "description": "One sentence on why this works as a short."},
                        "description": {"type": "string", "description": "Post description, one or two sentences."},
                        "hashtags": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
                    },
                    "required": ["start", "end", "title", "hook", "virality", "scores", "reason"],
                },
            }
        },
        "required": ["clips"],
    },
}

SYSTEM_PROMPT = (
    "You are a senior short-form video editor who finds the moments in long videos that perform "
    "best as YouTube Shorts and TikToks. You judge: hook (do the first 3 seconds stop the scroll), "
    "flow (does it make sense on its own, with a clear beginning and payoff), value (insight, humor, "
    "emotion or surprise), and trend (fits what audiences currently share). Be honest with scores: "
    "most moments are 40-75, only exceptional ones go above 90."
)

WINDOW_SECONDS = 45 * 60
WINDOW_OVERLAP = 60


def transcript_lines(words: list[dict], max_words: int = 28, gap: float = 1.2) -> list[dict]:
    """Group words into short timestamped lines, breaking at sentence ends and pauses."""
    lines: list[dict] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        if cur and (w["start"] - cur[-1]["end"] > gap or len(cur) >= max_words):
            lines.append(cur)
            cur = []
        cur.append(w)
        if w["word"][-1:] in ".?!" and len(cur) >= 4:
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return [{"start": ln[0]["start"], "end": ln[-1]["end"], "text": " ".join(x["word"] for x in ln)} for ln in lines]


def claude_tool_call(system: str, user: str, tool: dict, max_tokens: int = 4096) -> dict:
    """One Messages API call that must answer through `tool`. Returns the tool input."""
    if not config.ANTHROPIC_API_KEY:
        raise AIError("Clip scoring is not configured. Set ANTHROPIC_API_KEY.")
    body = {
        "model": config.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": user}],
    }
    if config.ANTHROPIC_EFFORT:
        body["output_config"] = {"effort": config.ANTHROPIC_EFFORT}
    headers = {"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    resp = _post_with_retry(lambda: requests.post(config.ANTHROPIC_URL, headers=headers,
                                                  data=json.dumps(body), timeout=300), "Claude")
    data = resp.json()
    usage = data.get("usage") or {}
    log.info("claude usage model=%s input_tokens=%s output_tokens=%s", data.get("model", config.ANTHROPIC_MODEL),
             usage.get("input_tokens"), usage.get("output_tokens"))
    for block in data.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool["name"]:
            return block.get("input") or {}
    raise AIError("Claude did not return clip data.")


def find_moments(words: list[dict], min_len: int, max_len: int, max_clips: int,
                 call: Callable[[str, str, dict], dict] = claude_tool_call) -> list[dict]:
    """Score the transcript window by window and return the best non-overlapping clips."""
    if not words:
        return []
    lines = transcript_lines(words)
    total = words[-1]["end"]
    per_window = max(4, math.ceil(max_clips * 1.5 * min(1.0, WINDOW_SECONDS / max(total, 1))))
    candidates: list[dict] = []
    start = words[0]["start"]
    while start < total:
        end = start + WINDOW_SECONDS
        chunk = [ln for ln in lines if start <= ln["start"] < end]
        if chunk:
            text = "\n".join(f"[{ln['start']:.1f}] {ln['text']}" for ln in chunk)
            prompt = (
                f"Find up to {per_window} moments in this transcript that would work as standalone "
                f"vertical shorts between {min_len} and {max_len} seconds long. Each clip must start "
                f"right where a strong first line begins and end after the payoff, not mid-thought. "
                f"Use the bracketed timestamps (seconds) for start and end.\n\n"
                f"<transcript>\n{text}\n</transcript>"
            )
            result = call(SYSTEM_PROMPT, prompt, CLIP_TOOL)
            candidates += clean_candidates(result.get("clips", []))
        if end >= total:
            break
        start = end - WINDOW_OVERLAP
    return pick_moments([snap(c, words, min_len, max_len) for c in candidates], max_clips)


def _num(v, default: float = 0.0) -> float:
    try:
        n = float(v)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def clean_candidates(raw) -> list[dict]:
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        s, e = _num(c.get("start"), -1), _num(c.get("end"), -1)
        if s < 0 or e <= s:
            continue
        scores = c.get("scores") if isinstance(c.get("scores"), dict) else {}
        out.append({
            "start": s, "end": e,
            "title": str(c.get("title") or "Untitled clip")[:90],
            "hook": str(c.get("hook") or "")[:80],
            "virality": int(max(0, min(100, _num(c.get("virality"))))),
            "scores": {k: int(max(0, min(100, _num(scores.get(k))))) for k in ("hook", "flow", "value", "trend")},
            "reason": str(c.get("reason") or "")[:300],
            "description": str(c.get("description") or "")[:500],
            "hashtags": [("#" + str(h).lstrip("#").replace(" ", ""))[:40]
                         for h in (c.get("hashtags") or []) if str(h).strip()][:6],
        })
    return out


def snap(c: dict, words: list[dict], min_len: int, max_len: int) -> dict:
    """Align a clip to word boundaries and keep it within [min_len, max_len], preferring sentence ends."""
    inside = [w for w in words if w["start"] >= c["start"] - 0.4]
    if not inside:
        return {**c, "start": c["start"], "end": c["start"]}
    s = inside[0]["start"]
    hard_end = s + max_len
    target = min(c["end"], hard_end)
    usable = [w for w in inside if w["end"] <= hard_end + 0.05]
    if not usable:
        return {**c, "start": s, "end": s}
    # Last word ending at or before the model's end; extend to a sentence end if the clip is too short.
    end_word = max((w for w in usable if w["end"] <= target + 0.4), key=lambda w: w["end"], default=usable[0])
    e = end_word["end"]
    if e - s < min_len or end_word["word"][-1:] not in ".?!":
        later = [w for w in usable if w["end"] >= max(e, s + min_len) and w["word"][-1:] in ".?!"]
        if later:
            e = later[0]["end"]
        elif e - s < min_len:
            e = usable[-1]["end"]
    return {**c, "start": round(s, 2), "end": round(min(e + 0.25, hard_end), 2)}


def pick_moments(candidates: list[dict], max_clips: int, min_seconds: float = 8.0) -> list[dict]:
    """Highest virality first, skipping clips that are too short or overlap one already picked."""
    picked: list[dict] = []
    for cand in sorted(candidates, key=lambda x: x.get("virality", 0), reverse=True):
        s, e = cand["start"], cand["end"]
        if e - s < min_seconds:
            continue
        if any(s < p["end"] - 1 and e > p["start"] + 1 for p in picked):
            continue
        picked.append(cand)
        if len(picked) >= max_clips:
            break
    return picked


def ai_status() -> dict:
    return {"scoring": bool(config.ANTHROPIC_API_KEY), "transcription": bool(config.TRANSCRIBE_API_KEY),
            "model": config.ANTHROPIC_MODEL}

