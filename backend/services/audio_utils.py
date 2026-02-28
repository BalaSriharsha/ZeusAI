"""
Audio utilities for processing phone stream audio (Twilio and Exotel).

Both providers normalise to slin16 PCM before entering the queue:
  - Twilio   : mulaw (8-bit) decoded to slin16 PCM at ingestion time
  - Exotel   : already slin16 PCM (16-bit, 8kHz, mono, little-endian)

All downstream code (VAD, WAV conversion, STT) works with raw slin16 PCM.
"""

from __future__ import annotations
import asyncio
import io
import logging
import struct
import time
import wave

logger = logging.getLogger(__name__)

# Average absolute amplitude (slin16) below this is treated as silence.
# slin16 values range from -32768 to 32767.
ENERGY_THRESHOLD = 40
# How many seconds of silence signal the end of a speech turn
SILENCE_DURATION = 2.0
# Maximum duration to wait for speech (seconds)
MAX_SPEECH_WAIT = 45.0
# Minimum speech duration to be worth transcribing (0.4s at 8kHz 16-bit)
MIN_SPEECH_BYTES = 6400  # 8000 samples/s * 0.4s * 2 bytes


def mulaw_to_linear(b: int) -> int:
    """Decode a single mulaw byte to signed 16-bit linear PCM sample."""
    b = ~b & 0xFF
    sign = b & 0x80
    exponent = (b >> 4) & 0x07
    mantissa = b & 0x0F
    magnitude = ((mantissa << 3) + 0x84) << exponent
    magnitude -= 0x84
    return -magnitude if sign else magnitude


def mulaw_to_pcm_bytes(mulaw_bytes: bytes) -> bytes:
    """
    Convert raw mulaw bytes (from Twilio) to signed 16-bit little-endian PCM.
    Call this at queue-insertion time so the queue always holds slin16 PCM.
    """
    samples = [mulaw_to_linear(b) for b in mulaw_bytes]
    return struct.pack(f"<{len(samples)}h", *samples)


def pcm_to_mulaw_bytes(pcm_bytes: bytes) -> bytes:
    """
    Convert raw signed 16-bit little-endian PCM to raw mulaw bytes.
    Used to prepare TTS audio for Twilio Media Streams.
    """
    num_samples = len(pcm_bytes) // 2
    linear_samples = struct.unpack(f"<{num_samples}h", pcm_bytes)
    return bytes(_linear_to_mulaw(s) for s in linear_samples)


def chunk_energy(data: bytes) -> float:
    """Calculate average absolute amplitude of a slin16 PCM chunk."""
    num_samples = len(data) // 2
    if num_samples == 0:
        return 0.0
    samples = struct.unpack(f"<{num_samples}h", data[:num_samples * 2])
    return sum(abs(s) for s in samples) / num_samples


def is_speech(data: bytes, threshold: float = ENERGY_THRESHOLD) -> bool:
    """Check if a slin16 PCM chunk contains speech above the threshold."""
    return chunk_energy(data) > threshold


def pcm16_to_wav(pcm_bytes: bytes, sample_rate: int = 8000) -> bytes:
    """
    Wrap raw 16-bit little-endian PCM bytes into WAV format.
    Returns a complete WAV file as bytes, suitable for STT.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
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
        queue: asyncio.Queue receiving slin16 PCM audio chunks (bytes)
        timeout: Maximum seconds to wait for any speech
        silence_duration: Seconds of silence to end a speech turn
        energy_threshold: Amplitude threshold to distinguish speech from silence

    Returns:
        Raw slin16 PCM bytes of the speech segment, or empty bytes if nothing detected.
    """
    audio_buffer = bytearray()
    speech_detected = False
    silence_start: float | None = None
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            chunk = await asyncio.wait_for(queue.get(), timeout=0.3)
        except asyncio.TimeoutError:
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


# ------------------------------------------------
# DTMF Tone Generation
# ------------------------------------------------

# ITU-T standard DTMF frequency pairs
_DTMF_FREQS: dict[str, tuple[int, int]] = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
}

import math

def generate_dtmf_tone(
    digit: str,
    sample_rate: int = 8000,
    duration: float = 0.25,
    gap: float = 0.05,
    amplitude: float = 0.5,
) -> bytes:
    """
    Generate a DTMF tone as raw slin16 PCM (16-bit, mono, little-endian).

    Works directly for Exotel streams. For Twilio, convert to mulaw
    with generate_dtmf_tone_mulaw().

    Args:
        digit: One of 0-9, *, #
        sample_rate: Sample rate in Hz (8000 for telephony)
        duration: Tone duration in seconds
        gap: Silence gap after the tone in seconds
        amplitude: Volume (0.0 to 1.0)

    Returns:
        Raw slin16 PCM bytes (tone + silence gap)
    """
    freqs = _DTMF_FREQS.get(digit)
    if not freqs:
        logger.warning(f"[DTMF] Unknown digit: {digit}")
        return b""

    f1, f2 = freqs
    max_val = 32767 * amplitude
    num_tone_samples = int(sample_rate * duration)
    num_gap_samples = int(sample_rate * gap)

    samples = []
    for i in range(num_tone_samples):
        t = i / sample_rate
        value = (math.sin(2 * math.pi * f1 * t) +
                 math.sin(2 * math.pi * f2 * t)) / 2
        samples.append(int(value * max_val))

    # Silence gap
    samples.extend([0] * num_gap_samples)

    return struct.pack(f"<{len(samples)}h", *samples)


def _linear_to_mulaw(sample: int) -> int:
    """Encode a signed 16-bit linear PCM sample to mulaw byte."""
    sign = 0x80 if sample < 0 else 0
    val = sample if sample >= 0 else -sample
    
    if val > 32635:
        val = 32635
    val += 132
    
    exponent = 7
    for exp in range(7, -1, -1):
        if val & (0x80 << exp):
            exponent = exp
            break
            
    mantissa = (val >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def generate_dtmf_tone_mulaw(
    digit: str,
    sample_rate: int = 8000,
    duration: float = 0.25,
    gap: float = 0.05,
    amplitude: float = 0.5,
) -> bytes:
    """
    Generate a DTMF tone as raw mulaw bytes (for Twilio streams).
    """
    pcm = generate_dtmf_tone(digit, sample_rate, duration, gap, amplitude)
    if not pcm:
        return b""
    # Convert slin16 PCM to mulaw
    num_samples = len(pcm) // 2
    linear_samples = struct.unpack(f"<{num_samples}h", pcm)
    return bytes(_linear_to_mulaw(s) for s in linear_samples)
