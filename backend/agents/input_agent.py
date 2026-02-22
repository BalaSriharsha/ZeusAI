"""
Agent 1 -- Input Agent

Responsibilities:
  1. Receive user's voice or text input from the browser
  2. Transcribe it using Groq Whisper (if voice)
  3. Extract structured intent using Groq LLM
  4. Resolve the target phone number (from intent or registry)
  5. Return a CallState ready for the call
"""

from __future__ import annotations
import logging
import re
from typing import Optional

from backend.services import groq_stt, groq_llm
from backend.models.schemas import (
    UserIntent, IntentType, CallState, CallStatus,
)
from backend.config import settings

logger = logging.getLogger(__name__)

# Maps raw LLM intent strings to IntentType
_INTENT_MAP = {
    "book_appointment": IntentType.BOOK_APPOINTMENT,
    "cancel_appointment": IntentType.CANCEL_APPOINTMENT,
    "reschedule_appointment": IntentType.RESCHEDULE_APPOINTMENT,
    "check_status": IntentType.CHECK_STATUS,
    "general_inquiry": IntentType.GENERAL_INQUIRY,
    "complaint": IntentType.COMPLAINT,
    "phone_call": IntentType.PHONE_CALL,
}


def _normalize(text: str) -> str:
    """Normalize a string for fuzzy matching: lowercase, underscores, strip non-alnum."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


class InputAgent:
    """Processes user input and prepares a call session."""

    def __init__(self):
        self.hospital_numbers = settings.get_hospital_numbers()
        logger.info(
            f"[Agent1] Loaded registry with {len(self.hospital_numbers)} entries: "
            f"{list(self.hospital_numbers.keys())}"
        )

    async def process_voice_input(self, audio_bytes: bytes) -> UserIntent:
        """Transcribe audio via Groq Whisper, then extract intent."""
        raw_text = await groq_stt.transcribe_audio(audio_bytes)
        logger.info(f"[Agent1] User said: {raw_text}")
        intent = await self._extract_intent(raw_text)
        intent.raw_text = raw_text
        return intent

    async def process_text_input(self, text: str) -> UserIntent:
        """Extract intent from typed text."""
        logger.info(f"[Agent1] User typed: {text}")
        intent = await self._extract_intent(text)
        intent.raw_text = text
        return intent

    async def _extract_intent(self, text: str) -> UserIntent:
        """Use Groq LLM to extract structured intent."""
        raw = await groq_llm.extract_intent(text)

        intent = UserIntent(
            intent=_INTENT_MAP.get(raw.get("intent", ""), IntentType.UNKNOWN),
            target_entity=raw.get("target_entity"),
            target_phone=raw.get("target_phone"),
            task_description=raw.get("task_description"),
            hospital_name=raw.get("hospital_name"),
            hospital_branch=raw.get("hospital_branch"),
            hospital_city=raw.get("hospital_city"),
            doctor_name=raw.get("doctor_name"),
            doctor_specialty=raw.get("doctor_specialty"),
            appointment_date=raw.get("appointment_date"),
            user_name=raw.get("user_name") or settings.default_user_name,
            user_phone=raw.get("user_phone") or settings.default_user_phone,
        )

        # Auto-fill target_entity from hospital_name if not set
        if not intent.target_entity and intent.hospital_name:
            parts = [intent.hospital_name]
            if intent.hospital_branch:
                parts.append(intent.hospital_branch)
            if intent.hospital_city:
                parts.append(intent.hospital_city)
            intent.target_entity = ", ".join(parts)

        # Auto-fill task_description from intent if not set
        if not intent.task_description:
            intent.task_description = self._default_task_description(intent)

        logger.info(
            f"[Agent1] Extracted: intent={intent.intent.value}, "
            f"target_entity={intent.target_entity}, "
            f"hospital_name={intent.hospital_name}, "
            f"hospital_branch={intent.hospital_branch}, "
            f"target_phone={intent.target_phone}"
        )
        return intent

    def _default_task_description(self, intent: UserIntent) -> str:
        """Build a fallback task description from known fields."""
        descriptions = {
            IntentType.BOOK_APPOINTMENT: "book an appointment",
            IntentType.CANCEL_APPOINTMENT: "cancel an appointment",
            IntentType.RESCHEDULE_APPOINTMENT: "reschedule an appointment",
            IntentType.CHECK_STATUS: "check appointment status",
            IntentType.GENERAL_INQUIRY: "make a general inquiry",
            IntentType.COMPLAINT: "file a complaint",
        }
        desc = descriptions.get(intent.intent, "make a phone call")
        if intent.doctor_name:
            desc += f" with Dr. {intent.doctor_name}"
        if intent.doctor_specialty:
            desc += f" ({intent.doctor_specialty})"
        return desc

    def resolve_target_phone(self, intent: UserIntent) -> Optional[str]:
        """
        Determine the phone number to call.

        Priority:
          1. target_phone from intent (user mentioned a number explicitly)
          2. Registry lookup by hospital_name + branch
          3. Registry lookup by target_entity
          4. None (will use simulation)
        """
        # 1. Already has a target phone from user input
        if intent.target_phone:
            logger.info(f"[Agent1] Target phone from intent: {intent.target_phone}")
            return intent.target_phone

        # 2. Try hospital registry with hospital_name
        if intent.hospital_name:
            phone = self._lookup_registry_by_name(
                intent.hospital_name, intent.hospital_branch,
            )
            if phone:
                logger.info(f"[Agent1] Target phone from registry (hospital_name): {phone}")
                return phone

        # 3. Try hospital registry with target_entity
        if intent.target_entity:
            phone = self._lookup_registry_by_name(intent.target_entity, None)
            if phone:
                logger.info(f"[Agent1] Target phone from registry (target_entity): {phone}")
                return phone

        logger.info("[Agent1] No target phone found -- will use simulation mode")
        return None

    def _lookup_registry_by_name(
        self,
        name: str,
        branch: str | None,
    ) -> Optional[str]:
        """
        Look up a phone number from the registry using flexible matching.

        Tries (in order):
          1. Exact key match (name + branch normalized)
          2. Fuzzy: any registry key that CONTAINS the normalized name
          3. Fuzzy: any registry key where the normalized name shares >70% of characters
        """
        if not name:
            return None

        name_norm = _normalize(name)

        # Build exact key
        if branch:
            exact_key = _normalize(name + branch)
        else:
            exact_key = name_norm

        logger.debug(
            f"[Agent1] Registry lookup: name_norm={name_norm}, "
            f"exact_key={exact_key}, "
            f"registry_keys={list(self.hospital_numbers.keys())}"
        )

        # 1. Direct match
        for key, number in self.hospital_numbers.items():
            key_norm = _normalize(key)
            if key_norm == exact_key:
                logger.info(f"[Agent1] Registry exact match: {key}")
                return number

        # 2. Name+branch substring in key (or key contains name+branch)
        if branch:
            branch_norm = _normalize(branch)
            for key, number in self.hospital_numbers.items():
                key_norm = _normalize(key)
                if name_norm in key_norm and branch_norm in key_norm:
                    logger.info(f"[Agent1] Registry fuzzy match (name+branch): {key}")
                    return number

        # 3. Name-only substring match
        for key, number in self.hospital_numbers.items():
            key_norm = _normalize(key)
            if name_norm in key_norm or key_norm in name_norm:
                logger.info(f"[Agent1] Registry name-only match: {key}")
                return number

        # 4. Loose match: check if most words from the name appear in any key
        name_words = set(re.findall(r"[a-z]+", name.lower()))
        for key, number in self.hospital_numbers.items():
            key_words = set(re.findall(r"[a-z]+", key.lower()))
            overlap = name_words & key_words
            if len(overlap) >= max(1, len(name_words) - 1):
                logger.info(
                    f"[Agent1] Registry word-overlap match: {key} "
                    f"(overlap={overlap})"
                )
                return number

        logger.warning(
            f"[Agent1] Registry lookup failed: name={name}, branch={branch}, "
            f"exact_key={exact_key}"
        )
        return None

    async def prepare_session(
        self,
        intent: UserIntent,
        call_id: str,
    ) -> CallState:
        """
        Prepare a call session.
        Resolves the target phone number and stores it in the intent.
        """
        target_phone = self.resolve_target_phone(intent)
        if target_phone:
            intent.target_phone = target_phone

        label = intent.target_entity or "target"
        if target_phone:
            label += f" ({target_phone})"

        mode = "real call" if target_phone else "simulation"

        return CallState(
            call_sid=call_id,
            status=CallStatus.IN_PROGRESS,
            user_intent=intent,
            conversation_history=[{
                "role": "system",
                "text": f"Session ready for {label} [{mode}]. Start the call when ready.",
            }],
        )
