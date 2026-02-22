"""
Configuration management — loads .env and exposes typed settings.
"""

from __future__ import annotations
import json
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Groq
    groq_api_key: str = Field(..., alias="GROQ_API_KEY")
    groq_stt_model: str = Field("whisper-large-v3-turbo", alias="GROQ_STT_MODEL")
    groq_llm_model: str = Field("llama-3.3-70b-versatile", alias="GROQ_LLM_MODEL")

    # Deepgram TTS
    deepgram_api_key: str = Field(..., alias="DEEPGRAM_API_KEY")
    deepgram_tts_model: str = Field("aura-asteria-en", alias="DEEPGRAM_TTS_MODEL")

    # Agent TTS voice (for our agent's spoken responses)
    agent_tts_voice: str = Field("aura-orion-en", alias="AGENT_TTS_VOICE")

    # Hospital Agent connection
    hospital_agent_url: str = Field("ws://localhost:8001", alias="HOSPITAL_AGENT_URL")

    # Twilio (disabled -- not used in agent-to-agent mode)
    twilio_account_sid: str = Field("", alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field("", alias="TWILIO_AUTH_TOKEN")
    twilio_phone_number: str = Field("", alias="TWILIO_PHONE_NUMBER")

    # Redis
    redis_url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")

    # App
    app_host: str = Field("0.0.0.0", alias="APP_HOST")
    app_port: int = Field(8000, alias="APP_PORT")
    public_base_url: str = Field("http://localhost:8000", alias="PUBLIC_BASE_URL")

    # User profile defaults
    default_user_name: str = Field("Bala", alias="DEFAULT_USER_NAME")
    default_user_phone: str = Field("9304566336", alias="DEFAULT_USER_PHONE")

    # Hospital registry (JSON string)
    hospital_registry: str = Field("{}", alias="HOSPITAL_REGISTRY")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    def get_hospital_numbers(self) -> dict[str, str]:
        """Parse hospital registry JSON into a dict."""
        return json.loads(self.hospital_registry)


settings = Settings()
