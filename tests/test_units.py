"""Pure-function tests: auth, AI response handling, moment picking, captions, billing."""
import hashlib
import hmac
import time

import pytest

from app import ai, auth, billing, config, media, pipeline, social


def words_from(text, gap=0.4):
    return [{"start": i * gap, "end": i * gap + 0.3, "word": w} for i, w in enumerate(text.split())]


def test_password_hashing():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct" not in h
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")


def test_token_sealing_round_trip():
    blob = auth.seal({"refresh_token": "abc"})
    assert "abc" not in blob
    assert auth.unseal(blob) == {"refresh_token": "abc"}


def test_throttle():
    t = auth.Throttle(limit=2, window=60)
    assert not t.blocked("k")
    t.fail("k"); t.fail("k")
    assert t.blocked("k")
    t.clear("k")
    assert not t.blocked("k")


def test_words_from_response_uses_offsets_and_segment_fallback():
    w = ai.words_from_response({"words": [{"word": " hi", "start": 1, "end": 1.2}]}, offset=100)
    assert w == [{"start": 101.0, "end": 101.2, "word": "hi"}]
    w = ai.words_from_response({"segments": [{"start": 0, "end": 2, "text": "one two"}]}, offset=10)
    assert [x["word"] for x in w] == ["one", "two"] and w[0]["start"] == 10 and w[1]["end"] == 12


def test_clean_candidates_drops_bad_entries_and_clamps():
    out = ai.clean_candidates([
        {"start": 5, "end": 2}, "nope", {"start": "x", "end": 9},
        {"start": 1, "end": 20, "virality": 140, "scores": {"hook": -5}, "hashtags": ["a b", "#c", ""]},
    ])
    assert len(out) == 1
    c = out[0]
    assert c["virality"] == 100 and c["scores"]["hook"] == 0 and c["hashtags"] == ["#ab", "#c"]


def test_snap_lands_on_words_and_sentence_ends():
    text = " ".join(f"w{i}." if i % 10 == 9 else f"w{i}" for i in range(200))
    words = words_from(text)
    c = ai.snap({"start": 3.1, "end": 14.0}, words, min_len=10, max_len=30)
    starts = {round(w["start"], 2) for w in words}
    assert round(c["start"], 2) in starts
    last = max((w for w in words if w["end"] <= c["end"]), key=lambda w: w["end"])
    assert last["word"].endswith(".")
    assert 10 <= c["end"] - c["start"] <= 30


def test_snap_respects_max_length():
    words = words_from(" ".join(["word"] * 300))
    c = ai.snap({"start": 0, "end": 100}, words, min_len=15, max_len=30)
    assert c["end"] - c["start"] <= 30


def test_pick_moments_avoids_overlap_and_short_clips():
    picked = ai.pick_moments([
        {"start": 0, "end": 20, "virality": 50},
        {"start": 10, "end": 30, "virality": 90},
        {"start": 40, "end": 44, "virality": 99},
        {"start": 50, "end": 70, "virality": 70},
    ], max_clips=5)
    assert [p["virality"] for p in picked] == [90, 70]


def test_find_moments_windows_long_transcripts():
    words = words_from(" ".join(f"w{i}." for i in range(9000)))  # about an hour
    calls = []

    def fake(system, prompt, tool):
        calls.append(prompt)
        return {"clips": []}
    ai.find_moments(words, 20, 60, 5, call=fake)
    assert len(calls) == 2 and all("<transcript>" in p for p in calls)


def test_build_ass_escapes_and_highlights():
    words = [{"start": 1, "end": 1.3, "word": "hi{\\b1}"}, {"start": 1.4, "end": 1.7, "word": "there"}]
    out = media.build_ass(words, 0.5, 5, "karaoke", "#112233", hook="Big {news}")
    assert "{\\b1}" not in out and "HI" in out
    assert "\\c&H332211&" in out
    assert "Hook,,0,0,0,,{\\fad(150,250)}Big news" in out
    assert out.count("Dialogue: 0,") == 2  # one event per spoken word in karaoke mode


def test_ass_colors():
    assert media.ass_color("#FF8000") == "&H000080FF"
    assert media.ass_override("#FF8000") == "&H0080FF&"
    assert media.ass_color("junk") == media.ass_color("#FFE14D")


@pytest.mark.parametrize("url", ["ftp://x.com/a", "http://localhost/a", "https://printer.local/a", "", "javascript:x"])
def test_validate_url_rejects(url):
    with pytest.raises(media.MediaError):
        media.validate_url(url, resolve=False)


def test_validate_url_blocks_private_ips():
    for url in ("http://127.0.0.1/a", "http://192.168.1.10/a", "http://[::1]/a", "http://169.254.169.254/latest"):
        with pytest.raises(media.MediaError):
            media.validate_url(url)


def test_clean_settings_caps_to_plan_and_fills_from_brand():
    s = pipeline.clean_settings({"max_clips": 99, "length": "long", "layout": "bogus"}, config.PLANS["free"],
                                {"caption_style": "bold", "layout": "fit"})
    assert s["max_clips"] == 6 and (s["min_len"], s["max_len"]) == (60, 90)
    assert s["caption_style"] == "bold" and s["layout"] == "fit"


def test_stripe_signature():
    payload = b'{"id":"evt"}'
    now = time.time()
    t = int(now)
    sig = hmac.new(b"sec", f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    assert billing.verify_signature(payload, f"t={t},v1=nope,v1={sig}", "sec", now=now)
    assert not billing.verify_signature(payload, f"t={t},v1={sig}", "other", now=now)
    assert not billing.verify_signature(payload, f"t={t - 1000},v1={sig}", "sec", now=now)


def test_minutes_rounding():
    assert billing.minutes_for(1) == 1 and billing.minutes_for(60) == 1 and billing.minutes_for(61) == 2


def test_tiktok_chunking():
    assert social.tiktok_chunks(5_000_000) == (5_000_000, 1)
    size = 150 * 1024 * 1024
    chunk, count = social.tiktok_chunks(size)
    assert chunk == 10 * 1024 * 1024 and count == 15
    assert size - chunk * (count - 1) < 2 * chunk  # last chunk absorbs the remainder within limits
