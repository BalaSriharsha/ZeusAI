"""
Text-to-Speech service using Sarvam AI (Bulbul v3).

Bulbul v3 is purpose-built for Indian languages and accents.
Supports 11 languages (10 Indian + Indian English), 30+ voices,
code-mixed text (e.g. Hinglish), and natural Indian prosody.

Setup:
  1. Sign up at https://dashboard.sarvam.ai
  2. Create an API key
  3. Set SARVAM_API_KEY in your .env

Available voices (Bulbul v3):
  Male   : shubh (default), aditya, rahul, rohan, amit, dev, ratan, varun,
           manan, sumit, kabir, aayan, tarun, sunny, gokul, vijay, mohit
  Female : ritu, priya, neha, pooja, simran, kavya, ishita, shreya, roopa,
           tanya, shruti, suhani, kavitha, rupali

Supported language codes:
  en-IN  Indian English (default)
  hi-IN  Hindi       | te-IN  Telugu    | ta-IN  Tamil
  kn-IN  Kannada     | ml-IN  Malayalam | mr-IN  Marathi
  bn-IN  Bengali     | gu-IN  Gujarati  | pa-IN  Punjabi
  od-IN  Odia

Docs: https://docs.sarvam.ai/api-reference-docs/api-guides-tutorials/text-to-speech/rest-api
"""

from __future__ import annotations
import base64
import io
import logging

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
_MAX_CHARS = 2500


async def _sarvam_tts(
    text: str,
    speech_sample_rate: int,
    audio_format: str,
    speaker: str | None = None,
) -> bytes:
    """
    Core call to Sarvam AI TTS.

    Args:
        text: Text to synthesize (max 2500 chars).
        speech_sample_rate: 8000 for Twilio mulaw, 24000 for browser.
        audio_format: "mulaw" for Twilio, "mp3" for browser.
        speaker: Override the default speaker from config.

    Returns:
        Raw audio bytes.
    """
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS]
        logger.warning(f"[TTS] Text truncated to {_MAX_CHARS} chars")

    payload = {
        "text": text,
        "model": "bulbul:v3",
        "target_language_code": settings.sarvam_tts_language,
        "speaker": speaker or settings.sarvam_tts_speaker,
        "speech_sample_rate": speech_sample_rate,
        "audio_format": audio_format,
    }

    headers = {
        "api-subscription-key": settings.sarvam_api_key,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            SARVAM_TTS_URL,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        result = response.json()

    audios = result.get("audios", [])
    if not audios:
        raise ValueError(f"Sarvam TTS returned no audio for text: {text[:60]}...")

    audio_bytes = base64.b64decode(audios[0])
    logger.info(
        f"[TTS] Generated {len(audio_bytes)} bytes "
        f"({audio_format} {speech_sample_rate}Hz) for: {text[:60]}..."
    )
    return audio_bytes


async def text_to_speech_mp3(
    text: str,
    model: str | None = None,
) -> bytes:
    """
    Generate MP3 audio for browser playback.
    The `model` parameter is accepted for API compatibility but unused
    (speaker is configured via SARVAM_TTS_SPEAKER).
    """
    return await _sarvam_tts(
        text=text,
        speech_sample_rate=24000,
        audio_format="mp3",
    )


async def text_to_speech_for_twilio(
    text: str,
    model: str | None = None,
) -> bytes:
    """
    Generate 8kHz mulaw audio for Twilio Media Streams.
    The `model` parameter is accepted for API compatibility but unused.
    """
    return await _sarvam_tts(
        text=text,
        speech_sample_rate=8000,
        audio_format="mulaw",
    )


async def text_to_speech_for_call(
    text: str,
    model: str | None = None,
) -> bytes:
    """
    Generate 8kHz linear16 PCM audio for Exotel Voice Streaming.
    Returns raw 16-bit little-endian PCM bytes (no WAV header).
    The `model` parameter is accepted for API compatibility but unused.
    """
    audio_bytes = await _sarvam_tts(
        text=text,
        speech_sample_rate=8000,
        audio_format="linear16",
    )
    # Strip WAV header if Sarvam returns WAV-wrapped linear16
    if audio_bytes[:4] == b"RIFF":
        buf = io.BytesIO(audio_bytes)
        import wave as _wave
        with _wave.open(buf, "rb") as wf:
            return wf.readframes(wf.getnframes())
    return audio_bytes


async def text_to_speech_for_browser(
    text: str,
    model: str | None = None,
) -> bytes:
    """Generate WAV audio for browser playback."""
    return await _sarvam_tts(
        text=text,
        speech_sample_rate=24000,
        audio_format="wav",
    )
