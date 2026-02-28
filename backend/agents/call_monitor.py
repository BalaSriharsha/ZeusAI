"""
Agent 2 -- Call Monitor Agent

Responsibilities:
  1. Receive audio (from pre-recorded files)
  2. Transcribe via Groq Whisper
  3. Use Groq LLM to classify the IVR prompt type
  4. Pass the classification to Agent 3 for action
"""

from __future__ import annotations
import asyncio
import io
import logging
import struct
import time
from typing import Callable, Optional

from backend.services import sarvam_stt, groq_llm
from backend.models.schemas import (
    IVRClassification, IVRPromptType, DTMFOption, CallState,
)

logger = logging.getLogger(__name__)

# Silence gap threshold for splitting audio into turns (seconds)
TURN_GAP_THRESHOLD = 2.0


class CallMonitorAgent:
    """
    Processes audio, transcribes IVR speech,
    and classifies the prompt type for Agent 3.
    """

    def __init__(
        self,
        call_state: CallState,
        on_classification: Callable[[IVRClassification], None] | None = None,
        on_transcript: Callable[[str, str], None] | None = None,  # (role, text)
    ):
        self.call_state = call_state
        self.on_classification = on_classification
        self.on_transcript = on_transcript

        # Conversation tracking
        self._conversation_history: list[dict] = []

    async def process_audio_file(self, audio_bytes: bytes) -> list[dict]:
        """
        Process a complete pre-recorded audio file.

        Uses verbose transcription to get timestamped segments, groups
        them into IVR turns based on silence gaps, then classifies
        each turn.

        Returns:
            List of dicts, each containing:
              - transcript: str
              - classification: IVRClassification
              - start: float (seconds)
              - end: float (seconds)
        """
        # Step 1: Get verbose transcription with timestamps
        try:
            verbose = await sarvam_stt.transcribe_audio_verbose(
                audio_bytes,
                language="en",
            )
        except Exception as e:
            logger.error(f"[Agent2] Verbose transcription failed: {e}")
            # Fallback to simple transcription
            return await self._process_simple_transcription(audio_bytes)

        segments = verbose.get("segments", [])
        if not segments:
            logger.warning("[Agent2] No segments from verbose transcription, falling back")
            return await self._process_simple_transcription(audio_bytes)

        # Step 2: Group segments into IVR turns
        turns = self._group_segments_into_turns(segments)
        logger.info(f"[Agent2] Split audio into {len(turns)} turn(s)")

        # Step 3: Process each turn
        results = []
        for i, turn in enumerate(turns):
            transcript = turn["text"]
            if not transcript or len(transcript.strip()) < 3:
                continue

            logger.info(f"[Agent2] Turn {i+1}: {transcript}")

            # Track in conversation
            self._conversation_history.append({
                "role": "hospital_ivr",
                "text": transcript,
            })

            if self.on_transcript:
                self.on_transcript("hospital_ivr", transcript)

            # Classify
            classification = await self._classify_transcript(transcript)
            logger.info(f"[Agent2] Turn {i+1} classified as: {classification.prompt_type}")

            results.append({
                "transcript": transcript,
                "classification": classification,
                "start": turn["start"],
                "end": turn["end"],
            })

        return results

    async def _process_simple_transcription(self, audio_bytes: bytes) -> list[dict]:
        """Fallback: transcribe entire audio as a single turn."""
        try:
            stt_result = await sarvam_stt.transcribe_audio(
                audio_bytes,
                prompt="Hospital IVR system speaking. May include menu options with numbers.",
            )
            transcript = stt_result["transcript"]
        except Exception as e:
            logger.error(f"[Agent2] Simple transcription failed: {e}")
            return []

        if not transcript or len(transcript.strip()) < 3:
            logger.debug("[Agent2] Empty or very short transcript")
            return []

        logger.info(f"[Agent2] Full transcript: {transcript}")

        self._conversation_history.append({
            "role": "hospital_ivr",
            "text": transcript,
        })

        if self.on_transcript:
            self.on_transcript("hospital_ivr", transcript)

        classification = await self._classify_transcript(transcript)

        return [{
            "transcript": transcript,
            "classification": classification,
            "start": 0.0,
            "end": 0.0,
        }]

    async def _classify_transcript(self, transcript: str) -> IVRClassification:
        """Classify a single transcript using the LLM."""
        try:
            raw_classification = await groq_llm.classify_ivr_prompt(
                transcript,
                self._conversation_history,
            )
        except Exception as e:
            logger.error(f"[Agent2] Classification failed: {e}")
            return IVRClassification(
                prompt_type=IVRPromptType.UNKNOWN,
                raw_transcript=transcript,
                message=transcript,
            )

        classification = IVRClassification(
            prompt_type=self._map_prompt_type(
                raw_classification.get("prompt_type", "unknown")
            ),
            raw_transcript=transcript,
            dtmf_options=[
                DTMFOption(key=opt["key"], label=opt["label"])
                for opt in raw_classification.get("dtmf_options", [])
            ],
            info_fields_requested=raw_classification.get("info_fields_requested", []),
            date_format=raw_classification.get("date_format"),
            message=raw_classification.get("message", transcript),
        )

        return classification

    def _group_segments_into_turns(
        self,
        segments: list[dict],
        gap_threshold: float = TURN_GAP_THRESHOLD,
    ) -> list[dict]:
        """
        Group Whisper segments into IVR turns based on silence gaps.

        A gap larger than gap_threshold seconds between consecutive
        segments indicates a turn boundary (where the caller would
        have been speaking).
        """
        if not segments:
            return []

        turns = []
        current_segments = [segments[0]]

        for i in range(1, len(segments)):
            prev_end = segments[i - 1].get("end", 0)
            curr_start = segments[i].get("start", 0)
            gap = curr_start - prev_end

            if gap >= gap_threshold:
                # Turn boundary found
                text = " ".join(
                    s.get("text", "").strip() for s in current_segments
                ).strip()
                turns.append({
                    "text": text,
                    "start": current_segments[0].get("start", 0),
                    "end": current_segments[-1].get("end", 0),
                })
                current_segments = [segments[i]]
            else:
                current_segments.append(segments[i])

        # Flush the last turn
        if current_segments:
            text = " ".join(
                s.get("text", "").strip() for s in current_segments
            ).strip()
            turns.append({
                "text": text,
                "start": current_segments[0].get("start", 0),
                "end": current_segments[-1].get("end", 0),
            })

        return turns

    def _map_prompt_type(self, type_str: str) -> IVRPromptType:
        """Map raw type string to enum."""
        mapping = {
            "greeting": IVRPromptType.GREETING,
            "open_question": IVRPromptType.OPEN_QUESTION,
            "confirmation": IVRPromptType.CONFIRMATION,
            "info_request": IVRPromptType.INFO_REQUEST,
            "dtmf_menu": IVRPromptType.DTMF_MENU,
            "date_input": IVRPromptType.DATE_INPUT,
            "hold_music": IVRPromptType.HOLD_MUSIC,
            "success_message": IVRPromptType.SUCCESS_MESSAGE,
            "farewell": IVRPromptType.FAREWELL,
        }
        return mapping.get(type_str, IVRPromptType.UNKNOWN)

    def add_agent_response(self, text: str) -> None:
        """Track our agent's responses in conversation history."""
        self._conversation_history.append({
            "role": "our_agent",
            "text": text,
        })
