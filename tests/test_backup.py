"""Backups must be encrypted, restorable and tamper-evident."""
import sqlite3
import uuid

import pytest

from app import backup, config, db


def test_backup_roundtrip_and_wrong_passphrase(tmp_path):
    uid = uuid.uuid4().hex[:16]
    db.create_user(uid, f"{uid}@example.com", "", "x")
    out = backup.create_backup(tmp_path / "b", "a-long-test-passphrase")
    assert out.read_bytes().startswith(backup.MAGIC)
    assert b"SQLite format" not in out.read_bytes()                      # encrypted, not just copied
    info = backup.verify_backup(out, "a-long-test-passphrase")
    assert info["integrity"] == "ok" and info["users"] >= 1
    restored = backup.restore_backup(out, "a-long-test-passphrase", tmp_path / "restore")
    row = sqlite3.connect(restored).execute("SELECT email FROM users WHERE id = ?", (uid,)).fetchone()
    assert row[0] == f"{uid}@example.com"
    with pytest.raises(SystemExit):
        backup.verify_backup(out, "not-the-right-passphrase")
    db.delete_user_everything(uid)


def test_short_passphrase_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        backup.create_backup(tmp_path / "b", "short")


def test_prune_removes_only_old_backups(tmp_path):
    import os, time
    d = tmp_path / "b"
    d.mkdir()
    old, new = d / "verticlip-old.bak", d / "verticlip-new.bak"
    old.write_bytes(b"x"); new.write_bytes(b"x")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    assert backup.prune(d, 14) == 1 and new.exists() and not old.exists()
