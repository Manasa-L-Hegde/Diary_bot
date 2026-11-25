"""Kannada glossary: built-in starter list + terms the family adds via /term."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .utils import clean_text, has_kannada

BASE_PATH = Path(__file__).with_name("glossary.json")


def _load(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def as_prompt_text(user_path: Path | None = None) -> str:
    base = _load(BASE_PATH)
    user = _load(user_path) if user_path else {}
    lines: list[str] = []
    for section in ("terms", "units", "numbers", "dates"):
        items = list(base.get(section, [])) + list(user.get(section, []))
        if items:
            lines.append(f"[{section}]")
            lines += [f"{i['kn']} = {i['en']}" for i in items]
    return "\n".join(lines)


def add_user_term(user_path: Path, kn: str, en: str) -> None:
    kn_c, en_c = clean_text(kn, 60), clean_text(en, 80)
    if not kn_c or not en_c or not has_kannada(kn_c) or has_kannada(en_c):
        raise ValueError("Use the form: Kannada word = English meaning")
    data = _load(user_path) or {}
    terms = data.setdefault("terms", [])
    terms[:] = [t for t in terms if t.get("kn") != kn_c]  # newest wins
    terms.append({"kn": kn_c, "en": en_c})
    user_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = user_path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, user_path)
