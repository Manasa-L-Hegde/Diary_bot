"""Encrypted backups. Run daily from cron:  python -m diary_bot.backup

* The database is copied with SQLite's online-backup API (safe while the bot runs), then encrypted.
* Encrypted audio files are mirrored (they are already encrypted; never deleted from the mirror).
* anchors.log records the ledger head hashes so truncation of the live DB can be detected later.
* Point DIARY_BACKUP_DIR at a Google Drive / Dropbox synced folder for off-machine safety.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from . import db
from .crypto import Vault, _atomic_write


def make_backup(db_path: Path, audio_dir: Path, backup_dir: Path, vault: Vault, keep: int = 30) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    src = db.connect(db_path)
    fd, tmp_name = tempfile.mkstemp(dir=backup_dir, suffix=".tmpdb")
    os.close(fd)
    try:
        dest = sqlite3.connect(tmp_name)
        src.backup(dest)
        dest.close()
        blob = Path(tmp_name).read_bytes()
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = backup_dir / f"diary-{stamp}.db.enc"
    _atomic_write(out, vault.encrypt(blob))

    mirror = backup_dir / "audio"
    mirror.mkdir(exist_ok=True)
    if audio_dir.exists():
        for f in audio_dir.glob("*.enc"):
            if not (mirror / f.name).exists():
                shutil.copy2(f, mirror / f.name)

    with open(backup_dir / "anchors.log", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": stamp, "file": out.name, "head": db.head(src)}) + "\n")
    src.close()

    for old in sorted(backup_dir.glob("diary-*.db.enc"))[:-keep]:
        old.unlink()
    return out


def restore_backup(enc_file: Path, dest_db: Path, vault: Vault) -> tuple[bool, list[str]]:
    """Decrypt a backup to dest_db (must not exist) and verify its hash chain."""
    if Path(dest_db).exists():
        raise FileExistsError("Refusing to overwrite an existing database")
    Path(dest_db).write_bytes(vault.decrypt(Path(enc_file).read_bytes()))
    conn = db.connect(dest_db)
    try:
        return db.verify_chain(conn)
    finally:
        conn.close()


def main() -> None:
    from .config import load_settings

    s = load_settings()
    out = make_backup(s.db_path, s.audio_dir, s.backup_dir, Vault(s.enc_key))
    print(f"Backup written: {out}")


if __name__ == "__main__":
    main()
