"""Telegram front-end. Run with:  python -m diary_bot.bot

Flow: voice note -> encrypted audio saved -> Kannada transcript -> English entries (pending)
      -> user taps Save / Discard -> confirmed entries appear in /today, /month, /report.
"""
from __future__ import annotations

import asyncio
import functools
import html
import logging
import re
import time
from collections import defaultdict, deque
from contextlib import closing
from datetime import date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import db, exports, extract, glossary, stt
from .config import load_settings
from .crypto import Vault, store_audio
from .utils import clean_text, fmt_inr, to_paise

log = logging.getLogger("diary_bot")
MAX_AUDIO_BYTES = 5_000_000
FIX_FIELDS = {"amount": "amount_paise", "date": "entry_date", "item": "item_en",
              "category": "category", "party": "party", "notes": "notes_en", "type": "direction"}
FLAG_TEXT = {
    "large_amount": "very large amount - please double-check",
    "model_unsure": "I was not sure about this one",
    "date_unclear": "date was unclear, used today",
    "future_date": "date is in the future",
    "direction_unclear": "could not tell if paid or received",
    "kannada_in_english_field": "English text needs review",
    "breakdown_mismatch": "parts do not add up to the total you said - please check",
}
HELP = (
    "<b>Daily Diary bot</b>\n"
    "Send a <b>voice note in Kannada</b> (up to 30 seconds), e.g. who you paid, what for, how much.\n"
    "I will show the English entry - tap <b>Save</b> only if it is correct.\n\n"
    "/today - today's entries\n"
    "/month [YYYY-MM] - month summary\n"
    "/report [YYYY-MM] - Excel + PDF files\n"
    "/opening &lt;amount&gt; [YYYY-MM-DD] - cash in hand when you start (set once)\n"
    "/balance - money left in hand\n"
    "/fix &lt;id&gt; &lt;amount|date|item|category|party|notes|type&gt; &lt;value&gt;\n"
    "/term ಕನ್ನಡ = English - teach me a word\n"
    "/verify - check the ledger has not been tampered with"
)


# ----------------------------------------------------------------- security helpers
def authorized(fn):
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        s = context.application.bot_data["settings"]
        u, chat = update.effective_user, update.effective_chat
        if u is None or chat is None or chat.type != "private" or u.id not in s.allowed_user_ids:
            log.warning("blocked update from user id=%s", getattr(u, "id", None))
            return  # silent: do not reveal the bot to strangers
        return await fn(update, context)
    return wrapper


def _rate_ok(context, user_id: int) -> bool:
    s = context.application.bot_data["settings"]
    q = context.application.bot_data["hits"][user_id]
    now = time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= s.max_notes_per_hour:
        return False
    q.append(now)
    return True


# ----------------------------------------------------------------- formatting
def _h(x) -> str:
    return html.escape(str(x or ""))


def _entry_block(r) -> str:
    d = date.fromisoformat(r["entry_date"]).strftime("%d %b %Y")
    kind = "Paid" if r["direction"] == "expense" else "Received"
    lines = [f"<b>#{r['entry_id']}</b> · {d} · {kind}",
             f"{_h(r['item_en'])} - <b>Rs {fmt_inr(r['amount_paise'])}</b> ({_h(r['category'])})"]
    if r["party"]:
        lines.append(f"Party: {_h(r['party'])}")
    if r["notes_en"]:
        lines.append(f"Note: {_h(r['notes_en'])}")
    if r["amount_spoken_kn"]:
        lines.append(f"Amount heard: <i>{_h(r['amount_spoken_kn'])}</i>")
    for f in (r["flags"] or "").split(","):
        if f in FLAG_TEXT:
            lines.append(f"⚠️ {FLAG_TEXT[f]}")
    return "\n".join(lines)


def _month_range(arg: str | None, tz) -> tuple[str, str, str]:
    if arg:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", arg):
            raise ValueError("Use the format YYYY-MM, e.g. 2026-11")
        y, m = int(arg[:4]), int(arg[5:])
    else:
        n = datetime.now(tz)
        y, m = n.year, n.month
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    last = date.fromordinal(end.toordinal() - 1)
    return start.isoformat(), last.isoformat(), start.strftime("%B %Y")


def _real(rows):
    """Entries people think of as transactions (hides the system 'Opening Balance' row)."""
    return [r for r in rows if r["category"] != db.OPENING]


def _confirmation_html(transcript: str, rows, dropped: int, unclear: str | None = None) -> str:
    head = f"🎙 <b>I heard</b>\n<i>{_h(transcript)}</i>\n\n"
    if not rows:
        return head + f"Nothing was saved. {_h(unclear)}\nPlease say it again."
    paid, got = db.totals(rows)
    total = f"<b>Total of this note:</b> Paid Rs {fmt_inr(paid)}" + (f" · Received Rs {fmt_inr(got)}" if got else "")
    extra = f"\n\n(I could not understand {dropped} item(s) - please repeat them.)" if dropped else ""
    return head + "\n\n".join(_entry_block(r) for r in rows) + f"\n\n{total}" + extra + "\n\n<b>Is this correct?</b>"


# ----------------------------------------------------------------- core pipeline
async def _process(update, context, transcript: str, *, source: str, audio_sha=None,
                   audio_file=None, duration_ms=None, provider=None):
    s = context.application.bot_data["settings"]
    msg = update.effective_message
    now = datetime.now(s.tz)
    result = await asyncio.to_thread(extract.extract_entries, transcript, now,
                                     model=s.claude_model, user_glossary=s.glossary_user_path)
    with closing(db.connect(s.db_path)) as conn:
        rec_id = db.add_recording(conn, telegram_user_id=update.effective_user.id, source=source,
                                  transcript_kn=transcript, audio_sha256=audio_sha,
                                  audio_file=audio_file, duration_ms=duration_ms, stt_provider=provider)
        ids = db.add_entries(conn, rec_id, result.entries) if result.entries else []
        rows = [db.latest_version(conn, i) for i in ids]
    if not rows:
        await msg.reply_html(_confirmation_html(transcript, [], 0, result.unclear_reason))
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Save", callback_data=f"ok:{rec_id}"),
                                InlineKeyboardButton("❌ Discard", callback_data=f"no:{rec_id}")]])
    await msg.reply_html(_confirmation_html(transcript, rows, result.dropped), reply_markup=kb)


@authorized
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = context.application.bot_data["settings"]
    vault: Vault = context.application.bot_data["vault"]
    msg = update.effective_message
    v = msg.voice or msg.audio
    if v is None:
        return
    if v.duration and v.duration > s.max_voice_seconds:
        await msg.reply_text(f"Please keep each voice note under {s.max_voice_seconds} seconds.")
        return
    if v.file_size and v.file_size > MAX_AUDIO_BYTES:
        await msg.reply_text("That recording is too large.")
        return
    if not _rate_ok(context, update.effective_user.id):
        await msg.reply_text("Too many notes this hour. Please try again later.")
        return
    await msg.reply_text("🎙 Got it, processing...")
    try:
        f = await v.get_file()
        audio = bytes(await f.download_as_bytearray())
        sha, fname = store_audio(vault, s.audio_dir, audio)  # saved before anything can fail
        transcript, model_used = await stt.transcribe_kannada(audio, s.sarvam_api_key, s.sarvam_model)
        await _process(update, context, transcript, source="voice", audio_sha=sha, audio_file=fname,
                       duration_ms=int((v.duration or 0) * 1000), provider=f"sarvam:{model_used}")
    except stt.STTError as e:
        await msg.reply_text(f"I could not transcribe that: {e} Your recording is safely stored. Please try again.")
    except Exception as e:  # never log message content or URLs (they can contain the bot token)
        log.error("voice processing failed: %s", type(e).__name__)
        await msg.reply_text("Something went wrong. Nothing was saved to the diary. Please try again.")


@authorized
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = clean_text(update.effective_message.text, 1000)
    if not text:
        return
    if not _rate_ok(context, update.effective_user.id):
        await update.effective_message.reply_text("Too many notes this hour. Please try again later.")
        return
    try:
        await _process(update, context, text, source="text")
    except Exception as e:
        log.error("text processing failed: %s", type(e).__name__)
        await update.effective_message.reply_text("Something went wrong. Nothing was saved. Please try again.")


@authorized
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = context.application.bot_data["settings"]
    q = update.callback_query
    await q.answer()
    action, _, rid = (q.data or "").partition(":")
    if action not in ("ok", "no") or not rid.isdigit():
        return
    new = "confirmed" if action == "ok" else "rejected"
    with closing(db.connect(s.db_path)) as conn:
        for e in db.entries_for_recording(conn, int(rid)):
            db.set_status(conn, e["entry_id"], new)
        rows = db.entries_for_recording(conn, int(rid))
    if not rows:
        return
    ok = [r for r in rows if r["status"] == "confirmed"]
    banner = "✅ <b>Saved</b>" if ok else "❌ <b>Discarded</b> - nothing was added"
    shown = ok if ok else rows
    bal = ""
    if ok:
        with closing(db.connect(s.db_path)) as conn:
            if db.has_opening(conn):
                b = db.balance_upto(conn, "9999-12-31")
                bal = f"\n\n💰 <b>Balance in hand: Rs {fmt_inr(b['balance'])}</b>"
    await q.edit_message_text(banner + "\n\n" + "\n\n".join(_entry_block(r) for r in shown) + bal,
                              parse_mode=ParseMode.HTML)


# ----------------------------------------------------------------- commands
@authorized
async def cmd_start(update, context):
    await update.effective_message.reply_html(HELP)


@authorized
async def cmd_today(update, context):
    s = context.application.bot_data["settings"]
    today = datetime.now(s.tz).date().isoformat()
    with closing(db.connect(s.db_path)) as conn:
        rows = _real(db.current_entries(conn, today, today))
    if not rows:
        await update.effective_message.reply_text("No saved entries today.")
        return
    e, i = db.totals(rows)
    await update.effective_message.reply_html(
        "\n\n".join(_entry_block(r) for r in rows)
        + f"\n\n<b>Total paid:</b> Rs {fmt_inr(e)}   <b>Received:</b> Rs {fmt_inr(i)}")


@authorized
async def cmd_month(update, context):
    s = context.application.bot_data["settings"]
    try:
        a, b, label = _month_range(context.args[0] if context.args else None, s.tz)
    except ValueError as e:
        await update.effective_message.reply_text(str(e))
        return
    with closing(db.connect(s.db_path)) as conn:
        rows = _real(db.current_entries(conn, a, b))
    e, i = db.totals(rows)
    by: dict[str, int] = defaultdict(int)
    for r in rows:
        if r["direction"] == "expense":
            by[r["category"]] += r["amount_paise"]
    lines = [f"<b>{label}</b> - {len(rows)} entries",
             f"Paid: Rs {fmt_inr(e)}   Received: Rs {fmt_inr(i)}   Net: Rs {fmt_inr(i - e)}", ""]
    lines += [f"{_h(k)}: Rs {fmt_inr(v)}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])]
    await update.effective_message.reply_html("\n".join(lines))


@authorized
async def cmd_report(update, context):
    s = context.application.bot_data["settings"]
    try:
        a, b, label = _month_range(context.args[0] if context.args else None, s.tz)
    except ValueError as e:
        await update.effective_message.reply_text(str(e))
        return
    with closing(db.connect(s.db_path)) as conn:
        rows = _real(db.current_entries(conn, a, b))
        opening = db.period_opening(conn, a, b)
        ok, _problems = db.verify_chain(conn)
    stem = s.export_dir / f"diary-{a[:7]}"
    x = await asyncio.to_thread(exports.build_xlsx, rows, label, stem.with_suffix(".xlsx"), ok, opening)
    p = await asyncio.to_thread(exports.build_pdf, rows, label, stem.with_suffix(".pdf"), ok, opening)
    for path in (p, x):
        with open(path, "rb") as fh:
            await update.effective_message.reply_document(fh, filename=path.name)
    if not ok:
        await update.effective_message.reply_text("⚠️ Integrity check FAILED. Run /verify and restore from backup.")


@authorized
async def cmd_fix(update, context):
    s = context.application.bot_data["settings"]
    a = context.args or []
    msg = update.effective_message
    if len(a) < 3 or not a[0].isdigit() or a[1].lower() not in FIX_FIELDS:
        await msg.reply_text("Usage: /fix <id> <amount|date|item|category|party|notes|type> <value>")
        return
    field, value = FIX_FIELDS[a[1].lower()], " ".join(a[2:])
    try:
        if field == "amount_paise":
            value = to_paise(value)
            if value <= 0:
                raise ValueError("Amount must be above zero.")
        elif field == "entry_date":
            value = date.fromisoformat(value).isoformat()
        elif field == "category":
            match = next((c for c in extract.CATEGORIES if c.lower() == value.lower()), None)
            if not match:
                raise ValueError("Category must be one of: " + ", ".join(extract.CATEGORIES))
            value = match
        elif field == "direction":
            if value.lower() not in ("expense", "income"):
                raise ValueError("Type must be expense or income.")
            value = value.lower()
        else:
            value = clean_text(value, 200)
            if not value:
                raise ValueError("Value cannot be empty.")
        with closing(db.connect(s.db_path)) as conn:
            ver = db.amend_entry(conn, int(a[0]), **{field: value})
            row = db.latest_version(conn, int(a[0]))
    except ValueError as e:
        await msg.reply_text(str(e))
        return
    await msg.reply_html(f"Updated (version {ver}); the old version is kept in history.\n\n" + _entry_block(row))


@authorized
async def cmd_term(update, context):
    s = context.application.bot_data["settings"]
    text = (update.effective_message.text or "").partition(" ")[2]
    kn, sep, en = text.partition("=")
    try:
        if not sep:
            raise ValueError("Use the form: /term ಕನ್ನಡ ಪದ = English meaning")
        glossary.add_user_term(s.glossary_user_path, kn, en)
    except ValueError as e:
        await update.effective_message.reply_text(str(e))
        return
    await update.effective_message.reply_text("Added. I will use this from the next note.")


@authorized
async def cmd_opening(update, context):
    s = context.application.bot_data["settings"]
    msg = update.effective_message
    a = context.args or []
    try:
        if not a:
            raise ValueError("Usage: /opening <amount> [YYYY-MM-DD]  (cash in hand at the start of that day)")
        amount = to_paise(a[0])
        if amount < 0:
            raise ValueError("Amount cannot be negative.")
        on = date.fromisoformat(a[1]).isoformat() if len(a) > 1 else datetime.now(s.tz).date().isoformat()
        with closing(db.connect(s.db_path)) as conn:
            if db.has_opening(conn):
                raise ValueError("Opening balance is already set. To change it use /fix with its entry id.")
            db.add_opening_balance(conn, update.effective_user.id, amount, on)
    except ValueError as e:
        await msg.reply_text(str(e))
        return
    await msg.reply_html(f"Opening balance set: <b>Rs {fmt_inr(amount)}</b> from {date.fromisoformat(on):%d %b %Y}.")


@authorized
async def cmd_balance(update, context):
    s = context.application.bot_data["settings"]
    today = datetime.now(s.tz).date()
    with closing(db.connect(s.db_path)) as conn:
        if not db.has_opening(conn):
            await update.effective_message.reply_text(
                "Tell me the cash you had in hand when you started, e.g. /opening 50000, then I can show the balance.")
            return
        b = db.balance_upto(conn, "9999-12-31")
    await update.effective_message.reply_html(
        f"💰 <b>Balance as of {today:%d %b %Y}</b>\n"
        f"Opening balance: Rs {fmt_inr(b['opening'])}\n"
        f"Received so far: Rs {fmt_inr(b['income'])}\n"
        f"Paid so far: Rs {fmt_inr(b['expense'])}\n"
        f"<b>Balance in hand: Rs {fmt_inr(b['balance'])}</b>")


@authorized
async def cmd_verify(update, context):
    s = context.application.bot_data["settings"]
    with closing(db.connect(s.db_path)) as conn:
        ok, problems = db.verify_chain(conn)
        h = db.head(conn)
    if ok:
        await update.effective_message.reply_text(
            f"✅ Ledger intact: {h['recordings']['rows']} recordings, {h['entry_versions']['rows']} entry versions.")
    else:
        await update.effective_message.reply_text("⚠️ Problems found:\n" + "\n".join(problems[:10]))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    s = load_settings()
    app = Application.builder().token(s.telegram_token).build()
    app.bot_data.update(settings=s, vault=Vault(s.enc_key), hits=defaultdict(deque))
    for name, fn in (("start", cmd_start), ("help", cmd_start), ("today", cmd_today),
                     ("month", cmd_month), ("report", cmd_report), ("fix", cmd_fix),
                     ("term", cmd_term), ("verify", cmd_verify),
                     ("opening", cmd_opening), ("balance", cmd_balance)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("Diary bot started for %d allowed user(s)", len(s.allowed_user_ids))
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
