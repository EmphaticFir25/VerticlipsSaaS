FROM python:3.12-slim

# ffmpeg cuts and renders clips (libass burns the captions).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY assets ./assets

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data
VOLUME ["/app/data"]
EXPOSE 33214
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:33214/healthz', timeout=4)" || exit 1

# One process: the background workers live inside it and share its SQLite database.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "33214", "--proxy-headers", "--forwarded-allow-ips", "*"]
