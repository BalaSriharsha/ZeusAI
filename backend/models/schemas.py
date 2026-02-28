"""
Pydantic models used across the system.
"""

from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel


# ------------------------------------------
# User Intent (extracted by Agent 1)
# ------------------------------------------

class IntentType(str, Enum):
    BOOK_APPOINTMENT = "book_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    CHECK_STATUS = "check_status"
    GENERAL_INQUIRY = "general_inquiry"
    COMPLAINT = "complaint"
    PHONE_CALL = "phone_call"
    UNKNOWN = "unknown"


class UserIntent(BaseModel):
    intent: IntentType = IntentType.UNKNOWN

    # Generic target (who to call and why)
    target_entity: Optional[str] = None      # "Apollo Hospital", "SBI Bank", etc.
    target_phone: Optional[str] = None       # Phone number to call
    task_description: Optional[str] = None   # "book appointment", "check balance"

    # Domain-specific fields (optional, filled when relevant)
    hospital_name: Optional[str] = None
    hospital_branch: Optional[str] = None
    hospital_city: Optional[str] = None
    doctor_name: Optional[str] = None
    doctor_specialty: Optional[str] = None
    appointment_date: Optional[str] = None   # ISO format or raw string

    # User details
    user_name: Optional[str] = None
    user_phone: Optional[str] = None
    user_dob: Optional[str] = None
    user_age: Optional[str] = None
    user_gender: Optional[str] = None
    user_weight: Optional[str] = None
    user_height: Optional[str] = None

    # Detected language from STT (e.g. "hi-IN", "te-IN", "en-IN")
    detected_language: Optional[str] = None

    raw_text: str = ""


# ------------------------------------------
# IVR Classification (produced by Agent 2)
# ------------------------------------------

class IVRPromptType(str, Enum):
    GREETING = "greeting"
    OPEN_QUESTION = "open_question"
    CONFIRMATION = "confirmation"
    INFO_REQUEST = "info_request"
    DTMF_MENU = "dtmf_menu"
    DATE_INPUT = "date_input"
    HOLD_MUSIC = "hold_music"
    SUCCESS_MESSAGE = "success_message"
    FAREWELL = "farewell"
    UNKNOWN = "unknown"


class DTMFOption(BaseModel):
    key: str
    label: str


class IVRClassification(BaseModel):
    prompt_type: IVRPromptType
    raw_transcript: str = ""
    dtmf_options: list[DTMFOption] = []
    info_fields_requested: list[str] = []
    date_format: Optional[str] = None
    message: Optional[str] = None


# ------------------------------------------
# Action Decision (produced by Agent 3)
# ------------------------------------------

class ActionType(str, Enum):
    SPEAK = "speak"
    DTMF = "dtmf"
    WAIT = "wait"
    END_CALL = "end_call"


class AgentAction(BaseModel):
    action_type: ActionType
    speech_text: Optional[str] = None
    dtmf_digits: Optional[str] = None
    reasoning: str = ""


# ------------------------------------------
# Call State
# ------------------------------------------

class CallStatus(str, Enum):
    IDLE = "idle"
    INITIATING = "initiating"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class CallState(BaseModel):
    call_sid: Optional[str] = None
    status: CallStatus = CallStatus.IDLE
    user_intent: Optional[UserIntent] = None
    conversation_history: list[dict] = []
    current_ivr_classification: Optional[IVRClassification] = None


# ------------------------------------------
# WebSocket Messages
# ------------------------------------------

class WSMessageType(str, Enum):
    USER_AUDIO = "user_audio"
    USER_TEXT = "user_text"
    CALL_STATUS = "call_status"
    TRANSCRIPT = "transcript"
    AGENT_ACTION = "agent_action"
    ERROR = "error"
    CALL_ENDED = "call_ended"


class WSMessage(BaseModel):
    type: WSMessageType
    data: dict = {}
