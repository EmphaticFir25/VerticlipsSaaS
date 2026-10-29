"""Test setup: a throwaway data dir, fake OpenAI/Anthropic endpoints, and a live app server."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="verticlip-test-"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


AI_PORT = free_port()
APP_PORT = free_port()
WEBHOOK_SECRET = "whsec_test"

# Configure before the app is imported: config is read once at import time.
os.environ.update({
    "DATA_DIR": str(TMP / "data"),
    "SECRET_KEY": "test-secret",
    "PUBLIC_BASE": f"http://127.0.0.1:{APP_PORT}",
    "ANTHROPIC_API_KEY": "test-anthropic",
    "ANTHROPIC_URL": f"http://127.0.0.1:{AI_PORT}/v1/messages",
    "OPENAI_API_KEY": "test-openai",
    "TRANSCRIBE_URL": f"http://127.0.0.1:{AI_PORT}/v1/audio/transcriptions",
    "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
    "STRIPE_PRICE_PRO": "price_pro",
    "STRIPE_PRICE_STUDIO": "price_studio",
    "WORKERS": "1",
    "HIBP_CHECK": "false",
    "API_WRITES_PER_MINUTE": "100000",
    "SIGNUPS_PER_HOUR": "10000",
})

SPEECH = ("Most founders think the hard part is the idea. It is not. The hard part is year two. "
          "Nobody tells you that. Here is what we did instead and it changed everything.").split()


class FakeAI(BaseHTTPRequestHandler):
    """Stands in for OpenAI's transcription API and Anthropic's Messages API."""
    calls: list[str] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        FakeAI.calls.append(self.path)
        if self.path.endswith("/audio/transcriptions"):
            assert self.headers.get("authorization") == "Bearer test-openai"
            assert b"timestamp_granularities[]" in body
            words, t = [], 0.0
            while t < 38:
                words.append({"word": SPEECH[len(words) % len(SPEECH)], "start": t, "end": t + 0.3})
                t += 0.4
            out = {"text": "", "words": words}
        else:
            assert self.headers.get("x-api-key") == "test-anthropic"
            req = json.loads(body)
            assert req["tool_choice"]["name"] == "report_clips"
            out = {"content": [{"type": "tool_use", "name": "report_clips", "id": "t1", "input": {"clips": [
                {"start": 1.0, "end": 19, "title": "Year two is the real test", "hook": "Nobody tells you this",
                 "virality": 91, "scores": {"hook": 93, "flow": 88, "value": 90, "trend": 75},
                 "reason": "Contrarian opener with a payoff.", "hashtags": ["startups", "#founder"]},
                {"start": 20.0, "end": 37, "title": "What we did instead", "hook": "", "virality": 72,
                 "scores": {"hook": 70, "flow": 75, "value": 71, "trend": 60}, "reason": "Clear story."},
            ]}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="session")
def sample_video() -> Path:
    path = TMP / "sample.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
                    "-f", "lavfi", "-i", "sine=frequency=330", "-t", "40", "-c:v", "libx264", "-preset",
                    "ultrafast", "-c:a", "aac", str(path)], check=True)
    return path


@pytest.fixture(scope="session")
def server():
    import uvicorn
    ai = ThreadingHTTPServer(("127.0.0.1", AI_PORT), FakeAI)
    threading.Thread(target=ai.serve_forever, daemon=True).start()

    from app.main import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=APP_PORT, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{APP_PORT}"
    srv.should_exit = True
    ai.shutdown()
