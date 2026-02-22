"""
AI Phone Agent -- Main Application

FastAPI server that orchestrates:
  - Browser WebSocket (user <-> system)
  - Exotel phone calls (to the real target number) via Voice Streaming
  - Agent pipeline (transcription -> classification -> action)
"""

from __future__ import annotations
import asyncio
import base64
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Dict

import httpx

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.agents import InputAgent, CallMonitorAgent, ActionAgent
from backend.models.schemas import (
    CallState, CallStatus, ActionType,
)
from backend.services import tts_service, sarvam_stt
from backend.services.audio_utils import (
    receive_speech, pcm16_to_wav, mulaw_to_pcm_bytes,
)

# ------------------------------------------------
# Logging
# ------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------
# State Management
# ------------------------------------------------

active_calls: Dict[str, CallState] = {}
browser_connections: Dict[str, WebSocket] = {}
call_tasks: Dict[str, asyncio.Task] = {}

# Twilio Media Stream state
twilio_streams: Dict[str, WebSocket] = {}
twilio_stream_sids: Dict[str, str] = {}
twilio_call_sids: Dict[str, str] = {}

# Exotel Voice Stream state
exotel_streams: Dict[str, WebSocket] = {}
exotel_stream_sids: Dict[str, str] = {}
exotel_call_sids: Dict[str, str] = {}
# Mapping from Exotel call_sid -> internal call_id (for stream correlation)
exotel_sid_to_call_id: Dict[str, str] = {}

# Audio queues for real-call mode (stream handler -> conversation loop)
# Queue always contains slin16 PCM bytes regardless of provider
audio_queues: Dict[str, asyncio.Queue] = {}

# Exotel API base URL (Mumbai cluster for India)
_EXOTEL_BASE_URL = "https://api.in.exotel.com/v1/Accounts"

# ------------------------------------------------
# Twilio Client
# ------------------------------------------------

_twilio_client = None


def _get_twilio_client():
    global _twilio_client
    if _twilio_client is None:
        from twilio.rest import Client
        _twilio_client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
        )
    return _twilio_client


def _sanitize_phone(number: str) -> str:
    """
    Clean a phone number to E.164 format (+91XXXXXXXXXX for Indian numbers).
    """
    cleaned = re.sub(r'[^\d+]', '', number)

    if not cleaned.startswith('+'):
        if len(cleaned) == 10:
            cleaned = '+91' + cleaned
        elif len(cleaned) == 12 and cleaned.startswith('91'):
            cleaned = '+' + cleaned
        else:
            cleaned = '+' + cleaned
    return cleaned


# ------------------------------------------------
# Application
# ------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI Phone Agent starting up...")
    logger.info(f"   Groq LLM:       {settings.groq_llm_model}")
    logger.info(f"   Sarvam STT:     {settings.sarvam_stt_model}")
    logger.info(f"   Sarvam TTS:     {settings.sarvam_tts_speaker} / {settings.sarvam_tts_language}")
    logger.info(f"   Twilio number:  {settings.twilio_phone_number or '(not set)'}")
    logger.info(f"   Exotel number:  {settings.exotel_phone_number or '(not set)'}")
    logger.info(f"   Exotel app:     {settings.exotel_app_id or '(not set)'}")
    logger.info(f"   Public URL:     {settings.public_base_url}")
    logger.info(f"   Exotel stream:  {settings.public_base_url}/exotel/stream")
    logger.info(f"   Twilio stream:  {settings.public_base_url}/twilio/stream/<call_id>")
    yield
    for task in call_tasks.values():
        task.cancel()
    logger.info("AI Phone Agent shutting down...")


app = FastAPI(
    title="AI Phone Agent",
    version="4.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


# ------------------------------------------------
# Frontend
# ------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    with open("frontend/index.html") as f:
        return HTMLResponse(content=f.read())


# ------------------------------------------------
# Browser WebSocket
# ------------------------------------------------

@app.websocket("/ws/browser")
async def browser_websocket(ws: WebSocket):
    await ws.accept()
    call_id = str(uuid.uuid4())
    browser_connections[call_id] = ws
    logger.info(f"[WS] Browser connected: {call_id}")

    input_agent = InputAgent()

    try:
        await _send_to_browser(ws, "call_status", {
            "status": "connected",
            "message": "Connected. Type or speak your request.",
            "call_id": call_id,
            "default_name": settings.default_user_name,
            "default_phone": settings.default_user_phone,
        })

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "")
            data = msg.get("data", {})

            if msg_type == "user_text":
                text = data.get("text", "")
                if text:
                    await _handle_user_input(
                        ws, call_id, input_agent, text=text,
                        user_name=data.get("user_name"),
                        user_phone=data.get("user_phone"),
                    )

            elif msg_type == "user_audio":
                audio_b64 = data.get("audio", "")
                if audio_b64:
                    audio_bytes = base64.b64decode(audio_b64)
                    await _handle_user_input(
                        ws, call_id, input_agent, audio=audio_bytes,
                        user_name=data.get("user_name"),
                        user_phone=data.get("user_phone"),
                    )

    except WebSocketDisconnect:
        logger.info(f"[WS] Browser disconnected: {call_id}")
    except Exception as e:
        logger.error(f"[WS] Browser error: {e}")
    finally:
        browser_connections.pop(call_id, None)
        active_calls.pop(call_id, None)
        audio_queues.pop(call_id, None)
        task = call_tasks.pop(call_id, None)
        if task:
            task.cancel()


async def _handle_user_input(
    ws: WebSocket,
    call_id: str,
    input_agent: InputAgent,
    text: str = None,
    audio: bytes = None,
    user_name: str = None,
    user_phone: str = None,
):
    try:
        await _send_to_browser(ws, "call_status", {
            "status": "processing",
            "message": "Understanding your request...",
        })

        if audio:
            intent = await input_agent.process_voice_input(audio)
        else:
            intent = await input_agent.process_text_input(text)

        if user_name:
            intent.user_name = user_name
        if user_phone:
            intent.user_phone = user_phone

        await _send_to_browser(ws, "transcript", {
            "role": "user",
            "text": intent.raw_text,
        })

        await _send_to_browser(ws, "call_status", {
            "status": "intent_extracted",
            "message": f"Intent: {intent.intent.value}",
            "intent": intent.model_dump(),
        })

        call_state = await input_agent.prepare_session(intent, call_id)
        active_calls[call_id] = call_state

        if call_state.status == CallStatus.FAILED:
            await _send_to_browser(ws, "error", {
                "message": "Session setup failed.",
            })
            return

        target = intent.target_entity or "target"
        if not intent.target_phone:
            await _send_to_browser(ws, "error", {
                "message": (
                    f"No phone number found for \"{target}\". "
                    "Add the number to PHONE_REGISTRY in your .env, "
                    "or include the phone number directly in your request."
                ),
            })
            return

        await _send_to_browser(ws, "ready_for_call", {
            "call_id": call_id,
            "message": f"Ready to call {target} at {intent.target_phone}. Click Start Call.",
            "target_entity": intent.target_entity,
            "target_phone": intent.target_phone,
        })

    except Exception as e:
        logger.error(f"[Handler] Error processing input: {e}")
        await _send_to_browser(ws, "error", {"message": str(e)})


async def _send_to_browser(ws: WebSocket, msg_type: str, data: dict):
    try:
        await ws.send_json({"type": msg_type, "data": data})
    except Exception as e:
        logger.error(f"[WS] Failed to send to browser: {e}")


# ------------------------------------------------
# Twilio -- Create / End Call
# ------------------------------------------------

async def _create_twilio_call(call_id: str, to_phone: str) -> str:
    """Create a Twilio outbound call with a bidirectional Media Stream."""
    loop = asyncio.get_event_loop()

    from_phone = _sanitize_phone(settings.twilio_phone_number)
    to_phone_clean = _sanitize_phone(to_phone)

    base = settings.public_base_url
    ws_scheme = "wss" if base.startswith("https") else "ws"
    host = base.replace("https://", "").replace("http://", "")
    stream_url = f"{ws_scheme}://{host}/twilio/stream/{call_id}"

    twiml_str = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Pause length="1"/>'
        f'<Connect><Stream url="{stream_url}" /></Connect>'
        '</Response>'
    )

    logger.info(
        f"[Twilio] Creating call: {from_phone} -> {to_phone_clean}, "
        f"stream: {stream_url}"
    )

    def _create():
        client = _get_twilio_client()
        call = client.calls.create(
            to=to_phone_clean,
            from_=from_phone,
            twiml=twiml_str,
        )
        return call.sid

    sid = await loop.run_in_executor(None, _create)
    twilio_call_sids[call_id] = sid
    return sid


async def _end_twilio_call(call_sid: str):
    loop = asyncio.get_event_loop()

    def _end():
        client = _get_twilio_client()
        client.calls(call_sid).update(status="completed")

    try:
        await loop.run_in_executor(None, _end)
        logger.info(f"[Twilio] Call {call_sid} ended")
    except Exception as e:
        logger.warning(f"[Twilio] Failed to end call {call_sid}: {e}")


# ------------------------------------------------
# Twilio -- Media Stream WebSocket
# ------------------------------------------------

@app.websocket("/twilio/stream/{call_id}")
async def twilio_media_stream(ws: WebSocket, call_id: str):
    await ws.accept()
    logger.info(f"[Twilio] Media Stream connected for call {call_id}")

    stream_sid = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "connected":
                logger.info(f"[Twilio] Stream protocol connected: {call_id}")

            elif event == "start":
                stream_sid = msg.get("streamSid", "")
                twilio_streams[call_id] = ws
                twilio_stream_sids[call_id] = stream_sid
                logger.info(f"[Twilio] Stream started: {stream_sid}")

            elif event == "media":
                queue = audio_queues.get(call_id)
                if queue:
                    payload = msg.get("media", {}).get("payload", "")
                    if payload:
                        # Normalize mulaw -> slin16 PCM before queuing
                        mulaw = base64.b64decode(payload)
                        pcm = mulaw_to_pcm_bytes(mulaw)
                        await queue.put(pcm)

            elif event == "stop":
                logger.info(f"[Twilio] Stream stopped: {stream_sid}")
                break

    except WebSocketDisconnect:
        logger.info(f"[Twilio] Stream disconnected: {call_id}")
    except Exception as e:
        logger.error(f"[Twilio] Stream error: {e}")
    finally:
        twilio_streams.pop(call_id, None)
        twilio_stream_sids.pop(call_id, None)
        queue = audio_queues.get(call_id)
        if queue:
            await queue.put(None)  # sentinel value


async def _send_audio_to_twilio(call_id: str, mulaw_bytes: bytes):
    ws = twilio_streams.get(call_id)
    stream_sid = twilio_stream_sids.get(call_id)
    if not ws or not stream_sid:
        return

    try:
        await ws.send_text(json.dumps({
            "event": "clear",
            "streamSid": stream_sid,
        }))
    except Exception:
        return

    CHUNK_SIZE = 640
    for i in range(0, len(mulaw_bytes), CHUNK_SIZE):
        chunk = mulaw_bytes[i:i + CHUNK_SIZE]
        payload = base64.b64encode(chunk).decode()
        try:
            await ws.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": payload},
            }))
        except Exception:
            break

    # mulaw at 8kHz: 8000 bytes/sec
    duration = len(mulaw_bytes) / 8000.0
    await asyncio.sleep(duration + 0.3)


# ------------------------------------------------
# Exotel -- Create / End Call
# ------------------------------------------------

async def _create_exotel_call(call_id: str, to_phone: str) -> str:
    """
    Initiate an Exotel outbound call to `to_phone`.

    Exotel calls the target number, and when answered, executes the App Bazaar
    flow (EXOTEL_APP_ID) which contains a Voicebot Applet pointing at our
    /exotel/stream WebSocket endpoint.

    Returns the Exotel call SID and stores it in `exotel_sid_to_call_id`
    so the stream handler can correlate the incoming WebSocket with this call.
    """
    url = f"{_EXOTEL_BASE_URL}/{settings.exotel_account_sid}/Calls/connect"
    to_phone_clean = _sanitize_phone(to_phone)
    caller_id = _sanitize_phone(settings.exotel_phone_number)
    app_url = (
        f"http://my.exotel.com/{settings.exotel_account_sid}"
        f"/exoml/start_voice/{settings.exotel_app_id}"
    )

    logger.info(
        f"[Exotel] Creating call: {caller_id} -> {to_phone_clean}, "
        f"app_id: {settings.exotel_app_id}"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url,
            auth=(settings.exotel_api_key, settings.exotel_api_token),
            data={
                "From": to_phone_clean,
                "CallerId": caller_id,
                "Url": app_url,
            },
        )
        response.raise_for_status()
        result = response.json()

    call_sid = result["Call"]["Sid"]
    exotel_sid_to_call_id[call_sid] = call_id
    exotel_call_sids[call_id] = call_sid
    logger.info(f"[Exotel] Call created: SID={call_sid}")
    return call_sid


async def _end_exotel_call(call_sid: str):
    """Hang up an active Exotel call."""
    url = (
        f"{_EXOTEL_BASE_URL}/{settings.exotel_account_sid}"
        f"/Calls/{call_sid}"
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                url,
                auth=(settings.exotel_api_key, settings.exotel_api_token),
                data={"Status": "completed"},
            )
        logger.info(f"[Exotel] Call {call_sid} ended")
    except Exception as e:
        logger.warning(f"[Exotel] Failed to end call {call_sid}: {e}")


# ------------------------------------------------
# SMS Summary (dispatches by provider)
# ------------------------------------------------

async def _send_sms_summary(
    to_phone: str,
    intent,
    turn_count: int,
    conversation_log: list[dict],
    provider: str = "exotel",
    call_sid: str | None = None,
):
    to_phone_clean = _sanitize_phone(to_phone)

    lines = ["AI Phone Agent - Call Summary", ""]

    if intent:
        if intent.target_entity:
            lines.append(f"Called: {intent.target_entity}")
        if intent.task_description:
            lines.append(f"Task: {intent.task_description}")
        if intent.target_phone:
            lines.append(f"Number: {intent.target_phone}")
        if intent.doctor_name:
            doc = intent.doctor_name
            if intent.doctor_specialty:
                doc += f" ({intent.doctor_specialty})"
            lines.append(f"Doctor: {doc}")
        if intent.appointment_date:
            lines.append(f"Date: {intent.appointment_date}")
        if intent.user_name:
            lines.append(f"Patient: {intent.user_name}")

    lines.append("")
    lines.append(f"Call completed in {turn_count} turns.")

    if conversation_log:
        last_other = None
        for entry in reversed(conversation_log):
            if entry.get("speaker") in ("hospital", "other_party"):
                last_other = entry.get("text", "")
                break
        if last_other:
            if len(last_other) > 200:
                last_other = last_other[:197] + "..."
            lines.append("")
            lines.append(f"Last response: \"{last_other}\"")

    if call_sid:
        lines.append("")
        lines.append(f"Call SID: {call_sid}")

    body = "\n".join(lines)

    if provider == "twilio":
        from_phone = _sanitize_phone(settings.twilio_phone_number)
        loop = asyncio.get_event_loop()

        def _send_twilio():
            client = _get_twilio_client()
            msg = client.messages.create(
                to=to_phone_clean,
                from_=from_phone,
                body=body,
            )
            return msg.sid

        try:
            msg_sid = await loop.run_in_executor(None, _send_twilio)
            logger.info(f"[Twilio] SMS sent to {to_phone_clean}: {msg_sid}")
            return msg_sid
        except Exception as e:
            logger.error(f"[Twilio] SMS failed to {to_phone_clean}: {e}")
            return None
    else:
        from_phone = _sanitize_phone(settings.exotel_phone_number)
        url = f"{_EXOTEL_BASE_URL}/{settings.exotel_account_sid}/Sms/send"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    auth=(settings.exotel_api_key, settings.exotel_api_token),
                    data={
                        "From": from_phone,
                        "To": to_phone_clean,
                        "Body": body,
                        "SmsType": "transactional",
                    },
                )
                response.raise_for_status()
                result = response.json()
            msg_sid = result.get("SMSMessage", {}).get("Sid", "")
            logger.info(f"[Exotel] SMS sent to {to_phone_clean}: {msg_sid}")
            return msg_sid
        except Exception as e:
            logger.error(f"[Exotel] SMS failed to {to_phone_clean}: {e}")
            return None


# ------------------------------------------------
# Exotel -- Voice Stream WebSocket
# ------------------------------------------------

@app.websocket("/exotel/stream")
async def exotel_stream(ws: WebSocket):
    """
    Bidirectional WebSocket endpoint for Exotel Voice Streaming.

    Configured as the Voicebot Applet URL in the Exotel App Bazaar dashboard.
    Exotel connects here when a call is answered. The `start` message contains
    the call_sid which we use to correlate with our internal call_id.

    Audio format: raw slin16 PCM (16-bit, 8kHz, mono, little-endian), base64 encoded.
    """
    await ws.accept()
    logger.info("[Exotel] Voice Stream WebSocket connected")

    call_id = None
    stream_sid = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "connected":
                logger.info("[Exotel] Stream protocol handshake complete")

            elif event == "start":
                start_data = msg.get("start", {})
                stream_sid = start_data.get("stream_sid") or msg.get("stream_sid", "")
                call_sid = start_data.get("call_sid", "")
                from_number = start_data.get("from", "")
                to_number = start_data.get("to", "")

                logger.info(
                    f"[Exotel] Stream started: stream_sid={stream_sid}, "
                    f"call_sid={call_sid}, {from_number} -> {to_number}"
                )

                # Wait briefly for the API response to populate the mapping
                # (API call → call_sid stored → WebSocket start event)
                for _ in range(50):
                    call_id = exotel_sid_to_call_id.get(call_sid)
                    if call_id:
                        break
                    await asyncio.sleep(0.1)

                if call_id:
                    exotel_streams[call_id] = ws
                    exotel_stream_sids[call_id] = stream_sid
                    logger.info(f"[Exotel] Stream correlated to call {call_id}")
                else:
                    logger.warning(
                        f"[Exotel] Could not correlate stream for "
                        f"call_sid={call_sid}. Known SIDs: "
                        f"{list(exotel_sid_to_call_id.keys())}"
                    )

            elif event == "media":
                if call_id:
                    queue = audio_queues.get(call_id)
                    if queue:
                        payload = msg.get("media", {}).get("payload", "")
                        if payload:
                            chunk = base64.b64decode(payload)
                            await queue.put(chunk)

            elif event == "stop":
                logger.info(f"[Exotel] Stream stopped: stream_sid={stream_sid}")
                break

    except WebSocketDisconnect:
        logger.info(f"[Exotel] Stream WebSocket disconnected: stream_sid={stream_sid}")
    except Exception as e:
        logger.error(f"[Exotel] Stream error: {e}")
    finally:
        if call_id:
            exotel_streams.pop(call_id, None)
            exotel_stream_sids.pop(call_id, None)
        # Signal the conversation loop that the stream is gone
        if call_id:
            queue = audio_queues.get(call_id)
            if queue:
                await queue.put(None)  # sentinel value


async def _send_audio_to_exotel(call_id: str, pcm_bytes: bytes):
    """
    Send linear16 PCM audio to the Exotel stream for the given call.

    Sends a `clear` event first (to cancel any pending audio), then streams
    the PCM in 3200-byte chunks (each a multiple of 320 bytes as required).
    """
    ws = exotel_streams.get(call_id)
    stream_sid = exotel_stream_sids.get(call_id)
    if not ws or not stream_sid:
        return

    try:
        await ws.send_text(json.dumps({
            "event": "clear",
            "stream_sid": stream_sid,
        }))
    except Exception:
        return

    # 3200 bytes = 200ms at 8kHz 16-bit; within Exotel's valid chunk range
    CHUNK_SIZE = 3200
    for i in range(0, len(pcm_bytes), CHUNK_SIZE):
        chunk = pcm_bytes[i:i + CHUNK_SIZE]
        # Pad to nearest multiple of 320 bytes
        remainder = len(chunk) % 320
        if remainder:
            chunk = chunk + b'\x00' * (320 - remainder)
        payload = base64.b64encode(chunk).decode()
        try:
            await ws.send_text(json.dumps({
                "event": "media",
                "stream_sid": stream_sid,
                "media": {"payload": payload},
            }))
        except Exception:
            break

    # Wait for audio playback to finish (8kHz, 16-bit = 16000 bytes/sec)
    duration = len(pcm_bytes) / 16000.0
    await asyncio.sleep(duration + 0.3)


# ------------------------------------------------
# Provider-Dispatch Helpers
# ------------------------------------------------

async def _create_call(call_id: str, to_phone: str, provider: str) -> str:
    """Create an outbound call via the chosen provider. Returns call SID."""
    if provider == "twilio":
        return await _create_twilio_call(call_id, to_phone)
    return await _create_exotel_call(call_id, to_phone)


async def _end_call(call_sid: str, provider: str):
    """Hang up an active call via the chosen provider."""
    if provider == "twilio":
        await _end_twilio_call(call_sid)
    else:
        await _end_exotel_call(call_sid)


def _stream_connected(call_id: str, provider: str) -> bool:
    """Check whether the audio stream WebSocket is open for this call."""
    if provider == "twilio":
        return call_id in twilio_streams
    return call_id in exotel_streams


async def _tts_for_stream(text: str, provider: str) -> bytes:
    """Generate TTS audio in the format expected by the chosen provider."""
    if provider == "twilio":
        return await tts_service.text_to_speech_for_twilio(text)
    return await tts_service.text_to_speech_for_call(text)


async def _send_audio_stream(call_id: str, audio_bytes: bytes, provider: str):
    """Send TTS audio to the phone stream for the chosen provider."""
    if provider == "twilio":
        await _send_audio_to_twilio(call_id, audio_bytes)
    else:
        await _send_audio_to_exotel(call_id, audio_bytes)


# ------------------------------------------------
# Start Call -- Dispatcher
# ------------------------------------------------

@app.post("/api/start-call/{call_id}")
async def start_call(call_id: str, provider: str = "exotel"):
    if call_id not in active_calls:
        return JSONResponse(status_code=404, content={"error": "No active session."})

    call_state = active_calls[call_id]
    if not call_state.user_intent:
        return JSONResponse(status_code=400, content={"error": "No intent extracted."})

    browser_ws = browser_connections.get(call_id)
    if not browser_ws:
        return JSONResponse(status_code=400, content={"error": "No browser connection."})

    if call_id in call_tasks:
        return JSONResponse(status_code=400, content={"error": "Call already in progress."})

    if provider not in ("twilio", "exotel"):
        return JSONResponse(status_code=400, content={"error": "Unknown provider."})

    task = asyncio.create_task(_run_call(call_id, call_state, browser_ws, provider))
    call_tasks[call_id] = task

    return JSONResponse(content={"status": "ok", "message": "Call started.", "provider": provider})


async def _run_call(
    call_id: str,
    call_state: CallState,
    browser_ws: WebSocket,
    provider: str = "exotel",
):
    intent = call_state.user_intent
    target_phone = intent.target_phone
    if target_phone:
        await _run_call_real(call_id, call_state, browser_ws, target_phone, provider)
    else:
        await _send_to_browser(browser_ws, "error", {
            "message": "No target phone number — cannot place call.",
        })



# ------------------------------------------------
# Real Call Mode
# ------------------------------------------------

async def _run_call_real(
    call_id: str,
    call_state: CallState,
    browser_ws: WebSocket,
    target_phone: str,
    provider: str = "exotel",
):
    """
    Call the target via the chosen provider (Twilio or Exotel).
    The conversation loop is identical for both — only stream setup and
    audio formatting differ, handled by the dispatch helpers.
    """
    provider_label = provider.capitalize()
    call_sid = None
    conversation_log = []
    try:
        intent = call_state.user_intent
        action_agent = ActionAgent(call_state)
        target_label = intent.target_entity or target_phone

        queue = asyncio.Queue()
        audio_queues[call_id] = queue

        await _send_to_browser(browser_ws, "call_status", {
            "status": "calling",
            "message": f"Dialing {target_label} ({target_phone}) via {provider_label}...",
            "provider": provider,
        })

        call_sid = await _create_call(call_id, target_phone, provider)

        await _send_to_browser(browser_ws, "call_status", {
            "status": "ringing",
            "message": f"Ringing {target_label}... (SID: {call_sid})",
            "provider_sid": call_sid,
            "provider": provider,
        })

        # Wait for the stream WebSocket to connect
        STREAM_WAIT_SECS = 90
        for _ in range(STREAM_WAIT_SECS):
            if _stream_connected(call_id, provider):
                break
            await asyncio.sleep(1)

        if not _stream_connected(call_id, provider):
            stream_url = (
                f"{settings.public_base_url}/twilio/stream/{call_id}"
                if provider == "twilio"
                else f"{settings.public_base_url}/exotel/stream"
            )
            await _send_to_browser(browser_ws, "error", {
                "message": (
                    f"Call to {target_label} failed -- {provider_label} did not connect "
                    f"the audio stream after {STREAM_WAIT_SECS}s. "
                    f"Check that PUBLIC_BASE_URL is publicly reachable "
                    f"(current: {settings.public_base_url}). "
                    f"Expected stream URL: {stream_url}"
                ),
            })
            return

        logger.info(f"[Call] {provider_label} stream connected for {target_label}")

        await _send_to_browser(browser_ws, "call_status", {
            "status": "in_call",
            "message": f"Connected to {target_label}. Waiting for them to speak...",
        })

        turn_count = 0
        MAX_SILENT_ROUNDS = 3
        silent_rounds = 0

        while True:
            await _send_to_browser(browser_ws, "agent_update", {
                "agent": 2,
                "text": "Listening...",
                "active": True,
            })

            speech_pcm = await receive_speech(queue, timeout=45.0)

            if not speech_pcm:
                silent_rounds += 1
                logger.info(
                    f"[Call] No speech detected "
                    f"(round {silent_rounds}/{MAX_SILENT_ROUNDS})"
                )
                if silent_rounds >= MAX_SILENT_ROUNDS:
                    logger.info("[Call] Max silent rounds reached, ending.")
                    break

                nudge = "Hello? Are you still there?"
                turn_count += 1
                conversation_log.append({"speaker": "agent", "text": nudge})
                try:
                    stream_audio = await _tts_for_stream(nudge, provider)
                    await _send_audio_stream(call_id, stream_audio, provider)
                except Exception as e:
                    logger.error(f"[Call] Nudge TTS failed: {e}")
                try:
                    mp3_nudge = await tts_service.text_to_speech_mp3(nudge)
                    nudge_b64 = base64.b64encode(mp3_nudge).decode()
                except Exception:
                    nudge_b64 = ""
                await _send_to_browser(browser_ws, "call_turn", {
                    "speaker": "agent",
                    "text": nudge,
                    "audio_b64": nudge_b64,
                    "turn": turn_count,
                    "action_type": "speak",
                })
                continue

            silent_rounds = 0

            # Transcribe
            wav_bytes = pcm16_to_wav(speech_pcm)
            transcript = await sarvam_stt.transcribe_audio(wav_bytes)

            if not transcript or len(transcript.strip()) < 2:
                logger.info("[Call] Empty transcript, continuing...")
                continue

            turn_count += 1
            conversation_log.append({"speaker": "other_party", "text": transcript})
            logger.info(f"[Call] Other party: {transcript[:80]}")

            await _send_to_browser(browser_ws, "call_turn", {
                "speaker": "hospital",
                "text": transcript,
                "audio_b64": "",
                "turn": turn_count,
            })
            await _send_to_browser(browser_ws, "agent_update", {
                "agent": 2,
                "text": f"Heard: \"{transcript[:60]}\"",
                "active": True,
            })

            agent_action = await action_agent.handle_raw_transcript(transcript)
            await _send_to_browser(browser_ws, "agent_update", {
                "agent": 3,
                "text": f"Action: {agent_action.action_type.value}",
                "active": True,
            })

            display_text = ""
            agent_audio_b64 = ""

            if agent_action.action_type in (ActionType.SPEAK, ActionType.END_CALL):
                display_text = agent_action.speech_text or ""
                if display_text:
                    try:
                        stream_audio = await _tts_for_stream(display_text, provider)
                        await _send_audio_stream(call_id, stream_audio, provider)
                    except Exception as e:
                        logger.error(f"[Call] Agent stream TTS failed: {e}")
                    try:
                        mp3 = await tts_service.text_to_speech_mp3(display_text)
                        agent_audio_b64 = base64.b64encode(mp3).decode()
                    except Exception as e:
                        logger.error(f"[Call] Agent browser TTS failed: {e}")

            elif agent_action.action_type == ActionType.DTMF:
                display_text = f"[Pressed {agent_action.dtmf_digits}]"
                logger.info(f"[Call] DTMF action (agent should speak instead): {agent_action.dtmf_digits}")

            elif agent_action.action_type == ActionType.WAIT:
                display_text = f"[Waiting: {agent_action.reasoning}]"

            turn_count += 1
            conversation_log.append({"speaker": "agent", "text": display_text})
            await _send_to_browser(browser_ws, "call_turn", {
                "speaker": "agent",
                "text": display_text,
                "audio_b64": agent_audio_b64,
                "turn": turn_count,
                "action_type": agent_action.action_type.value,
                "dtmf_digits": agent_action.dtmf_digits,
                "reasoning": agent_action.reasoning,
            })

            if agent_action.action_type == ActionType.END_CALL:
                break

        call_state.status = CallStatus.COMPLETED
        await _send_to_browser(browser_ws, "call_complete", {
            "total_turns": turn_count,
            "message": f"Call to {target_label} completed. ({turn_count} turns)",
        })

        sms_phone = intent.user_phone or settings.default_user_phone
        sms_sid = await _send_sms_summary(
            to_phone=sms_phone,
            intent=intent,
            turn_count=turn_count,
            conversation_log=conversation_log,
            provider=provider,
            call_sid=call_sid,
        )
        if sms_sid:
            await _send_to_browser(browser_ws, "call_status", {
                "status": "sms_sent",
                "message": f"SMS summary sent to {sms_phone}.",
            })

    except Exception as e:
        err_str = str(e)
        logger.error(f"[Call] Error ({provider}): {err_str}")

        if provider == "twilio":
            if "not allowed to call" in err_str or "21215" in err_str:
                user_msg = (
                    f"Twilio blocked the call to {target_phone}. "
                    "Go to console.twilio.com > Voice > Settings > Geo Permissions "
                    "and enable India. Save and retry."
                )
            elif "21219" in err_str or "unverified" in err_str.lower():
                user_msg = (
                    f"The number {target_phone} is unverified on your Twilio trial account. "
                    "Verify it at console.twilio.com > Phone Numbers > Verified Caller IDs."
                )
            else:
                user_msg = f"Twilio call error: {err_str}"
        else:
            if "401" in err_str or "Unauthorised" in err_str or "unauthorized" in err_str.lower():
                user_msg = (
                    "Exotel authentication failed. Check EXOTEL_API_KEY, "
                    "EXOTEL_API_TOKEN, and EXOTEL_ACCOUNT_SID in your .env."
                )
            elif "402" in err_str:
                user_msg = (
                    "Exotel payment required. Check your account credits or plan."
                )
            else:
                user_msg = f"Exotel call error: {err_str}"

        await _send_to_browser(browser_ws, "error", {"message": user_msg})
    finally:
        call_tasks.pop(call_id, None)
        audio_queues.pop(call_id, None)
        # Clean up provider-specific SID mappings
        if provider == "twilio":
            twilio_call_sids.pop(call_id, None)
        else:
            exotel_sid_to_call_id.pop(exotel_call_sids.pop(call_id, ""), None)
        if call_state.status != CallStatus.COMPLETED:
            call_state.status = CallStatus.FAILED
        if call_sid:
            await _end_call(call_sid, provider)


# ------------------------------------------------
# Health Check & API
# ------------------------------------------------


# ------------------------------------------------
# Health Check & API
# ------------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "mode": "generic-phone-agent",
        "active_sessions": len(active_calls),
        "active_calls": len(call_tasks),
        "twilio_streams": len(twilio_streams),
        "exotel_streams": len(exotel_streams),
    }


@app.get("/api/calls")
async def list_calls():
    return {
        call_id: {
            "status": state.status.value,
            "intent": state.user_intent.model_dump() if state.user_intent else None,
        }
        for call_id, state in active_calls.items()
    }


# ------------------------------------------------
# Run
# ------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
        log_level="info",
    )
