"""Builds docs/landing-preview.html: the landing page as ONE file (fonts and styles inlined) that opens by double-clicking.
Sign-in, live pricing and customer quotes still need the running app.   Run:  python scripts/build_preview.py
"""
import base64
import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
s = (ROOT / "web/index.html").read_text()
css = (ROOT / "web/static/fonts/fonts.css").read_text()


def inline(m):
    return "url(data:font/woff2;base64," + base64.b64encode((ROOT / "web/static/fonts" / m.group(1)).read_bytes()).decode() + ")"


faces = [f for f in re.findall(r"@font-face \{.*?\}", css, flags=re.S) if "latin-ext" not in f]
s = re.sub(r'<link rel="preload"[^>]*>\n?', "", s)
s = s.replace('<link rel="stylesheet" href="/static/fonts/fonts.css" />',
              "<style>\n" + "\n".join(re.sub(r"url\(/static/fonts/([^)]+)\)", inline, f) for f in faces) + "\n</style>")
tokens = {"SEO_TITLE": "Verticlip: turn one long video into a week of shorts",
          "SEO_DESC": "Paste a long video. Verticlip finds the best moments, scores them, adds animated captions and sends them to YouTube Shorts and TikTok.",
          "SEO_PATH": "", "PUBLIC_BASE": "", "MIN_AGE": "16"}
for k, v in tokens.items():
    s = s.replace("{{" + k + "}}", html.escape(v))
icon = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect x='6.5' y='2.5' width='11' "
        "height='19' rx='3' fill='none' stroke='%230e1424' stroke-width='2'/%3E%3Cpath d='M10.5 8.5v7l5-3.5z'/%3E%3C/svg%3E")
s = s.replace('<link rel="icon" href="/assets/verticlip-logo.svg" />', f'<link rel="icon" href="{icon}" />')
s = re.sub(r'<link rel="canonical"[^>]*>\n?|<meta property="og:[^>]*>\n?|<meta name="twitter:[^>]*>\n?|'
           r'<script type="application/ld\+json">.*?</script>\n?', "", s, flags=re.S)
s = s.replace("<head>", "<head>\n<!-- Standalone preview of the Verticlip landing page. Fonts and styles are inlined, so it opens by "
                        "double-clicking. Sign-in, live pricing and customer quotes need the running app. -->", 1)
assert "{{" not in s and "googleapis" not in s
(ROOT / "docs/landing-preview.html").write_text(s)
print("wrote docs/landing-preview.html", len(s) // 1024, "KB")
