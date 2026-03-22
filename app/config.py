"""Centralized configuration for CHILI. Loads from .env with type safety."""
from pydantic import Field, field_validator
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

    # Fallback LLM — defaults to Google Gemini free tier (OpenAI-compatible endpoint).
    # Get a free key at https://aistudio.google.com/apikey
    premium_api_key: str = ""
    premium_model: str = "gemini-2.0-flash"
    premium_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

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

    # 0x DEX aggregator (free tier, for MetaMask swap quotes)
    zerox_api_key: str = ""

    # SMS Notifications (trading alerts)
    sms_phone: str = ""              # 10-digit US phone number, e.g. "8509774415"
    sms_carrier: str = "verizon"     # verizon, att, tmobile, sprint, uscellular, boost, cricket, metro, mint, visible, google_fi
    alerts_enabled: bool = True

    # Twilio (optional SMS upgrade — if empty, email-to-SMS gateway is used)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""    # Twilio phone number with country code, e.g. "+18001234567"

    # Telegram Bot (free, no quota — preferred for trading alerts)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Google OAuth SSO (Sign in with Google)
    google_client_id: str = ""
    google_client_secret: str = ""
    session_secret: str = "chili-session-change-me"  # sign session cookies

    # Broker credentials — DEPRECATED: use the in-app setup dialogs instead.
    # These .env values serve as a fallback when no per-user DB credentials exist.
    robinhood_username: str = ""
    robinhood_password: str = ""
    robinhood_totp_secret: str = ""
    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""

    # Massive.com market data (primary — real-time quotes & aggregates)
    massive_api_key: str = ""
    massive_base_url: str = "https://api.massive.com"
    massive_ws_url: str = "wss://socket.massive.com"
    massive_use_websocket: bool = True
    massive_max_rps: int = 100

    # Polygon.io market data (secondary fallback — replaces yfinance for speed)
    polygon_api_key: str = ""
    polygon_base_url: str = "https://api.polygon.io"
    use_polygon: bool = False  # feature flag: set USE_POLYGON=true in .env to enable
    polygon_max_rps: int = 5  # soft cap; governor will smooth bursts around this

    # Learning schedule
    learning_interval_hours: int = 2  # how often to run learning cycle (hours)
    # If a cycle crashes without clearing _learning_status["running"], the brain worker would skip
    # forever; clear the lock after this many seconds (default 3h).
    learning_cycle_stale_seconds: int = 10800

    # Brain resource / queue tuning (raise parallel for high-core machines; watch API rate limits)
    brain_max_cpu_pct: int | None = None  # cap queue pattern workers to this % of logical CPUs (None = no cap)
    brain_backtest_parallel: int = 14     # ScanPatterns to backtest in parallel (queue step)
    brain_queue_batch_size: int = 80      # patterns pulled from queue per learning cycle
    brain_smart_bt_max_workers: int | None = 28  # max threads per insight ticker pool (None = max(8, cpu*2))
    brain_queue_target_tickers: int = 60  # tickers per pattern in queue backtest (more = heavier per pattern)
    brain_use_gpu_ml: bool = False       # GPU for pattern meta-learner (LightGBM) — ML train step only, not queue BT

    # Pattern backtest queue: how soon a pattern is eligible again (was hardcoded 7).
    brain_retest_interval_days: int = 7
    # When the retest queue is thin, add oldest-tested active patterns up to this many per cycle.
    brain_queue_exploration_enabled: bool = True
    brain_queue_exploration_max: int = 40

    # Evolution: variant ranking = weight_sharpe * adj_sharpe + weight_wr * wr + weight_return * avg_return_pct
    brain_evolution_weight_sharpe: float = 1.0
    brain_evolution_weight_wr: float = 2.0
    brain_evolution_weight_return: float = 0.01
    brain_evolution_min_trades: int = 5
    brain_evolution_min_trades_penalty: float = 0.25  # scales fitness when n_backtests < min_trades

    # Code Brain
    code_brain_repos: str = ""         # comma-separated local repo paths to index
    code_brain_interval_hours: int = 4  # how often to run code learning cycle
    code_brain_max_files: int = 5000    # safety cap per repo

    # Reasoning Brain
    reasoning_interval_hours: int = 6     # how often to run reasoning cycle
    reasoning_max_web_searches: int = 10  # cap per cycle to avoid abuse
    reasoning_enabled: bool = True        # feature flag
    reasoning_insight_chat_enabled: bool = True  # enable proactive Insight Chat
    reasoning_max_goals: int = 5                 # max concurrent learning goals

    # Project Brain (autonomous agents)
    project_brain_enabled: bool = True
    project_brain_auto_cycle_minutes: int = 60
    project_brain_max_web_searches: int = 5

    # Trading freshness / staleness guardrails
    top_picks_warn_age_min: int = 15   # warn when picks batch is older than N minutes
    proposal_warn_age_min: int = 60    # warn when proposal is older than N minutes
    pick_warn_drift_pct: float = 10.0  # warn when price has drifted >N% from entry

    # Database — PostgreSQL only (required). See .env.example and docs/DATABASE_POSTGRES.md.
    # Example (host → Docker Compose postgres): postgresql://chili:chili@localhost:5433/chili
    database_url: str = Field(..., description="PostgreSQL connection URL")
    # Pool: brain worker + parallel queue backtests can hold many connections; default 30 is too small.
    database_pool_size: int = 25
    database_max_overflow: int = 55

    # Optional shared secret so an external Brain UI (different port / origin) can trigger
    # GET/POST /api/v1/brain-next-cycle without chili_device_token. Set in .env as BRAIN_V1_WAKE_SECRET.
    # Send header: X-Chili-Brain-Wake-Secret: <same value>. Use a long random string; never commit it.
    brain_v1_wake_secret: str = ""

    # Brain HTTP service (chili-brain/) — when set, workers or scripts can delegate via brain_client
    brain_service_url: str = ""  # e.g. http://brain:8090 (Compose) or http://127.0.0.1:8090
    brain_internal_secret: str = ""  # Bearer token for POST /v1/run-learning-cycle (match CHILI_BRAIN_INTERNAL_SECRET on brain)

    # Brain learning worker: UI starts the Docker Compose ``brain-worker`` service (not subprocess).
    brain_worker_compose_service: str = "brain-worker"
    # Optional: restrict to one compose project label (empty = match any project with that service name)
    brain_worker_compose_project: str = ""

    @field_validator("database_url")
    @classmethod
    def _postgres_database_url(cls, v: str) -> str:
        url = (v or "").strip()
        if not url:
            raise ValueError(
                "DATABASE_URL is required. Set a PostgreSQL URL in .env "
                "(see .env.example), e.g. postgresql://chili:chili@localhost:5433/chili"
            )
        lowered = url.lower()
        if not (
            lowered.startswith("postgresql://")
            or lowered.startswith("postgresql+psycopg2://")
            or lowered.startswith("postgresql+psycopg://")
        ):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL URL "
                "(postgresql://... or postgresql+psycopg2://...). See .env.example."
            )
        return url

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
