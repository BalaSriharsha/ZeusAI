"""
Hospital Script Generator

Uses the LLM to dynamically generate realistic hospital-agent-side
dialogue for two IVR scenarios, based entirely on the user's extracted
intent. Nothing is hardcoded -- the LLM produces every line.
"""

from __future__ import annotations
import json
import logging

from backend.services import groq_llm
from backend.models.schemas import UserIntent

logger = logging.getLogger(__name__)

SCRIPT_GENERATION_SYSTEM = """You are a realistic hospital IVR dialogue scriptwriter.

Given a caller's intent, you generate the COMPLETE dialogue that a hospital's phone system
(the hospital agent) would say during a call. You produce two separate scenario scripts.

SCENARIO 1 -- CONVERSATIONAL IVR:
The hospital agent is an AI assistant that speaks naturally.
Flow pattern:
- Greets the caller and asks how it can help (open question)
- Caller states their need
- Hospital agent repeats back what it understood and asks for confirmation
- Caller confirms
- Hospital agent asks for personal details (name, mobile number)
- Caller provides them
- Hospital agent repeats the details back and asks for confirmation
- Caller confirms
- Hospital agent says please wait, processing
- [Hold music / waiting]
- Hospital agent confirms the action was completed with full details
- Hospital agent says goodbye

SCENARIO 2 -- DTMF MENU IVR:
The hospital agent is a touch-tone menu system.
Flow pattern:
- Greets the caller and asks for name and mobile number
- Caller provides them
- Hospital agent repeats details and asks for confirmation
- Caller confirms
- Hospital agent presents the MAIN MENU with numbered options relevant to the
  caller's type of request (e.g. previous appointment, new appointment, cancel,
  reschedule, check status, talk to agent, repeat, end call)
- Caller presses the matching key
- Hospital agent confirms the selection
- Caller confirms
- Hospital agent presents a CATEGORY MENU if applicable (specialties, departments, etc.)
  with numbered options -- include the caller's desired category among them
- Caller presses the matching key
- Hospital agent confirms
- Caller confirms
- Hospital agent presents a SPECIFIC CHOICE MENU if applicable (specific doctors,
  time slots, etc.) with numbered options -- include the caller's desired choice among them
- Caller presses the matching key
- Hospital agent confirms
- Caller confirms
- If a date/time is needed: hospital presents date entry options
  (today, tomorrow, manual entry via keypad), caller selects manual and enters
  the date, hospital confirms the date
- Hospital agent says please wait, processing
- [Hold music / waiting]
- Hospital agent confirms the action was completed with full details
- Hospital agent says goodbye

CRITICAL RULES:
1. Generate ONLY based on the provided intent. Adapt the entire flow to what the
   caller is trying to do (booking, cancelling, rescheduling, checking status, etc.).
2. Every menu must include the option the caller would need, plus other realistic options.
3. The hospital agent name, greeting style, and personality should match the hospital
   mentioned in the intent.
4. Confirmation lines must repeat back the specific details from the intent.
5. The final success message must include ALL relevant details.
6. Do NOT include any placeholder text. Every line must be a complete, speakable sentence.
7. If the intent is missing some details (e.g. no doctor name), adapt: skip that menu
   or have the hospital agent ask for it conversationally.
8. For menus, always present the options as: "If you need X press 1, If you need Y press 2, ..."

Return JSON in this exact structure:
{
  "scenario_1": {
    "title": "short title for this scenario",
    "description": "one line describing the scenario type",
    "turns": [
      {
        "hospital_says": "exact dialogue the hospital agent speaks",
        "then_caller": "brief description of what the caller does next"
      }
    ]
  },
  "scenario_2": {
    "title": "short title for this scenario",
    "description": "one line describing the scenario type",
    "turns": [
      {
        "hospital_says": "exact dialogue the hospital agent speaks",
        "then_caller": "brief description of what the caller does next"
      }
    ]
  }
}

The last turn in each scenario should have "then_caller": "Call ends" since the hospital
agent says goodbye and the call terminates."""


async def generate_hospital_scripts(intent: UserIntent) -> dict:
    """
    Use the LLM to generate both scenario scripts based on the user's intent.

    Args:
        intent: The structured user intent extracted by Agent 1.

    Returns:
        Dict with scenario_1 and scenario_2, each containing title,
        description, and a list of turns with hospital_says and then_caller.
    """
    intent_summary = json.dumps(intent.model_dump(), indent=2, default=str)

    messages = [
        {"role": "system", "content": SCRIPT_GENERATION_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Generate the hospital agent dialogue scripts for both scenarios "
                f"based on this caller intent:\n\n{intent_summary}"
            ),
        },
    ]

    logger.info("[ScriptGen] Generating hospital scripts via LLM...")

    result = await groq_llm.chat_completion(
        messages=messages,
        temperature=0.3,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        logger.warning("[ScriptGen] Failed to parse JSON, trying to extract...")
        start = result.find("{")
        end = result.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(result[start:end])
        else:
            raise ValueError("LLM did not return valid JSON for script generation")

    # Validate structure
    for key in ("scenario_1", "scenario_2"):
        if key not in parsed:
            raise ValueError(f"LLM response missing {key}")
        scenario = parsed[key]
        if "turns" not in scenario or not isinstance(scenario["turns"], list):
            raise ValueError(f"{key} missing turns array")

    logger.info(
        f"[ScriptGen] Generated: "
        f"scenario_1={len(parsed['scenario_1']['turns'])} turns, "
        f"scenario_2={len(parsed['scenario_2']['turns'])} turns"
    )

    return parsed
