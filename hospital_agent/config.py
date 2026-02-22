"""
Hospital Agent -- Configuration
Reads from the same .env as the main app.
"""

from __future__ import annotations
from pydantic_settings import BaseSettings
from pydantic import Field


class HospitalSettings(BaseSettings):
    groq_api_key: str = Field(..., alias="GROQ_API_KEY")
    groq_llm_model: str = Field("llama-3.3-70b-versatile", alias="GROQ_LLM_MODEL")
    deepgram_api_key: str = Field(..., alias="DEEPGRAM_API_KEY")
    hospital_tts_voice: str = Field("aura-luna-en", alias="HOSPITAL_TTS_VOICE")
    hospital_port: int = Field(8001, alias="HOSPITAL_AGENT_PORT")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = HospitalSettings()
