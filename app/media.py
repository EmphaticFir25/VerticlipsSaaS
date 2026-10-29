"""Video plumbing: URL safety, yt-dlp, ffprobe/ffmpeg, captions (ASS) and 9:16 rendering."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import config

MAX_URL_LEN = 2048
AUDIO_CHUNK_SECONDS = 1200  # 20 min of 48 kbps mono mp3 is about 7 MB, well under the 25 MB API limit
CAPTION_STYLES = ("karaoke", "bold", "clean")
LAYOUTS = ("fill", "fit")


class MediaError(RuntimeError):
    pass


def trim(text: str, limit: int = 600) -> str:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip()]
    return "\n".join(lines[-8:])[-limit:]


def run(cmd: list[str], label: str, cwd: Optional[Path] = None, timeout: int = 7200) -> str:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise MediaError(f"{cmd[0]} is not installed on the server.")
    except subprocess.TimeoutExpired:
        raise MediaError(f"{label} took too long and was stopped.")
    if proc.returncode != 0:
        raise MediaError(f"{label} failed: {trim(proc.stderr)}")
    return proc.stdout


# ---------- URL safety ----------

def validate_url(url: str, resolve: bool = True) -> str:
    """Accept only public http(s) URLs. Blocks localhost and private networks so a customer
    cannot point the downloader at the server's own network."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if len(url) > MAX_URL_LEN or parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise MediaError("Enter a full http:// or https:// video link.")
    host = parsed.hostname
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise MediaError("That link points to a private network.")
    if resolve:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise MediaError("That link's website could not be found.")
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise MediaError("That link points to a private network.")
    return url


# ---------- probing & download ----------

def remote_info(url: str) -> dict:
    """Title, duration and thumbnail for a link, without downloading the video."""
    out = run([sys.executable, "-m", "yt_dlp", "--dump-single-json", "--no-playlist", "--skip-download",
               "--no-warnings", "--", url], "Reading the link", timeout=120)
    try:
        info = json.loads(out)
    except ValueError:
        raise MediaError("Could not read that link.")
    return {"title": str(info.get("title") or "")[:200], "duration": float(info.get("duration") or 0),
            "is_live": bool(info.get("is_live"))}


def download(url: str, out_dir: Path) -> Path:
    out = out_dir / "source.mp4"
    run([sys.executable, "-m", "yt_dlp", "-f", "bv*[height<=1080][vcodec!=none]+ba/b[height<=1080]/b",
         "--merge-output-format", "mp4", "--no-playlist", "--no-warnings", "--no-part",
         "-o", str(out), "--", url], "Downloading the video")
    if not out.exists():
        raise MediaError("The download finished but produced no video file.")
    return out


def probe(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
              "Reading the video", timeout=120)
    data = json.loads(out or "{}")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def thumbnail(src: Path, out: Path, at: float, width: int = 640) -> bool:
    try:
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(at, 0):.2f}", "-i", str(src),
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)], "Thumbnail", timeout=120)
    except MediaError:
        return False
    return out.exists()


def audio_chunks(src: Path, out_dir: Path) -> list[tuple[Path, float]]:
    """Split the audio into mono mp3 chunks for the transcription API. Returns (path, offset) pairs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("audio_*.mp3"):
        old.unlink()
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", "48k", "-f", "segment", "-segment_time", str(AUDIO_CHUNK_SECONDS),
         "-reset_timestamps", "1", str(out_dir / "audio_%03d.mp3")], "Extracting audio")
    chunks, offset = [], 0.0
    for path in sorted(out_dir.glob("audio_*.mp3")):
        chunks.append((path, offset))
        offset += probe(path)["duration"]
    if not chunks:
        raise MediaError("The video has no audio to transcribe.")
    return chunks


# ---------- captions ----------

def ass_color(hex_color: str, alpha: str = "00") -> str:
    """#RRGGBB -> &HAABBGGRR"""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hex_color or "")
    rgb = m.group(1) if m else "FFE14D"
    return f"&H{alpha}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}".upper()


def ass_override(hex_color: str) -> str:
    """#RRGGBB -> &HBBGGRR& for inline colour tags."""
    return ass_color(hex_color)[:2] + ass_color(hex_color)[4:] + "&"


def _ts(t: float) -> str:
    t = max(t, 0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    # Braces start ASS override tags and backslashes escape; strip them from spoken text.
    return re.sub(r"[{}\\\r\n]", "", text).strip()


def build_ass(words: list[dict], start: float, end: float, style: str = "karaoke",
              highlight: str = "#FFE14D", hook: str = "", hook_seconds: float = 3.5) -> str:
    """ASS subtitles for one clip: word-timed captions plus an optional hook title at the top."""
    hl = ass_color(highlight)
    hl_tag = ass_override(highlight)
    if style == "bold":
        cap_style = f"Style: Cap,Poppins,84,&H00111111,&H000000FF,{hl},&H00000000,1,0,3,10,0,2,70,70,420,1"
        per_line = 3
    elif style == "clean":
        cap_style = "Style: Cap,Poppins,74,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,1,4,2,2,70,70,420,1"
        per_line = 5
    else:  # karaoke: white words, the spoken word lights up
        cap_style = "Style: Cap,Poppins,92,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,1,6,3,2,70,70,440,1"
        per_line = 3
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"{cap_style}\n"
        f"Style: Hook,Poppins,70,&H00111111,&H000000FF,&H00FFFFFF,&H00000000,1,0,3,18,0,8,90,90,260,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    if hook.strip():
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(min(hook_seconds, end - start))},Hook,,0,0,0,,"
                      f"{{\\fad(150,250)}}{_clean(hook)}")
    inside = [w for w in words if start <= w["start"] < end]
    for i in range(0, len(inside), per_line):
        group = inside[i:i + per_line]
        texts = [_clean(w["word"]).upper() for w in group]
        if not any(texts):
            continue
        g_end = group[-1]["end"] - start
        if style == "karaoke":
            for j, w in enumerate(group):
                s = w["start"] - start
                e = group[j + 1]["start"] - start if j + 1 < len(group) else g_end
                line = " ".join(f"{{\\c{hl_tag}}}{t}{{\\c&HFFFFFF&}}" if k == j else t
                                for k, t in enumerate(texts))
                events.append(f"Dialogue: 0,{_ts(s)},{_ts(max(e, s + 0.05))},Cap,,0,0,0,,{line}")
        else:
            s = group[0]["start"] - start
            events.append(f"Dialogue: 0,{_ts(s)},{_ts(g_end)},Cap,,0,0,0,,{' '.join(texts)}")
    return header + "\n".join(events) + "\n"


# ---------- render ----------

def ff_escape(path: Path) -> str:
    """Escape a path for use as an ffmpeg filter option value."""
    return path.as_posix().replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


def video_filters(layout: str, has_logo: bool, watermark: bool, captions: Optional[str]) -> tuple[str, str]:
    """Build the filter_complex graph. Returns (graph, output label)."""
    parts = []
    if layout == "fit":
        parts.append("[0:v]split=2[bgs][fgs]")
        parts.append("[bgs]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                     "boxblur=24:4,eq=brightness=-0.08[bg]")
        parts.append("[fgs]scale=1080:1920:force_original_aspect_ratio=decrease[fg]")
        parts.append("[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    else:
        parts.append("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1[v0]")
    last = "[v0]"
    if has_logo:
        parts.append("[1:v]scale=150:-1[lg]")
        parts.append(f"{last}[lg]overlay=W-w-48:60[v1]")
        last = "[v1]"
    if captions:
        parts.append(f"{last}subtitles={captions}:fontsdir='{ff_escape(config.FONTS_DIR)}'[v2]")
        last = "[v2]"
    if watermark:
        font = ff_escape(config.FONTS_DIR / "Poppins-Bold.ttf")
        parts.append(f"{last}drawtext=fontfile='{font}':text='Made with Verticlip':fontsize=34:"
                     f"fontcolor=white@0.85:borderw=3:bordercolor=black@0.55:x=(w-text_w)/2:y=h-150[v3]")
        last = "[v3]"
    return ";".join(parts), last


def render_clip(source: Path, out: Path, start: float, end: float, *, layout: str = "fill",
                logo: Optional[Path] = None, watermark: bool = False, ass_name: Optional[str] = None) -> None:
    graph, last = video_filters(layout, logo is not None, watermark, ass_name)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
           "-i", str(source)]
    if logo is not None:
        cmd += ["-i", str(logo)]
    cmd += ["-filter_complex", graph, "-map", last, "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            out.name]
    run(cmd, "Rendering the clip", cwd=out.parent)
    if not out.exists():
        raise MediaError("Rendering finished but produced no clip.")
