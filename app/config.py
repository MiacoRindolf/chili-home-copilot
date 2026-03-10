"""Centralized configuration for CHILI. Loads from .env with type safety."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Ollama (local planner, wellness, RAG, vision)
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "phi4-mini"
    wellness_model: str = "phi4-mini"
    ollama_vision_model: str = "llama3.2-vision"

    # Primary LLM — defaults to Groq free tier (Llama 3.3 70B, ~800 tok/s).
    # Override with Ollama or other OpenAI-compatible provider.
    llm_api_key: str = ""
    openai_api_key: str = ""  # backward compat; used as primary if llm_api_key empty
    llm_model: str = "llama-3.3-70b-versatile"
    llm_base_url: str = "https://api.groq.com/openai/v1"

    # Premium LLM (e.g. OpenAI)
    premium_api_key: str = ""
    premium_model: str = "gpt-5.2"
    premium_base_url: str = "https://api.openai.com/v1"

    # Vision fallback (often same as premium)
    openai_vision_model: str = "gpt-4o-mini"

    # Email (pairing codes)
    email_user: str = ""
    email_password: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587

    # Optional
    weather_location: str = ""

    # Module marketplace / registry
    # Optional HTTPS URL pointing to a JSON index that describes available
    # third-party modules. When empty, the marketplace operates in
    # "local only" mode and only shows modules already installed under
    # data/modules/.
    module_registry_url: str = ""

    # Optional modules (comma-separated: planner,intercom,voice,projects)
    # Empty means: enable all known modules.
    chili_modules: str = "planner,intercom,voice,projects"

    # Desktop command refinement: LLM corrects ASR and normalizes app names (mobile/desktop API).
    desktop_refinement_enabled: bool = True

    # Robinhood (read-only portfolio sync)
    robinhood_username: str = ""
    robinhood_password: str = ""
    robinhood_totp_secret: str = ""  # optional: base32 TOTP secret; if empty, SMS-based MFA is used

    @property
    def primary_api_key(self) -> str:
        """Primary LLM key: LLM_API_KEY or OPENAI_API_KEY."""
        return self.llm_api_key or self.openai_api_key or ""

    @property
    def premium_api_key_resolved(self) -> str:
        """Premium key: PREMIUM_API_KEY or primary for vision fallback."""
        return self.premium_api_key or self.primary_api_key or ""


# Load once at import
settings = Settings()
