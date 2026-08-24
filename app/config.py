from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./aigent-dploy.db"
    cms_base_url: str = "http://localhost:8000"
    voice_stub_url: str = "http://localhost:8000/stubs/voice"
    email_stub_url: str = "http://localhost:8000/stubs/email"
    fax_stub_url: str = "http://localhost:8000/stubs/fax"
    portal_stub_url: str = "http://localhost:8000/stubs/portal"
    scheduler_interval_seconds: float = 2.0

    # Skill library (progressive disclosure: index in context, bodies on demand)
    skills_root: str = "skills"

    # Guardrail: per-config guardrail_focus strings stay in the DB, but the
    # review loop only runs when this is on. Toggle with GUARDRAIL_ENABLED=true.
    guardrail_enabled: bool = False

    # Mistral (MISTRAL_API_KEY is read from the environment automatically)
    mistral_api_key: str = ""
    mistral_chat_model: str = "mistral-small-latest"
    mistral_stt_model: str = "voxtral-mini-latest"
    mistral_tts_model: str = "voxtral-mini-tts-latest"
    mistral_tts_voice: str = "en_paul_neutral"          # the agent's voice
    mistral_tts_provider_voice: str = "gb_jane_neutral"  # the other party's voice
    mistral_tts_format: str = "mp3"
    mistral_temperature: float = 0.1
    mistral_max_tool_rounds: int = 8
    mistral_history_events: int = 20   # recent audit events injected as memory


settings = Settings()
