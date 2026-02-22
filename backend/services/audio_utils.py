"""
Audio utilities for processing Twilio Media Stream audio.

Handles mulaw decoding, energy calculation, voice activity detection,
and conversion to WAV for STT.
"""

from __future__ import annotations
import asyncio
import io
import logging
import struct
import time

logger = logging.getLogger(__name__)

# Mulaw silence threshold -- average absolute PCM amplitude below this is silence.
# Lowered to 40 to catch quieter phone audio from real Twilio calls.
ENERGY_THRESHOLD = 40
# How many seconds of silence signal the end of a speech turn
SILENCE_DURATION = 2.0
# Maximum duration to wait for speech (seconds)
MAX_SPEECH_WAIT = 45.0
# Minimum speech duration to be worth transcribing (seconds at 8kHz)
MIN_SPEECH_BYTES = 3200  # ~0.4 seconds


def mulaw_to_linear(b: int) -> int:
    """Decode a single mulaw byte to signed 16-bit linear PCM."""
    b = ~b & 0xFF
    sign = b & 0x80
    exponent = (b >> 4) & 0x07
    mantissa = b & 0x0F
    magnitude = ((mantissa << 3) + 0x84) << exponent
    magnitude -= 0x84
    return -magnitude if sign else magnitude


def chunk_energy(data: bytes) -> float:
    """Calculate average absolute amplitude of a mulaw audio chunk."""
    if not data:
        return 0.0
    total = sum(abs(mulaw_to_linear(b)) for b in data)
    return total / len(data)


def is_speech(data: bytes, threshold: float = ENERGY_THRESHOLD) -> bool:
    """Check if a mulaw audio chunk contains speech above the threshold."""
    return chunk_energy(data) > threshold


def mulaw_to_wav(mulaw_bytes: bytes, sample_rate: int = 8000) -> bytes:
    """
    Convert raw mulaw audio bytes to WAV format (16-bit PCM).
    Returns a complete WAV file as bytes, suitable for STT.
    """
    # Decode mulaw to 16-bit signed PCM
    pcm_samples = [mulaw_to_linear(b) for b in mulaw_bytes]

    # Write WAV
    buf = io.BytesIO()
    import wave
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(pcm_samples)}h", *pcm_samples))

    return buf.getvalue()


async def receive_speech(
    queue: asyncio.Queue,
    timeout: float = MAX_SPEECH_WAIT,
    silence_duration: float = SILENCE_DURATION,
    energy_threshold: float = ENERGY_THRESHOLD,
) -> bytes:
    """
    Receive and buffer audio chunks from an asyncio Queue until the
    speaker stops talking (detected by silence after speech).

    Args:
        queue: asyncio.Queue receiving mulaw audio chunks (bytes)
        timeout: Maximum seconds to wait for any speech
        silence_duration: Seconds of silence to end a speech turn
        energy_threshold: Amplitude threshold to distinguish speech from silence

    Returns:
        Raw mulaw bytes of the speech segment, or empty bytes if nothing detected.
    """
    audio_buffer = bytearray()
    speech_detected = False
    silence_start: float | None = None
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            chunk = await asyncio.wait_for(queue.get(), timeout=0.3)
        except asyncio.TimeoutError:
            # No audio received
            if speech_detected and silence_start is not None:
                if time.time() - silence_start >= silence_duration:
                    break
            continue

        # None is a sentinel value meaning the stream has ended
        if chunk is None:
            logger.info("[Audio] Stream ended (sentinel received)")
            break

        audio_buffer.extend(chunk)
        energy = chunk_energy(chunk)

        if energy > energy_threshold:
            speech_detected = True
            silence_start = None
        elif speech_detected:
            if silence_start is None:
                silence_start = time.time()
            elif time.time() - silence_start >= silence_duration:
                break

    if not speech_detected or len(audio_buffer) < MIN_SPEECH_BYTES:
        return bytes()

    return bytes(audio_buffer)
