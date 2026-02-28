"""
Groq LLM service -- uses LLaMA 3.3 70B for intent extraction,
IVR classification, and response generation.
"""

from __future__ import annotations
import json
import logging
from typing import Any

from groq import Groq

from backend.config import settings

logger = logging.getLogger(__name__)

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.groq_api_key)
    return _client


async def chat_completion(
    messages: list[dict[str, str]],
    temperature: float = 0.1,
    max_tokens: int = 1024,
    response_format: dict | None = None,
) -> str:
    """
    Generic chat completion via Groq.

    Args:
        messages: List of {role, content} dicts
        temperature: Sampling temperature
        max_tokens: Max output tokens
        response_format: Optional {"type": "json_object"} for JSON mode

    Returns:
        The assistant's response text
    """
    client = _get_client()
    kwargs: dict[str, Any] = dict(
        model=settings.groq_llm_model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if response_format:
        kwargs["response_format"] = response_format

    try:
        response = client.chat.completions.create(**kwargs)
        result = response.choices[0].message.content.strip()
        logger.debug(f"[LLM] Response: {result[:200]}...")
        return result
    except Exception as e:
        logger.error(f"[LLM] Chat completion failed: {e}")
        raise


async def extract_json(
    messages: list[dict[str, str]],
    temperature: float = 0.05,
) -> dict:
    """
    Chat completion that returns parsed JSON.
    Uses Groq JSON mode for reliable structured output.
    """
    raw = await chat_completion(
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[LLM] Failed to parse JSON, trying to extract: {raw[:200]}")
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
        raise


# ------------------------------------------
# Specialized prompts
# ------------------------------------------

INTENT_EXTRACTION_SYSTEM = """\
You are an intent extraction system for an AI phone agent.
Given user speech, extract structured information about who they want to call \
and what they want to accomplish.

Return JSON with these fields:
{
  "intent": "book_appointment" | "cancel_appointment" | "reschedule_appointment" \
| "check_status" | "general_inquiry" | "complaint" | "phone_call" | "unknown",
  "target_entity": "name of the organization or person to call (string or null)",
  "target_phone": "phone number to call if explicitly mentioned (string or null)",
  "task_description": "brief summary of what the user wants to accomplish (string or null)",
  "hospital_name": "string or null (only if calling a hospital/clinic)",
  "hospital_branch": "string or null (e.g., Madinaguda branch)",
  "hospital_city": "string or null",
  "doctor_name": "string or null",
  "doctor_specialty": "string or null (e.g., Dermatologist, Neurologist)",
  "appointment_date": "string or null (preserve original format)",
  "user_name": "string or null",
  "user_phone": "string or null",
  "user_dob": "string or null (Date of Birth)",
  "user_age": "string or null",
  "user_gender": "string or null",
  "user_weight": "string or null",
  "user_height": "string or null"
}

Rules:
- Extract only what is explicitly mentioned. Use null for missing fields.
- target_entity = the organization, company, or person the user wants to call.
- task_description = a brief one-sentence summary of the user's goal.
- For hospital-related tasks, also fill the hospital/doctor fields.
- For non-hospital tasks (banks, airlines, restaurants, government, etc.), \
  fill target_entity and task_description; leave hospital fields as null.
- intent should be "phone_call" for generic tasks that do not fit the other types.\
"""


IVR_CLASSIFICATION_SYSTEM = """\
You are an IVR (Interactive Voice Response) call analyzer.
Given a transcript of what the other party on the phone just said, classify it.

Return JSON with these fields:
{
  "prompt_type": "greeting" | "open_question" | "confirmation" | "info_request" \
| "dtmf_menu" | "date_input" | "hold_music" | "success_message" | "farewell" | "unknown",
  "dtmf_options": [{"key": "1", "label": "description"}],
  "info_fields_requested": ["name", "phone", "email"],
  "date_format": "ddmmyyyy",
  "message": "summary of what was said"
}

Classification rules:
- "greeting": Initial hello/welcome message
- "open_question": Asks what the caller needs (free-form)
- "confirmation": Asks "Is that correct?" or similar yes/no question
- "info_request": Asks for specific information (name, phone, account number, etc.)
- "dtmf_menu": Lists numbered options to press on the keypad
- "date_input": Asks to enter a date via keypad
- "hold_music": Mentions waiting or playing hold music
- "success_message": Confirms action was completed successfully
- "farewell": Thank you / goodbye message\
"""


RESPONSE_GENERATION_SYSTEM = """\
You are an AI assistant making a phone call on behalf of a user.
You must respond naturally and concisely to the other party on the line.

Context about the call:
- User Intent: {intent_json}
- Conversation so far: {conversation_history}

Rules:
1. Be concise and natural -- respond as a real person would on a phone call
2. For confirmations, simply say "Yes, that is correct"
3. For open questions, state the user's need clearly in one sentence
4. For info requests, provide only the requested information
5. Never volunteer extra information unless asked
6. If asked for name and phone, provide both in one response

Return JSON:
{{
  "action_type": "speak" | "dtmf" | "wait" | "end_call",
  "speech_text": "what to say (if action_type is speak)",
  "dtmf_digits": "digits to press (if action_type is dtmf)",
  "reasoning": "brief explanation of why this action"
}}\
"""


async def extract_intent(user_text: str) -> dict:
    """Extract structured intent from user's spoken command."""
    return await extract_json([
        {"role": "system", "content": INTENT_EXTRACTION_SYSTEM},
        {"role": "user", "content": user_text},
    ])


async def classify_ivr_prompt(
    transcript: str,
    conversation_history: list[dict] = None,
) -> dict:
    """Classify what the other party on the phone just said."""
    history_text = ""
    if conversation_history:
        history_text = "\n\nConversation so far:\n" + "\n".join(
            f"{turn['role']}: {turn['text']}" for turn in conversation_history[-6:]
        )

    return await extract_json([
        {"role": "system", "content": IVR_CLASSIFICATION_SYSTEM},
        {"role": "user", "content": f"Other party said: \"{transcript}\"{history_text}"},
    ])


async def generate_response(
    ivr_classification: dict,
    user_intent: dict,
    conversation_history: list[dict],
) -> dict:
    """Decide what action to take and generate a response."""
    system_prompt = RESPONSE_GENERATION_SYSTEM.format(
        intent_json=json.dumps(user_intent, indent=2),
        conversation_history=json.dumps(conversation_history[-8:], indent=2),
    )

    return await extract_json([
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"The other party just said something classified as: "
                f"{json.dumps(ivr_classification, indent=2)}\n\n"
                f"What should I do next?"
            ),
        },
    ])
