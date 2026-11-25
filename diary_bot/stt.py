"""Kannada speech-to-text via Sarvam AI (REST endpoint, audio up to 30 s).

Verified against Sarvam docs: POST https://api.sarvam.ai/speech-to-text,
header `api-subscription-key`, multipart `file`, `model`, `language_code`.
`saaras:v3` is their recommended model (extra `mode` field); `saarika:v2.5` is the
legacy model and is used automatically as a fallback.
"""
from __future__ import annotations

import asyncio

import httpx

API_URL = "https://api.sarvam.ai/speech-to-text"
FALLBACK_MODEL = "saarika:v2.5"
RETRY_STATUS = {429, 500, 502, 503, 504}


class STTError(Exception):
    """Raised with a short, non-sensitive message."""


async def transcribe_kannada(
    audio: bytes,
    api_key: str,
    model: str = "saaras:v3",
    *,
    client: httpx.AsyncClient | None = None,
    retries: int = 3,
    backoff: float = 1.0,
) -> tuple[str, str]:
    """Returns (kannada_transcript, model_used)."""
    models = [model] + ([FALLBACK_MODEL] if model != FALLBACK_MODEL else [])
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    last = "unknown error"
    fn = "voice.ogg"
    ct = "audio/ogg"
    if audio.startswith(b"\x1a\x45\xdf\xa3"):
        fn, ct = "voice.webm", "audio/webm"
    elif audio.startswith(b"RIFF"):
        fn, ct = "voice.wav", "audio/wav"
    elif audio.startswith(b"ID3") or audio.startswith(b"\xff\xfb"):
        fn, ct = "voice.mp3", "audio/mpeg"

    try:
        for m in models:
            data = {"model": m, "language_code": "kn-IN"}
            if m.startswith("saaras"):
                data["mode"] = "transcribe"
            for attempt in range(retries):
                try:
                    r = await client.post(
                        API_URL,
                        headers={"api-subscription-key": api_key},
                        data=data,
                        files={"file": (fn, audio, ct)},
                    )
                except httpx.HTTPError as e:
                    last = f"network error ({type(e).__name__})"
                    await asyncio.sleep(backoff * (2 ** attempt))
                    continue
                if r.status_code == 200:
                    text = (r.json().get("transcript") or "").strip()
                    if not text:
                        raise STTError("No speech detected in the recording.")
                    return text, m
                last = f"Sarvam returned HTTP {r.status_code}"
                if r.status_code in RETRY_STATUS:
                    await asyncio.sleep(backoff * (2 ** attempt))
                    continue
                if r.status_code in (401, 403):
                    raise STTError("Sarvam rejected the API key.")
                break  # other 4xx: try the fallback model
        raise STTError(last)
    finally:
        if own:
            await client.aclose()
