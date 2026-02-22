"""
AI Phone Agent -- Main Application

FastAPI server that orchestrates:
  - Browser WebSocket (user <-> system)
  - Twilio phone calls (to the real target number) with Media Streams
  - Simulated calls via hospital agent WebSocket (fallback when no target number)
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

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.agents import InputAgent, CallMonitorAgent, ActionAgent
from backend.models.schemas import (
    CallState, CallStatus, ActionType,
)
from backend.services import tts_service, groq_stt
from backend.services.audio_utils import (
    receive_speech, mulaw_to_wav,
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

# Audio queues for real-call mode (stream handler -> call loop)
audio_queues: Dict[str, asyncio.Queue] = {}

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
    """Clean a phone number to E.164 format."""
    cleaned = re.sub(r'[^\d+]', '', number)
    if not cleaned.startswith('+'):
        if len(cleaned) == 10:
            cleaned = '+91' + cleaned
        elif len(cleaned) == 11 and cleaned.startswith('1'):
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
    logger.info(f"   Groq STT:       {settings.groq_stt_model}")
    logger.info(f"   Agent voice:    {settings.agent_tts_voice}")
    logger.info(f"   Hospital sim:   {settings.hospital_agent_url}")
    logger.info(f"   Twilio number:  {settings.twilio_phone_number}")
    logger.info(f"   Public URL:     {settings.public_base_url}")
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
    allow_credentials=True,
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

        # Tell browser the call mode
        target = intent.target_entity or "target"
        if intent.target_phone:
            mode_msg = f"Will call {target} at {intent.target_phone} via Twilio."
        else:
            mode_msg = f"No phone number found for {target}. Will use simulated agent."

        await _send_to_browser(ws, "ready_for_call", {
            "call_id": call_id,
            "message": f"Intent extracted. {mode_msg} Click Start Call.",
            "call_mode": "real" if intent.target_phone else "simulated",
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

async def _create_twilio_call(
    call_id: str,
    to_phone: str,
    play_intro: bool = False,
) -> str:
    """
    Create a Twilio outbound call with a bidirectional Media Stream.

    Args:
        call_id: Internal session ID
        to_phone: E.164 phone number to call
        play_intro: If True, play an intro message before connecting stream
                    (for listener calls). False for real target calls.
    """
    loop = asyncio.get_event_loop()

    from_phone = _sanitize_phone(settings.twilio_phone_number)
    to_phone_clean = _sanitize_phone(to_phone)

    base = settings.public_base_url
    ws_scheme = "wss" if base.startswith("https") else "ws"
    host = base.replace("https://", "").replace("http://", "")
    stream_url = f"{ws_scheme}://{host}/twilio/stream/{call_id}"

    if play_intro:
        twiml_str = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            '<Say voice="Polly.Amy">'
            'Connecting to the AI phone agent. Please listen.'
            '</Say>'
            f'<Connect><Stream url="{stream_url}" /></Connect>'
            '</Response>'
        )
    else:
        # Real target call: add a brief greeting so the person knows
        # someone is on the line, then start the bidirectional stream.
        twiml_str = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            '<Say voice="Polly.Amy">'
            'Hello, please hold while we connect you to our assistant.'
            '</Say>'
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

    return await loop.run_in_executor(None, _create)


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
# Twilio -- SMS Summary
# ------------------------------------------------

async def _send_sms_summary(
    to_phone: str,
    intent,
    turn_count: int,
    conversation_log: list[dict],
    twilio_sid: str | None = None,
):
    loop = asyncio.get_event_loop()

    from_phone = _sanitize_phone(settings.twilio_phone_number)
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

    if twilio_sid:
        lines.append("")
        lines.append(f"Call SID: {twilio_sid}")

    body = "\n".join(lines)

    def _send():
        client = _get_twilio_client()
        message = client.messages.create(
            to=to_phone_clean,
            from_=from_phone,
            body=body,
        )
        return message.sid

    try:
        msg_sid = await loop.run_in_executor(None, _send)
        logger.info(f"[Twilio] SMS sent to {to_phone_clean}: {msg_sid}")
        return msg_sid
    except Exception as e:
        logger.error(f"[Twilio] SMS failed to {to_phone_clean}: {e}")
        return None


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
                # Forward inbound audio to the audio queue (for real-call STT)
                queue = audio_queues.get(call_id)
                if queue:
                    payload = msg.get("media", {}).get("payload", "")
                    if payload:
                        chunk = base64.b64decode(payload)
                        await queue.put(chunk)

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
        # Signal the conversation loop that the stream is gone
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

    duration = len(mulaw_bytes) / 8000.0
    await asyncio.sleep(duration + 0.3)


# ------------------------------------------------
# Start Call -- Dispatcher
# ------------------------------------------------

@app.post("/api/start-call/{call_id}")
async def start_call(call_id: str, mode: str = Query("conversational")):
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

    task = asyncio.create_task(_run_call(call_id, mode, call_state, browser_ws))
    call_tasks[call_id] = task

    return JSONResponse(content={"status": "ok", "message": "Call started."})


async def _run_call(
    call_id: str,
    mode: str,
    call_state: CallState,
    browser_ws: WebSocket,
):
    """Dispatch to real-call or simulated-call based on target_phone."""
    intent = call_state.user_intent
    target_phone = intent.target_phone

    if target_phone and settings.twilio_account_sid and settings.twilio_auth_token:
        await _run_call_real(call_id, mode, call_state, browser_ws, target_phone)
    else:
        await _run_call_simulated(call_id, mode, call_state, browser_ws)


def _build_agent_intro(intent) -> str:
    """
    Build a natural opening line for the AI agent when it starts a real call.
    The agent should introduce itself clearly so the other party knows who
    is calling and why.
    """
    user_name = intent.user_name or "a patient"
    target = intent.target_entity or "your organization"
    task = intent.task_description or ""

    # Build a contextual introduction
    parts = [f"Hello, I am an AI assistant calling on behalf of {user_name}."]

    if task:
        parts.append(f"I am calling to {task.lower().rstrip('.')}.")
    elif intent.intent.value == "book_appointment":
        doctor_info = ""
        if intent.doctor_name:
            doctor_info = f" with Dr. {intent.doctor_name}"
        elif intent.doctor_specialty:
            doctor_info = f" with a {intent.doctor_specialty}"
        date_info = ""
        if intent.appointment_date:
            date_info = f" on {intent.appointment_date}"
        parts.append(
            f"I would like to book an appointment{doctor_info}{date_info}."
        )
    else:
        parts.append(f"I need some assistance from {target}.")

    parts.append("Could you please help me with this?")

    return " ".join(parts)


# ------------------------------------------------
# Real Call Mode -- Twilio call to actual target
# ------------------------------------------------

async def _run_call_real(
    call_id: str,
    mode: str,
    call_state: CallState,
    browser_ws: WebSocket,
    target_phone: str,
):
    """
    Call the actual target via Twilio. The conversation is driven by:
      - Incoming audio from the target (via Media Stream -> STT)
      - Our agent decides a response (via LLM)
      - Outgoing audio sent back (via TTS -> Media Stream)
    """
    twilio_sid = None
    conversation_log = []
    try:
        intent = call_state.user_intent
        action_agent = ActionAgent(call_state)
        target_label = intent.target_entity or target_phone

        # -- Create audio queue for incoming audio --
        queue = asyncio.Queue()
        audio_queues[call_id] = queue

        # -- Create Twilio call to target --
        await _send_to_browser(browser_ws, "call_status", {
            "status": "calling",
            "message": f"Dialing {target_label} ({target_phone}) via Twilio...",
        })

        twilio_sid = await _create_twilio_call(
            call_id, target_phone, play_intro=False,
        )
        twilio_call_sids[call_id] = twilio_sid

        await _send_to_browser(browser_ws, "call_status", {
            "status": "ringing",
            "message": f"Ringing {target_label}... (SID: {twilio_sid})",
            "twilio_sid": twilio_sid,
        })

        # Wait for stream to connect (generous timeout for trial accounts)
        STREAM_WAIT_SECS = 90
        for _ in range(STREAM_WAIT_SECS):
            if call_id in twilio_streams:
                break
            await asyncio.sleep(1)

        if call_id not in twilio_streams:
            await _send_to_browser(browser_ws, "error", {
                "message": (
                    f"Call to {target_label} failed -- no stream connection "
                    f"after {STREAM_WAIT_SECS}s. Make sure the other party "
                    f"answered and pressed a key (trial accounts require this)."
                ),
            })
            return

        logger.info(f"[Call-Real] Stream connected for {target_label}")

        await _send_to_browser(browser_ws, "call_status", {
            "status": "phone_connected",
            "message": f"Connected to {target_label}. Starting conversation...",
        })

        turn_count = 0
        MAX_SILENT_ROUNDS = 3  # allow this many consecutive silent rounds
        silent_rounds = 0

        # -- Agent speaks FIRST (introduce itself) --
        intro_text = _build_agent_intro(intent)
        logger.info(f"[Call-Real] Agent intro: {intro_text}")

        turn_count += 1
        conversation_log.append({"speaker": "agent", "text": intro_text})

        intro_audio_b64 = ""
        try:
            mulaw_intro = await tts_service.text_to_speech_for_twilio(
                intro_text, model=settings.agent_tts_voice,
            )
            await _send_audio_to_twilio(call_id, mulaw_intro)
        except Exception as e:
            logger.error(f"[Call-Real] Agent intro TTS (Twilio) failed: {e}")

        try:
            mp3_intro = await tts_service.text_to_speech_mp3(
                intro_text, model=settings.agent_tts_voice,
            )
            intro_audio_b64 = base64.b64encode(mp3_intro).decode()
        except Exception as e:
            logger.error(f"[Call-Real] Agent intro TTS (browser) failed: {e}")

        await _send_to_browser(browser_ws, "call_turn", {
            "speaker": "agent",
            "text": intro_text,
            "audio_b64": intro_audio_b64,
            "turn": turn_count,
            "action_type": "speak",
        })

        await _send_to_browser(browser_ws, "call_status", {
            "status": "in_call",
            "message": f"In conversation with {target_label}...",
        })

        # -- Conversation loop --
        while True:
            # -- Listen: receive speech from the target --
            await _send_to_browser(browser_ws, "agent_update", {
                "agent": 2,
                "text": "Listening...",
                "active": True,
            })

            speech_mulaw = await receive_speech(queue, timeout=45.0)

            if not speech_mulaw:
                silent_rounds += 1
                logger.info(
                    f"[Call-Real] No speech detected "
                    f"(round {silent_rounds}/{MAX_SILENT_ROUNDS})"
                )

                if silent_rounds >= MAX_SILENT_ROUNDS:
                    logger.info("[Call-Real] Max silent rounds reached, ending.")
                    break

                # Prompt the other party again
                nudge = (
                    "Hello? Are you still there? "
                    "I am an AI assistant calling on behalf of a patient. "
                    "Could you please respond?"
                )
                turn_count += 1
                conversation_log.append({"speaker": "agent", "text": nudge})

                try:
                    mulaw_nudge = await tts_service.text_to_speech_for_twilio(
                        nudge, model=settings.agent_tts_voice,
                    )
                    await _send_audio_to_twilio(call_id, mulaw_nudge)
                except Exception as e:
                    logger.error(f"[Call-Real] Nudge TTS failed: {e}")

                try:
                    mp3_nudge = await tts_service.text_to_speech_mp3(
                        nudge, model=settings.agent_tts_voice,
                    )
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

            # Reset silence counter when we do get speech
            silent_rounds = 0

            # -- Transcribe --
            wav_bytes = mulaw_to_wav(speech_mulaw)
            transcript = await groq_stt.transcribe_audio(
                wav_bytes,
                prompt=f"Phone call with {target_label}.",
            )

            if not transcript or len(transcript.strip()) < 2:
                logger.info("[Call-Real] Empty transcript, continuing...")
                continue

            turn_count += 1
            conversation_log.append({
                "speaker": "other_party",
                "text": transcript,
            })

            logger.info(f"[Call-Real] Other party: {transcript[:80]}")

            # Forward to browser (no TTS audio for the other party since
            # we heard them live through the stream)
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

            # -- Agent decides action --
            agent_action = await action_agent.handle_raw_transcript(transcript)

            await _send_to_browser(browser_ws, "agent_update", {
                "agent": 3,
                "text": f"Action: {agent_action.action_type.value}",
                "active": True,
            })

            # -- Build response --
            display_text = ""
            agent_audio_b64 = ""

            if agent_action.action_type in (ActionType.SPEAK, ActionType.END_CALL):
                display_text = agent_action.speech_text or ""
                if display_text:
                    # Generate mulaw for Twilio and mp3 for browser
                    try:
                        mulaw = await tts_service.text_to_speech_for_twilio(
                            display_text, model=settings.agent_tts_voice,
                        )
                        await _send_audio_to_twilio(call_id, mulaw)
                    except Exception as e:
                        logger.error(f"[Call-Real] Agent Twilio TTS failed: {e}")

                    try:
                        mp3 = await tts_service.text_to_speech_mp3(
                            display_text, model=settings.agent_tts_voice,
                        )
                        agent_audio_b64 = base64.b64encode(mp3).decode()
                    except Exception as e:
                        logger.error(f"[Call-Real] Agent browser TTS failed: {e}")

            elif agent_action.action_type == ActionType.DTMF:
                display_text = f"[Pressed {agent_action.dtmf_digits}]"
                logger.info(
                    f"[Call-Real] DTMF not yet supported in real calls: "
                    f"{agent_action.dtmf_digits}"
                )

            elif agent_action.action_type == ActionType.WAIT:
                display_text = f"[Waiting: {agent_action.reasoning}]"

            turn_count += 1
            conversation_log.append({
                "speaker": "agent",
                "text": display_text,
            })

            # Forward to browser
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

        # -- Call completed --
        call_state.status = CallStatus.COMPLETED
        await _send_to_browser(browser_ws, "call_complete", {
            "total_turns": turn_count,
            "message": f"Call to {target_label} completed.",
        })

        # -- SMS summary --
        sms_phone = intent.user_phone or settings.default_user_phone
        sms_sid = await _send_sms_summary(
            to_phone=sms_phone,
            intent=intent,
            turn_count=turn_count,
            conversation_log=conversation_log,
            twilio_sid=twilio_sid,
        )
        if sms_sid:
            await _send_to_browser(browser_ws, "call_status", {
                "status": "sms_sent",
                "message": f"SMS summary sent to {sms_phone}.",
            })

    except Exception as e:
        logger.error(f"[Call-Real] Error: {e}")
        await _send_to_browser(browser_ws, "error", {
            "message": f"Call error: {str(e)}",
        })
    finally:
        call_tasks.pop(call_id, None)
        audio_queues.pop(call_id, None)
        twilio_call_sids.pop(call_id, None)
        if call_state.status != CallStatus.COMPLETED:
            call_state.status = CallStatus.FAILED
        if twilio_sid:
            await _end_twilio_call(twilio_sid)


# ------------------------------------------------
# Simulated Call Mode -- Hospital Agent WebSocket
# ------------------------------------------------

async def _run_call_simulated(
    call_id: str,
    mode: str,
    call_state: CallState,
    browser_ws: WebSocket,
):
    """
    Use the hospital agent WebSocket for a simulated call.
    This is the fallback when no real target phone number is available.
    """
    twilio_sid = None
    conversation_log = []
    try:
        intent = call_state.user_intent
        monitor = CallMonitorAgent(call_state)
        action_agent = ActionAgent(call_state)
        target_label = intent.target_entity or "simulated agent"

        # Optionally call user's phone so they can listen
        twilio_ok = False
        if settings.twilio_account_sid and settings.twilio_auth_token:
            try:
                listener_phone = intent.user_phone or settings.default_user_phone
                await _send_to_browser(browser_ws, "call_status", {
                    "status": "calling",
                    "message": f"Dialing your phone ({listener_phone}) to listen...",
                })

                twilio_sid = await _create_twilio_call(
                    call_id, listener_phone, play_intro=True,
                )
                twilio_call_sids[call_id] = twilio_sid

                await _send_to_browser(browser_ws, "call_status", {
                    "status": "ringing",
                    "message": f"Phone ringing... (SID: {twilio_sid})",
                    "twilio_sid": twilio_sid,
                })

                for _ in range(45):
                    if call_id in twilio_streams:
                        break
                    await asyncio.sleep(1)

                if call_id in twilio_streams:
                    twilio_ok = True
                    await _send_to_browser(browser_ws, "call_status", {
                        "status": "phone_connected",
                        "message": "Phone connected. Starting simulated call...",
                    })
                else:
                    await _send_to_browser(browser_ws, "call_status", {
                        "status": "calling",
                        "message": "Phone not answered. Browser audio only.",
                    })
            except Exception as e:
                logger.warning(f"[Twilio] Listener call failed: {e}")
                await _send_to_browser(browser_ws, "call_status", {
                    "status": "calling",
                    "message": f"Twilio error: {e}. Browser audio only.",
                })

        # -- Connect to hospital agent --
        await _send_to_browser(browser_ws, "call_status", {
            "status": "calling",
            "message": f"Connecting to {target_label}...",
        })

        uri = f"{settings.hospital_agent_url}/ws/call"

        async with websockets.connect(uri) as hospital_ws:
            await hospital_ws.send(json.dumps({
                "type": "call_start",
                "intent": intent.model_dump(mode="json"),
                "mode": mode,
            }))

            await _send_to_browser(browser_ws, "call_status", {
                "status": "in_call",
                "message": f"Simulated call with {target_label} in progress...",
            })

            turn_count = 0

            while True:
                raw = await hospital_ws.recv()
                msg = json.loads(raw)

                if msg.get("type") != "hospital_speech":
                    continue

                hospital_text = msg["text"]
                hospital_audio = msg.get("audio_b64", "")
                expects = msg.get("expects", "speech")
                call_ended = msg.get("call_ended", False)

                turn_count += 1

                conversation_log.append({
                    "speaker": "hospital",
                    "text": hospital_text,
                })

                await _send_to_browser(browser_ws, "call_turn", {
                    "speaker": "hospital",
                    "text": hospital_text,
                    "audio_b64": hospital_audio,
                    "turn": turn_count,
                })

                await _send_to_browser(browser_ws, "agent_update", {
                    "agent": 2,
                    "text": f"Heard: \"{hospital_text[:60]}...\"",
                    "active": True,
                })

                # Stream hospital speech to Twilio phone (listener)
                if twilio_ok and call_id in twilio_streams:
                    try:
                        mulaw = await tts_service.text_to_speech_for_twilio(
                            hospital_text, model="aura-luna-en",
                        )
                        await _send_audio_to_twilio(call_id, mulaw)
                    except Exception as e:
                        logger.warning(f"[Twilio] Sim hospital TTS failed: {e}")

                if call_ended:
                    break

                if expects == "none":
                    continue

                # -- Classify --
                monitor._conversation_history.append({
                    "role": "hospital_ivr",
                    "text": hospital_text,
                })
                classification = await monitor._classify_transcript(hospital_text)

                await _send_to_browser(browser_ws, "agent_update", {
                    "agent": 2,
                    "text": f"Classified: {classification.prompt_type.value}",
                    "active": True,
                })

                # -- Action --
                agent_action = await action_agent.handle_classification(classification)

                await _send_to_browser(browser_ws, "agent_update", {
                    "agent": 3,
                    "text": f"Action: {agent_action.action_type.value}",
                    "active": True,
                })

                # -- Build response --
                agent_audio_b64 = ""
                display_text = ""

                if agent_action.action_type in (ActionType.SPEAK, ActionType.END_CALL):
                    display_text = agent_action.speech_text or ""
                    if display_text:
                        try:
                            mp3 = await tts_service.text_to_speech_mp3(
                                display_text, model=settings.agent_tts_voice,
                            )
                            agent_audio_b64 = base64.b64encode(mp3).decode()
                        except Exception as e:
                            logger.error(f"[TTS] Agent mp3 failed: {e}")

                elif agent_action.action_type == ActionType.DTMF:
                    display_text = f"[Pressed {agent_action.dtmf_digits}]"

                elif agent_action.action_type == ActionType.WAIT:
                    display_text = f"[Waiting: {agent_action.reasoning}]"

                if agent_action.speech_text:
                    monitor.add_agent_response(agent_action.speech_text)

                turn_count += 1

                conversation_log.append({
                    "speaker": "agent",
                    "text": display_text,
                })

                await _send_to_browser(browser_ws, "call_turn", {
                    "speaker": "agent",
                    "text": display_text,
                    "audio_b64": agent_audio_b64,
                    "turn": turn_count,
                    "action_type": agent_action.action_type.value,
                    "dtmf_digits": agent_action.dtmf_digits,
                    "reasoning": agent_action.reasoning,
                })

                # Stream agent speech to Twilio phone (listener)
                if (
                    twilio_ok
                    and call_id in twilio_streams
                    and agent_action.action_type in (ActionType.SPEAK, ActionType.END_CALL)
                    and agent_action.speech_text
                ):
                    try:
                        mulaw = await tts_service.text_to_speech_for_twilio(
                            agent_action.speech_text,
                            model=settings.agent_tts_voice,
                        )
                        await _send_audio_to_twilio(call_id, mulaw)
                    except Exception as e:
                        logger.warning(f"[Twilio] Sim agent TTS failed: {e}")

                # -- Send to hospital agent --
                if agent_action.action_type == ActionType.SPEAK:
                    await hospital_ws.send(json.dumps({
                        "type": "caller_speech",
                        "text": agent_action.speech_text or "",
                    }))
                elif agent_action.action_type == ActionType.DTMF:
                    await hospital_ws.send(json.dumps({
                        "type": "caller_dtmf",
                        "digits": agent_action.dtmf_digits or "",
                    }))
                elif agent_action.action_type == ActionType.END_CALL:
                    if agent_action.speech_text:
                        await hospital_ws.send(json.dumps({
                            "type": "caller_speech",
                            "text": agent_action.speech_text,
                        }))
                    await hospital_ws.send(json.dumps({"type": "call_end"}))
                    break
                elif agent_action.action_type == ActionType.WAIT:
                    await hospital_ws.send(json.dumps({
                        "type": "caller_speech",
                        "text": "Mm-hmm",
                    }))

        # -- Call completed --
        call_state.status = CallStatus.COMPLETED
        await _send_to_browser(browser_ws, "call_complete", {
            "total_turns": turn_count,
            "message": "Simulated call completed.",
        })

        # -- SMS summary --
        if settings.twilio_account_sid and settings.twilio_auth_token:
            sms_phone = intent.user_phone or settings.default_user_phone
            sms_sid = await _send_sms_summary(
                to_phone=sms_phone,
                intent=intent,
                turn_count=turn_count,
                conversation_log=conversation_log,
                twilio_sid=twilio_sid,
            )
            if sms_sid:
                await _send_to_browser(browser_ws, "call_status", {
                    "status": "sms_sent",
                    "message": f"SMS summary sent to {sms_phone}.",
                })

    except websockets.exceptions.ConnectionClosed as e:
        logger.error(f"[Call-Sim] Hospital agent disconnected: {e}")
        await _send_to_browser(browser_ws, "error", {
            "message": "Hospital agent disconnected unexpectedly.",
        })
    except ConnectionRefusedError:
        logger.error(f"[Call-Sim] Could not connect to hospital agent")
        await _send_to_browser(browser_ws, "error", {
            "message": (
                "Could not connect to simulated agent. "
                "Make sure it is running on port 8001."
            ),
        })
    except Exception as e:
        logger.error(f"[Call-Sim] Error: {e}")
        await _send_to_browser(browser_ws, "error", {
            "message": f"Call error: {str(e)}",
        })
    finally:
        call_tasks.pop(call_id, None)
        twilio_call_sids.pop(call_id, None)
        if call_state.status != CallStatus.COMPLETED:
            call_state.status = CallStatus.FAILED
        if twilio_sid:
            await _end_twilio_call(twilio_sid)


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
