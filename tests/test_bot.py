"""End-to-end handler tests with fake Telegram objects (no network)."""
import asyncio
from collections import defaultdict, deque
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from diary_bot import bot, db, extract
from diary_bot.config import Settings
from diary_bot.crypto import Vault, generate_key

OWNER, STRANGER = 111, 999


@pytest.fixture
def ctx(tmp_path):
    s = Settings(telegram_token="T", allowed_user_ids=frozenset({OWNER}), sarvam_api_key="K",
                 enc_key=generate_key(), claude_model="m", sarvam_model="saaras:v3",
                 data_dir=tmp_path, backup_dir=tmp_path / "bk", tz=ZoneInfo("Asia/Kolkata"),
                 max_voice_seconds=30, max_notes_per_hour=3)
    (tmp_path / "audio").mkdir()
    app = SimpleNamespace(bot_data={"settings": s, "vault": Vault(s.enc_key), "hits": defaultdict(deque)})
    return SimpleNamespace(application=app, args=[]), s


def make_update(user_id=OWNER, voice=None, text=None, chat_type="private", data=None):
    msg = MagicMock()
    msg.voice, msg.audio, msg.text = voice, None, text
    msg.reply_text, msg.reply_html = AsyncMock(), AsyncMock()
    q = None
    if data:
        q = MagicMock()
        q.data = data
        q.answer, q.edit_message_text = AsyncMock(), AsyncMock()
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type=chat_type),
                           effective_message=msg, callback_query=q)


def fake_voice(duration=8):
    f = MagicMock()
    f.download_as_bytearray = AsyncMock(return_value=bytearray(b"OggS-fake-audio"))
    return SimpleNamespace(duration=duration, file_size=4000, get_file=AsyncMock(return_value=f))


def patch_pipeline(monkeypatch, party="Ramesh <b>&"):
    async def fake_stt(audio, key, model):
        return "ನಿನ್ನೆ ಕೂಲಿ ಎರಡೂವರೆ ಸಾವಿರ", "saaras:v3"

    def fake_extract(transcript, now, **kw):
        return extract.normalize_entries([dict(entry_date="2026-11-21", direction="expense", amount_rupees=2500,
                                               amount_as_spoken="ಎರಡೂವರೆ ಸಾವಿರ", item_en="Labour wages",
                                               item_kn="ಕೂಲಿ", category="Labour", party=party, uncertain=False)],
                                         now.date())

    monkeypatch.setattr(bot.stt, "transcribe_kannada", fake_stt)
    monkeypatch.setattr(bot.extract, "extract_entries", fake_extract)


def test_stranger_is_silently_ignored(ctx, monkeypatch):
    c, s = ctx
    patch_pipeline(monkeypatch)
    u = make_update(user_id=STRANGER, voice=fake_voice())
    asyncio.run(bot.on_voice(u, c))
    asyncio.run(bot.cmd_today(make_update(user_id=STRANGER), c))
    u.effective_message.reply_text.assert_not_called()
    u.effective_message.reply_html.assert_not_called()
    assert not s.db_path.exists()  # nothing stored at all


def test_group_chat_is_ignored(ctx, monkeypatch):
    c, s = ctx
    patch_pipeline(monkeypatch)
    u = make_update(voice=fake_voice(), chat_type="group")
    asyncio.run(bot.on_voice(u, c))
    u.effective_message.reply_text.assert_not_called()


def test_voice_to_confirmed_entry(ctx, monkeypatch):
    c, s = ctx
    patch_pipeline(monkeypatch)
    u = make_update(voice=fake_voice())
    asyncio.run(bot.on_voice(u, c))

    # reply shows the English entry, escapes HTML, and offers Save/Discard
    html_reply = u.effective_message.reply_html.await_args
    text = html_reply.args[0]
    assert "Rs <b>2,500.00</b>" not in text and "Rs 2,500.00" in text
    assert "&lt;b&gt;" in text and "<b>&" not in text.split("Party:")[1]
    kb = html_reply.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in kb] == ["ok:1", "no:1"]

    with closing(db.connect(s.db_path)) as conn:
        rec = db.get_recording(conn, 1)
        assert rec["transcript_kn"].startswith("ನಿನ್ನೆ") and rec["stt_provider"] == "sarvam:saaras:v3"
        assert (s.audio_dir / rec["audio_file"]).exists()
        assert b"OggS-fake-audio" not in (s.audio_dir / rec["audio_file"]).read_bytes()
        assert db.entries_for_recording(conn, 1)[0]["status"] == "pending"
        assert db.current_entries(conn, "2026-01-01", "2026-12-31") == []  # not in ledger yet

    # tap Save
    b = make_update(data="ok:1")
    asyncio.run(bot.on_button(b, c))
    assert "Saved" in b.callback_query.edit_message_text.await_args.args[0]
    with closing(db.connect(s.db_path)) as conn:
        assert len(db.current_entries(conn, "2026-01-01", "2026-12-31")) == 1
        n = conn.execute("SELECT COUNT(*) n FROM entry_versions").fetchone()["n"]
    # double tap and a late Discard change nothing
    asyncio.run(bot.on_button(make_update(data="ok:1"), c))
    asyncio.run(bot.on_button(make_update(data="no:1"), c))
    with closing(db.connect(s.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM entry_versions").fetchone()["n"] == n
        assert db.current_entries(conn, "2026-01-01", "2026-12-31")[0]["status"] == "confirmed"


def test_discard_and_bad_callback(ctx, monkeypatch):
    c, s = ctx
    patch_pipeline(monkeypatch)
    asyncio.run(bot.on_voice(make_update(voice=fake_voice()), c))
    d = make_update(data="no:1")
    asyncio.run(bot.on_button(d, c))
    assert "Discarded" in d.callback_query.edit_message_text.await_args.args[0]
    for junk in ("ok:abc", "drop:1", "ok:", "'; DROP TABLE x;--"):
        asyncio.run(bot.on_button(make_update(data=junk), c))  # must not raise
    with closing(db.connect(s.db_path)) as conn:
        assert db.current_entries(conn, "2026-01-01", "2026-12-31") == []
        assert db.verify_chain(conn)[0]


def test_limits_and_stt_failure(ctx, monkeypatch):
    c, s = ctx
    patch_pipeline(monkeypatch)
    long = make_update(voice=fake_voice(duration=45))
    asyncio.run(bot.on_voice(long, c))
    assert "under 30 seconds" in long.effective_message.reply_text.await_args.args[0]

    async def boom(*a, **k):
        raise bot.stt.STTError("No speech detected in the recording.")
    monkeypatch.setattr(bot.stt, "transcribe_kannada", boom)
    u = make_update(voice=fake_voice())
    asyncio.run(bot.on_voice(u, c))
    assert "safely stored" in u.effective_message.reply_text.await_args.args[0]
    assert any(s.audio_dir.glob("*.enc"))  # audio kept for retry even though STT failed

    patch_pipeline(monkeypatch)
    for _ in range(3):
        asyncio.run(bot.on_voice(make_update(voice=fake_voice()), c))
    limited = make_update(voice=fake_voice())
    asyncio.run(bot.on_voice(limited, c))
    assert "Too many notes" in limited.effective_message.reply_text.await_args.args[0]


def test_fix_command_and_validation(ctx, monkeypatch):
    c, s = ctx
    patch_pipeline(monkeypatch)
    asyncio.run(bot.on_voice(make_update(voice=fake_voice()), c))
    asyncio.run(bot.on_button(make_update(data="ok:1"), c))

    def fix(*args):
        u = make_update()
        c.args = list(args)
        asyncio.run(bot.cmd_fix(u, c))
        return u.effective_message

    m = fix("1", "amount", "2,600")
    assert "version 3" in m.reply_html.await_args.args[0] and "2,600.00" in m.reply_html.await_args.args[0]
    assert "Category must be" in fix("1", "category", "Nonsense").reply_text.await_args.args[0]
    assert "Amount must" in fix("1", "amount", "0").reply_text.await_args.args[0]
    assert fix("1", "date", "31-12-2026").reply_text.await_args is not None  # invalid date rejected, no crash
    assert fix("1", "amount", "abc").reply_text.await_args is not None
    with closing(db.connect(s.db_path)) as conn:
        assert db.current_entries(conn, "2026-01-01", "2026-12-31")[0]["amount_paise"] == 260000
        assert db.verify_chain(conn)[0]


def test_report_and_month_commands(ctx, monkeypatch):
    c, s = ctx
    patch_pipeline(monkeypatch)
    asyncio.run(bot.on_voice(make_update(voice=fake_voice()), c))
    asyncio.run(bot.on_button(make_update(data="ok:1"), c))
    c.args = ["2026-11"]
    u = make_update()
    u.effective_message.reply_document = AsyncMock()
    asyncio.run(bot.cmd_report(u, c))
    names = [call.kwargs["filename"] for call in u.effective_message.reply_document.await_args_list]
    assert names == ["diary-2026-11.pdf", "diary-2026-11.xlsx"]
    m = make_update()
    asyncio.run(bot.cmd_month(m, c))
    assert "Labour" in m.effective_message.reply_html.await_args.args[0]
    c.args = ["2026-13"]
    bad = make_update()
    asyncio.run(bot.cmd_month(bad, c))
    assert "YYYY-MM" in bad.effective_message.reply_text.await_args.args[0]


def test_opening_balance_and_balance_in_save_banner(ctx, monkeypatch):
    c, s = ctx
    patch_pipeline(monkeypatch)

    def run(cmd, *args):
        u = make_update()
        c.args = list(args)
        asyncio.run(cmd(u, c))
        return u.effective_message

    assert "/opening 50000" in run(bot.cmd_balance).reply_text.await_args.args[0]
    assert "Rs 50,000.00" in run(bot.cmd_opening, "50000", "2026-11-01").reply_html.await_args.args[0]
    assert "already set" in run(bot.cmd_opening, "60000").reply_text.await_args.args[0]
    assert "Usage" in run(bot.cmd_opening).reply_text.await_args.args[0]
    assert run(bot.cmd_opening, "abc").reply_text.await_args is not None

    v = make_update(voice=fake_voice())
    asyncio.run(bot.on_voice(v, c))
    assert "Total of this note:</b> Paid Rs 2,500.00" in v.effective_message.reply_html.await_args.args[0]
    kb = v.effective_message.reply_html.await_args.kwargs["reply_markup"].inline_keyboard[0]
    b = make_update(data=kb[0].callback_data)  # press the real Save button (recording #2; #1 is the opening balance)
    asyncio.run(bot.on_button(b, c))
    assert "Balance in hand: Rs 47,500.00" in b.callback_query.edit_message_text.await_args.args[0]

    text = run(bot.cmd_balance).reply_html.await_args.args[0]
    assert "Opening balance: Rs 50,000.00" in text and "Paid so far: Rs 2,500.00" in text and "Rs 47,500.00" in text
    month = run(bot.cmd_month, "2026-11").reply_html.await_args.args[0]
    assert "1 entries" in month and "Received: Rs 0.00" in month  # opening balance is not a transaction or income


def test_breakdown_mismatch_warning_shown_to_user():
    rows = [dict(entry_id=1, entry_date="2026-11-22", direction="expense", amount_paise=1_800_000,
                 item_en="Labour", category="Labour", party=None, amount_spoken_kn=None, notes_en=None,
                 flags="breakdown_mismatch", status="pending")]
    html_text = bot._confirmation_html("x", rows, 0)
    assert "parts do not add up" in html_text and "Total of this note:</b> Paid Rs 18,000.00" in html_text
