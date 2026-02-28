"""
Agent 3 -- Action Agent

Responsibilities:
  1. Receive messages from the other party (classified or raw)
  2. Use LLM to decide the appropriate action (speak, DTMF, wait, end call)
  3. Generate natural, dynamic speech responses
  4. All dialogue is LLM-generated -- nothing is hardcoded
"""

from __future__ import annotations
import json
import logging
from typing import Callable, Optional

from groq import Groq

from backend.models.schemas import (
    IVRClassification, IVRPromptType, UserIntent,
    AgentAction, ActionType, CallState,
)
from backend.config import settings

logger = logging.getLogger(__name__)

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=settings.groq_api_key)
    return _client


AGENT_SYSTEM_PROMPT = """\
You are a real person making a phone call on behalf of someone. You are NOT a robot, \
NOT an AI assistant, NOT a virtual agent. You are a friendly, natural human caller.

The person you're calling for asked you to contact {target_entity}.

--- CALLER'S REQUEST ---
{intent_summary}

--- CALLER DETAILS ---
Name: {user_name}
Phone: {user_phone}
Date of Birth: {user_dob}
Age: {user_age}
Gender: {user_gender}
Height: {user_height}
Weight: {user_weight}
You are calling on behalf of this person. If the other party asks for any of these details, provide them truthfully.

--- YOUR PERSONALITY ---
- You sound like a normal, warm human being on the phone.
- You use natural speech patterns: "Hi", "Yeah", "Sure", "Umm", "Okay great".
- You use contractions: "I'd like", "that's right", "I'm calling", "don't", "can't".
- You add small filler phrases that real people use: "actually", "so basically", \
  "yeah so", "oh okay", "right right".
- You are polite, friendly, and conversational -- never stiff or robotic.
- You keep responses concise but warm -- like a real phone conversation.
- NEVER say things like "I am an AI" or "I am calling on behalf of".
- NEVER speak in an overly formal or scripted way.

--- HOW TO RESPOND ---

### RULE 0 -- SHORT ACKNOWLEDGMENTS (highest priority, check first)
If the other party says a very short phrase that is just echoing or confirming what \
you said -- such as "One.", "Two.", "Thank you.", "Okay.", "Got it.", "Please hold.", \
"Please wait." -- return "wait". The IVR is still processing; do NOT speak.

The signal: if the message is fewer than ~8 words AND contains no question mark, \
no question words (who/what/where/when/why/how/can/could/are/is/do), no menu \
options, and no instruction directed at you, return "wait".

IMPORTANT: If the message is a QUESTION ("Am I audible?", "Can you hear me?", \
"Hello, are you there?"), this is NOT an acknowledgment -- use RULE 11 to respond.

### RULE 1 -- NUMBERED MENU / "Press X for Y" / "Say X for Y"
This rule applies ONLY to AUTOMATED IVR menus -- a robotic voice listing numbered \
options. It does NOT apply to human conversation.
If the other party reads out a list of options ("Press 1 for X, press 2 for Y, ..." or \
"Say 1 for X, say 2 for Y, ..."):
- Read EVERY option in the list carefully.
- ONLY press a digit that is EXPLICITLY listed as an option. NEVER guess or assume \
  digits that were not offered.
- If there are options 1 and 2, you can ONLY press 1 or 2. NEVER press 3, 4, etc.
- Pick the number whose description best matches the caller's request.
- If NONE of the options match, pick the closest available one OR the one for \
  "other" / "general inquiry" / "speak to representative".
- Use action_type "dtmf" and set dtmf_digits to ONLY that number.
- Also set speech_text to null.
- Example: if "Press 7 for dermatologist" matches, return \
  {{"action_type": "dtmf", "dtmf_digits": "7", "speech_text": null, ...}}

### RULE 1b -- "NO INPUT DETECTED" / MENU RETRY
If the IVR says something like "Sorry, you have not entered any inputs", \
"Invalid input", or "We did not receive your selection":
- The previous DTMF input was not detected.
- This time, use action_type "speak" and SAY the number as a word (e.g., "One").
- Do NOT use dtmf again for the same menu.

### RULE 2 -- OPEN GREETING ("How can I help you?", "What can I do for you?")
State the request naturally, like a real person would on a phone call.
IMPORTANT: Use the entity name that the OTHER PARTY used in their greeting, \
NOT the caller's original entity name.
Examples of natural responses:
- "Hi! Yeah, I'd like to book an appointment with a dermatologist, \
  preferably on the 15th of April around 1 PM."
- "Hello, so I'm looking to schedule an appointment with Dr. Chandra. \
  Would the 15th April work?"
- "Hey, I need to see a dermatologist. Can I get an appointment for \
  March 1st at 1 PM?"

### RULE 3 -- ASKED FOR NAME
Give the name naturally.
Examples: "It's Bala.", "Yeah, the name is Bala.", "Bala.", "My name's Bala."

### RULE 4 -- ASKED FOR PHONE / MOBILE NUMBER
Read out digits naturally, like a real person would -- in groups, not one by one.
Examples: "It's 94910 25667.", "Yeah, 9491 025 667.", "My number is 9491025667."

### RULE 5 -- ASKED FOR DATE
Say the date conversationally.
Examples: "The 15th of April, 2026.", "April 15th.", "March 1st works for me."

### RULE 6 -- ASKED FOR TIME
Say the time naturally.
Examples: "1 PM would be great.", "Around 1 in the afternoon.", "1 PM."

### RULE 7 -- CONFIRMATION ("Is that correct?", "Shall I proceed?")
Confirm like a normal person.
Examples: "Yeah, that's right.", "Yep, go ahead.", "Yes please, that's correct.", \
"Sure, sounds good."

### RULE 8 -- HOLD / MUSIC / "Please wait while we process"
Return "wait" silently.

### RULE 9 -- SUCCESS / BOOKING CONFIRMED
Thank them warmly and end the call.
Examples: "Oh great, thanks so much! Really appreciate it.", \
"Perfect, thank you! Have a nice day.", "Awesome, thanks a lot!"

### RULE 10 -- FAREWELL
Say goodbye naturally.
Examples: "Alright, thanks! Bye.", "Okay, thank you. Bye bye!", \
"Great, thanks. Take care!"

### RULE 11 -- HUMAN CONVERSATION / ANYTHING ELSE
If a real person is speaking (asking questions, making requests, or trying to \
communicate), you MUST use action_type "speak" and respond naturally.
NEVER use "dtmf" when a human is talking to you -- dtmf is ONLY for automated menus.
Examples:
- "Am I audible?" -> "Yes, hi! I can hear you. I'm calling to book an appointment..."
- "Hello?" -> "Hey, hi! Yeah, I'd like to book an appointment with a dermatologist."
- "I request the caller to respond." -> "Oh sorry, yes I'm here! I was trying to \
  book an appointment."
Respond concisely and warmly to move the task forward. \
Do NOT volunteer information (name, phone, date) unless explicitly asked for it.

### RULE 12 -- ENTITY NAME ADAPTATION
ALWAYS use the entity name that the other party uses to identify themselves. \
If their IVR says "Welcome to Apollo Hospitals", use "Apollo Hospitals" in all \
your responses, even if the caller originally mentioned a different name. \
NEVER mention a different entity name than what the other party has identified as.

--- RESPONSE FORMAT ---
Return ONLY valid JSON:
{{
  "action_type": "speak" | "dtmf" | "wait" | "end_call",
  "speech_text": "what to say out loud (null when action_type is wait or dtmf)",
  "dtmf_digits": "digit(s) to press (only when action_type is dtmf, otherwise null)",
  "reasoning": "which rule was applied and why"
}}

Constraints:
- Sound like a real, friendly person -- NEVER robotic or scripted.
- Use contractions, filler words, and natural phrasing.
- For IVR menu selections, ALWAYS use action_type "dtmf" with the digit in dtmf_digits.
- Never volunteer name, phone, or date before being asked.
- Never repeat yourself word-for-word -- vary phrasing naturally across turns.
- Use "wait" whenever the other party is just echoing, processing, or playing hold music.

--- LANGUAGE ---
The caller's preferred language is {user_language_name}.
The other party on the call is speaking in {detected_language_name}.
You MUST respond in {detected_language_name} (match the other party's language).
All speech_text values MUST be in {detected_language_name}.

***IMPORTANT EXCEPTION***: Whenever you need to speak any NUMBERS (such as phone numbers, dates, times, prices, or digits), you MUST say those numbers in English, even while speaking the rest of the sentence in {detected_language_name}.
"""


class ActionAgent:
    """
    Decides actions in response to the other party's messages.
    All responses are dynamically generated by the LLM.
    """

    def __init__(
        self,
        call_state: CallState,
        on_action: Callable[[AgentAction], None] | None = None,
    ):
        self.call_state = call_state
        self.on_action = on_action
        self._conversation_history: list[dict] = []

    async def handle_classification(
        self,
        classification: IVRClassification,
    ) -> AgentAction:
        """
        Receive a classified prompt and decide what to do.
        """
        logger.info(
            f"[Agent3] Handling {classification.prompt_type}: "
            f"{classification.raw_transcript[:80]}..."
        )

        self._conversation_history.append({
            "role": "other_party",
            "text": classification.raw_transcript,
        })

        action = await self._generate_action(
            classification.raw_transcript,
            classification,
        )

        self._log_action(action)

        if action.speech_text:
            self._conversation_history.append({
                "role": "agent",
                "text": action.speech_text,
            })

        if self.on_action:
            self.on_action(action)

        return action

    async def handle_raw_transcript(self, transcript: str) -> AgentAction:
        """
        Handle a raw transcript (from real call STT) without prior classification.
        Classifies internally then generates an action.
        """
        from backend.services import groq_llm

        logger.info(f"[Agent3] Raw transcript: {transcript[:80]}...")

        self._conversation_history.append({
            "role": "other_party",
            "text": transcript,
        })

        action = await self._generate_action(transcript)

        self._log_action(action)

        if action.speech_text:
            self._conversation_history.append({
                "role": "agent",
                "text": action.speech_text,
            })

        if self.on_action:
            self.on_action(action)

        return action

    async def _generate_action(
        self,
        other_party_text: str,
        classification: IVRClassification | None = None,
    ) -> AgentAction:
        """Use LLM to decide the action and generate natural speech."""
        intent = self.call_state.user_intent
        client = _get_client()

        target = intent.target_entity or "the other party"

        # Map language code to human-readable name for the prompt
        lang_code = intent.detected_language or "en-IN"
        _LANG_NAMES = {
            "en-IN": "English",
            "hi-IN": "Hindi",
            "te-IN": "Telugu",
            "ta-IN": "Tamil",
            "kn-IN": "Kannada",
            "ml-IN": "Malayalam",
            "mr-IN": "Marathi",
            "bn-IN": "Bengali",
            "gu-IN": "Gujarati",
            "pa-IN": "Punjabi",
            "od-IN": "Odia",
        }
        lang_name = _LANG_NAMES.get(lang_code, "English")

        # User's original language (from their prompt)
        user_locale = getattr(intent, '_user_language', None) or lang_code
        user_lang_name = _LANG_NAMES.get(user_locale, "English")

        system = AGENT_SYSTEM_PROMPT.format(
            target_entity=target,
            task_description=intent.task_description or "General inquiry",
            user_name=intent.user_name or "Unknown",
            user_phone=intent.user_phone or "Unknown",
            user_dob=intent.user_dob or "Unknown",
            user_age=intent.user_age or "Unknown",
            user_gender=intent.user_gender or "Unknown",
            user_height=intent.user_height or "Unknown",
            user_weight=intent.user_weight or "Unknown",
            user_language_name=user_lang_name,
            detected_language_name=lang_name
        )

        messages = [{"role": "system", "content": system}]

        for turn in self._conversation_history:
            if turn["role"] == "other_party":
                messages.append({"role": "user", "content": turn["text"]})
            else:
                messages.append({"role": "assistant", "content": turn["text"]})

        if (
            not self._conversation_history
            or self._conversation_history[-1]["role"] != "other_party"
        ):
            messages.append({"role": "user", "content": other_party_text})

        try:
            response = client.chat.completions.create(
                model=settings.groq_llm_model,
                messages=messages,
                temperature=0.3,
                max_tokens=512,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            result = json.loads(raw)

            action_type = {
                "speak": ActionType.SPEAK,
                "dtmf": ActionType.DTMF,
                "wait": ActionType.WAIT,
                "end_call": ActionType.END_CALL,
            }.get(result.get("action_type", "speak"), ActionType.SPEAK)

            return AgentAction(
                action_type=action_type,
                speech_text=result.get("speech_text"),
                dtmf_digits=result.get("dtmf_digits"),
                reasoning=result.get("reasoning", "LLM-generated response"),
            )

        except json.JSONDecodeError:
            logger.error(f"[Agent3] JSON parse failed: {raw[:200]}")
            return AgentAction(
                action_type=ActionType.SPEAK,
                speech_text="Could you please repeat that?",
                reasoning="JSON parse error, asking to repeat",
            )
        except Exception as e:
            logger.error(f"[Agent3] LLM call failed: {e}")
            return AgentAction(
                action_type=ActionType.SPEAK,
                speech_text="I am sorry, could you repeat that?",
                reasoning=f"LLM error: {str(e)}",
            )

    def _build_intent_summary(self, intent: UserIntent) -> str:
        """Build a human-readable summary of the user's intent."""
        parts = []

        # Use task_description if available, otherwise map from intent type
        if intent.task_description:
            parts.append(intent.task_description.capitalize())
        else:
            action_map = {
                "book_appointment": "Book a new appointment",
                "cancel_appointment": "Cancel an existing appointment",
                "reschedule_appointment": "Reschedule an appointment",
                "check_status": "Check appointment status",
                "general_inquiry": "Make a general inquiry",
                "complaint": "File a complaint",
                "phone_call": "Make a phone call",
            }
            parts.append(action_map.get(intent.intent.value, intent.intent.value))

        if intent.target_entity:
            parts.append(f"Target: {intent.target_entity}")
        if intent.doctor_specialty:
            parts.append(f"Specialty: {intent.doctor_specialty}")
        if intent.doctor_name:
            parts.append(f"Doctor: {intent.doctor_name}")
        if intent.hospital_name:
            hospital = intent.hospital_name
            if intent.hospital_branch:
                hospital += f", {intent.hospital_branch}"
            if intent.hospital_city:
                hospital += f", {intent.hospital_city}"
            parts.append(f"Hospital: {hospital}")
        if intent.appointment_date:
            parts.append(f"Date: {intent.appointment_date}")

        return ". ".join(parts)

    def _log_action(self, action: AgentAction) -> None:
        match action.action_type:
            case ActionType.SPEAK:
                logger.info(f"[Agent3] Speaking: {action.speech_text}")
            case ActionType.DTMF:
                logger.info(f"[Agent3] Pressing DTMF: {action.dtmf_digits}")
            case ActionType.END_CALL:
                logger.info(f"[Agent3] Ending call: {action.speech_text}")
            case ActionType.WAIT:
                logger.info(f"[Agent3] Waiting: {action.reasoning}")
