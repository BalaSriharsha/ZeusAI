"""
Text-to-Speech service using Sarvam AI.

Supports Bulbul v2 and v3 models. v3 has more voices but may
occasionally be unavailable; the service automatically falls back to v2.

Setup:
  1. Sign up at https://dashboard.sarvam.ai
  2. Create an API key
  3. Set SARVAM_API_KEY in your .env

Available voices:
  Bulbul v3: shubh, aditya, rahul, rohan, amit, dev, ratan, varun,
             manan, sumit, kabir, aayan, tarun, sunny, gokul, vijay,
             mohit, ritu, priya, neha, pooja, simran, kavya, ishita,
             shreya, roopa, tanya, shruti, suhani, kavitha, rupali
  Bulbul v2: arya, karun, hitesh, anushka, abhilash, manisha, vidya

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

# V3 -> V2 speaker mapping (v3 speakers are NOT compatible with v2)
_V2_FALLBACK_SPEAKER = "manisha"

# Models to try in order
_MODELS = ["bulbul:v3", "bulbul:v2"]


async def _sarvam_tts(
    text: str,
    speech_sample_rate: int,
    audio_format: str,
    speaker: str | None = None,
    language: str | None = None,
) -> bytes:
    """
    Core call to Sarvam AI TTS with automatic model fallback.

    Tries bulbul:v3 first. If it fails (500), falls back to bulbul:v2
    with a compatible speaker.

    Args:
        text: Text to synthesize (max 2500 chars).
        speech_sample_rate: 8000 for Twilio, 24000 for browser.
        audio_format: "wav", "mp3", etc.
        speaker: Override the default speaker from config.
        language: Override the default TTS language from config.

    Returns:
        Raw audio bytes.
    """
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS]
        logger.warning(f"[TTS] Text truncated to {_MAX_CHARS} chars")

    chosen_speaker = speaker or settings.sarvam_tts_speaker

    headers = {
        "api-subscription-key": settings.sarvam_api_key,
        "Content-Type": "application/json",
    }

    last_error = None

    for model in _MODELS:
        # V2 requires different speakers than V3
        if model == "bulbul:v2":
            model_speaker = _V2_FALLBACK_SPEAKER
        else:
            model_speaker = chosen_speaker

        payload = {
            "text": text,
            "model": model,
            "target_language_code": language or settings.sarvam_tts_language,
            "speaker": model_speaker,
            "speech_sample_rate": speech_sample_rate,
            "audio_format": audio_format,
        }

        try:
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
                raise ValueError(f"Sarvam TTS returned no audio")

            audio_bytes = base64.b64decode(audios[0])
            logger.info(
                f"[TTS] Generated {len(audio_bytes)} bytes "
                f"({model} {audio_format} {speech_sample_rate}Hz, "
                f"speaker={model_speaker}) for: {text[:60]}..."
            )
            return audio_bytes

        except Exception as e:
            last_error = e
            logger.warning(
                f"[TTS] {model} (speaker={model_speaker}) failed: {e}. "
                f"{'Trying next model...' if model != _MODELS[-1] else 'No more models.'}"
            )

    raise last_error or ValueError(f"All TTS models failed for: {text[:60]}...")


async def text_to_speech_mp3(
    text: str,
    model: str | None = None,
    language: str | None = None,
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
        language=language,
    )


async def text_to_speech_for_twilio(
    text: str,
    model: str | None = None,
    language: str | None = None,
) -> bytes:
    """
    Generate 8kHz raw mulaw audio for Twilio Media Streams.

    Requests WAV format from Sarvam TTS at 8kHz, strips the WAV header
    to get raw PCM, then converts to mulaw for Twilio.
    """
    from backend.services.audio_utils import pcm_to_mulaw_bytes

    audio_bytes = await _sarvam_tts(
        text=text,
        speech_sample_rate=8000,
        audio_format="wav",
        language=language,
    )

    # Strip WAV header to get raw PCM
    if audio_bytes[:4] == b"RIFF":
        buf = io.BytesIO(audio_bytes)
        import wave as _wave
        with _wave.open(buf, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            pcm_bytes = wf.readframes(wf.getnframes())
            logger.info(
                f"[TTS] WAV: {n_channels}ch, {sampwidth*8}-bit, "
                f"{framerate}Hz, {len(pcm_bytes)} PCM bytes"
            )
    else:
        pcm_bytes = audio_bytes

    # Convert signed 16-bit little-endian PCM to raw mulaw
    mulaw_bytes = pcm_to_mulaw_bytes(pcm_bytes)
    logger.info(
        f"[TTS] Converted {len(pcm_bytes)} PCM bytes -> "
        f"{len(mulaw_bytes)} mulaw bytes"
    )
    return mulaw_bytes


async def text_to_speech_for_call(
    text: str,
    model: str | None = None,
    language: str | None = None,
) -> bytes:
    """
    Generate 8kHz linear16 PCM audio for Exotel Voice Streaming.
    Returns raw 16-bit little-endian PCM bytes (no WAV header).
    The `model` parameter is accepted for API compatibility but unused.
    """
    audio_bytes = await _sarvam_tts(
        text=text,
        speech_sample_rate=8000,
        audio_format="wav",
        language=language,
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
    language: str | None = None,
) -> bytes:
    """Generate WAV audio for browser playback."""
    return await _sarvam_tts(
        text=text,
        speech_sample_rate=24000,
        audio_format="wav",
        language=language,
    )
