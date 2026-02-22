"""
Hospital Agent -- Main Application

A simulated hospital phone system that accepts calls via WebSocket
and responds using LLM-driven conversation logic with TTS audio.

Run separately: python -m uvicorn hospital_agent.main:app --port 8001 --reload
"""

from __future__ import annotations
import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from hospital_agent.config import settings
from hospital_agent.brain import generate_turn

# ------------------------------------------------
# Logging
# ------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"


# ------------------------------------------------
# TTS
# ------------------------------------------------

async def generate_tts(text: str) -> str:
    """Generate TTS audio as base64 mp3 using Deepgram Aura."""
    params = {
        "model": settings.hospital_tts_voice,
        "encoding": "mp3",
    }
    headers = {
        "Authorization": f"Token {settings.deepgram_api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                DEEPGRAM_TTS_URL,
                params=params,
                headers=headers,
                json={"text": text},
            )
            resp.raise_for_status()
            return base64.b64encode(resp.content).decode()
    except Exception as e:
        logger.error(f"[TTS] Failed: {e}")
        return ""


# ------------------------------------------------
# Application
# ------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Hospital Agent starting on port {settings.hospital_port}...")
    logger.info(f"   Voice: {settings.hospital_tts_voice}")
    yield
    logger.info("Hospital Agent shutting down...")


app = FastAPI(title="Hospital Agent", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------
# Call WebSocket
# ------------------------------------------------

@app.websocket("/ws/call")
async def handle_call(ws: WebSocket):
    """
    WebSocket endpoint simulating a hospital phone line.

    Protocol:
      Caller sends:
        {"type": "call_start", "intent": {...}, "mode": "conversational"|"dtmf"}
        {"type": "caller_speech", "text": "..."}
        {"type": "caller_dtmf", "digits": "2"}
        {"type": "call_end"}

      Hospital sends:
        {"type": "hospital_speech", "text": "...", "audio_b64": "...",
         "expects": "speech"|"dtmf"|"none", "call_ended": false}
    """
    await ws.accept()
    logger.info("[Call] New call connected")

    conversation: list[dict] = []
    mode = "conversational"
    intent_summary = "{}"

    try:
        # Wait for call_start
        raw = await ws.receive_text()
        msg = json.loads(raw)

        if msg.get("type") != "call_start":
            await ws.close(code=1008, reason="Expected call_start")
            return

        intent_summary = json.dumps(msg.get("intent", {}), default=str)
        mode = msg.get("mode", "conversational")
        logger.info(f"[Call] Started in {mode} mode")

        # Conversation loop
        while True:
            # Hospital generates a turn
            turn = await generate_turn(mode, intent_summary, conversation)
            speech = turn["speech"]
            expects = turn["expects"]
            hold = turn.get("hold", False)
            call_should_end = turn.get("call_should_end", False)

            # Generate TTS audio
            audio_b64 = await generate_tts(speech)

            # Track in history
            conversation.append({"role": "hospital", "text": speech})

            # Send to caller
            await ws.send_text(json.dumps({
                "type": "hospital_speech",
                "text": speech,
                "audio_b64": audio_b64,
                "expects": expects,
                "call_ended": call_should_end,
            }))

            logger.info(f"[Call] Hospital: {speech[:80]}...")

            if call_should_end:
                logger.info("[Call] Call ended by hospital")
                break

            # If hold, wait a few seconds then continue (hospital generates next turn)
            if hold:
                await asyncio.sleep(3)
                continue

            # If expects "none", hospital continues (rare edge case)
            if expects == "none":
                continue

            # Wait for caller response
            raw = await ws.receive_text()
            caller_msg = json.loads(raw)

            if caller_msg.get("type") == "call_end":
                logger.info("[Call] Caller ended the call")
                break
            elif caller_msg.get("type") == "caller_speech":
                caller_text = caller_msg.get("text", "")
                conversation.append({"role": "caller", "text": caller_text})
                logger.info(f"[Call] Caller: {caller_text[:80]}...")
            elif caller_msg.get("type") == "caller_dtmf":
                digits = caller_msg.get("digits", "")
                conversation.append({"role": "caller", "text": f"[Pressed {digits}]"})
                logger.info(f"[Call] Caller pressed: {digits}")

    except WebSocketDisconnect:
        logger.info("[Call] Caller disconnected")
    except Exception as e:
        logger.error(f"[Call] Error: {e}")
    finally:
        logger.info("[Call] Call session ended")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "hospital-agent"}


# ------------------------------------------------
# Run
# ------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "hospital_agent.main:app",
        host="0.0.0.0",
        port=settings.hospital_port,
        reload=True,
    )
