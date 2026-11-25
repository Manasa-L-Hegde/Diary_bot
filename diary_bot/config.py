"""Settings come from environment variables / .env. Nothing secret is ever hard-coded."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    sarvam_api_key: str
    enc_key: str
    claude_model: str
    sarvam_model: str
    data_dir: Path
    backup_dir: Path
    tz: ZoneInfo
    max_voice_seconds: int
    max_notes_per_hour: int
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "diary.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def glossary_user_path(self) -> Path:
        return self.data_dir / "glossary_user.json"


def _need(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise ConfigError(f"Missing required setting: {name} (see .env.example)")
    return v


def load_settings() -> Settings:
    from dotenv import load_dotenv

    load_dotenv()

    raw_ids = _need("ALLOWED_USER_IDS")
    try:
        ids = frozenset(int(x) for x in raw_ids.split(",") if x.strip())
    except ValueError as e:
        raise ConfigError("ALLOWED_USER_IDS must be comma-separated numbers") from e
    if not ids:
        # Refuse to run an open bot that handles financial records.
        raise ConfigError("ALLOWED_USER_IDS is empty; refusing to start an open bot")

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not gemini_key and not anthropic_key:
        raise ConfigError("Missing required setting: GEMINI_API_KEY or ANTHROPIC_API_KEY (see .env.example)")

    data_dir = Path(os.getenv("DIARY_DATA_DIR", "data")).resolve()
    backup_dir = Path(os.getenv("DIARY_BACKUP_DIR", str(data_dir / "backups"))).resolve()
    for d in (data_dir, data_dir / "audio", data_dir / "exports", backup_dir):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass

    return Settings(
        telegram_token=_need("TELEGRAM_BOT_TOKEN"),
        allowed_user_ids=ids,
        sarvam_api_key=_need("SARVAM_API_KEY"),
        enc_key=_need("DIARY_ENC_KEY"),
        claude_model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
        sarvam_model=os.getenv("SARVAM_STT_MODEL", "saaras:v3"),
        data_dir=data_dir,
        backup_dir=backup_dir,
        tz=ZoneInfo(os.getenv("DIARY_TZ", "Asia/Kolkata")),
        max_voice_seconds=int(os.getenv("MAX_VOICE_SECONDS", "30")),
        max_notes_per_hour=int(os.getenv("MAX_NOTES_PER_HOUR", "40")),
        gemini_api_key=gemini_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    )
