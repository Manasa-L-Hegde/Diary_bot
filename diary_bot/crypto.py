"""Encryption at rest for audio files and backups (Fernet = AES-128-CBC + HMAC-SHA256).

Run `python -m diary_bot.crypto genkey` to create a key.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_FILE_RE = re.compile(r"^[0-9a-f]{64}\.enc$")


class Vault:
    def __init__(self, key: str):
        try:
            self._f = Fernet(key.encode())
        except Exception as e:  # malformed key
            raise ValueError("DIARY_ENC_KEY is not a valid key; generate one with genkey") from e

    def encrypt(self, data: bytes) -> bytes:
        return self._f.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        try:
            return self._f.decrypt(token)
        except InvalidToken as e:
            raise ValueError("Decryption failed: wrong key or corrupted file") from e


def generate_key() -> str:
    return Fernet.generate_key().decode()


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def store_audio(vault: Vault, audio_dir: Path, data: bytes) -> tuple[str, str]:
    """Encrypt and store audio. Returns (sha256 of plaintext, stored filename)."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    name = f"{sha}.enc"
    target = audio_dir / name
    if not target.exists():
        _atomic_write(target, vault.encrypt(data))
    return sha, name


def load_audio(vault: Vault, audio_dir: Path, filename: str) -> bytes:
    if not _FILE_RE.match(filename):  # blocks path traversal
        raise ValueError("invalid audio filename")
    return vault.decrypt((audio_dir / filename).read_bytes())


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "genkey":
        print(generate_key())
    else:
        print("usage: python -m diary_bot.crypto genkey")
