"""
Configuration management — loads .env and exposes typed settings.
"""

from __future__ import annotations
import json
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Groq (LLM only — STT is handled by Sarvam AI)
    groq_api_key: str = Field(..., alias="GROQ_API_KEY")
    groq_llm_model: str = Field("llama-3.3-70b-versatile", alias="GROQ_LLM_MODEL")

    # Sarvam AI (STT + TTS — optimized for Indian languages)
    sarvam_api_key: str = Field(..., alias="SARVAM_API_KEY")
    sarvam_stt_model: str = Field("saarika:v2.5", alias="SARVAM_STT_MODEL")
    sarvam_tts_speaker: str = Field("shubh", alias="SARVAM_TTS_SPEAKER")
    sarvam_tts_language: str = Field("en-IN", alias="SARVAM_TTS_LANGUAGE")

    # Twilio
    twilio_account_sid: str = Field("", alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field("", alias="TWILIO_AUTH_TOKEN")
    twilio_phone_number: str = Field("", alias="TWILIO_PHONE_NUMBER")

    # Exotel (Indian telephony provider)
    exotel_api_key: str = Field("", alias="EXOTEL_API_KEY")
    exotel_api_token: str = Field("", alias="EXOTEL_API_TOKEN")
    exotel_account_sid: str = Field("", alias="EXOTEL_ACCOUNT_SID")
    exotel_phone_number: str = Field("", alias="EXOTEL_PHONE_NUMBER")
    # App Bazaar flow ID with the Voicebot Applet (configured in Exotel dashboard)
    exotel_app_id: str = Field("", alias="EXOTEL_APP_ID")

    # Redis
    redis_url: str = Field("redis://localhost:6379/0", alias="REDIS_URL")

    # App
    app_host: str = Field("0.0.0.0", alias="APP_HOST")
    app_port: int = Field(8000, alias="APP_PORT")
    public_base_url: str = Field("http://localhost:8000", alias="PUBLIC_BASE_URL")

    # User profile defaults
    default_user_name: str = Field("Bala", alias="DEFAULT_USER_NAME")
    default_user_phone: str = Field("9304566336", alias="DEFAULT_USER_PHONE")

    # Phone registry (JSON string mapping entity names to phone numbers)
    # Supports both PHONE_REGISTRY and legacy HOSPITAL_REGISTRY env var names
    phone_registry: str = Field("{}", alias="PHONE_REGISTRY")
    hospital_registry: str = Field("{}", alias="HOSPITAL_REGISTRY")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    def get_phone_numbers(self) -> dict[str, str]:
        """Parse phone registry JSON into a dict (checks PHONE_REGISTRY first)."""
        registry = json.loads(self.phone_registry)
        if not registry:
            registry = json.loads(self.hospital_registry)
        return registry

    def get_hospital_numbers(self) -> dict[str, str]:
        """Alias for get_phone_numbers() for backward compatibility."""
        return self.get_phone_numbers()


settings = Settings()
