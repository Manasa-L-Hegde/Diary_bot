"""Modern, mobile-friendly Web Dashboard for Daily Diary.
Kannada voice/text -> Gemini extraction -> Confirm -> Tamper-evident Ledger -> Excel/PDF.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import date, datetime
import hashlib
from io import BytesIO
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import db, exports, extract, stt
from .config import load_settings
from .crypto import Vault, store_audio
from .utils import fmt_inr

app = FastAPI(title="Daily Diary Bot - Web Edition")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

settings = load_settings()
vault = Vault(settings.enc_key)


class ConfirmRequest(BaseModel):
    rec_id: int
    action: Literal["save", "discard"]


class OpeningRequest(BaseModel):
    amount_rupees: float
    opening_date: str | None = None


class TextRequest(BaseModel):
    text: str


@app.get("/api/balance")
def get_balance():
    with closing(db.connect(settings.db_path)) as conn:
        b = db.balance_upto(conn, "9999-12-31")
        has_open = db.has_opening(conn)
        h = db.head(conn)
        chain_ok, _ = db.verify_chain(conn)
    return {
        "has_opening": has_open,
        "opening": b["opening"] / 100,
        "income": b["income"] / 100,
        "expense": b["expense"] / 100,
        "balance": b["balance"] / 100,
        "recordings_count": h["recordings"]["rows"],
        "entries_count": h["entry_versions"]["rows"],
        "ledger_verified": chain_ok,
    }


@app.get("/api/entries")
def get_entries(period: str = "today", month: str | None = None):
    now = datetime.now(settings.tz)
    today_str = now.date().isoformat()
    with closing(db.connect(settings.db_path)) as conn:
        if period == "today":
            rows = db.current_entries(conn, today_str, today_str)
        elif period == "month":
            m = month or now.strftime("%Y-%m")
            rows = db.entries_for_month(conn, m)
        else:
            rows = db.current_entries(conn, "2000-01-01", "2099-12-31")
    
    # Exclude system opening balance row from ledger table display
    items = []
    for r in rows:
        if r["category"] == db.OPENING:
            continue
        items.append({
            "entry_id": r["entry_id"],
            "recording_id": r["recording_id"],
            "date": r["entry_date"],
            "direction": r["direction"],
            "amount": r["amount_paise"] / 100,
            "item_en": r["item_en"],
            "item_kn": r["item_kn"] or "",
            "category": r["category"],
            "party": r["party"] or "",
            "notes": r["notes_en"] or "",
            "spoken": r["amount_spoken_kn"] or "",
            "flags": r["flags"] or "",
        })
    return {"entries": items}


@app.post("/api/voice")
async def process_voice(file: UploadFile = File(...)):
    raw_audio = await file.read()
    if not raw_audio:
        raise HTTPException(status_code=400, detail="Empty audio file")
    
    now = datetime.now(settings.tz)
    audio_sha, enc_name = store_audio(vault, settings.audio_dir, raw_audio)

    # Transcribe with Sarvam AI
    try:
        transcript, provider = await stt.transcribe_kannada(
            raw_audio,
            settings.sarvam_api_key,
            model=settings.sarvam_model,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Speech recognition error: {e}")

    # Extract with Gemini
    result = await asyncio.to_thread(
        extract.extract_entries,
        transcript,
        now,
        model=settings.gemini_model,
        user_glossary=settings.glossary_user_path,
    )

    with closing(db.connect(settings.db_path)) as conn:
        rec_id = db.add_recording(
            conn,
            telegram_user_id=1,  # web user
            source="voice",
            transcript_kn=transcript,
            audio_sha256=audio_sha,
            audio_file=enc_name,
            duration_ms=None,
            stt_provider=provider,
        )
        entry_ids = db.add_entries(conn, rec_id, result.entries) if result.entries else []
        drafts = [dict(db.latest_version(conn, i)) for i in entry_ids]

    clean_drafts = []
    for d in drafts:
        clean_drafts.append({
            "entry_id": d["entry_id"],
            "date": d["entry_date"],
            "direction": d["direction"],
            "amount": d["amount_paise"] / 100,
            "item_en": d["item_en"],
            "item_kn": d.get("item_kn") or "",
            "category": d["category"],
            "party": d.get("party") or "",
            "notes": d.get("notes_en") or "",
            "spoken": d.get("amount_spoken_kn") or "",
            "flags": d.get("flags") or "",
        })

    return {
        "rec_id": rec_id,
        "transcript": transcript,
        "entries": clean_drafts,
        "unclear_reason": result.unclear_reason if not clean_drafts else None,
    }


@app.post("/api/text")
async def process_text(req: TextRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    
    now = datetime.now(settings.tz)
    result = await asyncio.to_thread(
        extract.extract_entries,
        text,
        now,
        model=settings.gemini_model,
        user_glossary=settings.glossary_user_path,
    )

    with closing(db.connect(settings.db_path)) as conn:
        rec_id = db.add_recording(
            conn,
            telegram_user_id=1,
            source="text",
            transcript_kn=text,
            audio_sha256=None,
            audio_file=None,
            duration_ms=None,
            stt_provider="user_text",
        )
        entry_ids = db.add_entries(conn, rec_id, result.entries) if result.entries else []
        drafts = [dict(db.latest_version(conn, i)) for i in entry_ids]

    clean_drafts = []
    for d in drafts:
        clean_drafts.append({
            "entry_id": d["entry_id"],
            "date": d["entry_date"],
            "direction": d["direction"],
            "amount": d["amount_paise"] / 100,
            "item_en": d["item_en"],
            "item_kn": d.get("item_kn") or "",
            "category": d["category"],
            "party": d.get("party") or "",
            "notes": d.get("notes_en") or "",
            "spoken": d.get("amount_spoken_kn") or "",
            "flags": d.get("flags") or "",
        })

    return {
        "rec_id": rec_id,
        "transcript": text,
        "entries": clean_drafts,
        "unclear_reason": result.unclear_reason if not clean_drafts else None,
    }


@app.post("/api/confirm")
def confirm_entry(req: ConfirmRequest):
    status = "confirmed" if req.action == "save" else "rejected"
    with closing(db.connect(settings.db_path)) as conn:
        for e in db.entries_for_recording(conn, req.rec_id):
            db.set_status(conn, e["entry_id"], status)
        b = db.balance_upto(conn, "9999-12-31")
    return {
        "ok": True,
        "status": status,
        "balance": b["balance"] / 100,
    }


@app.post("/api/opening")
def set_opening(req: OpeningRequest):
    paise = int(req.amount_rupees * 100)
    now = datetime.now(settings.tz)
    d = req.opening_date or now.date().isoformat()
    with closing(db.connect(settings.db_path)) as conn:
        if not db.has_opening(conn):
            db.add_opening_balance(conn, 1, paise, d)
        b = db.balance_upto(conn, "9999-12-31")
    return {"ok": True, "balance": b["balance"] / 100}


@app.get("/api/export/excel")
def export_excel(month: str | None = None):
    now = datetime.now(settings.tz)
    m = month or now.strftime("%Y-%m")
    try:
        y_str, mo_str = m.split("-")
        y, mo = int(y_str), int(mo_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid month format, use YYYY-MM")

    with closing(db.connect(settings.db_path)) as conn:
        buf = exports.build_excel(conn, y, mo)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="Diary_{m}.xlsx"'},
    )


@app.get("/api/export/pdf")
def export_pdf(month: str | None = None):
    now = datetime.now(settings.tz)
    m = month or now.strftime("%Y-%m")
    try:
        y_str, mo_str = m.split("-")
        y, mo = int(y_str), int(mo_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid month format, use YYYY-MM")

    with closing(db.connect(settings.db_path)) as conn:
        buf = exports.build_pdf(conn, y, mo)
    return Response(
        content=buf.getvalue(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Diary_{m}.pdf"'},
    )


INDEX_HTML = """<!DOCTYPE html>
<html lang="kn">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ದೈನಂದಿನ ದಿನಚರಿ - Daily Diary Ledger</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    @keyframes pulse-ring {
      0% { transform: scale(0.95); opacity: 0.8; }
      50% { transform: scale(1.15); opacity: 0.4; }
      100% { transform: scale(0.95); opacity: 0.8; }
    }
    .recording-pulse {
      animation: pulse-ring 1.5s infinite ease-in-out;
    }
  </style>
</head>
<body class="bg-slate-50 text-slate-800 font-sans min-h-screen pb-16">

  <!-- Header -->
  <header class="bg-emerald-700 text-white shadow-lg sticky top-0 z-40">
    <div class="max-w-4xl mx-auto px-4 py-3 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-full bg-emerald-800 flex items-center justify-center text-white text-xl shadow">
          <i class="fa-solid fa-book-bookmark"></i>
        </div>
        <div>
          <h1 class="text-xl font-bold tracking-tight">ದೈನಂದಿನ ದಿನಚರಿ</h1>
          <p class="text-xs text-emerald-200">Daily Farm & Home Voice Ledger</p>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <span id="ledgerBadge" class="text-xs px-2.5 py-1 rounded-full bg-emerald-800/80 text-emerald-200 font-medium flex items-center gap-1.5">
          <i class="fa-solid fa-shield-halved text-emerald-300"></i> ಸುರಕ್ಷಿತ (Verified)
        </span>
      </div>
    </div>
  </header>

  <main class="max-w-4xl mx-auto px-4 py-5 space-y-5">

    <!-- Balance Summary Card -->
    <div class="bg-gradient-to-br from-emerald-800 to-teal-900 rounded-2xl p-5 text-white shadow-xl">
      <div class="flex items-center justify-between mb-4">
        <span class="text-sm font-medium text-emerald-200 uppercase tracking-wider flex items-center gap-1.5">
          <i class="fa-solid fa-wallet"></i> ಕೈಯಲ್ಲಿರುವ ಹಣ (Cash in Hand)
        </span>
        <button onclick="openOpeningModal()" class="text-xs bg-white/15 hover:bg-white/25 px-2.5 py-1 rounded-lg text-emerald-100 transition">
          <i class="fa-solid fa-pen-to-square"></i> ಆರಂಭಿಕ ನಗದು (Opening)
        </button>
      </div>

      <div class="mb-4">
        <div class="text-3xl md:text-4xl font-extrabold tracking-tight" id="balanceAmount">₹ 0.00</div>
      </div>

      <div class="grid grid-cols-3 gap-2 pt-3 border-t border-emerald-700/60 text-xs md:text-sm">
        <div class="bg-white/10 rounded-xl p-2.5">
          <div class="text-emerald-300 mb-0.5">ಆರಂಭಿಕ (Opening)</div>
          <div class="font-bold text-white text-base" id="openingAmount">₹ 0.00</div>
        </div>
        <div class="bg-white/10 rounded-xl p-2.5">
          <div class="text-emerald-300 mb-0.5">ಬಂದ ಹಣ (Received)</div>
          <div class="font-bold text-emerald-300 text-base" id="incomeAmount">+ ₹ 0.00</div>
        </div>
        <div class="bg-white/10 rounded-xl p-2.5">
          <div class="text-emerald-300 mb-0.5">ಖರ್ಚು (Paid)</div>
          <div class="font-bold text-rose-300 text-base" id="expenseAmount">- ₹ 0.00</div>
        </div>
      </div>
    </div>

    <!-- Voice Recording & Input Section -->
    <div class="bg-white rounded-2xl p-6 shadow-sm border border-slate-200">
      <div class="text-center mb-6">
        <h2 class="text-lg font-bold text-slate-800">ಮಾತನಾಡಿ ಲೆಕ್ಕ ಬರೆಯಿರಿ (Speak Your Entry)</h2>
        <p class="text-xs text-slate-500 mt-1">ಕನ್ನಡದಲ್ಲಿ ಮಾತನಾಡಿ (ಉದಾ: "ನಿನ್ನೆ ರಮೇಶ್ ಗೆ ಕೂಲಿ 2500 ಕೊಟ್ಟೆ")</p>
      </div>

      <!-- Mic Button Area -->
      <div class="flex flex-col items-center justify-center my-4">
        <div class="relative">
          <div id="pulseRing" class="hidden absolute inset-0 rounded-full bg-rose-500 recording-pulse"></div>
          <button id="micBtn" onclick="toggleRecording()" class="relative z-10 w-24 h-24 rounded-full bg-emerald-600 hover:bg-emerald-700 text-white flex flex-col items-center justify-center shadow-lg hover:shadow-xl transition-all duration-200 active:scale-95">
            <i id="micIcon" class="fa-solid fa-microphone text-3xl mb-1"></i>
            <span id="micLabel" class="text-xs font-semibold">ರೆಕಾರ್ಡ್</span>
          </button>
        </div>
        <div id="timerDisplay" class="mt-3 text-sm font-semibold text-slate-600 hidden">0:00 / 0:30</div>
        <p id="statusHint" class="text-xs text-slate-400 mt-2">ಬಟನ್ ಒತ್ತಿ ಮಾತನಾಡಿ, ಮುಗಿದಾಗ ಮತ್ತೊಮ್ಮೆ ಒತ್ತಿ</p>
      </div>

      <!-- Or Type Text Toggle -->
      <div class="mt-6 pt-5 border-t border-slate-100">
        <details class="group">
          <summary class="cursor-pointer text-xs font-medium text-emerald-700 hover:text-emerald-800 flex items-center justify-center gap-1 select-none">
            <span>ಅಥವಾ ಇಲ್ಲಿ ಟೈಪ್ ಮಾಡಿ (Or type text note)</span>
            <i class="fa-solid fa-chevron-down group-open:rotate-180 transition-transform"></i>
          </summary>
          <div class="mt-3 space-y-2">
            <textarea id="textInput" rows="2" placeholder="ಕನ್ನಡ ಅಥವಾ ಇಂಗ್ಲಿಷ್‌ನಲ್ಲಿ ಬರೆಯಿರಿ... (ಉದಾ: ರಮೇಶ್ ಕೂಲಿ 2500)" class="w-full text-sm p-3 rounded-xl border border-slate-200 focus:outline-none focus:ring-2 focus:ring-emerald-500"></textarea>
            <button onclick="submitText()" class="w-full bg-slate-800 hover:bg-slate-900 text-white text-xs font-semibold py-2.5 rounded-xl transition">
              <i class="fa-solid fa-paper-plane mr-1.5"></i> ಲೆಕ್ಕ ಮಾಡಿ (Process Note)
            </button>
          </div>
        </details>
      </div>
    </div>

    <!-- Processing State Banner -->
    <div id="loadingBanner" class="hidden bg-emerald-50 border border-emerald-200 rounded-2xl p-6 text-center shadow-sm">
      <div class="inline-block animate-spin text-emerald-600 text-3xl mb-2">
        <i class="fa-solid fa-circle-notch"></i>
      </div>
      <div class="text-sm font-bold text-emerald-900" id="loadingTitle">ಧ್ವನಿಯನ್ನು ಪರಿವರ್ತಿಸಲಾಗುತ್ತಿದೆ...</div>
      <div class="text-xs text-emerald-700 mt-1" id="loadingSub">Sarvam AI & Gemini Flash ಕೆಲಸ ಮಾಡುತ್ತಿವೆ</div>
    </div>

    <!-- Confirmation Modal / Card -->
    <div id="confirmCard" class="hidden bg-white border-2 border-amber-300 rounded-2xl p-5 shadow-lg space-y-4">
      <div class="flex items-center justify-between pb-3 border-b border-slate-100">
        <span class="text-xs font-bold uppercase tracking-wider text-amber-700 bg-amber-100 px-2.5 py-0.5 rounded-full flex items-center gap-1">
          <i class="fa-solid fa-triangle-exclamation"></i> ಪರಿಶೀಲಿಸಿ (Review Entry)
        </span>
        <span class="text-xs text-slate-400" id="confirmTime"></span>
      </div>

      <div class="bg-slate-50 p-3 rounded-xl border border-slate-100">
        <div class="text-xs text-slate-500 font-medium mb-1">ಕೇಳಿದ ಮಾತು (Transcribed Speech):</div>
        <div class="text-sm font-semibold text-slate-800" id="confirmTranscript"></div>
      </div>

      <div id="confirmEntriesContainer" class="space-y-2"></div>

      <div id="unclearWarning" class="hidden text-xs text-rose-600 bg-rose-50 p-2.5 rounded-xl border border-rose-200 font-medium"></div>

      <!-- Action Buttons -->
      <div class="grid grid-cols-2 gap-3 pt-2">
        <button onclick="confirmAction('save')" class="bg-emerald-600 hover:bg-emerald-700 text-white font-bold py-3 rounded-xl shadow transition active:scale-95 flex items-center justify-center gap-2">
          <i class="fa-solid fa-check text-lg"></i>
          <span>ಉಳಿಸಿ (Save)</span>
        </button>
        <button onclick="confirmAction('discard')" class="bg-slate-200 hover:bg-slate-300 text-slate-700 font-bold py-3 rounded-xl transition active:scale-95 flex items-center justify-center gap-2">
          <i class="fa-solid fa-xmark text-lg"></i>
          <span>ಬೇಡ (Discard)</span>
        </button>
      </div>
    </div>

    <!-- Entries & Reports Section -->
    <div class="bg-white rounded-2xl p-5 shadow-sm border border-slate-200 space-y-4">
      <div class="flex items-center justify-between flex-wrap gap-2">
        <h2 class="text-base font-bold text-slate-800 flex items-center gap-2">
          <i class="fa-solid fa-list-check text-emerald-600"></i>
          <span>ದಿನಚರಿ ವಿವರಗಳು (Entries)</span>
        </h2>
        <div class="flex items-center gap-2">
          <select id="periodFilter" onchange="loadEntries()" class="text-xs bg-slate-100 font-medium px-3 py-1.5 rounded-lg border border-slate-200 focus:outline-none">
            <option value="today">ಇಂದು (Today)</option>
            <option value="month">ಈ ತಿಂಗಳು (This Month)</option>
            <option value="all">ಎಲ್ಲವೂ (All Time)</option>
          </select>
          <div class="relative group">
            <button class="text-xs bg-emerald-50 text-emerald-700 hover:bg-emerald-100 font-semibold px-3 py-1.5 rounded-lg transition flex items-center gap-1.5 border border-emerald-200">
              <i class="fa-solid fa-download"></i> ರಫ್ತು (Export)
            </button>
            <div class="hidden group-hover:block absolute right-0 top-full mt-1 w-44 bg-white border border-slate-200 rounded-xl shadow-xl z-20 py-1 text-xs">
              <a href="/api/export/excel" class="block px-4 py-2 hover:bg-emerald-50 text-slate-700 flex items-center gap-2">
                <i class="fa-solid fa-file-excel text-emerald-600"></i> Excel (.xlsx) ಡೌನ್‌ಲೋಡ್
              </a>
              <a href="/api/export/pdf" class="block px-4 py-2 hover:bg-emerald-50 text-slate-700 flex items-center gap-2">
                <i class="fa-solid fa-file-pdf text-rose-600"></i> PDF (.pdf) ಡೌನ್‌ಲೋಡ್
              </a>
            </div>
          </div>
        </div>
      </div>

      <!-- Entries Table / Cards -->
      <div id="entriesList" class="space-y-2">
        <div class="text-center py-8 text-slate-400 text-xs">ದಾಖಲೆಗಳು ಲೋಡ್ ಆಗುತ್ತಿವೆ...</div>
      </div>
    </div>

  </main>

  <!-- Opening Balance Modal -->
  <div id="openingModal" class="hidden fixed inset-0 bg-slate-900/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl max-w-sm w-full p-5 space-y-4 shadow-2xl">
      <div class="flex items-center justify-between">
        <h3 class="font-bold text-slate-800 text-base">ಆರಂಭಿಕ ನಗದು (Opening Cash)</h3>
        <button onclick="closeOpeningModal()" class="text-slate-400 hover:text-slate-600"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <p class="text-xs text-slate-500">ಲೆಕ್ಕ ಶುರುಮಾಡುವಾಗ ನಿಮ್ಮ ಕೈಯಲ್ಲಿದ್ದ ನಗದು ಮೊತ್ತವನ್ನು ನಮೂದಿಸಿ.</p>
      <div>
        <label class="block text-xs font-medium text-slate-600 mb-1">ಮೊತ್ತ (Rupees)</label>
        <div class="relative">
          <span class="absolute left-3 top-2.5 text-slate-400 font-bold">₹</span>
          <input type="number" id="openingInput" placeholder="5000" class="w-full text-base font-bold pl-8 pr-3 py-2 border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-emerald-500">
        </div>
      </div>
      <div class="flex justify-end gap-2 pt-2">
        <button onclick="closeOpeningModal()" class="px-4 py-2 text-xs font-semibold rounded-xl text-slate-600 hover:bg-slate-100">ರದ್ದು</button>
        <button onclick="saveOpening()" class="px-4 py-2 text-xs font-bold rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white shadow">ಉಳಿಸಿ (Save)</button>
      </div>
    </div>
  </div>

  <script>
    let currentRecId = null;
    let mediaRecorder = null;
    let audioChunks = [];
    let recordTimer = null;
    let recordSeconds = 0;

    // Load initial data
    window.addEventListener('DOMContentLoaded', () => {
      fetchBalance();
      loadEntries();
    });

    async function fetchBalance() {
      try {
        const res = await fetch('/api/balance');
        const data = await res.json();
        document.getElementById('balanceAmount').innerText = '₹ ' + data.balance.toLocaleString('en-IN', {minimumFractionDigits: 2});
        document.getElementById('openingAmount').innerText = '₹ ' + data.opening.toLocaleString('en-IN', {minimumFractionDigits: 2});
        document.getElementById('incomeAmount').innerText = '+ ₹ ' + data.income.toLocaleString('en-IN', {minimumFractionDigits: 2});
        document.getElementById('expenseAmount').innerText = '- ₹ ' + data.expense.toLocaleString('en-IN', {minimumFractionDigits: 2});
      } catch (err) {
        console.error(err);
      }
    }

    async function loadEntries() {
      const period = document.getElementById('periodFilter').value;
      const list = document.getElementById('entriesList');
      try {
        const res = await fetch(`/api/entries?period=${period}`);
        const data = await res.json();
        if (!data.entries || data.entries.length === 0) {
          list.innerHTML = `<div class="text-center py-8 text-slate-400 text-xs">ಯಾವುದೇ ದಾಖಲೆಗಳಿಲ್ಲ (No entries found)</div>`;
          return;
        }
        list.innerHTML = data.entries.map(e => {
          const isExpense = e.direction === 'expense';
          const badgeClass = isExpense ? 'bg-rose-100 text-rose-700 border-rose-200' : 'bg-emerald-100 text-emerald-700 border-emerald-200';
          const icon = isExpense ? 'fa-arrow-up text-rose-500' : 'fa-arrow-down text-emerald-500';
          const sign = isExpense ? '-' : '+';
          return `
            <div class="flex items-center justify-between p-3.5 bg-slate-50 hover:bg-slate-100/80 rounded-xl border border-slate-100 transition">
              <div class="flex items-center gap-3">
                <div class="w-9 h-9 rounded-full bg-white border border-slate-200 flex items-center justify-center shadow-sm">
                  <i class="fa-solid ${icon} text-sm"></i>
                </div>
                <div>
                  <div class="text-sm font-bold text-slate-800 flex items-center gap-1.5">
                    <span>${e.item_en}</span>
                    ${e.party ? `<span class="text-xs font-normal text-slate-500">(${e.party})</span>` : ''}
                  </div>
                  <div class="text-xs text-slate-400 flex items-center gap-2 mt-0.5">
                    <span>${e.date}</span>
                    <span>•</span>
                    <span class="text-slate-500">${e.category}</span>
                    ${e.spoken ? `<span class="italic text-emerald-700 font-kannada">"${e.spoken}"</span>` : ''}
                  </div>
                </div>
              </div>
              <div class="text-right">
                <div class="text-sm font-extrabold ${isExpense ? 'text-rose-600' : 'text-emerald-600'}">
                  ${sign} ₹ ${e.amount.toLocaleString('en-IN', {minimumFractionDigits: 2})}
                </div>
                <span class="text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded border ${badgeClass}">
                  ${isExpense ? 'ಖರ್ಚು' : 'ಆದಾಯ'}
                </span>
              </div>
            </div>
          `;
        }).join('');
      } catch (err) {
        list.innerHTML = `<div class="text-center py-4 text-rose-500 text-xs">Error loading entries</div>`;
      }
    }

    // Audio recording logic
    async function toggleRecording() {
      if (mediaRecorder && mediaRecorder.state === 'recording') {
        stopRecording();
      } else {
        startRecording();
      }
    }

    async function startRecording() {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        audioChunks = [];
        mediaRecorder = new MediaRecorder(stream);
        mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
        mediaRecorder.onstop = uploadAudio;
        mediaRecorder.start();

        // UI state
        document.getElementById('pulseRing').classList.remove('hidden');
        document.getElementById('micBtn').classList.replace('bg-emerald-600', 'bg-rose-600');
        document.getElementById('micBtn').classList.replace('hover:bg-emerald-700', 'hover:bg-rose-700');
        document.getElementById('micIcon').className = 'fa-solid fa-stop text-3xl mb-1';
        document.getElementById('micLabel').innerText = 'ನಿಲ್ಲಿಸಿ (Stop)';
        document.getElementById('statusHint').innerText = 'ಕನ್ನಡದಲ್ಲಿ ಮಾತನಾಡಿ...';
        
        recordSeconds = 0;
        const timer = document.getElementById('timerDisplay');
        timer.classList.remove('hidden');
        timer.innerText = '0:00 / 0:30';
        recordTimer = setInterval(() => {
          recordSeconds++;
          const sec = recordSeconds < 10 ? '0' + recordSeconds : recordSeconds;
          timer.innerText = `0:${sec} / 0:30`;
          if (recordSeconds >= 30) stopRecording();
        }, 1000);

      } catch (err) {
        alert('ಮೈಕ್ರೋಫೋನ್ ಅನುಮತಿ ಸಿಗಲಿಲ್ಲ: ' + err.message);
      }
    }

    function stopRecording() {
      if (mediaRecorder && mediaRecorder.state === 'recording') {
        mediaRecorder.stop();
        mediaRecorder.stream.getTracks().forEach(track => track.stop());
      }
      clearInterval(recordTimer);
      document.getElementById('pulseRing').classList.add('hidden');
      document.getElementById('micBtn').classList.replace('bg-rose-600', 'bg-emerald-600');
      document.getElementById('micBtn').classList.replace('hover:bg-rose-700', 'hover:bg-emerald-700');
      document.getElementById('micIcon').className = 'fa-solid fa-microphone text-3xl mb-1';
      document.getElementById('micLabel').innerText = 'ರೆಕಾರ್ಡ್';
      document.getElementById('timerDisplay').classList.add('hidden');
      document.getElementById('statusHint').innerText = 'ಬಟನ್ ಒತ್ತಿ ಮಾತನಾಡಿ, ಮುಗಿದಾಗ ಮತ್ತೊಮ್ಮೆ ಒತ್ತಿ';
    }

    async function uploadAudio() {
      const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
      const formData = new FormData();
      formData.append('file', audioBlob, 'voice.webm');

      showLoading(true, 'ಧ್ವನಿಯನ್ನು ಪರಿವರ್ತಿಸಲಾಗುತ್ತಿದೆ...', 'Sarvam AI ಮತ್ತು Gemini Flash ಲೆಕ್ಕ ಮಾಡುತ್ತಿವೆ');
      try {
        const res = await fetch('/api/voice', { method: 'POST', body: formData });
        showLoading(false);
        const data = await res.json().catch(() => null);
        if (res.ok && data) {
          showConfirmation(data);
        } else {
          alert('Error: ' + (data && data.detail ? data.detail : 'Failed to process audio'));
        }
      } catch (err) {
        showLoading(false);
        alert('Network error: ' + err.message);
      }
    }

    async function submitText() {
      const text = document.getElementById('textInput').value.trim();
      if (!text) return;
      showLoading(true, 'ವಿವರಗಳನ್ನು ಓದಲಾಗುತ್ತಿದೆ...', 'Gemini Flash ಲೆಕ್ಕವನ್ನು ಪರಿಶೀಲಿಸುತ್ತಿದೆ');
      try {
        const res = await fetch('/api/text', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text })
        });
        showLoading(false);
        const data = await res.json().catch(() => null);
        if (res.ok && data) {
          document.getElementById('textInput').value = '';
          showConfirmation(data);
        } else {
          alert('Error: ' + (data && data.detail ? data.detail : 'Failed to process note'));
        }
      } catch (err) {
        showLoading(false);
        alert('Network error: ' + err.message);
      }
    }

    function showLoading(show, title, sub) {
      const b = document.getElementById('loadingBanner');
      if (show) {
        document.getElementById('loadingTitle').innerText = title;
        document.getElementById('loadingSub').innerText = sub;
        b.classList.remove('hidden');
        document.getElementById('confirmCard').classList.add('hidden');
      } else {
        b.classList.add('hidden');
      }
    }

    function showConfirmation(data) {
      currentRecId = data.rec_id;
      document.getElementById('confirmTranscript').innerText = data.transcript || '(ಯಾವುದೇ ಮಾತು ಕೇಳಿಸಲಿಲ್ಲ)';
      document.getElementById('confirmTime').innerText = new Date().toLocaleTimeString();

      const container = document.getElementById('confirmEntriesContainer');
      const unclear = document.getElementById('unclearWarning');

      if (!data.entries || data.entries.length === 0) {
        container.innerHTML = '';
        unclear.innerText = data.unclear_reason || 'ಮೊತ್ತ ಅಥವಾ ವಿವರ ಸರಿಯಾಗಿ ಅರ್ಥವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.';
        unclear.classList.remove('hidden');
      } else {
        unclear.classList.add('hidden');
        container.innerHTML = data.entries.map(e => `
          <div class="p-3 bg-emerald-50/70 border border-emerald-200 rounded-xl space-y-1">
            <div class="flex items-center justify-between">
              <span class="text-xs font-bold uppercase tracking-wider ${e.direction === 'expense' ? 'text-rose-700 bg-rose-100' : 'text-emerald-700 bg-emerald-100'} px-2 py-0.5 rounded">
                ${e.direction === 'expense' ? 'ಖರ್ಚು (Paid)' : 'ಆದಾಯ (Received)'}
              </span>
              <span class="text-base font-extrabold text-slate-800">₹ ${e.amount.toLocaleString('en-IN', {minimumFractionDigits: 2})}</span>
            </div>
            <div class="text-sm font-bold text-slate-800">${e.item_en} ${e.party ? `— ${e.party}` : ''}</div>
            <div class="text-xs text-slate-500 flex items-center justify-between pt-1">
              <span>ದಿನಾಂಕ: <b>${e.date}</b></span>
              <span>ವರ್ಗ: <b>${e.category}</b></span>
            </div>
            ${e.spoken ? `<div class="text-xs text-emerald-800 font-medium">ಹೇಳಿದ ಮೊತ್ತ: "${e.spoken}"</div>` : ''}
          </div>
        `).join('');
      }

      document.getElementById('confirmCard').classList.remove('hidden');
      document.getElementById('confirmCard').scrollIntoView({ behavior: 'smooth' });
    }

    async function confirmAction(action) {
      if (!currentRecId) return;
      try {
        const res = await fetch('/api/confirm', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rec_id: currentRecId, action })
        });
        const data = await res.json();
        document.getElementById('confirmCard').classList.add('hidden');
        currentRecId = null;
        fetchBalance();
        loadEntries();
      } catch (err) {
        alert('Action error: ' + err.message);
      }
    }

    function openOpeningModal() { document.getElementById('openingModal').classList.remove('hidden'); }
    function closeOpeningModal() { document.getElementById('openingModal').classList.add('hidden'); }

    async function saveOpening() {
      const val = parseFloat(document.getElementById('openingInput').value);
      if (isNaN(val) || val < 0) return alert('ದಯವಿಟ್ಟು ಸರಿಯಾದ ಮೊತ್ತ ನಮೂದಿಸಿ');
      try {
        const res = await fetch('/api/opening', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ amount_rupees: val })
        });
        closeOpeningModal();
        fetchBalance();
        loadEntries();
      } catch (err) {
        alert('Failed to set opening: ' + err.message);
      }
    }
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(content=INDEX_HTML)


def main():
    import uvicorn
    uvicorn.run("diary_bot.web:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
