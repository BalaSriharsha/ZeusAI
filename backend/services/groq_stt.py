"""
Groq Speech-to-Text service using Whisper.
Handles both one-shot transcription and chunked streaming.
"""

from __future__ import annotations
import io
import tempfile
import logging
from pathlib import Path

from groq import Groq

from backend.config import settings

logger = logging.getLogger(__name__)

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.groq_api_key)
    return _client


async def transcribe_audio(
    audio_bytes: bytes,
    language: str = "en",
    prompt: str = "",
) -> str:
    """
    Transcribe an audio buffer using Groq Whisper.

    Args:
        audio_bytes: Raw audio data (WAV, MP3, FLAC, OGG, WebM, etc.)
        language: Language hint (ISO 639-1)
        prompt: Optional prompt for context

    Returns:
        Transcription text
    """
    client = _get_client()

    # Write to a temp file (Groq SDK expects a file-like object with a name)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp_path = Path(tmp.name)

    try:
        with open(tmp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model=settings.groq_stt_model,
                file=audio_file,
                language=language,
                prompt=prompt,
                response_format="text",
            )
        result = transcription.strip() if isinstance(transcription, str) else transcription.text.strip()
        logger.info(f"[STT] Transcribed: {result[:100]}...")
        return result
    except Exception as e:
        logger.error(f"[STT] Transcription failed: {e}")
        raise
    finally:
        tmp_path.unlink(missing_ok=True)


async def transcribe_audio_verbose(
    audio_bytes: bytes,
    language: str = "en",
) -> dict:
    """
    Transcribe with timestamps and segment info (verbose JSON mode).
    Useful for understanding timing of IVR responses.
    """
    client = _get_client()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp_path = Path(tmp.name)

    try:
        with open(tmp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model=settings.groq_stt_model,
                file=audio_file,
                language=language,
                response_format="verbose_json",
            )
        return {
            "text": transcription.text,
            "segments": [
                {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                }
                for seg in (transcription.segments or [])
            ],
        }
    except Exception as e:
        logger.error(f"[STT] Verbose transcription failed: {e}")
        raise
    finally:
        tmp_path.unlink(missing_ok=True)
