"""Append-only, hash-chained SQLite ledger.

Design goals ("should not get worn out"):
  * Nothing is ever UPDATEd or DELETEd. Triggers abort any attempt.
  * Edits and confirmations are NEW VERSIONS of an entry; history is kept forever.
  * Every row stores a hash of (previous row hash + its own contents). If anyone alters
    or removes a row outside the app, verify_chain() reports it.
  * Money is stored as integer paise, never floats.
  * The original Kannada transcript (and encrypted audio reference) is stored with
    every recording, so entries can be re-derived with better models in future.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

GENESIS = "0" * 64
OPENING = "Opening Balance"  # system category: cash in hand when tracking started (not "income")

REC_FIELDS = [
    "received_at", "telegram_user_id", "source", "audio_sha256",
    "audio_file", "duration_ms", "stt_provider", "transcript_kn",
]
ENTRY_FIELDS = [
    "entry_id", "version", "recording_id", "status", "entry_date", "direction",
    "amount_paise", "item_en", "item_kn", "category", "party", "notes_en",
    "amount_spoken_kn", "flags", "created_at",
]
AMENDABLE = {
    "entry_date", "direction", "amount_paise", "item_en", "item_kn",
    "category", "party", "notes_en",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  telegram_user_id INTEGER NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('voice','text')),
  audio_sha256 TEXT,
  audio_file TEXT,
  duration_ms INTEGER,
  stt_provider TEXT,
  transcript_kn TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  recording_id INTEGER NOT NULL REFERENCES recordings(id),
  status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected')),
  entry_date TEXT NOT NULL,
  direction TEXT NOT NULL CHECK(direction IN ('expense','income')),
  amount_paise INTEGER NOT NULL CHECK(amount_paise >= 0),
  item_en TEXT NOT NULL,
  item_kn TEXT,
  category TEXT NOT NULL,
  party TEXT,
  notes_en TEXT,
  amount_spoken_kn TEXT,
  flags TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  row_hash TEXT NOT NULL,
  UNIQUE(entry_id, version)
);
CREATE INDEX IF NOT EXISTS idx_entry_date ON entry_versions(entry_date);
CREATE INDEX IF NOT EXISTS idx_entry_rec ON entry_versions(recording_id);

CREATE TRIGGER IF NOT EXISTS recordings_no_update BEFORE UPDATE ON recordings
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS recordings_no_delete BEFORE DELETE ON recordings
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS entry_versions_no_update BEFORE UPDATE ON entry_versions
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS entry_versions_no_delete BEFORE DELETE ON entry_versions
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(prev: str, fields: dict) -> str:
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{prev}|{payload}".encode("utf-8")).hexdigest()


def connect(path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")  # serialises writers so the hash chain stays linear
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _last_hash(conn, table: str) -> str:
    row = conn.execute(f"SELECT row_hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    return row["row_hash"] if row else GENESIS


def add_recording(conn, *, telegram_user_id: int, source: str, transcript_kn: str,
                  audio_sha256=None, audio_file=None, duration_ms=None,
                  stt_provider=None, received_at=None) -> int:
    fields = {
        "received_at": received_at or _now(),
        "telegram_user_id": int(telegram_user_id),
        "source": source,
        "audio_sha256": audio_sha256,
        "audio_file": audio_file,
        "duration_ms": duration_ms,
        "stt_provider": stt_provider,
        "transcript_kn": transcript_kn,
    }
    with tx(conn):
        prev = _last_hash(conn, "recordings")
        row_hash = _hash(prev, fields)
        cols = REC_FIELDS + ["prev_hash", "row_hash"]
        vals = [fields[c] for c in REC_FIELDS] + [prev, row_hash]
        cur = conn.execute(
            f"INSERT INTO recordings({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals
        )
        return cur.lastrowid


def _insert_version(conn, fields: dict) -> None:
    prev = _last_hash(conn, "entry_versions")
    row_hash = _hash(prev, fields)
    cols = ENTRY_FIELDS + ["prev_hash", "row_hash"]
    vals = [fields[c] for c in ENTRY_FIELDS] + [prev, row_hash]
    conn.execute(
        f"INSERT INTO entry_versions({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals
    )


def add_entries(conn, recording_id: int, entries: list[dict]) -> list[int]:
    """Insert new entries as version 1, status 'pending'. Returns the new entry ids."""
    ids: list[int] = []
    with tx(conn):
        nxt = conn.execute("SELECT COALESCE(MAX(entry_id),0)+1 AS n FROM entry_versions").fetchone()["n"]
        for e in entries:
            _insert_version(conn, {
                "entry_id": nxt, "version": 1, "recording_id": recording_id,
                "status": "pending",
                "entry_date": e["entry_date"], "direction": e["direction"],
                "amount_paise": int(e["amount_paise"]),
                "item_en": e["item_en"], "item_kn": e.get("item_kn"),
                "category": e["category"], "party": e.get("party"),
                "notes_en": e.get("notes_en"),
                "amount_spoken_kn": e.get("amount_spoken_kn"),
                "flags": e.get("flags", "") or "",
                "created_at": _now(),
            })
            ids.append(nxt)
            nxt += 1
    return ids


def latest_version(conn, entry_id: int):
    return conn.execute(
        "SELECT * FROM entry_versions WHERE entry_id=? ORDER BY version DESC LIMIT 1", (entry_id,)
    ).fetchone()


def _next_version_fields(cur: sqlite3.Row, **overrides) -> dict:
    fields = {k: cur[k] for k in ENTRY_FIELDS}
    fields["version"] = cur["version"] + 1
    fields["created_at"] = _now()
    fields.update(overrides)
    return fields


def set_status(conn, entry_id: int, new_status: str, expected: str = "pending") -> bool:
    """Move pending -> confirmed/rejected. Returns False if it was already handled (idempotent)."""
    with tx(conn):
        cur = latest_version(conn, entry_id)
        if cur is None or cur["status"] != expected:
            return False
        _insert_version(conn, _next_version_fields(cur, status=new_status))
        return True


def amend_entry(conn, entry_id: int, **changes) -> int:
    """Correct an entry by appending a new version (old versions stay forever)."""
    bad = set(changes) - AMENDABLE
    if bad:
        raise ValueError(f"cannot amend fields: {sorted(bad)}")
    with tx(conn):
        cur = latest_version(conn, entry_id)
        if cur is None:
            raise ValueError("no such entry")
        if cur["status"] == "rejected":
            raise ValueError("entry was discarded and cannot be amended")
        flags = [f for f in (cur["flags"] or "").split(",") if f]
        if "amended" not in flags:
            flags.append("amended")
        fields = _next_version_fields(cur, flags=",".join(flags), **changes)
        _insert_version(conn, fields)
        return fields["version"]


_LATEST = (
    "SELECT ev.* FROM entry_versions ev JOIN "
    "(SELECT entry_id, MAX(version) AS mv FROM entry_versions {inner} GROUP BY entry_id) l "
    "ON ev.entry_id = l.entry_id AND ev.version = l.mv "
)


def current_entries(conn, start: str, end: str, status: str = "confirmed") -> list[sqlite3.Row]:
    """Latest version of each entry whose date is within [start, end] (ISO dates)."""
    sql = _LATEST.format(inner="") + (
        "WHERE ev.status=? AND ev.entry_date BETWEEN ? AND ? ORDER BY ev.entry_date, ev.entry_id"
    )
    return conn.execute(sql, (status, start, end)).fetchall()


def entries_for_recording(conn, recording_id: int) -> list[sqlite3.Row]:
    sql = _LATEST.format(inner="WHERE recording_id=?") + "WHERE ev.recording_id=? ORDER BY ev.entry_id"
    return conn.execute(sql, (recording_id, recording_id)).fetchall()


def get_recording(conn, recording_id: int):
    return conn.execute("SELECT * FROM recordings WHERE id=?", (recording_id,)).fetchone()


def totals(rows) -> tuple[int, int]:
    """(expense_paise, income_paise) for the given entry rows."""
    exp = sum(r["amount_paise"] for r in rows if r["direction"] == "expense")
    inc = sum(r["amount_paise"] for r in rows
              if r["direction"] == "income" and r["category"] != OPENING)
    return exp, inc


def has_opening(conn) -> bool:
    return conn.execute("SELECT 1 FROM entry_versions WHERE category=? LIMIT 1", (OPENING,)).fetchone() is not None


def add_opening_balance(conn, telegram_user_id: int, amount_paise: int, on_date: str) -> int:
    """Record the cash in hand at the start of tracking. Allowed once; later changes use amend_entry."""
    if has_opening(conn):
        raise ValueError("Opening balance is already set")
    rec = add_recording(conn, telegram_user_id=telegram_user_id, source="text",
                        transcript_kn="[manual] opening balance")
    [eid] = add_entries(conn, rec, [dict(
        entry_date=on_date, direction="income", amount_paise=amount_paise,
        item_en="Opening balance (cash in hand)", item_kn=None, category=OPENING,
        party=None, notes_en=None, amount_spoken_kn=None, flags="")])
    set_status(conn, eid, "confirmed")
    return eid


def balance_upto(conn, upto: str) -> dict:
    """Opening + received - paid, over all confirmed entries dated up to `upto` (ISO date)."""
    rows = current_entries(conn, "0000-01-01", upto)
    opening = sum(r["amount_paise"] for r in rows if r["category"] == OPENING)
    expense, income = totals(rows)
    return {"opening": opening, "income": income, "expense": expense,
            "balance": opening + income - expense}


def period_opening(conn, start: str, end: str):
    """Balance brought forward into [start, end]; None if no opening balance was ever set."""
    if not has_opening(conn):
        return None
    prior = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    within = sum(r["amount_paise"] for r in current_entries(conn, start, end) if r["category"] == OPENING)
    return balance_upto(conn, prior)["balance"] + within


def head(conn) -> dict:
    """Current chain heads; log these somewhere off-machine to detect truncation."""
    out = {}
    for t in ("recordings", "entry_versions"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        out[t] = {"rows": n, "last_hash": _last_hash(conn, t)}
    return out


def verify_chain(conn) -> tuple[bool, list[str]]:
    problems: list[str] = []
    for table, names in (("recordings", REC_FIELDS), ("entry_versions", ENTRY_FIELDS)):
        prev = GENESIS
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY id"):
            if row["prev_hash"] != prev:
                problems.append(f"{table} row {row['id']}: chain broken (a row was removed, added or reordered)")
            expect = _hash(row["prev_hash"], {n: row[n] for n in names})
            if expect != row["row_hash"]:
                problems.append(f"{table} row {row['id']}: contents were changed")
            prev = row["row_hash"]
    return (not problems, problems)
