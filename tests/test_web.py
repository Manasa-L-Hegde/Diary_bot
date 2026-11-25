from fastapi.testclient import TestClient
from diary_bot.web import app

client = TestClient(app)

def test_web_index():
    response = client.get("/")
    assert response.status_code == 200
    assert "ದೈನಂದಿನ ದಿನಚರಿ" in response.text

def test_web_balance():
    response = client.get("/api/balance")
    assert response.status_code == 200
    data = response.json()
    assert "balance" in data
    assert "ledger_verified" in data

def test_web_text_and_confirm(monkeypatch):
    # Mock extract
    from diary_bot.extract import ExtractionResult
    fake_res = ExtractionResult(entries=[{
        "entry_date": "2026-11-22",
        "direction": "expense",
        "amount_paise": 150000,
        "item_en": "Fertilizer",
        "item_kn": "ಗೊಬ್ಬರ",
        "category": "Fertilizer",
        "party": "Shop",
        "notes_en": "",
        "amount_spoken_kn": "ಒಂದೂವರೆ ಸಾವಿರ",
        "flags": ""
    }])
    monkeypatch.setattr("diary_bot.extract.extract_entries", lambda *a, **kw: fake_res)

    res = client.post("/api/text", json={"text": "ಗೊಬ್ಬರ 1500"})
    assert res.status_code == 200
    d = res.json()
    assert d["rec_id"] > 0
    assert len(d["entries"]) == 1
    assert d["entries"][0]["amount"] == 1500.0

    # Confirm
    conf = client.post("/api/confirm", json={"rec_id": d["rec_id"], "action": "save"})
    assert conf.status_code == 200
    assert conf.json()["ok"] is True

    # Check entries list
    entries_res = client.get("/api/entries?period=all")
    assert entries_res.status_code == 200
    all_entries = entries_res.json()["entries"]
    assert any(e["item_en"] == "Fertilizer" and e["amount"] == 1500.0 for e in all_entries)

def test_web_voice(monkeypatch):
    from diary_bot.extract import ExtractionResult
    async def fake_stt(*a, **kw):
        return "ಕೂಲಿ 2000", "saaras:v3"
    fake_res = ExtractionResult(entries=[{
        "entry_date": "2026-11-22",
        "direction": "expense",
        "amount_paise": 200000,
        "item_en": "Labour wages",
        "item_kn": "ಕೂಲಿ",
        "category": "Labour",
        "party": "",
        "notes_en": "",
        "amount_spoken_kn": "ಎರಡು ಸಾವಿರ",
        "flags": ""
    }])
    monkeypatch.setattr("diary_bot.stt.transcribe_kannada", fake_stt)
    monkeypatch.setattr("diary_bot.extract.extract_entries", lambda *a, **kw: fake_res)

    res = client.post("/api/voice", files={"file": ("test.webm", b"fake-audio-bytes", "audio/webm")})
    assert res.status_code == 200
    d = res.json()
    assert d["rec_id"] > 0
    assert d["transcript"] == "ಕೂಲಿ 2000"
    assert len(d["entries"]) == 1
    assert d["entries"][0]["amount"] == 2000.0
