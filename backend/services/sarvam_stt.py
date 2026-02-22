"""
Speech-to-Text service using Sarvam AI (Saarika v2.5).

Sarvam AI is optimized for Indian languages and accents.
Supports auto-detection of 11+ Indian languages including Hindi, Telugu,
Tamil, Kannada, Bengali, Marathi, Gujarati, Malayalam, Punjabi, Odia,
and Indian English.

Setup:
  1. Sign up at https://dashboard.sarvam.ai
  2. Create an API key
  3. Set SARVAM_API_KEY in your .env

Docs: https://docs.sarvam.ai/api-reference-docs/speech-to-text/transcribe
"""

from __future__ import annotations
import logging

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"


async def transcribe_audio(
    audio_bytes: bytes,
    language: str = "unknown",
    prompt: str = "",
) -> str:
    """
    Transcribe an audio buffer using Sarvam AI STT.

    Args:
        audio_bytes: Raw audio data (WAV recommended at 16kHz mono).
        language: BCP-47 language code or "unknown" for auto-detection.
                  Auto-detection handles mixed Indian language audio well.
        prompt: Unused (kept for API compatibility with previous Groq STT).

    Returns:
        Transcription text.
    """
    headers = {"api-subscription-key": settings.sarvam_api_key}

    files = {"file": ("audio.wav", audio_bytes, "audio/wav")}
    data = {
        "model": settings.sarvam_stt_model,
        "language_code": language,
    }

    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            SARVAM_STT_URL,
            headers=headers,
            data=data,
            files=files,
        )
        response.raise_for_status()
        result = response.json()

    transcript = result.get("transcript", "").strip()
    detected = result.get("language_code") or "unknown"
    logger.info(f"[STT] Transcribed [{detected}]: {transcript[:100]}...")
    return transcript


async def transcribe_audio_verbose(
    audio_bytes: bytes,
    language: str = "unknown",
) -> dict:
    """
    Transcribe and return result wrapped in a segments dict so call_monitor
    can consume it the same way it consumed Groq verbose output.

    Sarvam does not provide per-segment timestamps on the REST endpoint,
    so a single segment covering the full audio is returned.

    Returns:
        {"text": str, "segments": [{"start": 0.0, "end": 30.0, "text": str}]}
    """
    transcript = await transcribe_audio(audio_bytes, language=language)

    if not transcript:
        return {"text": "", "segments": []}

    return {
        "text": transcript,
        "segments": [
            {
                "start": 0.0,
                "end": 30.0,
                "text": transcript,
            }
        ],
    }
