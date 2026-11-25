"""Small shared helpers: money, text cleaning."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

KANNADA_RE = re.compile(r"[ಀ-೿]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def has_kannada(text: str | None) -> bool:
    return bool(text and KANNADA_RE.search(text))


def to_paise(value) -> int:
    """Convert a rupee amount (int/float/str) to integer paise. Money is never stored as float."""
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as e:
        raise ValueError(f"not a number: {value!r}") from e
    if not d.is_finite():
        raise ValueError("amount must be finite")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_inr(paise: int) -> str:
    """Indian digit grouping: 12345678 paise -> '1,23,456.78'."""
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(int(paise)), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{sign}{s}.{p:02d}"


def clean_text(value, limit: int) -> str | None:
    """Strip control chars, collapse whitespace, cap length. Empty -> None."""
    if value is None:
        return None
    s = _CTRL_RE.sub("", str(value))
    s = " ".join(s.split())[:limit]
    return s or None
