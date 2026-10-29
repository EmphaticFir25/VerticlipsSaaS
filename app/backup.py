"""Encrypted backups of the database (and optionally clips and logos).

  python -m app.backup create            write an encrypted backup into BACKUP_DIR and prune old ones
  python -m app.backup verify <file>     decrypt it and check the database inside is healthy
  python -m app.backup restore <file> <target_dir>
  python -m app.backup loop              create one every 24 hours (used by the compose "backup" service)

BACKUP_PASSPHRASE protects the files. Keep it somewhere safe that is NOT on this server: without it a backup is unreadable.
Copy BACKUP_DIR off the machine too (rclone, rsync, S3), or one dead disk still loses everything.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config

MAGIC = b"VCBK1"
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", str(config.DATA_DIR.parent / "backups")))
KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "14") or 14)
INCLUDE_MEDIA = os.environ.get("BACKUP_INCLUDE_MEDIA", "false").lower() == "true"


def _key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase or len(passphrase) < 12:
        raise SystemExit("Set BACKUP_PASSPHRASE to at least 12 characters.")
    return base64.urlsafe_b64encode(hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 15, r=8, p=1, dklen=32, maxmem=2 ** 27))


def create_backup(dest_dir: Path, passphrase: str, include_media: bool = False, now: float | None = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "verticlip.db"
        src = sqlite3.connect(config.DB_PATH)
        dst = sqlite3.connect(snap)
        with dst:
            src.backup(dst)          # consistent copy even while the app is writing
        src.close(); dst.close()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snap, arcname="verticlip.db")
            brand = config.MEDIA_DIR / "brand"
            if brand.is_dir():
                tar.add(brand, arcname="media/brand")
            if include_media:
                for p in config.MEDIA_DIR.iterdir():
                    if p.name != "brand":
                        tar.add(p, arcname=f"media/{p.name}")
    salt = os.urandom(16)
    blob = MAGIC + salt + Fernet(_key(passphrase, salt)).encrypt(buf.getvalue())
    name = time.strftime("verticlip-%Y%m%d-%H%M%S.bak", time.gmtime(now or time.time()))
    out = dest_dir / name
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    return out


def _open(file: Path, passphrase: str) -> bytes:
    blob = Path(file).read_bytes()
    if not blob.startswith(MAGIC):
        raise SystemExit("That is not a Verticlip backup.")
    salt, token = blob[len(MAGIC):len(MAGIC) + 16], blob[len(MAGIC) + 16:]
    try:
        return Fernet(_key(passphrase, salt)).decrypt(token)
    except InvalidToken:
        raise SystemExit("Wrong passphrase, or the backup is damaged.")


def restore_backup(file: Path, passphrase: str, target: Path) -> Path:
    data = _open(file, passphrase)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(target, filter="data")   # "data" refuses paths that escape the target folder
    return target / "verticlip.db"


def verify_backup(file: Path, passphrase: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db_file = restore_backup(file, passphrase, Path(tmp))
        c = sqlite3.connect(db_file)
        check = c.execute("PRAGMA integrity_check").fetchone()[0]
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        c.close()
    if check != "ok":
        raise SystemExit(f"Database in the backup is damaged: {check}")
    return {"integrity": check, "users": users}


def prune(dest_dir: Path, keep_days: int, now: float | None = None) -> int:
    cutoff = (now or time.time()) - keep_days * 86400
    removed = 0
    for f in dest_dir.glob("verticlip-*.bak"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed


def main(argv: list[str]) -> None:
    pw = os.environ.get("BACKUP_PASSPHRASE", "")
    cmd = argv[0] if argv else ""
    if cmd == "create":
        out = create_backup(BACKUP_DIR, pw, INCLUDE_MEDIA)
        print(f"wrote {out} ({out.stat().st_size} bytes); pruned {prune(BACKUP_DIR, KEEP_DAYS)} old")
        print("verified:", verify_backup(out, pw))
    elif cmd == "verify" and len(argv) > 1:
        print(verify_backup(Path(argv[1]), pw))
    elif cmd == "restore" and len(argv) > 2:
        print("restored database to", restore_backup(Path(argv[1]), pw, Path(argv[2])))
    elif cmd == "loop":
        while True:
            try:
                main(["create"])
            except SystemExit as exc:
                print("[backup] failed:", exc)
            time.sleep(24 * 3600)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
