"""
Twilio telephony service — handles outbound calls, DTMF, and media streams.

This service:
  1. Initiates outbound calls via Twilio REST API
  2. Configures media streams to receive real-time audio from the call
  3. Sends DTMF tones into an active call
  4. Plays audio (TTS output) into an active call
  5. Handles call status webhooks
"""

from __future__ import annotations
import base64
import logging
import json
from typing import Optional

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from backend.config import settings

logger = logging.getLogger(__name__)

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


def initiate_call(
    to_number: str,
    call_id: str,
    status_callback_url: Optional[str] = None,
) -> str:
    """
    Initiate an outbound call via Twilio.

    The TwiML response instructs Twilio to:
    1. Open a bidirectional media stream to our WebSocket server
    2. This lets us receive audio from the call AND play audio into it

    Args:
        to_number: The toll-free number to call
        call_id: Our internal call ID for tracking
        status_callback_url: URL for Twilio to send status updates

    Returns:
        Twilio Call SID
    """
    client = _get_client()
    base_url = settings.public_base_url

    # The TwiML URL will be served by our FastAPI endpoint
    twiml_url = f"{base_url}/api/twilio/twiml/{call_id}"
    status_url = status_callback_url or f"{base_url}/api/twilio/status/{call_id}"

    call = client.calls.create(
        to=to_number,
        from_=settings.twilio_phone_number,
        url=twiml_url,
        status_callback=status_url,
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
        record=False,
    )

    logger.info(f"[TWILIO] Call initiated: SID={call.sid}, to={to_number}")
    return call.sid


def generate_stream_twiml(call_id: str) -> str:
    """
    Generate TwiML that connects a bidirectional media stream.

    This is served at the URL Twilio hits when the call connects.
    The media stream sends us real-time audio from the call,
    and allows us to send audio back.
    """
    base_url = settings.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
    stream_url = f"{base_url}/ws/twilio-stream/{call_id}"

    response = VoiceResponse()

    # Start a bidirectional media stream
    connect = Connect()
    stream = Stream(url=stream_url)
    stream.parameter(name="call_id", value=call_id)
    connect.append(stream)
    response.append(connect)

    # Keep the call alive (Twilio needs something after <Connect>)
    response.pause(length=3600)  # 1 hour max

    twiml = str(response)
    logger.info(f"[TWILIO] Generated stream TwiML for call {call_id}")
    return twiml


def send_dtmf(call_sid: str, digits: str) -> None:
    """
    Send DTMF tones into an active call.

    Args:
        call_sid: The Twilio Call SID
        digits: DTMF digits to send (e.g., "2", "15042026")
    """
    client = _get_client()

    # Use TwiML to play DTMF tones
    twiml = VoiceResponse()
    twiml.play(digits=digits)

    client.calls(call_sid).update(twiml=str(twiml))
    logger.info(f"[TWILIO] Sent DTMF: {digits} to call {call_sid}")


def play_audio_twiml(call_sid: str, audio_url: str) -> None:
    """
    Play an audio file into an active call.

    Args:
        call_sid: The Twilio Call SID
        audio_url: Public URL of the audio file to play
    """
    client = _get_client()

    twiml = VoiceResponse()
    twiml.play(audio_url)
    # Resume the media stream after playing
    connect = Connect()
    stream = Stream(url=f"{settings.public_base_url.replace('https://', 'wss://').replace('http://', 'ws://')}/ws/twilio-stream/{call_sid}")
    connect.append(stream)
    twiml.append(connect)
    twiml.pause(length=3600)

    client.calls(call_sid).update(twiml=str(twiml))
    logger.info(f"[TWILIO] Playing audio in call {call_sid}")


def end_call(call_sid: str) -> None:
    """Terminate an active call."""
    client = _get_client()
    client.calls(call_sid).update(status="completed")
    logger.info(f"[TWILIO] Ended call {call_sid}")


def encode_audio_for_stream(audio_bytes: bytes) -> str:
    """
    Encode audio bytes as base64 for sending via Twilio media stream.

    The audio must be 8kHz mulaw mono for Twilio.
    """
    return base64.b64encode(audio_bytes).decode("utf-8")


def create_media_message(audio_base64: str, stream_sid: str) -> str:
    """
    Create a Twilio media stream message to play audio.

    This is sent over the WebSocket connection to the Twilio media stream.
    """
    return json.dumps({
        "event": "media",
        "streamSid": stream_sid,
        "media": {
            "payload": audio_base64,
        },
    })


def create_mark_message(stream_sid: str, mark_name: str) -> str:
    """
    Create a mark message to track when audio finishes playing.
    """
    return json.dumps({
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {
            "name": mark_name,
        },
    })
