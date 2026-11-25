# Daily Diary Bot (Kannada voice -> English ledger)

A voice-first daily diary & ledger that turns spoken Kannada notes into structured English financial records using **Sarvam AI** and **Google Gemini** (or Claude), with an append-only, tamper-evident hash-chained SQLite database, available as both a **Web Dashboard** and a **Telegram Bot**.

```
Kannada Voice Note (Web / Telegram)
  -> Encrypted audio saved at rest (never lost)
  -> Sarvam Saaras/Saarika: Kannada speech-to-text
  -> Gemini / Claude: Kannada transcript -> English entries (glossary + strict schema + validation)
  -> Review screen: you tap Save / Discard (money is never saved unconfirmed)
  -> Append-only hash-chained SQLite ledger
  -> Export: Excel (with Kannada original speech) + PDF report
```

---

## Features

- 🎙️ **Voice & Text Input in Kannada**: Speak naturally via browser microphone or Telegram voice notes.
- ⚡ **Sarvam AI & Gemini Powered**: Uses Sarvam AI for Kannada speech recognition and Google Gemini Flash (or Claude) for ledger entry extraction.
- 📱 **Dual Interfaces**:
  - **Web Dashboard (`diary_bot.web`)**: Accessible in any browser on PC or mobile phone over local Wi-Fi with large buttons, live recording, and real-time cash balance tracking.
  - **Telegram Bot (`diary_bot.bot`)**: Run as a Telegram chat bot.
- 🔒 **Never Save Unconfirmed**: Confirmation card with **Save** and **Discard** before anything enters the ledger.
- 🛡️ **Tamper-Evident Ledger**: SQLite database with triggers preventing `UPDATE`/`DELETE` and cryptographic SHA-256 hash chaining.
- 🔐 **Encrypted at Rest**: Audio recordings and backups encrypted using AES/Fernet.
- 📊 **One-Click Exports**: Download monthly **Excel (.xlsx)** (includes Kannada original transcript) and **PDF** reports.

---

## Quickstart

### 1. Clone & Install Dependencies
```bash
git clone https://github.com/Manasa-L-Hegde/Diary_bot.git
cd Diary_bot
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure Credentials
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Generate an encryption key:
```bash
python -m diary_bot.crypto genkey
```
Paste it into `DIARY_ENC_KEY` in `.env`.

Add your API keys:
- **Sarvam AI**: `SARVAM_API_KEY` ([dashboard.sarvam.ai](https://dashboard.sarvam.ai))
- **Google Gemini**: `GEMINI_API_KEY` (Free at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey))
- **Telegram (optional)**: `TELEGRAM_BOT_TOKEN` & `ALLOWED_USER_IDS`

### 3. Run Tests
```bash
pytest -q
# All 37 tests should pass
```

### 4. Run the Application

#### Option A: Web Dashboard (Recommended)
```bash
python -m diary_bot.web
```
Open **http://localhost:8000** in your browser (or `http://<your-ip>:8000` on your mobile phone on the same Wi-Fi).

#### Option B: Telegram Bot
```bash
python -m diary_bot.bot
```

---

## Telegram Bot Commands

| Command | What it does |
|---|---|
| `/today` | Today's saved entries and totals |
| `/month [YYYY-MM]` | Month totals by category |
| `/report [YYYY-MM]` | Sends Excel + PDF |
| `/opening <amount> [date]` | Set opening cash in hand |
| `/balance` | Cash in hand (Opening + Received - Paid) |
| `/fix <id> <field> <value>` | Correct an entry (old version preserved in history) |
| `/term ಕನ್ನಡ = English` | Teach a custom farm term |
| `/verify` | Validates hash chain integrity |

---

## Security Model

- **Allowlist Only**: Unknown Telegram users are silently ignored.
- **Append-Only Ledger**: SQL triggers abort any `UPDATE` or `DELETE`. Every row is hash-chained.
- **Audio Encrypted at Rest**: Voice notes encrypted with AES-128 / Fernet.
- **Injection Defences**: Transcripts are treated as untrusted data; model outputs are validated via strict Python schemas. Formula injection protected for Excel/PDF exports.
