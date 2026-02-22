"""
Text-to-Speech service using Deepgram Aura.

Deepgram is used because Groq does not currently offer TTS.
This module is designed to be swappable — replace with ElevenLabs,
OpenAI TTS, or any other provider by implementing the same interface.

Setup:
  1. Sign up at https://console.deepgram.com
  2. Create an API key
  3. Set DEEPGRAM_API_KEY in your .env

Available Deepgram Aura voices:
  - aura-asteria-en   (female, natural)
  - aura-luna-en      (female, warm)
  - aura-stella-en    (female, professional)
  - aura-athena-en    (female, friendly)
  - aura-hera-en      (female, authoritative)
  - aura-orion-en     (male, natural)
  - aura-arcas-en     (male, warm)
  - aura-perseus-en   (male, deep)
  - aura-angus-en     (male, casual)
  - aura-orpheus-en   (male, professional)
  - aura-helios-en    (male, energetic)
  - aura-zeus-en      (male, authoritative)
"""

from __future__ import annotations
import logging
import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"


async def text_to_speech(
    text: str,
    model: str | None = None,
    sample_rate: int | None = None,
    encoding: str = "mp3",
) -> bytes:
    """
    Convert text to speech audio bytes using Deepgram Aura.

    Args:
        text: The text to synthesize
        model: Deepgram voice model (default from config)
        sample_rate: Audio sample rate (None lets Deepgram use default;
                     pass explicitly for mulaw/linear16)
        encoding: Audio encoding ('mp3' for browser, 'mulaw' for Twilio)

    Returns:
        Audio bytes
    """
    model = model or settings.deepgram_tts_model

    params = {
        "model": model,
        "encoding": encoding,
    }
    if sample_rate is not None:
        params["sample_rate"] = sample_rate

    headers = {
        "Authorization": f"Token {settings.deepgram_api_key}",
        "Content-Type": "application/json",
    }

    payload = {"text": text}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            DEEPGRAM_TTS_URL,
            params=params,
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        audio_bytes = response.content
        logger.info(f"[TTS] Generated {len(audio_bytes)} bytes for: {text[:60]}...")
        return audio_bytes


async def text_to_speech_for_browser(
    text: str,
    model: str | None = None,
) -> bytes:
    """
    Generate TTS audio suitable for browser playback (linear16 WAV).
    """
    return await text_to_speech(
        text=text,
        model=model,
        sample_rate=24000,
        encoding="linear16",
    )


async def text_to_speech_mp3(
    text: str,
    model: str | None = None,
) -> bytes:
    """
    Generate TTS audio as MP3 for browser playback.
    Does not pass sample_rate -- lets Deepgram use its default.
    """
    return await text_to_speech(
        text=text,
        model=model,
        encoding="mp3",
    )


async def text_to_speech_for_twilio(
    text: str,
    model: str | None = None,
) -> bytes:
    """
    Generate TTS audio suitable for Twilio telephony (8kHz mulaw).
    """
    return await text_to_speech(
        text=text,
        model=model,
        sample_rate=8000,
        encoding="mulaw",
    )


# ──────────────────────────────────────────
# Alternative TTS providers (swap in as needed)
# ──────────────────────────────────────────

"""
# === ElevenLabs TTS ===
# pip install elevenlabs
# Set ELEVENLABS_API_KEY in .env

from elevenlabs import generate, set_api_key

set_api_key(os.environ["ELEVENLABS_API_KEY"])

async def text_to_speech_elevenlabs(text: str, voice: str = "Rachel") -> bytes:
    audio = generate(text=text, voice=voice, model="eleven_turbo_v2_5")
    return audio


# === OpenAI TTS ===
# pip install openai
# Set OPENAI_API_KEY in .env

from openai import OpenAI

client = OpenAI()

async def text_to_speech_openai(text: str, voice: str = "alloy") -> bytes:
    response = client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text,
        response_format="wav",
    )
    return response.content


# === Cartesia TTS (Sonic) ===
# pip install cartesia
# Set CARTESIA_API_KEY in .env

import cartesia

client = cartesia.Cartesia(api_key=os.environ["CARTESIA_API_KEY"])

async def text_to_speech_cartesia(text: str) -> bytes:
    output = client.tts.bytes(
        model_id="sonic-2024-10-19",
        transcript=text,
        voice_id="a0e99841-438c-4a64-b679-ae501e7d6091",
        output_format={"container": "wav", "sample_rate": 8000, "encoding": "pcm_mulaw"},
    )
    return output
"""
