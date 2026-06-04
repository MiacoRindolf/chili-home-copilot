"""Centralized configuration for CHILI. Loads from .env with type safety."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
FAST_BACKTEST_BATCH_DEFAULT_LEAN_CYCLE = 0
FAST_BACKTEST_BATCH_DEFAULT_BACKTEST = 30
REGIME_GATE_DEFAULT_CRYPTO_ANCHOR_DIMENSIONS = "ticker_regime,cross_asset_regime"
REGIME_GATE_DEFAULT_EQUITY_ANCHOR_DIMENSIONS = "ticker_regime"
REGIME_GATE_DEFAULT_MIN_TRADES = 5
REGIME_GATE_DEFAULT_MAX_AGE_DAYS = 7
REGIME_GATE_DEFAULT_MIN_NEGATIVES = 2
BRAIN_QUEUE_MP_CHILD_TICKER_WORKERS_DEFAULT = 2
BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_ENABLED = True
BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_RESERVE_MB = 1536
BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_WORKER_MB = 768
BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_MIN_WORKERS = 1
BRAIN_QUEUE_LINEAGE_FIXED_CAP_DISABLED = 0
BRAIN_QUEUE_LINEAGE_DIVERSIFICATION_SHARE_DEFAULT = 0.10
BRAIN_QUEUE_LINEAGE_MIN_PER_BATCH_DEFAULT = 1
BRAIN_QUEUE_MARKET_HOURS_STOCK_LANE_DEFAULT = 2
BACKTEST_PRIORITY_SCORE_MAX = 100
BACKTEST_PRIORITY_DEFAULT_BYPASS_RETEST_FLOOR = BACKTEST_PRIORITY_SCORE_MAX
RECERT_QUEUE_DEFAULT_DISPATCH_INTERVAL_MINUTES = 60
RECERT_QUEUE_DEFAULT_DISPATCH_LIMIT = 5
RECERT_QUEUE_DEFAULT_BACKTEST_PRIORITY = 250
RECERT_QUEUE_DEFAULT_PRIORITY_PATTERN_IDS = "585"
RECERT_QUEUE_IMMEDIATE_DISPATCH_DEFAULT_ENABLED = True
RECERT_QUEUE_IMMEDIATE_DISPATCH_DEFAULT_ORIGINS = "autotrader_signal_fastlane"
RECERT_QUEUE_IMMEDIATE_DISPATCH_DEFAULT_PRIORITY = (
    RECERT_QUEUE_DEFAULT_BACKTEST_PRIORITY
)
DATABASE_DEFAULT_POOL_SIZE = 25
DATABASE_DEFAULT_MAX_OVERFLOW = 55
DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS = 30.0
DATABASE_DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS = 120_000
DATABASE_PYTEST_DEFAULT_POOL_SIZE = 1
DATABASE_PYTEST_DEFAULT_MAX_OVERFLOW = 1
DATABASE_PYTEST_DEFAULT_POOL_TIMEOUT_SECONDS = 5.0
BRAIN_EXIT_ENGINE_PARITY_DEFAULT_SAMPLE_PCT = 0.05
BRAIN_EXIT_ENGINE_BACKTEST_PARITY_DEFAULT_SAMPLE_PCT = (
    BRAIN_EXIT_ENGINE_PARITY_DEFAULT_SAMPLE_PCT
)
BRAIN_EXIT_ENGINE_BACKTEST_CLOSE_AGREEMENT_SAMPLE_PCT_DEFAULT = 0.25
BRAIN_EXIT_ENGINE_BACKTEST_INTERESTING_DRIFT_BPS_DEFAULT = 10.0
BRAIN_EXIT_ENGINE_BACKTEST_OPS_LOG_DEFAULT_ENABLED = False
AUTOTRADER_DEFAULT_CANDIDATE_BATCH_SIZE = 5
AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE = 50
AUTOTRADER_DEFAULT_TICK_INTERVAL_SECONDS = 10
AUTOTRADER_SCHEDULER_TICK_INTERVAL_DEFAULT_SECONDS = 60
AUTOTRADER_DEFAULT_TICK_MAX_SECONDS = 45
AUTOTRADER_MIN_TICK_MAX_SECONDS = 5
AUTOTRADER_MAX_TICK_MAX_SECONDS = 300
AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES = 15
AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_ENABLED = True
AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_MAX_AGE_SECONDS = (
    AUTOTRADER_DEFAULT_TICK_INTERVAL_SECONDS * 3
)
AUTOTRADER_FRESH_CANDIDATE_BURST_DEFAULT_ENABLED = True
AUTOTRADER_STALE_CANDIDATE_SWEEP_DEFAULT_SECONDS = (
    AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_MAX_AGE_SECONDS
)
AUTOTRADER_STALE_CANDIDATE_SWEEP_MAX_SECONDS = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES * SECONDS_PER_MINUTE
)
AUTOTRADER_CANDIDATE_PRICE_PREFETCH_DEFAULT_ENABLED = True
AUTOTRADER_STOCK_MOMENTUM_CONTEXT_GATE_DEFAULT_ENABLED = True
AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_QUEUE_PRESSURE_DEFAULT = 1.0
AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_GAP_PCT_DEFAULT = 5.0
AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_VOLUME_RATIO_DEFAULT = 2.0
AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_DEFAULT_ENABLED = True
AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_DEFAULT_MINUTES = 3
AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_MAX_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES
)
AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_DEFAULT_MIN_EDGE_GAP_BPS = 50
AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_DEFAULT_QUEUE_PRESSURE = 0.8
CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_MINUTES = 5
CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_MINUTES = 60
CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_SECONDS = (
    CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_MINUTES * SECONDS_PER_MINUTE
)
CRYPTO_EXIT_MISSING_QTY_BACKOFF_MIN_SECONDS = 0
CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_SECONDS = (
    CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_MINUTES * SECONDS_PER_MINUTE
)
CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_START_STREAK = 3
CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_START_STREAK = 20
AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_ENABLED = True
AUTOTRADER_LEGACY_MAX_SYMBOL_PRICE_DEFAULT_USD = 50.0
AUTOTRADER_FRACTIONAL_EQUITY_DEFAULT_ENABLED = True
AUTOTRADER_MAX_ENTRY_SLIPPAGE_DEFAULT_PCT = 1.0
AUTOTRADER_MAX_ENTRY_SLIPPAGE_CONFIG_LIMIT_PCT = 50.0
AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_ENABLED = True
AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_ASSET_TYPES = "stock"
AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_SLIPPAGE_MULTIPLE = 2.5
AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MIN_SLIPPAGE_MULTIPLE = 1.0
AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MAX_SLIPPAGE_MULTIPLE = 5.0
AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_MAX_PCT = 5.0
AUTOTRADER_FAVORABLE_ENTRY_DRIFT_CONFIG_LIMIT_PCT = 20.0
AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_ENABLED = True
AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_MINUTES = 20
AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_THRESHOLD = 3
AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_ASSET_TYPES = "stock,crypto"
PATTERN_DIRECTIONAL_DEFAULT_THRESHOLD_PCT = 1.5
PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS = 24
AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES * 2
)
AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_MAX_MINUTES = (
    PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS * MINUTES_PER_HOUR
)
AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES * 2
)
AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_MAX_MINUTES = (
    PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS * MINUTES_PER_HOUR
)
AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_MAX_AGE_HOURS = (
    PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS
)
PATTERN_DIRECTIONAL_DEFAULT_MAX_LOOKBACK_HOURS = (
    PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS * 7
)
PATTERN_DIRECTIONAL_DEFAULT_MAX_ALERTS_PER_RUN = 200
PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ENABLED = True
PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_LOOKBACK_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES * 6
)
PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ASSET_TYPES = "stock"
PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_MANAGED_REASONS = (
    "insufficient_directional_samples"
)
AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TICKS = 6
AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS = (
    AUTOTRADER_DEFAULT_TICK_INTERVAL_SECONDS
    * AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TICKS
)
AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_MIN_TTL_SECONDS = 0
AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_MAX_TTL_SECONDS = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES * 60
)
AUTOTRADER_OPTIONS_SUBSTITUTE_DEFAULT_REQUIRES_UNDERLYING_POSITIVE_EDGE = True
AUTOTRADER_SYNERGY_RETRY_DEFAULT_LOOKBACK_CYCLES = 4
AUTOTRADER_SYNERGY_RETRY_DEFAULT_LOOKBACK_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES
    * AUTOTRADER_SYNERGY_RETRY_DEFAULT_LOOKBACK_CYCLES
)
AUTOTRADER_SYNERGY_RETRY_MIN_LOOKBACK_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES
)
AUTOTRADER_SYNERGY_RETRY_MAX_LOOKBACK_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES * 24
)
AUTOTRADER_SYNERGY_RETRY_DEFAULT_MAX_PER_TICK = 2
AUTOTRADER_SYNERGY_DEFAULT_FRACTION = 0.25
AUTOTRADER_SYNERGY_DEFAULT_MAX_NOTIONAL_USD = 50.0
AUTOTRADER_SYNERGY_DEFAULT_MAX_TOTAL_ADD_FRACTION = 0.75
AUTOTRADER_SYNERGY_MIN_ACTIVE_SCALE_INS_PER_TRADE = 1
AUTOTRADER_SYNERGY_DEFAULT_MAX_SCALE_INS_PER_TRADE = max(
    AUTOTRADER_SYNERGY_MIN_ACTIVE_SCALE_INS_PER_TRADE,
    round(
        AUTOTRADER_SYNERGY_DEFAULT_MAX_TOTAL_ADD_FRACTION
        / AUTOTRADER_SYNERGY_DEFAULT_FRACTION
    ),
)
AUTOTRADER_SYNERGY_MAX_SCALE_INS_CONFIG_LIMIT = 10
AUTOTRADER_PROBATION_DEFAULT_NOTIONAL_MULTIPLIER = 0.25
AUTOTRADER_PROBATION_DEFAULT_MAX_TRADES_PER_PATTERN_PER_DAY = 1
AUTOTRADER_PROBATION_DEFAULT_MAX_TRADES_PER_DAY = 3
AUTOTRADER_PROBATION_DEFAULT_MAX_TRADES_PER_PATTERN_TICKER_PER_DAY = 1
AUTOTRADER_PROBATION_DEFAULT_CRYPTO_MAX_TRADES_PER_DAY = 6
AUTOTRADER_PROBATION_DEFAULT_CRYPTO_MIN_EXPECTED_NET_PCT_FOR_EXTRA_QUOTA = 1.0
AUTOTRADER_PROBATION_DEFAULT_MIN_CPCV_SHARPE = 1.0
AUTOTRADER_PROBATION_DEFAULT_MIN_REALIZED_TRADES = 5
AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN = 100
AUTOTRADER_PAPER_SHADOW_MAX_OPEN_CONFIG_LIMIT = 1000
AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_MAX_AGE_HOURS = 72
AUTOTRADER_PAPER_SHADOW_MAX_JANITOR_MAX_AGE_HOURS = 24 * 30
AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_BUFFER = 5
AUTOTRADER_PAPER_SHADOW_MAX_JANITOR_BUFFER = 100
AUTOTRADER_PAPER_SHADOW_DEFAULT_CAPACITY_EVICT_YOUNGEST_FIRST = True
AUTOTRADER_PAPER_DYNAMIC_DEFAULT_MONITOR_COOLDOWN_MINUTES = 5
AUTOTRADER_PAPER_DYNAMIC_MAX_MONITOR_COOLDOWN_MINUTES = 240
AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_ALLOW_DUPLICATE_OPEN = True
AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_LIGHTWEIGHT_SIZING_ENABLED = True
AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_SAME_ALERT_REASON_FAMILY = True
AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_RECENT_REASON_FAMILY_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES
)
AUTOTRADER_PAPER_SHADOW_QUEUE_SUPPRESSION_DEFAULT_PRESSURE = 0.6
AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_ENABLED = True
AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES * 2
)
AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_THRESHOLD = 1
AUTOTRADER_LLM_REVALIDATION_DEFAULT_SKIP_SHADOW_OBSERVATION = True
AUTOTRADER_LLM_REVALIDATION_DEFAULT_SKIP_OPTIONS_PATH = True
AUTOTRADER_SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_DEFAULT_ENABLED = False
AUTOTRADER_SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_DEFAULT_USD = 0.0
AUTOTRADER_MANAGED_EDGE_DEFAULT_MODE = "authoritative"
AUTOTRADER_MANAGED_EDGE_DEFAULT_ASSET_TYPES = "crypto,stock"
AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_DIRECTIONAL_SAMPLES = 8
AUTOTRADER_MANAGED_EDGE_DEFAULT_CAPTURE_FRACTION = 0.60
AUTOTRADER_MANAGED_EDGE_DEFAULT_ADVERSE_BUFFER = 1.50
AUTOTRADER_MANAGED_EDGE_DEFAULT_STATIC_TO_MANAGED_REWARD_RATIO = 1.50
AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_REWARD_FRACTION = 0.005
AUTOTRADER_MANAGED_EDGE_DEFAULT_MAX_REWARD_FRACTION = 0.08
AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_REWARD_RISK = 1.25
AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_EXPECTED_NET_PCT = 0.0
AUTOTRADER_EDGE_DEFAULT_MIN_EXPECTED_NET_AFTER_EMPIRICAL_COST_PCT = 0.25
AUTOTRADER_DIRECTIONAL_PROBABILITY_DEFAULT_Z = 1.0
AUTOTRADER_DIRECTIONAL_PROBABILITY_MAX_Z = 3.0
AUTOTRADER_DIRECTIONAL_PROBABILITY_DEFAULT_MAX_ROWS = 30
AUTOTRADER_DIRECTIONAL_PROBABILITY_MIN_ROWS = 1
AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_ENABLED = True
PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_LIFECYCLE_STAGES = (
    "shadow_promoted,pilot_promoted"
)
PATTERN_IMMINENT_HARD_RECERT_SHADOW_SIGNAL_LANE = "hard_recert_shadow"
PATTERN_IMMINENT_EQUITY_SESSION_SHADOW_SIGNAL_LANE = "equity_session_shadow"
PATTERN_IMMINENT_OFFSESSION_STOCK_SHADOW_DEFAULT_ENABLED = True
PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_LIFECYCLE_STAGES = "promoted,live"
PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_REASONS = (
    "negative_oos_recert,negative_realized_ev,weak_oos_win_rate_recert,"
    "promotion_gate_not_currently_passed,promotion_gate_not_passed,"
    "promotion_gate_failed,cpcv_promotion_gate_failed"
)
AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY = (
    BACKTEST_PRIORITY_DEFAULT_BYPASS_RETEST_FLOOR
)
AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_MIN_EXPECTED_NET_PCT = (
    AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_EXPECTED_NET_PCT
)
AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_LIFECYCLE_STAGES = (
    PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_LIFECYCLE_STAGES
)
AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_REBOOST_COOLDOWN_MINUTES = (
    AUTOTRADER_IMMINENT_SCANNER_CADENCE_MINUTES
)
PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_LOOKBACK_HOURS = 2.0
PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MIN_REJECTS = 6
PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MAX_AVG_RETURN_PCT = 0.0
PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MAX_AVG_EXPECTED_NET_PCT = -0.75
PATTERN_IMMINENT_COINBASE_SPOT_FILTER_DEFAULT_TTL_SECONDS = 3600
PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_COOLDOWN_MINUTES = 30.0
PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_MIN_FAILURES = 1
PATTERN_IMMINENT_SCORE_DEFAULT_TIME_BUDGET_SECONDS = 50.0
PATTERN_IMMINENT_DEFAULT_MAX_TICKERS_PER_PATTERN = 12
PATTERN_IMMINENT_DEFAULT_SUPPRESSED_DIAGNOSTIC_LIMIT = 40
PATTERN_IMMINENT_DEFAULT_MISSING_INDICATOR_SAMPLE_LIMIT = 8
PATTERN_IMMINENT_DEFAULT_READINESS_NEAR_MISS_LIMIT = 12
PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_MAX_GAP = 0.15
PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_ENABLED = True
PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_MAX_PER_RUN = 2
PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_MIN_READINESS_FRACTION = 0.50
PATTERN_IMMINENT_DEFAULT_TICKER_ROTATION_WINDOW_MINUTES = 5
PATTERN_IMMINENT_MIN_TICKER_ROTATION_WINDOW_MINUTES = 1
PATTERN_IMMINENT_DEFAULT_TICKER_ROTATION_EXPLORE_TICKERS = 3
PATTERN_IMMINENT_OPEN_POSITION_DEFLECTION_DEFAULT_ENABLED = True
BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_DEFAULT_ENABLED = True
BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_DEFAULT_LOOKBACK_MINUTES = 60
BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_MIN_LOOKBACK_MINUTES = 1
BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_MAX_LOOKBACK_MINUTES = 24 * 60
BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_DEFAULT_MIN_FAILURES = 2
BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_MIN_FAILURES = 1
BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_MAX_FAILURES = 20

# ── Config profiles ──────────────────────────────────────────────────────
CONFIG_PROFILES: dict[str, dict[str, Any]] = {
    "default": {},
    "conservative": {
        "brain_backtest_parallel": 6,
        "brain_research_integrity_strict": True,
        "momentum_max_notional_usd": 200.0,
        "momentum_max_spread_bps_live": 8.0,
    },
    "aggressive": {
        "brain_backtest_parallel": 24,
        "momentum_max_notional_usd": 1000.0,
        "momentum_max_spread_bps_live": 20.0,
    },
    "research": {
        "brain_research_integrity_strict": True,
        "brain_research_integrity_enabled": True,
        "chili_robinhood_spot_adapter_enabled": False,
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Config profile — applies preset defaults; env vars always win.
    brain_config_profile: str = "default"

    @model_validator(mode="before")
    @classmethod
    def _apply_profile(cls, values: dict[str, Any]) -> dict[str, Any]:
        profile_name = values.get(
            "brain_config_profile",
            values.get("BRAIN_CONFIG_PROFILE", "default"),
        )
        profile = CONFIG_PROFILES.get(profile_name, {})
        for key, default_val in profile.items():
            if key not in values and key.upper() not in values:
                values[key] = default_val
        return values

    # Ollama (local planner, wellness, RAG, vision)
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "phi4-mini"
    wellness_model: str = "phi4-mini"
    ollama_vision_model: str = "llama3.2-vision"

    # Groq / OpenAI-compat primary stack (tiers 2–3 in app.openai_client after OpenAI official).
    llm_api_key: str = ""
    # OpenAI official API (api.openai.com) — tried first when set. Also fills primary_api_key if llm_api_key empty.
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_API_KEY", "PAID_OPENAI_API_KEY"),
    )
    openai_model: str = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("OPENAI_MODEL", "PAID_OPENAI_MODEL"),
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices("OPENAI_BASE_URL", "PAID_OPENAI_BASE_URL"),
    )
    llm_model: str = "llama-3.3-70b-versatile"
    llm_base_url: str = "https://api.groq.com/openai/v1"

    # Fallback LLM â€” Google Gemini free tier (OpenAI-compatible endpoint), tier 4.
    # Get a free key at https://aistudio.google.com/apikey
    premium_api_key: str = ""
    premium_model: str = "gemini-2.0-flash"
    premium_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    # Cascade order toggle (Phase B, b1). When True AND both OPENAI_API_KEY and
    # LLM_API_KEY are set, reorder to Groq primary → Groq secondary →
    # OpenAI official → Gemini, saving paid OpenAI calls whenever the
    # free tier can answer adequately. Weak-response escalation still fires
    # up to OpenAI so quality is preserved (Phase B, b2).
    llm_free_tier_first: bool = True

    # In-process LLM reply cache (Phase B, b3). Shared by llm_caller.call_llm
    # call-sites that opt in via cacheable=True. 0 disables the cache.
    llm_cache_max_entries: int = 256
    llm_cache_ttl_seconds: int = 600

    # Per-provider daily token budgets (Phase C, c2). 0 means unlimited.
    # Groq bucket keeps its historical 85K preemptive threshold.
    openai_daily_token_limit: int = 0
    premium_daily_token_limit: int = 0

    # Paid LLM cost controls. ``shadow`` records spend only; ``enforce``
    # preemptively skips paid OpenAI calls after the daily budget is reached.
    chili_llm_premium_daily_budget_usd: float = 0.0
    chili_llm_cost_mode: str = "shadow"
    chili_llm_default_cheap_model: str = "gpt-5.4-mini"
    chili_llm_escalation_model: str = "gpt-5.5"
    chili_llm_purpose_model_overrides_json: str = "{}"

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

    # Kill switch for the /brain?domain=project developer cockpit (register repo,
    # planner handoff, code agents, suggest/apply/validate). When False, the
    # project-domain bootstrap and /api/brain/project/* + /api/brain/code/*
    # endpoints return HTTP 503 so the front end fails closed, and the brain
    # shell hides the project domain tab and skips rendering the pane.
    project_domain_enabled: bool = True

    # Desktop command refinement: LLM corrects ASR and normalizes app names (mobile/desktop API).
    desktop_refinement_enabled: bool = True

    # 0x DEX aggregator (free tier, for MetaMask swap quotes)
    zerox_api_key: str = ""

    # SMS Notifications (trading alerts)
    sms_phone: str = ""              # 10-digit US phone number, e.g. "8509774415"
    sms_carrier: str = "verizon"     # verizon, att, tmobile, sprint, uscellular, boost, cricket, metro, mint, visible, google_fi
    alerts_enabled: bool = True

    # Twilio (optional SMS upgrade â€” if empty, email-to-SMS gateway is used)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""    # Twilio phone number with country code, e.g. "+18001234567"

    # Telegram Bot (free, no quota â€” preferred for trading alerts)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Discord webhook (optional, parallel dispatch)
    discord_webhook_url: str = ""

    # Web Push / VAPID keys (optional, for PWA push notifications)
    vapid_private_key: str = ""
    vapid_contact_email: str = ""

    # Google OAuth SSO (Sign in with Google)
    google_client_id: str = ""
    google_client_secret: str = ""
    session_secret: str = "chili-session-change-me"  # sign session cookies

    # Broker credentials â€” DEPRECATED: use the in-app setup dialogs instead.
    # These .env values serve as a fallback when no per-user DB credentials exist.
    robinhood_username: str = ""
    robinhood_password: str = ""
    robinhood_totp_secret: str = ""
    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""

    # Massive.com market data (primary â€” real-time quotes & aggregates)
    massive_api_key: str = ""
    massive_base_url: str = "https://api.massive.com"
    massive_ws_url: str = "wss://socket.massive.com"
    massive_use_websocket: bool = True
    massive_max_rps: int = 100
    # Shared ``requests`` Session to api.massive.com: urllib3 pool must exceed peak concurrent
    # threads (batch OHLCV + snapshot batches + backtests) or logs "Connection pool is full".
    massive_http_pool_connections: int = 128
    massive_http_pool_maxsize: int = 512
    # Circuit breaker: after N consecutive Massive connection-class failures (e.g.
    # TCP refused from edge denylist), open breaker and skip calls for cooldown
    # period. Without this, a residential-IP block triggers ~180 retries/hr that
    # re-trigger Massive's abuse system and undo any support-side unblock.
    # See project memory project_massive_blocked.md for the 2026-04 incident.
    massive_breaker_failure_threshold: int = 5
    massive_breaker_cooldown_sec: int = 900

    # Polygon.io market data (secondary fallback â€” replaces yfinance for speed)
    polygon_api_key: str = ""
    polygon_base_url: str = "https://api.polygon.io"
    use_polygon: bool = False  # feature flag: set USE_POLYGON=true in .env to enable
    polygon_max_rps: int = 5  # soft cap; governor will smooth bursts around this

    # After Massive, allow Polygon + yfinance for OHLCV/quotes (scanner batch, prescreener, etc.).
    # Set MARKET_DATA_ALLOW_PROVIDER_FALLBACK=false to use Massive only and avoid Yahoo noise in logs.
    market_data_allow_provider_fallback: bool = True
    market_data_polygon_batch_workers: int = 48

    # Learning schedule (1h = faster research cycles if worker + OHLCV provider keep up)
    learning_interval_hours: int = 1
    # If a cycle crashes without clearing _learning_status["running"], the brain worker would skip
    # forever; clear the lock after this many seconds (default 3h).
    learning_cycle_stale_seconds: int = 10800

    # Phase 3: single-flight via `brain_cycle_lease` using dedicated DB sessions (admission only; legacy status authoritative for UI).
    brain_cycle_lease_enforcement_enabled: bool = False

    # Prediction-mirror rollout flags (phases 4-6). See ADR-004 and
    # `app/trading_brain/README.md` for the phase contract. All default
    # False; enable progressively per `docs/TRADING_BRAIN_PREDICTION_MIRROR_ROLLOUT.md`.
    # These flags were read via ``getattr(settings, ..., default)`` throughout the
    # trading-brain code for most of the rollout; tests that monkeypatch them
    # hit the pydantic "no such field" rail. Declared here so strict-settings
    # tests can toggle them directly. Phase 7's release-blocker grep on
    # ``[chili_prediction_ops]`` depends on the ops-log-enabled flag; its value
    # is still frozen by contract (see ADR-004).
    brain_prediction_dual_write_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BRAIN_PREDICTION_DUAL_WRITE_ENABLED"),
    )
    brain_prediction_read_compare_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BRAIN_PREDICTION_READ_COMPARE_ENABLED"),
    )
    brain_prediction_read_authoritative_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED"),
    )
    brain_prediction_read_max_age_seconds: int = Field(
        default=900,
        ge=1,
        validation_alias=AliasChoices("BRAIN_PREDICTION_READ_MAX_AGE_SECONDS"),
    )
    brain_prediction_ops_log_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_PREDICTION_OPS_LOG_ENABLED"),
    )
    brain_prediction_mirror_write_dedicated: bool = Field(
        default=False,
        validation_alias=AliasChoices("BRAIN_PREDICTION_MIRROR_WRITE_DEDICATED"),
    )
    # Brain resource / queue tuning (raise parallel for high-core machines; watch API rate limits)
    brain_max_cpu_pct: int | None = None  # cap queue pattern workers to this % of logical CPUs (None = no cap)
    brain_backtest_parallel: int = 18     # ScanPatterns to backtest in parallel (queue step); tune vs DB pool + provider caps
    # Queue step executor: threads (default, GIL-limited) or process (true multi-core; see docs/BRAIN_BACKTEST_QUEUE_MULTIPROCESS_PLAN.md)
    brain_queue_backtest_executor: str = "threads"  # threads | process
    brain_queue_process_cap: int | None = None  # max process pool workers (None = use brain_backtest_parallel)
    brain_mp_child_database_pool_size: int = 1   # SQLAlchemy pool per child process (avoid P * parent pool connections)
    brain_mp_child_database_max_overflow: int = 2
    brain_smart_bt_max_workers_in_process: int = BRAIN_QUEUE_MP_CHILD_TICKER_WORKERS_DEFAULT
    brain_queue_process_memory_guard_enabled: bool = Field(
        default=BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_ENABLED,
        validation_alias=AliasChoices("BRAIN_QUEUE_PROCESS_MEMORY_GUARD_ENABLED"),
    )
    brain_queue_process_memory_guard_reserve_mb: int = Field(
        default=BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_RESERVE_MB,
        ge=0,
        validation_alias=AliasChoices("BRAIN_QUEUE_PROCESS_MEMORY_GUARD_RESERVE_MB"),
    )
    brain_queue_process_memory_guard_worker_mb: int = Field(
        default=BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_WORKER_MB,
        ge=1,
        validation_alias=AliasChoices("BRAIN_QUEUE_PROCESS_MEMORY_GUARD_WORKER_MB"),
    )
    brain_queue_process_memory_guard_min_workers: int = Field(
        default=BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_MIN_WORKERS,
        ge=1,
        validation_alias=AliasChoices("BRAIN_QUEUE_PROCESS_MEMORY_GUARD_MIN_WORKERS"),
    )
    brain_queue_batch_size: int = 80      # patterns pulled from queue per learning cycle
    # Durable work ledger (event-first brain; not mesh activations)
    brain_work_ledger_enabled: bool = True
    brain_work_dispatch_batch_size: int = 8  # backtest_requested items per worker tick
    brain_work_lease_seconds: int = 900
    brain_work_max_attempts_default: int = 5
    brain_work_retry_base_seconds: int = 30
    brain_work_retry_multiplier: int = 2
    # When True, run_learning_cycle skips in-cycle queue drain; brain-worker work-ledger batch owns it.
    # Requires brain_work_ledger table to exist; set False to drain queue in-cycle.
    brain_work_delegate_queue_from_cycle: bool = False
    # Per-handler dispatch budgets (ledger round processes execution_feedback_digest before backtests).
    brain_work_exec_feedback_batch_size: int = 3
    brain_work_exec_feedback_debounce_seconds: int = 45
    brain_work_cash_deployment_producer_enabled: bool = True
    brain_work_cash_deployment_producer_interval_minutes: int = 30
    brain_work_cash_deployment_producer_window_days: int = 30
    brain_work_cash_deployment_producer_limit: int = 25
    brain_work_cash_deployment_noop_cooldown_minutes: int = 360
    brain_work_recent_done_dedupe_minutes: int = 120
    # Emit ``market_snapshots_batch`` outcome when scheduler snapshot job finishes.
    brain_work_snapshots_outcome_enabled: bool = True
    # Phase 1b of f-adaptive-promotion-architecture (2026-05-11).
    # When True: enqueue_outcome_event writes status='pending' (claimable)
    # instead of status='done' (terminal-at-insert); claim_work_batch and
    # release_stale_leases drop the event_kind='work' filter so outcomes
    # transit the same lifecycle as work events.
    # Historical status='done' rows stay ineligible. Default False — merge
    # produces zero behavior change. Flip via trading_settings.
    # Brief: docs/STRATEGY/QUEUED/f-brain-event-kind-unify.md
    # Memo:  docs/AUDITS/2026-05-11_dispatcher_silence.md
    chili_brain_outcome_claimable_enabled: bool = False
    brain_smart_bt_max_workers: int | None = 28  # max threads per insight ticker pool (None = max(8, cpu*2))

    # Brain I/O thread pools: cgroup / CHILI_CONTAINER_CPU_LIMIT aware (see brain_io_concurrency).
    brain_io_effective_cpus_override: float | None = None
    brain_io_workers_high: int | None = None
    brain_io_workers_med: int | None = None
    brain_io_workers_low: int | None = None
    brain_snapshot_io_workers: int | None = None
    brain_prediction_io_workers: int | None = None
    brain_market_snapshot_defer_while_learning_running: bool = True

    # NetEdgeRanker (Phase E) — calibrated expected-net-PnL scoring, shadow by default.
    # Rollout ladder mirrors the prediction-mirror: off -> shadow -> compare -> authoritative.
    # In any mode != "authoritative" the ranker MUST NOT gate entries, exits, sizing, or promotion.
    # See docs/TRADING_BRAIN_NET_EDGE_RANKER_ROLLOUT.md.
    brain_net_edge_ranker_mode: str = "shadow"
    brain_net_edge_ops_log_enabled: bool = True
    brain_net_edge_min_samples: int = 50
    brain_net_edge_cache_ttl_s: int = 300
    brain_net_edge_shadow_sample_pct: float = 1.0
    brain_net_edge_execution_drag_cost_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_NET_EDGE_EXECUTION_DRAG_COST_ENABLED"),
        description=(
            "When true, NetEdge adds a bounded missed-fill opportunity-cost "
            "penalty from recent positive-edge execution drag for the same pattern."
        ),
    )
    brain_net_edge_execution_drag_lookback_days: int = Field(
        default=7,
        ge=1,
        le=90,
        validation_alias=AliasChoices("BRAIN_NET_EDGE_EXECUTION_DRAG_LOOKBACK_DAYS"),
    )
    brain_net_edge_execution_drag_min_attempts: int = Field(
        default=3,
        ge=1,
        le=500,
        validation_alias=AliasChoices("BRAIN_NET_EDGE_EXECUTION_DRAG_MIN_ATTEMPTS"),
    )
    brain_net_edge_execution_drag_min_positive_events: int = Field(
        default=2,
        ge=1,
        le=100,
        validation_alias=AliasChoices("BRAIN_NET_EDGE_EXECUTION_DRAG_MIN_POSITIVE_EVENTS"),
    )
    brain_net_edge_execution_drag_cost_cap_fraction: float = Field(
        default=0.02,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("BRAIN_NET_EDGE_EXECUTION_DRAG_COST_CAP_FRACTION"),
        description=(
            "Maximum fraction-of-notional cost NetEdge may add for measured "
            "positive-edge execution drag."
        ),
    )

    # ExitEngine unification (Phase B) — canonical ExitEvaluator shadow rollout.
    # Rollout ladder mirrors the prediction-mirror + NetEdgeRanker contract:
    # off -> shadow -> compare -> authoritative. In any mode != "authoritative"
    # the canonical evaluator MUST NOT decide exits; it only logs parity against
    # the legacy backtest/live paths. See docs/TRADING_BRAIN_EXIT_ENGINE_ROLLOUT.md.
    brain_exit_engine_mode: str = "shadow"
    brain_exit_engine_ops_log_enabled: bool = True
    # Applies only to boring hold/hold agreement rows; disagreements and
    # actual exits are always persisted for cutover statistics.
    brain_exit_engine_parity_sample_pct: float = Field(
        default=BRAIN_EXIT_ENGINE_PARITY_DEFAULT_SAMPLE_PCT,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("BRAIN_EXIT_ENGINE_PARITY_SAMPLE_PCT"),
    )
    # Backtest refreshes can evaluate thousands of synthetic bars per worker
    # tick. Keep live parity sampling independent from backtest telemetry so
    # an operator can run live parity at full sample without slowing recert.
    brain_exit_engine_backtest_parity_sample_pct: float = Field(
        default=BRAIN_EXIT_ENGINE_BACKTEST_PARITY_DEFAULT_SAMPLE_PCT,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "BRAIN_EXIT_ENGINE_BACKTEST_PARITY_SAMPLE_PCT"
        ),
    )
    brain_exit_engine_backtest_close_agreement_sample_pct: float = Field(
        default=BRAIN_EXIT_ENGINE_BACKTEST_CLOSE_AGREEMENT_SAMPLE_PCT_DEFAULT,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "BRAIN_EXIT_ENGINE_BACKTEST_CLOSE_AGREEMENT_SAMPLE_PCT"
        ),
    )
    brain_exit_engine_backtest_interesting_drift_bps: float = Field(
        default=BRAIN_EXIT_ENGINE_BACKTEST_INTERESTING_DRIFT_BPS_DEFAULT,
        ge=0.0,
        validation_alias=AliasChoices(
            "BRAIN_EXIT_ENGINE_BACKTEST_INTERESTING_DRIFT_BPS"
        ),
    )
    brain_exit_engine_backtest_ops_log_enabled: bool = Field(
        default=BRAIN_EXIT_ENGINE_BACKTEST_OPS_LOG_DEFAULT_ENABLED,
        validation_alias=AliasChoices("BRAIN_EXIT_ENGINE_BACKTEST_OPS_LOG_ENABLED"),
    )

    # Economic-truth ledger (Phase A) — canonical append-only ledger of
    # entry/exit fills + fees + cash-delta + realized-PnL-delta. Shadow-only
    # until a later cutover phase. Rollout ladder mirrors Phase B/E:
    # off -> shadow -> compare -> authoritative. Legacy Trade.pnl and
    # PaperTrade.pnl remain authoritative until the cutover phase.
    # See docs/TRADING_BRAIN_ECONOMIC_LEDGER_ROLLOUT.md.
    brain_economic_ledger_mode: str = "shadow"
    brain_economic_ledger_ops_log_enabled: bool = True
    brain_economic_ledger_parity_tolerance_usd: float = 0.01
    brain_economic_ledger_require_parity_for_evolution: bool = True

    # PIT hygiene audit (Phase C) — classifies `ScanPattern.rules_json`
    # condition indicators against an explicit allow/deny list and writes
    # results to `trading_pit_audit_log`. Shadow-only until cutover. See
    # docs/TRADING_BRAIN_PIT_HYGIENE_ROLLOUT.md.
    brain_pit_audit_mode: str = "shadow"
    brain_pit_audit_ops_log_enabled: bool = True

    # Triple-barrier labels (Phase D) — replaces fixed-horizon binary labels
    # with (TP, SL, timeout) outcomes for training and economic promotion.
    # Shadow-only until cutover. See docs/TRADING_BRAIN_TRIPLE_BARRIER_ROLLOUT.md.
    brain_triple_barrier_mode: str = "shadow"
    brain_triple_barrier_tp_pct: float = 0.015
    brain_triple_barrier_sl_pct: float = 0.010
    brain_triple_barrier_max_bars: int = 5
    brain_triple_barrier_ops_log_enabled: bool = True
    # Promotion metric (Phase D) — controls how ModelRegistry picks winners.
    # accuracy  = legacy behavior (single-metric check_shadow_vs_active).
    # shadow    = compute economic metric alongside accuracy; log delta only.
    # economic  = expected-PnL + Brier composite is authoritative (future cutover).
    brain_promotion_metric_mode: str = "accuracy"

    # Execution-cost model (Phase F) — per-ticker rolling spread/slippage
    # + capacity cap. Read-only in shadow; flipping to authoritative lets
    # NetEdgeRanker / sizing consume the per-ticker estimates. See
    # docs/TRADING_BRAIN_EXECUTION_REALISM_ROLLOUT.md.
    # 2026-05-15 (post Phase B of evidence-fidelity): flipped from
    # "shadow" -> "authoritative" by operator. Writers populate the
    # rolling estimate table and downstream consumers may now read it.
    brain_execution_cost_mode: str = "authoritative"
    brain_execution_cost_default_fee_bps: float = 1.0
    brain_execution_cost_impact_cap_bps: float = 50.0
    brain_execution_cost_unverified_tca_outlier_bps: float = 500.0
    brain_execution_capacity_max_adv_frac: float = 0.05

    # Venue-truth telemetry (Phase F) — compares expected vs realized
    # costs per fill. Shadow writes to `trading_venue_truth_log` only.
    # 2026-05-15: flipped from "shadow" -> "authoritative" by operator.
    # The legacy phase-F lockdown release-blocker has been inverted; it
    # now fires on `mode=shadow` (regression detector). See
    # `scripts/check_venue_truth_release_blocker.ps1`.
    brain_venue_truth_mode: str = "authoritative"
    brain_venue_truth_ops_log_enabled: bool = True

    # Live brackets + reconciliation (Phase G) — persists bracket intent
    # per live Trade and runs a read-only sweep comparing local bracket
    # state to broker-reported open orders. In shadow mode no broker
    # writes happen; only `trading_bracket_intents` + `trading_bracket_
    # reconciliation_log` are populated. Flipping to authoritative is
    # Phase G.2 and requires extending the venue adapter protocol first.
    # See docs/TRADING_BRAIN_LIVE_BRACKETS_ROLLOUT.md.
    brain_live_brackets_mode: str = "shadow"
    brain_live_brackets_ops_log_enabled: bool = True
    brain_live_brackets_reconciliation_interval_s: int = 60
    brain_live_brackets_price_drift_bps: float = 25.0
    brain_live_brackets_qty_drift_abs: float = 1e-6
    # Phase G staged-sweep refactor: routes run_reconciliation_sweep through
    # four discrete stages (load_local / fetch_broker / classify_all / log_all)
    # instead of the legacy interleaved loop. Byte-for-byte SweepSummary parity
    # is asserted by ``TestStagedVsLegacyParity::test_staged_matches_legacy_summary``;
    # flipped to True after the refactor landed cleanly.
    brain_live_brackets_staged_sweep_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_LIVE_BRACKETS_STAGED_SWEEP_ENABLED"),
    )

    # Canonical position sizer (Phase H) — Kelly-from-NetEdgeRanker with
    # hard correlation bucket caps + single-ticker notional cap. Shadow
    # mode: emits `trading_position_sizer_log` rows in parallel with the
    # legacy sizer call-sites and NEVER changes the notional those sites
    # return. Authoritative cutover (replacing legacy sizers) is Phase
    # H.2. See docs/TRADING_BRAIN_POSITION_SIZER_ROLLOUT.md.
    brain_position_sizer_mode: str = "shadow"
    brain_position_sizer_ops_log_enabled: bool = True
    brain_position_sizer_equity_bucket_cap_pct: float = 15.0
    brain_position_sizer_crypto_bucket_cap_pct: float = 10.0
    brain_position_sizer_single_ticker_cap_pct: float = 7.5
    brain_position_sizer_kelly_scale: float = 0.25
    brain_position_sizer_max_risk_pct: float = 2.0

    # Phase I - Risk dial + weekly capital re-weighting (shadow rollout).
    # The risk dial modulates sizing aggressiveness; in Phase I it is
    # only persisted alongside PositionSizerLog rows and never applied
    # inside compute_proposal. Authoritative cutover is Phase I.2. See
    # docs/TRADING_BRAIN_RISK_DIAL_ROLLOUT.md.
    brain_risk_dial_mode: str = "shadow"
    brain_risk_dial_ops_log_enabled: bool = True
    brain_risk_dial_default_risk_on: float = 1.0
    brain_risk_dial_default_cautious: float = 0.7
    brain_risk_dial_default_risk_off: float = 0.3
    brain_risk_dial_drawdown_floor: float = 0.5
    brain_risk_dial_drawdown_trigger_pct: float = 10.0
    brain_risk_dial_ceiling: float = 1.5

    # Drawdown circuit breaker thresholds. ``portfolio_risk.get_drawdown_limits``
    # already reads these via ``getattr``; declaring them here lets pydantic
    # surface them as env-override-able (``BRAIN_RISK_MAX_5D_DD_PCT`` etc.).
    # Regime multipliers (risk_on × 1.5 / cautious × 1.0 / risk_off × 0.75)
    # are applied on top of these base values inside get_drawdown_limits.
    brain_risk_max_5d_dd_pct: float = 3.0
    brain_risk_max_30d_dd_pct: float = 8.0
    brain_risk_max_consec_losses: int = 5
    brain_risk_cooldown_hours: int = 24

    # Portfolio-level position + heat caps. ``portfolio_risk.get_risk_limits``
    # reads these via ``getattr`` but prior code had no pydantic field so env
    # overrides were silently ignored. Declaring here surfaces them as
    # ``BRAIN_RISK_MAX_POSITIONS`` / ``BRAIN_RISK_MAX_HEAT_PCT`` etc.
    brain_risk_max_positions: int = 10
    brain_risk_max_crypto: int = 5
    brain_risk_max_stocks: int = 8
    brain_risk_max_heat_pct: float = 6.0
    brain_risk_max_risk_per_trade_pct: float = 1.0
    brain_risk_max_same_ticker: int = 2
    # 2026-04-28: declared so BRAIN_RISK_MAX_AVG_CORRELATION env override
    # actually takes effect. Default stays 0.75 (matches portfolio_risk.py
    # RiskLimits.max_avg_correlation default); operator can raise via env
    # to e.g. 0.85 when the crypto pipeline needs more headroom.
    brain_risk_max_avg_correlation: float = 0.75
    # Also declare max_sector_pct for the same reason (was getattr-only).
    brain_risk_max_sector_pct: float = 40.0

    # Crypto-native pattern miner (2026-04-29). Spawns candidate
    # patterns from indicator signatures of profitable crypto trades.
    # See app/services/trading/crypto/pattern_miner.py.
    brain_crypto_miner_enabled: bool = True
    brain_crypto_miner_lookback_days: int = 30
    brain_crypto_miner_min_winners_per_signature: int = 3
    brain_crypto_miner_max_variants_per_run: int = 10

    # FIX 34 (2026-04-29): Independent fast_backtest timer. Pulls the
    # backtest-queue drain out of the after-cycle subtask sweep so it
    # runs every N seconds regardless of whether run_learning_cycle is
    # stuck on a stalled provider chain. Bridge to FIX 31 endgame.
    brain_fast_backtest_independent_loop: bool = True
    brain_fast_backtest_interval_s: int = 60
    brain_fast_backtest_batch_lean_cycle: int = FAST_BACKTEST_BATCH_DEFAULT_LEAN_CYCLE
    brain_fast_backtest_batch_backtest: int = FAST_BACKTEST_BATCH_DEFAULT_BACKTEST

    # FIX 36 (Phase 2 of FIX 31, 2026-04-29): event-driven mine handler.
    # Replaces Step 1 of run_learning_cycle by reacting to
    # market_snapshots_batch outcome events.
    brain_work_mine_batch_size: int = 1
    brain_mine_handler_min_snapshots: int = 10
    # Mining reads current snapshots and is expensive. After retries/backfills,
    # older snapshot-batch events are redundant; coalesce them so the queue
    # drains instead of rerunning full mining for stale batches.
    brain_mine_handler_obsolete_event_grace_seconds: int = 900

    # FIX 37 (Phase 2 #2, 2026-04-29): event-driven CPCV gate handler.
    # Reacts to backtest_completed events; runs CPCV promotion gate;
    # sets lifecycle_stage to backtested/challenged based on result.
    # Promotion (lifecycle_stage='promoted') is gated to handler #3.
    brain_work_cpcv_gate_batch_size: int = 8

    # FIX 38 (Phase 2 #3, 2026-04-29): promote handler. Flips lifecycle to
    # 'promoted' after passing both CPCV + realized-EV gates. Sole authority
    # for promotion finalize step. Cap low since promotion is rare and we
    # want each one logged cleanly.
    brain_work_promote_batch_size: int = 4

    # FIX 39 (Phase 2 #4+#5, 2026-04-29): trade-close fanout. Demote handler
    # re-checks realized EV gate and demotes if it now blocks. Regime ledger
    # handler rebuilds pattern_regime_ledger (throttled internally to once
    # per 60s). Both subscribe to live/paper/broker_fill close events.
    brain_work_trade_close_batch_size: int = 16

    # f-handler-pattern-stats (Phase 2 #6, 2026-05-05): event-driven recompute
    # of ScanPattern.{win_rate, avg_return_pct, trade_count} on trade close.
    # Subscribes to the same three close events as demote + regime_ledger;
    # the dispatcher fans out via ``brain_work_trade_close_batch_size`` above
    # (no separate dispatch slot). This setting is reserved for a future
    # per-handler throttle if the recompute (which can fetch OHLCV for
    # counterfactual exits per overheld trade) ever becomes a hot spot --
    # right now it's documentation of the intended per-handler cap.
    brain_work_pattern_stats_batch_size: int = 4

    # FIX 42 (2026-04-29): Coinbase OHLCV fallback for crypto. Triggered
    # when Massive is exhausted (circuit breaker OPEN, all variants dead).
    # Uses Coinbase's public /products/{pid}/candles endpoint — same product
    # IDs as the live-trading venue, geo-clean from US, no auth needed.
    brain_market_data_coinbase_fallback: bool = True

    # FIX 43 (2026-04-29): skip fast_backtest tick when Massive breaker is
    # OPEN. Prevents brain-worker from spawning doomed FractionalBacktest
    # workers that wedge waiting for unreachable data — observed at 115%
    # CPU sustained with ~10 simultaneous tqdm bars stuck at 0/N.
    brain_fast_backtest_skip_when_provider_down: bool = True

    brain_capital_reweight_mode: str = "shadow"
    brain_capital_reweight_ops_log_enabled: bool = True
    brain_capital_reweight_cron_day_of_week: str = "sun"
    brain_capital_reweight_cron_hour: int = 18
    brain_capital_reweight_lookback_days: int = 14
    brain_capital_reweight_max_single_bucket_pct: float = 35.0

    # Phase J - Drift monitor + re-cert queue (shadow rollout).
    brain_drift_monitor_mode: str = "shadow"
    brain_drift_monitor_ops_log_enabled: bool = True
    brain_drift_monitor_min_red_sample: int = 20
    brain_drift_monitor_min_yellow_sample: int = 10
    brain_drift_monitor_yellow_brier_abs: float = 0.10
    brain_drift_monitor_red_brier_abs: float = 0.20
    brain_drift_monitor_cusum_k: float = 0.05
    brain_drift_monitor_cusum_threshold_mult: float = 0.6
    brain_drift_monitor_sample_lookback_days: int = 30
    brain_drift_monitor_cron_hour: int = 5
    brain_drift_monitor_cron_minute: int = 30

    brain_recert_queue_mode: str = "shadow"
    brain_recert_queue_ops_log_enabled: bool = True
    brain_recert_queue_include_yellow: bool = False
    brain_recert_queue_dispatch_interval_minutes: int = (
        RECERT_QUEUE_DEFAULT_DISPATCH_INTERVAL_MINUTES
    )
    brain_recert_queue_dispatch_limit: int = RECERT_QUEUE_DEFAULT_DISPATCH_LIMIT
    brain_recert_queue_backtest_priority: int = RECERT_QUEUE_DEFAULT_BACKTEST_PRIORITY
    brain_recert_queue_priority_pattern_ids: str = (
        RECERT_QUEUE_DEFAULT_PRIORITY_PATTERN_IDS
    )
    brain_recert_queue_immediate_dispatch_enabled: bool = (
        RECERT_QUEUE_IMMEDIATE_DISPATCH_DEFAULT_ENABLED
    )
    brain_recert_queue_immediate_dispatch_origins: str = (
        RECERT_QUEUE_IMMEDIATE_DISPATCH_DEFAULT_ORIGINS
    )
    brain_recert_queue_immediate_dispatch_priority: int = (
        RECERT_QUEUE_IMMEDIATE_DISPATCH_DEFAULT_PRIORITY
    )

    # Phase K - Divergence panel + ops health endpoint (shadow rollout).
    brain_divergence_scorer_mode: str = "shadow"
    brain_divergence_scorer_ops_log_enabled: bool = True
    brain_divergence_scorer_min_layers_sampled: int = 1
    brain_divergence_scorer_yellow_threshold: float = 0.9
    brain_divergence_scorer_red_threshold: float = 1.8
    brain_divergence_scorer_lookback_days: int = 7
    brain_divergence_scorer_discovery_timeout_ms: int = 5000
    brain_divergence_scorer_cron_hour: int = 6
    brain_divergence_scorer_cron_minute: int = 15
    brain_divergence_scorer_layer_weight_ledger: float = 1.0
    brain_divergence_scorer_layer_weight_exit: float = 1.0
    brain_divergence_scorer_layer_weight_venue: float = 0.8
    brain_divergence_scorer_layer_weight_bracket: float = 1.0
    brain_divergence_scorer_layer_weight_sizer: float = 1.0

    brain_ops_health_enabled: bool = True
    brain_ops_health_lookback_days: int = 14

    # Phase L.17 - Macro regime expansion (shadow rollout).
    # One row per trading day is appended to trading_macro_regime_snapshots
    # by a daily scheduled sweep when mode != "off". L.17.1 never flips to
    # "authoritative"; the service layer hard-refuses that mode until the
    # L.17.2 plan is opened explicitly.
    brain_macro_regime_mode: str = "shadow"  # FIX F1 (2026-04-29): default to shadow until Phase L.20.2 opens authoritative
    brain_macro_regime_ops_log_enabled: bool = True
    brain_macro_regime_cron_hour: int = 6
    brain_macro_regime_cron_minute: int = 30
    brain_macro_regime_min_coverage_score: float = 0.5
    brain_macro_regime_trend_up_threshold: float = 0.01
    brain_macro_regime_strong_trend_threshold: float = 0.03
    brain_macro_regime_promote_threshold: float = 0.35
    brain_macro_regime_weight_rates: float = 0.45
    brain_macro_regime_weight_credit: float = 0.35
    brain_macro_regime_weight_usd: float = 0.20
    # Diagnostics endpoint default lookback (clamped [1, 180] at the route).
    brain_macro_regime_lookback_days: int = 14

    # Phase L.18 - Breadth + cross-sectional relative-strength (shadow).
    # One row per trading day is appended to
    # trading_breadth_relstr_snapshots by a daily scheduled sweep when
    # mode != "off". L.18.1 never flips to "authoritative"; the service
    # layer hard-refuses that mode until the L.18.2 plan is opened
    # explicitly.
    brain_breadth_relstr_mode: str = "shadow"  # FIX F1 (2026-04-29): default to shadow until Phase L.20.2 opens authoritative
    brain_breadth_relstr_ops_log_enabled: bool = True
    brain_breadth_relstr_cron_hour: int = 6
    brain_breadth_relstr_cron_minute: int = 45
    brain_breadth_relstr_min_coverage_score: float = 0.5
    brain_breadth_relstr_trend_up_threshold: float = 0.01
    brain_breadth_relstr_strong_trend_threshold: float = 0.03
    brain_breadth_relstr_tilt_threshold: float = 0.02
    brain_breadth_relstr_risk_on_ratio: float = 0.65
    brain_breadth_relstr_risk_off_ratio: float = 0.35
    brain_breadth_relstr_max_ohlcv_age_days: int = 7
    # Diagnostics endpoint default lookback (clamped [1, 180] at the route).
    brain_breadth_relstr_lookback_days: int = 14

    # Phase L.19 - Cross-asset signals v1 (shadow).
    # One row per trading day is appended to
    # trading_cross_asset_snapshots by a daily scheduled sweep when mode
    # != "off". L.19.1 never flips to "authoritative"; the service layer
    # hard-refuses that mode until the L.19.2 plan is opened explicitly.
    brain_cross_asset_mode: str = "shadow"  # FIX F1 (2026-04-29): default to shadow until Phase L.20.2 opens authoritative
    brain_cross_asset_ops_log_enabled: bool = True
    brain_cross_asset_cron_hour: int = 7
    brain_cross_asset_cron_minute: int = 0
    brain_cross_asset_min_coverage_score: float = 0.5
    brain_cross_asset_fast_lead_threshold: float = 0.01
    brain_cross_asset_slow_lead_threshold: float = 0.03
    brain_cross_asset_vix_percentile_shock: float = 0.80
    brain_cross_asset_beta_window_days: int = 60
    brain_cross_asset_composite_min_agreement: int = 2
    brain_cross_asset_max_ohlcv_age_days: int = 7
    # Diagnostics endpoint default lookback (clamped [1, 180] at the route).
    brain_cross_asset_lookback_days: int = 14

    # Phase L.20 - Per-ticker mean-reversion vs trend regime (shadow).
    # One row per (ticker, trading day) is appended to
    # trading_ticker_regime_snapshots by a daily scheduled sweep when mode
    # != "off". L.20.1 never flips to "authoritative"; the service layer
    # hard-refuses that mode until the L.20.2 plan is opened explicitly.
    # Additive-only: no existing consumer reads this table; L.17/L.18/L.19
    # snapshots are unchanged, and the existing ``hurst_proxy_from_closes``
    # in the momentum-neural pipeline is not touched.
    brain_ticker_regime_mode: str = "shadow"  # FIX F1 (2026-04-29): default to shadow until Phase L.20.2 opens authoritative
    brain_ticker_regime_ops_log_enabled: bool = True
    brain_ticker_regime_cron_hour: int = 7
    brain_ticker_regime_cron_minute: int = 15
    # Minimum number of daily close bars required per ticker before the
    # pure model runs (matches ``TickerRegimeConfig.min_bars``).
    brain_ticker_regime_min_bars: int = 40
    # Minimum coverage-score (fraction of scalars that are not None) for
    # a per-ticker row to be considered a complete observation. Rows
    # below this threshold are still persisted (so ops can see the
    # coverage signal) but are excluded from the sweep-level ``summary``
    # breakdown returned to the diagnostics endpoint.
    brain_ticker_regime_min_coverage_score: float = 0.5
    # Composite-label thresholds (echoed in the snapshot payload).
    brain_ticker_regime_ac1_trend: float = 0.05
    brain_ticker_regime_ac1_mean_revert: float = -0.05
    brain_ticker_regime_hurst_trend: float = 0.55
    brain_ticker_regime_hurst_mean_revert: float = 0.45
    brain_ticker_regime_vr_trend: float = 1.05
    brain_ticker_regime_vr_mean_revert: float = 0.95
    brain_ticker_regime_adx_trend: float = 20.0
    brain_ticker_regime_atr_period: int = 14
    # Universe cap - upper bound on the number of tickers processed per
    # sweep. Paired with the snapshot-universe builder to avoid
    # unbounded OHLCV fetches for a very large promoted / snapshot-held
    # set during shadow rollout.
    brain_ticker_regime_max_tickers: int = 250
    # Diagnostics endpoint default lookback (clamped [1, 30] at the route).
    brain_ticker_regime_lookback_days: int = 7

    # ---------------------------------------------------------------
    # Phase L.21 - Volatility term structure + cross-sectional
    # dispersion snapshot (shadow rollout).
    # One row per as_of_date is appended to
    # trading_vol_dispersion_snapshots by a daily scheduled sweep when
    # mode != "off". L.21.1 never flips to "authoritative"; the
    # service layer hard-refuses that mode until the L.21.2 plan is
    # opened explicitly. Additive-only: no existing consumer reads
    # this table; L.17/L.18/L.19/L.20 snapshots and
    # ``market_data.get_market_regime()`` are unchanged.
    brain_vol_dispersion_mode: str = "shadow"  # FIX F1 (2026-04-29): default to shadow until Phase L.20.2 opens authoritative
    brain_vol_dispersion_ops_log_enabled: bool = True
    brain_vol_dispersion_cron_hour: int = 7
    brain_vol_dispersion_cron_minute: int = 30
    # Minimum number of close bars required per leg before the pure
    # model runs. Matches VolatilityDispersionConfig.min_bars.
    brain_vol_dispersion_min_bars: int = 60
    # Minimum coverage-score below which composite labels are forced
    # to neutral (``vol_normal``, ``dispersion_normal``,
    # ``correlation_normal``). Rows below threshold are still
    # persisted so the soak / ops surface can see the coverage signal.
    brain_vol_dispersion_min_coverage_score: float = 0.5
    # Universe caps for dispersion and pairwise correlation. Keeps
    # the daily sweep tractable regardless of snapshot universe size.
    brain_vol_dispersion_universe_cap: int = 60
    brain_vol_dispersion_corr_sample_size: int = 30
    # Vol regime thresholds (VIXY spot, in VIX points).
    brain_vol_dispersion_vixy_low: float = 14.0
    brain_vol_dispersion_vixy_high: float = 22.0
    brain_vol_dispersion_vixy_spike: float = 30.0
    # SPY realised-vol bands (annualised, decimal fraction).
    brain_vol_dispersion_realized_vol_low: float = 0.12
    brain_vol_dispersion_realized_vol_high: float = 0.30
    # Cross-sectional return std bands (daily log-return scale).
    brain_vol_dispersion_cs_std_low: float = 0.012
    brain_vol_dispersion_cs_std_high: float = 0.025
    # Mean absolute pairwise correlation bands.
    brain_vol_dispersion_corr_low: float = 0.35
    brain_vol_dispersion_corr_high: float = 0.65
    # Diagnostics endpoint default lookback (clamped [1, 180] at the route).
    brain_vol_dispersion_lookback_days: int = 14

    # ------------------------------------------------------------------
    # Phase L.22 - intraday session regime snapshot (shadow rollout).
    # Daily post-close snapshot derived from SPY 5-minute bars. Captures
    # opening range, midday compression, power-hour, gap magnitude, and
    # a composite ``session_label`` classifying the day (trending /
    # range / reversal / gap-and-go / gap-fade / compressed / neutral).
    # ``mode`` supports ``off`` (default), ``shadow``, ``compare``; the
    # service layer hard-refuses ``authoritative`` until the L.22.2
    # plan is opened explicitly. Additive-only: no existing consumer
    # reads this table; L.17-L.21 snapshots and ``get_market_regime()``
    # are unchanged.
    brain_intraday_session_mode: str = "shadow"  # FIX F1 (2026-04-29): default to shadow until Phase L.20.2 opens authoritative
    brain_intraday_session_ops_log_enabled: bool = True
    # 22:00 local scheduler slot (post US cash close and after L.17-L.21
    # jobs at 06:30-07:30).
    # 2026-04-28: was a single int (22). Changed to str so we can take a
    # comma-separated cron expression and refresh the snapshot multiple
    # times intraday instead of once per day.
    brain_intraday_session_cron_hour: str = "11,13,15,16,22"
    brain_intraday_session_cron_minute: int = 0
    # Source symbol + OHLCV fetch parameters.
    brain_intraday_session_source_symbol: str = "SPY"
    brain_intraday_session_interval: str = "5m"
    brain_intraday_session_period: str = "5d"
    # Minimum number of 5-min RTH bars required before the composite
    # label becomes non-neutral. A full session = 78 bars.
    brain_intraday_session_min_bars: int = 40
    # Minimum coverage-score below which callers see ``None`` back.
    brain_intraday_session_min_coverage_score: float = 0.5
    # Opening-range and power-hour durations (minutes).
    brain_intraday_session_or_minutes: int = 30
    brain_intraday_session_power_minutes: int = 30
    # Session thresholds (fractions of open price).
    brain_intraday_session_or_range_low: float = 0.003
    brain_intraday_session_or_range_high: float = 0.012
    brain_intraday_session_midday_compression_cut: float = 0.5
    brain_intraday_session_gap_go: float = 0.005
    brain_intraday_session_gap_fade: float = 0.005
    brain_intraday_session_trending_close: float = 0.006
    brain_intraday_session_reversal_close: float = 0.003
    # Diagnostics endpoint default lookback (clamped [1, 180] at the route).
    brain_intraday_session_lookback_days: int = 14

    # ---- Phase M.1: pattern x regime performance ledger (shadow) ----
    # First consumer of L.17-L.22 snapshots: joins closed paper trades
    # to the most recent regime label per dimension at entry_date, then
    # writes one aggregate row per (pattern_id, regime_dimension,
    # regime_label) tuple to ``trading_pattern_regime_performance_daily``.
    # Shadow-only: no sizing/promotion/stop behaviour reads this table
    # in M.1. ``mode`` supports ``off`` (default), ``shadow``, ``compare``;
    # service hard-refuses ``authoritative`` until M.2 is opened.
    brain_pattern_regime_perf_mode: str = "shadow"  # FIX F1 (2026-04-29): default to shadow until Phase L.20.2 opens authoritative
    brain_pattern_regime_perf_ops_log_enabled: bool = True
    # 23:00 local scheduler slot (after L.22 at 22:00 has landed).
    brain_pattern_regime_perf_cron_hour: int = 23
    brain_pattern_regime_perf_cron_minute: int = 0
    # Rolling window of closed paper trades (exit_date within N days).
    brain_pattern_regime_perf_window_days: int = 90
    # Minimum trades per (pattern, dimension, label) cell before
    # ``has_confidence`` is True. Sub-threshold cells are still persisted
    # for visibility but excluded from the default diagnostics view.
    brain_pattern_regime_perf_min_trades_per_cell: int = 3
    # Safety cap: if more than N patterns have closed trades in the
    # window, the top-N by trade-count are kept and the rest are logged
    # as ``event=pattern_regime_perf_skipped reason=pattern_cap``.
    brain_pattern_regime_perf_max_patterns: int = 500
    # Diagnostics endpoint default lookback (clamped [1, 180] at route).
    brain_pattern_regime_perf_lookback_days: int = 14

    # ---- Phase M.2: pattern x regime authoritative consumers ----
    # Three independently-gated slices read the M.1 ledger and make
    # (or shadow) decisions. Each slice has its own mode flag; all
    # default to ``off``. Authoritative mode requires a live, un-
    # expired row in ``trading_governance_approvals`` for the slice's
    # ``action_type``. Missing/expired approval => service refuses
    # authoritative and emits a ``refused`` event.

    # M.2.a: NetEdgeRanker sizing tilt multiplier inside
    # ``position_sizer_emitter.emit_shadow_proposal``.
    brain_pattern_regime_tilt_mode: str = "shadow"
    brain_pattern_regime_tilt_ops_log_enabled: bool = True
    brain_pattern_regime_tilt_kill: bool = False
    # Multiplier bounds (hard clamp at model boundary).
    brain_pattern_regime_tilt_min_multiplier: float = 0.25
    brain_pattern_regime_tilt_max_multiplier: float = 2.00
    # At least this many confident ledger cells (``has_confidence=TRUE``)
    # must be available across 8 dimensions to tilt; otherwise
    # ``multiplier = 1.0`` with ``reason_code = insufficient_coverage``.
    brain_pattern_regime_tilt_min_confident_dimensions: int = 3
    # Ledger staleness tolerance: cell's as_of_date must be within
    # N days of today. Older cells are treated as unavailable.
    brain_pattern_regime_tilt_max_staleness_days: int = 5

    # M.2.b: promotion gate inside ``governance.request_pattern_to_live``.
    brain_pattern_regime_promotion_mode: str = "shadow"
    brain_pattern_regime_promotion_ops_log_enabled: bool = True
    brain_pattern_regime_promotion_kill: bool = False
    # Required confident-dimension coverage for a promotion decision.
    brain_pattern_regime_promotion_min_confident_dimensions: int = 3
    # Block promotion if this many dimensions show negative expectancy
    # (cell.expectancy < 0) with has_confidence=TRUE.
    brain_pattern_regime_promotion_block_on_negative_dimensions: int = 2
    # Minimum overall expectancy across confident dimensions to allow.
    brain_pattern_regime_promotion_min_mean_expectancy: float = 0.0

    # M.2.c: kill-switch / auto-quarantine (daily sweep at 23:05).
    brain_pattern_regime_killswitch_mode: str = "shadow"
    brain_pattern_regime_killswitch_ops_log_enabled: bool = True
    brain_pattern_regime_killswitch_kill: bool = False
    brain_pattern_regime_killswitch_cron_hour: int = 23
    brain_pattern_regime_killswitch_cron_minute: int = 5
    # Consecutive-day threshold: quarantine fires only when a pattern's
    # aggregate expectancy has been < threshold for N sequential
    # evaluation days. Prevents single-bad-day flakes from quarantining.
    brain_pattern_regime_killswitch_consecutive_days: int = 3
    brain_pattern_regime_killswitch_neg_expectancy_threshold: float = -0.005
    # Per-pattern circuit breaker: max quarantines per pattern per
    # rolling 30-day window. Prevents thrash.
    brain_pattern_regime_killswitch_max_per_pattern_30d: int = 1
    brain_pattern_regime_killswitch_lookback_days: int = 14

    # ------------------------------------------------------------------
    # Phase M.2-autopilot: auto-advance engine for M.2 slices.
    # When enabled, evaluates shadow->compare->authoritative transitions
    # daily for each slice and writes mode overrides to
    # ``trading_brain_runtime_modes``. Never skips stages, rate-limits
    # to at most one advance per slice per UTC day, and auto-reverts
    # on anomaly. See docs/TRADING_BRAIN_PATTERN_REGIME_M2_AUTOPILOT_ROLLOUT.md.
    # ------------------------------------------------------------------
    brain_pattern_regime_autopilot_enabled: bool = False
    brain_pattern_regime_autopilot_kill: bool = False
    brain_pattern_regime_autopilot_ops_log_enabled: bool = True
    # Daily evaluation cron. Default 06:15 local (before the macro /
    # breadth / cross-asset snapshot jobs so advances take effect for
    # the trading day).
    brain_pattern_regime_autopilot_cron_hour: int = 6
    brain_pattern_regime_autopilot_cron_minute: int = 15
    # Weekly summary cron (single ops line with per-slice stage).
    brain_pattern_regime_autopilot_weekly_cron_hour: int = 9
    brain_pattern_regime_autopilot_weekly_cron_dow: str = "mon"
    # Days-in-stage thresholds. Shadow -> compare after N_shadow BD;
    # compare -> authoritative after N_compare BD.
    brain_pattern_regime_autopilot_shadow_days: int = 5
    brain_pattern_regime_autopilot_compare_days: int = 10
    # Minimum decision-log rows required across the window to consider
    # evidence "flowing" (proves the slice is actually emitting
    # decisions, not silently no-op-ping).
    brain_pattern_regime_autopilot_min_decisions: int = 100
    # M.2.a tilt safety envelope: mean would-apply multiplier must lie
    # inside [min, max] over the compare window to unlock authoritative.
    brain_pattern_regime_autopilot_tilt_mult_min: float = 0.85
    brain_pattern_regime_autopilot_tilt_mult_max: float = 1.25
    # M.2.b promotion safety envelope: ratio of consumer_block over
    # baseline_allow must be <= ratio to unlock authoritative.
    brain_pattern_regime_autopilot_promo_block_max_ratio: float = 0.10
    # M.2.c kill-switch safety envelope: mean daily would-quarantine
    # count must be <= N/day to unlock authoritative.
    brain_pattern_regime_autopilot_ks_max_fires_per_day: float = 1.0
    # Auto-inserted governance approval expiry window (days).
    brain_pattern_regime_autopilot_approval_days: int = 30

    @field_validator("brain_queue_backtest_executor", mode="before")
    @classmethod
    def _normalize_queue_backtest_executor(cls, v: object) -> str:
        if v is None or v == "":
            return "threads"
        s = str(v).strip().lower()
        if s in ("process", "processes", "mp", "multiprocessing"):
            return "process"
        return "threads"
    brain_queue_target_tickers: int = 60  # tickers per pattern in queue backtest (more = heavier per pattern)
    # Intraday full-tier patterns are materially more expensive per ticker
    # than daily patterns. Keep each pass bounded and let evidence accumulate
    # across queue cycles; promotion/EV/CPCV gates still consume the persisted
    # evidence count and are not relaxed by this cap.
    brain_queue_intraday_timeframes: str = "1m,5m,15m"
    brain_queue_intraday_target_tickers: int = 24
    # Operational recert/debt lane: keep promoted/pilot/shadow evidence fresh
    # without allowing one pattern to monopolize the queue for an hour.
    brain_queue_operational_refresh_enabled: bool = True
    brain_queue_operational_refresh_lifecycles: str = "promoted,live,shadow_promoted,pilot_promoted"
    brain_queue_operational_target_tickers: int = 24
    brain_queue_operational_stored_refresh_max_tickers: int = 24
    brain_use_gpu_ml: bool = False       # GPU for pattern meta-learner (LightGBM) â€” ML train step only, not queue BT

    # Full learning cycle (run_learning_cycle): optional slim mode
    brain_secondary_miners_on_cycle: bool = True   # intraday/refine/exit/fakeout/sizing/inter-alert/timeframe/synergy steps
    # Learning cycle AI report: 0 = template-only (no LLM); N>0 = call LLM every Nth stored report for polish.
    learning_cycle_report_llm_every_n: int = 0

    # Pattern backtest queue: how soon a pattern is eligible again (was hardcoded 7).
    brain_retest_interval_days: int = 7
    # Lane-aware queue planner: preserve safety/recert lanes, fast-track
    # edge-evidence variants, and avoid one parent lineage consuming an
    # entire batch of expensive backtests.
    brain_queue_lane_planner_enabled: bool = True
    # Legacy fixed cap override. Keep at 0 for adaptive lineage diversification.
    brain_queue_max_per_lineage_per_batch: int = BRAIN_QUEUE_LINEAGE_FIXED_CAP_DISABLED
    brain_queue_lineage_max_batch_share: float = Field(
        default=BRAIN_QUEUE_LINEAGE_DIVERSIFICATION_SHARE_DEFAULT,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("BRAIN_QUEUE_LINEAGE_MAX_BATCH_SHARE"),
    )
    brain_queue_lineage_min_per_batch: int = Field(
        default=BRAIN_QUEUE_LINEAGE_MIN_PER_BATCH_DEFAULT,
        ge=0,
        validation_alias=AliasChoices("BRAIN_QUEUE_LINEAGE_MIN_PER_BATCH"),
    )
    brain_queue_lane_fetch_multiplier: int = 4
    brain_queue_edge_evidence_max_per_batch: int = 12
    brain_queue_prescreen_max_per_batch: int = 20
    # Near-promoted shadow/pilot patterns stay protected from demotion, but
    # repeated zero-trade queue runs should cool down so they do not monopolize
    # the high-priority lane every cycle.
    brain_queue_sparse_promotion_debt_cooldown_enabled: bool = True
    brain_queue_sparse_promotion_debt_zero_runs: int = 5
    brain_queue_sparse_promotion_debt_cooldown_minutes: int = 360
    # Promoted/live recert debt still blocks live trading, but an unresolved
    # recert should not be retested every batch after a fresh attempt unless
    # an explicit recert/manual boost bypasses the normal retest floor.
    brain_queue_recert_cooldown_enabled: bool = True
    brain_queue_recert_cooldown_minutes: int = 360
    # When the retest queue is thin, add oldest-tested active patterns up to this many per cycle.
    brain_queue_exploration_enabled: bool = True
    brain_queue_exploration_max: int = 40

    # Research integrity: causality checks + provenance on pattern backtests (Freqtrade-style hygiene, CHILI-native).
    brain_research_integrity_enabled: bool = True
    brain_research_integrity_strict: bool = True  # block promotion to promoted when causality fails
    brain_research_integrity_max_check_bars: int = 48

    # Lightweight prediction refresh: promoted ScanPatterns only (no full learning cycle).
    brain_fast_eval_enabled: bool = True
    # When True, APScheduler also runs fast eval on an interval. Default False: full
    # ``run_learning_cycle`` (worker or Learn) already refreshes the promoted cache.
    brain_fast_eval_scheduler_enabled: bool = False
    brain_fast_eval_interval_minutes: int = 10
    brain_fast_eval_max_tickers: int = 400

    # Snapshots + mining: canonical bar key (ticker, interval, bar_start_utc). Intraday is crypto-focused.
    brain_snapshot_top_tickers: int = 1000
    brain_intraday_snapshots_enabled: bool = True
    brain_intraday_intervals: str = "1m,5m,15m"
    brain_intraday_max_tickers: int = 1000
    brain_snapshot_backfill_years: int = 10
    brain_scheduled_snapshot_max_tickers: int = 120
    brain_scheduled_snapshot_workers: int = 2

    # Crypto universe for prescreen / ticker_universe: 0 = fetch all pages from provider (CoinGecko, capped by safety limit); N>0 = top N by market cap.
    brain_crypto_universe_max: int = 200
    # When True, merge Coinbase Advanced Trade USD spot product_ids into the crypto universe
    # (requires coinbase-advanced-py + COINBASE_API_KEY/SECRET; no UI connect() needed).
    brain_merge_coinbase_spot_universe: bool = True
    # After applying brain_crypto_universe_max, allow up to this many additional Coinbase-only
    # symbols (not in the capped CoinGecko list) so listed spot products are still scannable.
    brain_coinbase_universe_extra_cap: int = 600
    # Drop cryptos below this 24h USD volume when building universe (0 = off). Reduces illiquid tail when universe is large.
    brain_crypto_universe_min_volume_usd: float = 0.0
    # When False, prescreen uses a smaller crypto list (150) for faster cycles; True = merge full configured crypto universe into prescreen.
    brain_scan_include_full_crypto_universe: bool = True

    # Pattern mining: max tickers to pull OHLCV for per cycle (0 = no cap; use full merged mining list).
    brain_mine_patterns_max_tickers: int = 1000
    # Broad pattern mining is provider/network-bound and non-latency-sensitive.
    # Keep the live worker conservative by default; operators can raise this
    # when running a dedicated research box.
    brain_mine_patterns_workers: int | None = None
    brain_provider_preflight_enabled: bool = True
    brain_provider_preflight_data_check_enabled: bool = True
    brain_provider_preflight_cache_seconds: int = 60
    brain_provider_preflight_timeout_seconds: float = 1.5
    # Historical labeled snapshot rows mixed into pattern mining. Keep bounded:
    # this runs from market_snapshots_batch work events and must not monopolize
    # the DB while fresh OHLCV mining is also active.
    brain_mine_labeled_snapshot_limit: int = 5000
    # Require stability across chronological segments before save_insight from mine_patterns.
    brain_mining_purged_cpcv_enabled: bool = True
    # When True, CPCV + DSR + PBO gate blocks promotion after ensemble/DSR/holdout (HR1 path).
    # Default OFF: metrics computed at promotion-attempt time only; shadow / logging only.
    chili_cpcv_promotion_gate_enabled: bool = False
    # 2026-04-28: realized-EV gate. Sits alongside CPCV. Pattern must have
    # positive avg_return_pct (mean of trade returns = EV) and at least
    # ``chili_realized_ev_min_trades`` realized trades. CPCV alone wasn't
    # enough — 1047 passed it twice while losing money live.
    chili_realized_ev_gate_enabled: bool = True
    chili_realized_ev_min_trades: int = 5
    chili_realized_ev_min_avg_return_pct: float = 0.0
    chili_realized_ev_min_win_rate: float = 0.0
    # Let paper/shadow evidence from raw_realized_* clear the EV gate only
    # when corrected/live evidence is missing or still below the minimum sample.
    # It never overrides a live/corrected sample that already shows clear loss.
    chili_realized_ev_gate_allow_raw_fallback: bool = True
    chili_shadow_vetting_require_realized_ev_for_full: bool = True
    # 2026-04-28: ticker-scope autotune. Reads per-ticker realized PnL and
    # narrows ``ticker_scope`` from 'universal' to 'explicit_list' when
    # a pattern has both edge AND bleed tickers. The brain LEARNS its
    # ticker dependency rather than us banning tickers manually.
    chili_ticker_autotune_enabled: bool = True
    chili_ticker_autotune_min_total_trades: int = 5
    chili_ticker_autotune_min_trades_per_ticker: int = 2
    chili_ticker_autotune_lookback_days: int = 90
    chili_ticker_autotune_dry_run: bool = False
    # 2026-04-28: realized stats sync. Recomputes ScanPattern columns from
    # trading_trades plus qualified autotrader paper/shadow outcomes. Closes
    # the gap exposed by the audit (8 patterns had actual trades but stored
    # trade_count=0), and lets paper/pilot evidence clear thin-EV debt without
    # manual certification work.
    chili_realized_sync_enabled: bool = True
    chili_realized_sync_lookback_days: int = 365
    chili_realized_sync_min_n: int = 1
    chili_realized_sync_interval_minutes: int = 30
    chili_realized_sync_include_paper_dynamic: bool = True
    # f-canonical-outcome-layer Phase A (2026-05-14). Shadow-log raw-vs-
    # corrected win-rate divergence thresholds. INFO ≥ info_pct, WARNING
    # ≥ warn_pct. Phase A is pure observation -- no DB row, no metric --
    # so operators can tune without touching code.
    chili_canonical_outcome_divergence_info_pct: float = 0.20
    chili_canonical_outcome_divergence_warn_pct: float = 0.50
    # 2026-04-29 third-pass audit FIX B-1: daily realized-EV demote pass.
    # Re-applies the realized-EV gate to every promoted pattern; demotes
    # any that fail outside the configured settle window. Mig 206 is the
    # one-time retroactive sweep; this is the going-forward enforcement.
    chili_realized_ev_demote_pass_enabled: bool = True
    chili_realized_ev_demote_settle_days: int = 14
    # Round-12 (2026-04-30): backtest queue improvements.
    # 1. priority scorer runs daily and updates backtest_priority based
    #    on lifecycle/staleness/evidence-gap signals.
    chili_backtest_priority_scorer_enabled: bool = True
    # Daily scored priority should mostly order genuinely eligible queue rows,
    # not make every fresh challenged/candidate pattern pending again. Values
    # at or above this floor are treated as explicit operator/recert boosts and
    # may bypass the normal retest interval.
    chili_backtest_priority_bypass_retest_floor: int = Field(
        default=BACKTEST_PRIORITY_DEFAULT_BYPASS_RETEST_FLOOR,
        ge=1,
        validation_alias=AliasChoices(
            "CHILI_BACKTEST_PRIORITY_BYPASS_RETEST_FLOOR"
        ),
    )
    # Promotion-path evidence debt should drain before generic research
    # backlog. This does not relax CPCV or EV gates; it only prioritizes
    # shadow/pilot patterns whose stored gate reasons say they need more
    # CPCV path evidence before they can graduate.
    chili_backtest_prioritize_promotion_path_debt: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_BACKTEST_PRIORITIZE_PROMOTION_PATH_DEBT"
        ),
    )
    # 2. switch backtest executor to process pool for true CPU
    #    parallelism. Threads are GIL-bound on the indicator-compute
    #    portion of each backtest. Set per-worker memory cap so process
    #    pool doesn't blow up on large universes.
    chili_brain_queue_backtest_executor: str = "process"
    chili_brain_queue_process_cap: int = 6
    # 3. soft pause during US regular session: bound stock-only batches
    #    9:30-16:00 ET so live trading systems get market-data bandwidth,
    #    while still allowing a small recert/operational stock lane to drain.
    #    Off-hours throughput already 50/hr; the live-hours issue is
    #    contention, not research quality.
    chili_brain_queue_market_hours_pause: bool = True
    chili_brain_queue_market_hours_stock_lane_max_patterns: int = Field(
        default=BRAIN_QUEUE_MARKET_HOURS_STOCK_LANE_DEFAULT,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_BRAIN_QUEUE_MARKET_HOURS_STOCK_LANE_MAX_PATTERNS"
        ),
    )
    chili_brain_queue_market_hours_exploration_max: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_BRAIN_QUEUE_MARKET_HOURS_EXPLORATION_MAX"
        ),
    )
    # 4. zero-trade pattern demote: after N consecutive 0-trade backtest
    #    runs, demote queue_tier to 'prescreen' so the pattern only runs
    #    when the prescreen tier is enabled (rare). Prevents the queue
    #    burning cycles indefinitely on dead patterns.
    chili_backtest_zero_trade_demote_threshold: int = 3
    # 2026-04-28: pattern x regime ledger — turns shadow-mode regime
    # snapshots into actionable per-pattern evidence.
    chili_pattern_regime_ledger_enabled: bool = True
    chili_pattern_regime_ledger_window_days: int = 90
    chili_pattern_regime_ledger_min_trades: int = 3
    chili_pattern_regime_ledger_dry_run: bool = False
    # 2026-04-28: regime gate at the auto-trader entry funnel. Reads the
    # ledger to block patterns with confidently-negative expectancy in the
    # current ticker regime. Default mode is shadow (logs would-be-blocks
    # without enforcing) so the operator can audit before flipping live.
    chili_regime_gate_enabled: bool = True
    # 2026-04-28: flipped shadow -> live per operator. Gate is conservative
    # (only blocks confidently-negative-EV once the configured evidence
    # threshold is met). Strictly safer than allowing every entry through.
    chili_regime_gate_mode: str = "live"
    chili_regime_gate_min_trades: int = REGIME_GATE_DEFAULT_MIN_TRADES
    chili_regime_gate_max_age_days: int = REGIME_GATE_DEFAULT_MAX_AGE_DAYS
    # FIX 10 (2026-04-28 deep audit): multi-dim consensus threshold. The
    # gate consults all four dimensions (ticker_regime, breadth_regime,
    # cross_asset_regime, vol_regime) and BLOCKS only when at least this
    # many dimensions show confident-negative EV. 2-of-4 was operator-
    # selected: prevents single-dim noise from over-blocking while still
    # catching cases where multiple dimensions agree on regime risk.
    chili_regime_gate_min_negatives: int = REGIME_GATE_DEFAULT_MIN_NEGATIVES
    # Crypto markets are 24/7 and should not be vetoed by equity breadth
    # plus equity-vol risk alone. When enabled, a crypto regime block needs
    # at least one negative anchor from the listed dimensions.
    chili_regime_gate_require_crypto_anchor_negative: bool = True
    chili_regime_gate_crypto_anchor_dimensions: str = (
        REGIME_GATE_DEFAULT_CRYPTO_ANCHOR_DIMENSIONS
    )
    # Equity breadth/volatility headwinds are useful throttles, but a live
    # hard block should be anchored in asset-local evidence so broad market
    # noise does not leave the stock book permanently idle.
    chili_regime_gate_require_equity_anchor_negative: bool = True
    chili_regime_gate_equity_anchor_dimensions: str = (
        REGIME_GATE_DEFAULT_EQUITY_ANCHOR_DIMENSIONS
    )
    # When >0, :func:`evaluate_pattern_cpcv` subsamples labeled rows before CV/LightGBM
    # (memory safety for patterns with huge trading_pattern_trades). 0 = no cap.
    chili_cpcv_max_labeled_rows: int = 0
    # Small-sample CPCV (Q1.T1.5): purge/embargo scale with n; target paths capped.
    chili_cpcv_purge_frac: float = 0.05
    chili_cpcv_embargo_frac: float = 0.02
    # Minimum labeled rows after triple-barrier filtering for CPCV + gate evidence.
    chili_cpcv_min_trades: int = 15
    # Upper bound for ``min( cap, max(10, n // 5) )`` in ``optimal_folds_number`` path budget.
    chili_cpcv_target_paths_max: int = 100
    # n_trades in [min_trades, full_confidence) ⇒ gate may pass with ``provisional_sample_size`` tag.
    chili_cpcv_full_confidence_min_trades: int = 30
    # Q1.T1.7: CPCV combinatorial path count — provisional band vs full (parallel to trade tiers).
    chili_cpcv_n_paths_provisional_min: int = 20
    chili_cpcv_n_paths_full_min: int = 50
    # When True, scheduler-worker runs ``scripts/backfill_cpcv_metrics.py --commit`` weekly (Sun 04:00 ET).
    # Default OFF until operator validates a manual backfill run.
    chili_cpcv_weekly_backfill_enabled: bool = True
    # f-adaptive-cpcv-gate (Phase 2 of f-adaptive-promotion-architecture, 2026-05-11).
    # Adaptive CPCV gate wraps promotion_gate_passes with sample-size-aware,
    # pool-relative thresholds (Bayesian shrinkage + lower-CI percentiles +
    # Pareto frontier + portfolio marginal Sharpe). Default OFF — wrapper is
    # a byte-identical no-op. Documented in docs/runbooks/CPCV_ADAPTIVE_GATE.md
    # under the "operator policy, not magic" framing.
    chili_cpcv_adaptive_gate_enabled: bool = False
    # Target promotion pool size as a fraction of the active pattern pool.
    # 0.05 = top 5% by each metric (empirical q=0.95 percentile threshold).
    chili_cpcv_target_promotion_pool_pct: float = 0.05
    # Confidence-interval level for the sample-size-aware lower/upper bound.
    # Higher (0.95) = stricter promotion / smaller pool; lower (0.80) = looser.
    chili_cpcv_ci_level: float = 0.90
    # Portfolio marginal Sharpe contribution required to admit, in basis points.
    # 0.0 = any positive marginal contribution admits (effective no-op floor).
    chili_portfolio_marginal_sharpe_min_bps: float = 0.0
    # Phase E of f-evidence-fidelity-architecture (2026-05-14). When ON,
    # the adaptive CPCV gate applies a Benjamini-Hochberg adjustment to
    # the DSR pool-percentile threshold based on the candidate's
    # hypothesis-family size. Default OFF — the BH-adjusted threshold is
    # shadow-logged into ``pattern_family_trial_log`` regardless of the
    # flag so operators can observe legacy vs adjusted divergence for
    # the 7-day soak window before flipping. See
    # ``app/services/trading/family_fdr.py``.
    chili_family_fdr_enabled: bool = True
    # Q1.T3 phase 1: INSERT into ``unified_signals`` alongside existing payloads (default OFF).
    chili_unified_signal_enabled: bool = True
    # Q1.T3 phase 2: shadow-consume unified_signals from autotrader and log
    # parity discrepancies into unified_signal_consumer_parity_log. Decision is
    # still driven by BreakoutAlert; this is observation only. Flip ON after
    # phase 1 has accumulated unified_signals data for ~7 days. Default OFF.
    chili_unified_signal_consumer_enabled: bool = False
    # Q1.T4: adaptive strategy parameter learning. When ON, the background
    # learning pass updates strategy_parameter rows from realized outcomes.
    # Read path always works (code that calls get_parameter() always sees a
    # coherent value). Default OFF — enables shadow-mode reads first, then
    # operator flips ON when comfortable that the right outcomes are being
    # recorded against parameter use.
    chili_strategy_parameter_learning_enabled: bool = False
    # Q1.T5: Hierarchical Risk Parity portfolio sizing. When ON, replaces the
    # naive 2%-per-trade sizing with HRP-allocated sizing across the active
    # position covariance. When OFF (default), naive sizing is preserved and
    # HRP is computed in shadow for comparison via portfolio_sizing_log.
    chili_hrp_sizing_enabled: bool = False
    # Maximum calendar age for the newest bar in each symbol's HRP return
    # history. This tolerates weekends/market holidays while blocking
    # week-old snapshot feeds from driving live allocation weights.
    chili_hrp_returns_max_staleness_days: int = 5
    # Q2.T1: options lane scaffold. When OFF (default), all options code paths
    # are inert. When ON, paper-only by default (set chili_options_lane_live
    # to True for live broker submission via Tradier). Hard greeks-budget
    # enforcement is always active when this is ON; bypass only via
    # CHILI_OPTIONS_BUDGET_BYPASS=true (operator-supervised testing).
    chili_options_lane_enabled: bool = False
    chili_options_lane_live: bool = False
    # Task MM Phase 1: Robinhood options venue. The options lane scaffold
    # (Q2.T1) routes through Tradier by default; flip this to ON to use
    # the Robinhood options API instead. Same equity-scope OAuth token
    # the spot adapter uses, so no separate auth dance like crypto's
    # nummus. Operator-side prerequisite: RH options must be approved
    # at the appropriate level on the account (Level 2 buy / Level 3
    # spreads). Default OFF; flipping requires chili_options_lane_enabled
    # to also be ON.
    chili_options_venue_robinhood_enabled: bool = False
    # Q2.T2: forex lane scaffold (OANDA-first). When OFF (default), all FX
    # code paths are inert. When ON, paper-only by default (set
    # chili_forex_lane_live to True for live broker submission). Hard 10:1
    # effective-leverage cap is always enforced when ON; bypass via
    # CHILI_FOREX_LEVERAGE_BYPASS=true (testing only).
    chili_forex_lane_enabled: bool = False
    chili_forex_lane_live: bool = False
    # Q2.T3: crypto perps lane scaffold (Binance-first, Bybit slot).
    # When OFF (default), all perp code paths are inert. When ON, paper
    # only by default. Funding-rate ingestion runs on schedule when ON
    # so perp_funding accumulates regardless of trading.
    chili_perps_lane_enabled: bool = False
    chili_perps_lane_live: bool = False
    # Q2 Task K: pattern-survival meta-classifier. When OFF (default), all
    # meta-classifier code paths are inert — feature collection, prediction
    # writes, and downstream consumers all skip. When ON, the daily snapshot
    # job populates pattern_survival_features. Live wiring into demotion /
    # sizing decisions is gated separately by
    # chili_pattern_survival_decisions_enabled (Phase 3, default OFF).
    chili_pattern_survival_classifier_enabled: bool = False
    chili_pattern_survival_decisions_enabled: bool = False
    # K Phase 3 sub-flags (S.2). Each consumer of survival_probability is
    # gated independently so the operator can flip them on one at a time
    # following the staged rollout in
    # docs/PATTERN_SURVIVAL_PHASE_3_DESIGN.md (sizing first, demote
    # second, promote_gate last). All require chili_pattern_survival_-
    # decisions_enabled=True as the parent kill-switch — flipping the
    # parent OFF disables every sub-consumer regardless of its own flag.
    chili_pattern_survival_sizing_enabled: bool = False
    chili_pattern_survival_demote_enabled: bool = False
    chili_pattern_survival_promote_gate_enabled: bool = False
    # Sizing-multiplier floor: a low-survival pattern's notional is
    # multiplied by clamp(SIZING_FLOOR + (1-SIZING_FLOOR) * p, FLOOR, 1.0).
    # Default 0.25 means even patterns with p=0.0 keep 25% of their
    # HRP-allocated size. Tunable so the operator can sharpen the
    # gradient (lower floor = more aggressive risk-off) without code
    # changes.
    chili_pattern_survival_sizing_floor: float = 0.25
    # Demote / promote thresholds. Sourced as floats so per-environment
    # tuning is .env-only.
    chili_pattern_survival_demote_threshold: float = 0.30
    chili_pattern_survival_demote_streak_required: int = 3
    chili_pattern_survival_promote_gate_threshold: float = 0.40
    # Q1.T2: 3-state Gaussian HMM regime tags on snapshots (default OFF = byte parity with pre-T2).
    chili_regime_classifier_enabled: bool = True
    # When True, weekly retrain and backfill skip loading `regime_models/` for warm-start (cold EM fit).
    chili_regime_force_cold_fit: bool = False
    chili_regime_classifier_random_state: int = 42
    chili_regime_classifier_n_iter: int = 200
    chili_regime_classifier_weekly_cron_dow: str = "sun"
    chili_regime_classifier_weekly_cron_hour: int = 4
    chili_regime_classifier_weekly_cron_minute: int = 15
    # Extra SPY-regime × motif checks at end of mine_patterns.
    brain_regime_mining_enabled: bool = True
    # OHLC-derived ``learned_v1`` block on get_indicator_snapshot JSON.
    brain_snapshot_learned_v1_enabled: bool = True

    # Brain UI: "tradeable patterns" list (OOS % and trade count gates; promoted-only by default).
    brain_tradeable_min_oos_wr: float = 50.0
    brain_tradeable_min_oos_trades: int = 5
    brain_tradeable_limit: int = 20

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

    # Web/news search providers (app/search_providers.py).
    # Cascade tried left-to-right; each keyed provider self-skips when its key
    # (or searxng_url) is empty, so with no config below the effective provider
    # is DuckDuckGo — identical to the original single-backend behavior. Set any
    # key/URL to light that provider up ahead of DDG and spare the DDG rate limit.
    search_provider_order: str = "searxng,brave,tavily,serper,google_pse,duckduckgo"
    searxng_url: str = ""          # self-hosted SearXNG base URL, e.g. http://localhost:8888
    brave_api_key: str = ""        # Brave Search API
    tavily_api_key: str = ""       # Tavily API
    serper_api_key: str = ""       # Serper.dev API
    google_pse_key: str = ""       # Google Programmable Search API key
    google_pse_cx: str = ""        # Google Programmable Search engine id (cx)
    search_request_timeout: int = 20          # per-provider HTTP timeout (s)
    search_content_cache_ttl_sec: int = 1800  # page-content fetch cache TTL (s)
    # Background research (reasoning_brain / project_brain) enriches its top
    # results with fetched full-page article text before LLM summarization,
    # instead of summarizing from search snippets alone. ON by default — full
    # article text materially improves catalyst research. Set False to kill.
    search_fetch_sources: bool = True
    search_max_fetch: int = 3                  # max pages fetched per research query

    # External MCP (Model Context Protocol) client (app/mcp_client.py). Lets the
    # brain consume EXTERNAL MCP servers (e.g. SEC filings, news). ON — but inert
    # until mcp_servers_json lists a server (so default behavior is unchanged
    # until you add one). Read-only by policy: a hard in-code denylist blocks any
    # order/trade/withdraw-style tool even if allowlisted (see mcp_client.py).
    mcp_enabled: bool = True
    # JSON array of server configs, e.g.:
    # [{"id":"sec","name":"SEC EDGAR","transport":"sse","url":"https://...",
    #   "allowed_tools":["search","get_filing"]}]
    mcp_servers_json: str = ""

    # Teacher-escalation skill learning (app/teacher_escalation.py + teacher_hook).
    # On a failed chat turn, a strong "teacher" model distills a reusable skill in
    # the background (fire-and-forget; indexed into skill_memory). ON by default —
    # fires ONLY on detected failures, so cost is bounded to genuinely-failed
    # turns. Set False to kill (e.g. to cap paid-LLM spend).
    teacher_escalation_enabled: bool = True
    teacher_skill_dir: str = "data/skills"   # where FileSkillStore persists skills

    # Daily trading brief scheduled job (app/services/trading/daily_trading_brief.py).
    # Generates a per-user HTML brief once daily (read-only; reuses the on-demand
    # /api/brain/trading/brief stack). ON by default; writes HTML files under the
    # dir below — no broker/DB writes. Set False to kill the scheduled job.
    chili_daily_trading_brief_enabled: bool = True
    chili_daily_trading_brief_hour_pt: int = 17        # local America/Los_Angeles hour
    chili_daily_trading_brief_window_hours: int = 24   # lookback for the brief
    chili_daily_trading_brief_dir: str = "data/briefs"

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
    # Phase 0: harmful defaults frozen â€” scheduler cycle and chat injection off unless enabled.
    project_brain_scheduler_enabled: bool = False
    project_brain_chat_context_enabled: bool = False

    # Phase 1: coding-task validation runner (allowlist is hard-coded in code; not configurable).
    coding_validation_step_timeout_seconds: int = 120

    # Trading freshness / staleness guardrails
    top_picks_warn_age_min: int = 15   # warn when picks batch is older than N minutes
    proposal_warn_age_min: int = 60    # warn when proposal is older than N minutes
    pick_warn_drift_pct: float = 10.0  # warn when price has drifted >N% from entry

    # Database â€” PostgreSQL only (required). See .env.example and docs/DATABASE_POSTGRES.md.
    # Example (host â†’ Docker Compose postgres): postgresql://chili:chili@localhost:5433/chili
    database_url: str = Field(..., description="PostgreSQL connection URL")
    # Optional: same server, `chili_staging` — full copy of prod for operator dry-runs (CPCV, etc.). See docs/STAGING_DATABASE.md.
    staging_database_url: str = Field(
        default="",
        description="Optional PostgreSQL URL for production-shaped staging (e.g. chili_staging on localhost:5433)",
        validation_alias=AliasChoices("STAGING_DATABASE_URL", "staging_database_url"),
    )
    # Pool: brain worker + parallel queue backtests can hold many connections.
    # Pytest has its own much smaller cap below to avoid Windows socket exhaustion.
    database_pool_size: int = Field(
        default=DATABASE_DEFAULT_POOL_SIZE,
        ge=1,
        validation_alias=AliasChoices("DATABASE_POOL_SIZE", "database_pool_size"),
    )
    database_max_overflow: int = Field(
        default=DATABASE_DEFAULT_MAX_OVERFLOW,
        ge=0,
        validation_alias=AliasChoices("DATABASE_MAX_OVERFLOW", "database_max_overflow"),
    )
    database_pool_timeout_seconds: float = Field(
        default=DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
        ge=1.0,
        validation_alias=AliasChoices(
            "DATABASE_POOL_TIMEOUT_SECONDS",
            "database_pool_timeout_seconds",
        ),
    )
    database_idle_in_transaction_timeout_ms: int = Field(
        default=DATABASE_DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS,
        ge=0,
        validation_alias=AliasChoices(
            "DATABASE_IDLE_IN_TRANSACTION_TIMEOUT_MS",
            "database_idle_in_transaction_timeout_ms",
        ),
    )
    database_pytest_pool_size: int = Field(
        default=DATABASE_PYTEST_DEFAULT_POOL_SIZE,
        ge=1,
        validation_alias=AliasChoices(
            "DATABASE_PYTEST_POOL_SIZE",
            "database_pytest_pool_size",
        ),
    )
    database_pytest_max_overflow: int = Field(
        default=DATABASE_PYTEST_DEFAULT_MAX_OVERFLOW,
        ge=0,
        validation_alias=AliasChoices(
            "DATABASE_PYTEST_MAX_OVERFLOW",
            "database_pytest_max_overflow",
        ),
    )
    database_pytest_pool_timeout_seconds: float = Field(
        default=DATABASE_PYTEST_DEFAULT_POOL_TIMEOUT_SECONDS,
        ge=1.0,
        validation_alias=AliasChoices(
            "DATABASE_PYTEST_POOL_TIMEOUT_SECONDS",
            "database_pytest_pool_timeout_seconds",
        ),
    )

    # Optional shared secret so an external Brain UI (different port / origin) can trigger
    # GET/POST /api/v1/brain-next-cycle without chili_device_token. Set in .env as BRAIN_V1_WAKE_SECRET.
    # Send header: X-Chili-Brain-Wake-Secret: <same value>. Use a long random string; never commit it.
    brain_v1_wake_secret: str = ""

    # Brain HTTP service (chili-brain/) â€” when set, workers or scripts can delegate via brain_client
    brain_service_url: str = ""  # e.g. http://brain:8090 (Compose) or http://127.0.0.1:8090
    brain_internal_secret: str = ""  # Bearer token for POST /v1/run-learning-cycle (match CHILI_BRAIN_INTERNAL_SECRET on brain)

    # Standalone brain worker + APScheduler: attribute mined TradingInsights to this user so they
    # appear under GET /api/trading/learn/patterns for that login. When unset, insights use user_id NULL
    # (see api_learned_patterns: global rows are merged for logged-in users).
    brain_default_user_id: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices(
            "CHILI_BRAIN_DEFAULT_USER_ID",
            "BRAIN_DEFAULT_USER_ID",
        ),
    )

    # APScheduler process split: ``all`` (single process), ``web`` (no heavy market scans),
    # ``worker`` (heavy scans + heartbeat only), ``none`` (no scheduler — use with a separate worker).
    # Default ``none`` keeps stray host uvicorn processes from duplicating Docker workers.
    chili_scheduler_role: str = Field(
        default="none",
        validation_alias=AliasChoices("CHILI_SCHEDULER_ROLE"),
    )
    # Set to true in the web (chili) container when a separate scheduler-worker runs APScheduler.
    # Operator readiness will treat web-light jobs as available even though the local role is "none".
    chili_scheduler_runs_externally: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_SCHEDULER_RUNS_EXTERNALLY"),
    )

    # Brain learning worker: UI starts the Docker Compose ``brain-worker`` service (not subprocess).
    brain_worker_compose_service: str = "brain-worker"
    # Optional: restrict to one compose project label (empty = match any project with that service name)
    brain_worker_compose_project: str = ""

    # Coinbase/crypto momentum intelligence (neural mesh only — not learning-cycle).
    chili_momentum_neural_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NEURAL_ENABLED"),
    )
    # Closed-loop automation outcomes → neural evolution (Phase 9; durable rows + viability hints).
    chili_momentum_neural_feedback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NEURAL_FEEDBACK_ENABLED"),
    )
    # Autopilot profitability: pattern/momentum/regime entry gates (paper runner).
    chili_momentum_entry_gates_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_GATES_ENABLED"),
    )
    # Keep parent variant active when refining so paper A/B sessions can run in parallel.
    chili_momentum_ab_test_on_refinement: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AB_TEST_ON_REFINEMENT"),
    )
    # Scale allocator notional by rolling Sharpe-like score from recent outcomes.
    chili_momentum_performance_sizing_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PERFORMANCE_SIZING_ENABLED"),
    )
    # Block entries when family×regime×session history is clearly negative (queries DB).
    chili_momentum_family_regime_prefilter_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FAMILY_REGIME_PREFILTER_ENABLED"),
    )

    # ── Phase 2C: Hebbian plasticity on neural mesh edges ───────────────────
    # Outcome-driven edge-weight updates. Defaults conservative: feature flag OFF,
    # dry_run ON, so even when enabled the first rollout writes audit rows
    # without mutating edge weights. Only flip dry_run=false after shadow-mode
    # validation on 20+ closed trades.
    chili_mesh_plasticity_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MESH_PLASTICITY_ENABLED"),
    )
    chili_mesh_plasticity_dry_run: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MESH_PLASTICITY_DRY_RUN"),
    )
    # Circuit breaker: if an edge has drifted more than this from its pre-live
    # snapshot, refuse further mutations on it and log with reason='drift_cap'.
    # 0.0 disables the check.
    chili_mesh_plasticity_drift_cap: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MESH_PLASTICITY_DRIFT_CAP"),
    )
    chili_mesh_plasticity_learning_rate: float = Field(
        default=0.05,
        ge=0.0, le=0.5,
        validation_alias=AliasChoices("CHILI_MESH_PLASTICITY_LEARNING_RATE"),
    )
    chili_mesh_plasticity_daily_budget: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MESH_PLASTICITY_DAILY_BUDGET"),
    )
    chili_mesh_plasticity_per_edge_cooldown_trades: int = Field(
        default=5,
        ge=0,
        validation_alias=AliasChoices("CHILI_MESH_PLASTICITY_PER_EDGE_COOLDOWN_TRADES"),
    )

    # Robinhood spot venue adapter (execution layer; equities via robin_stocks).
    chili_robinhood_spot_adapter_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED"),
    )
    chili_robinhood_legend_quote_fallback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_LEGEND_QUOTE_FALLBACK_ENABLED"),
    )
    chili_robinhood_legend_quote_max_age_seconds: float = Field(
        default=1200.0,
        ge=30.0,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_LEGEND_QUOTE_MAX_AGE_SECONDS"),
    )
    chili_robinhood_legend_quote_cache_seconds: float = Field(
        default=10.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_LEGEND_QUOTE_CACHE_SECONDS"),
    )
    chili_robinhood_legend_quote_timeout_seconds: float = Field(
        default=8.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_LEGEND_QUOTE_TIMEOUT_SECONDS"),
    )

    # Coinbase spot venue adapter (execution layer; neural momentum may consume readiness only).
    chili_coinbase_spot_adapter_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_COINBASE_SPOT_ADAPTER_ENABLED"),
    )
    chili_coinbase_ws_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_COINBASE_WS_ENABLED"),
    )
    chili_autopilot_price_bus_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOPILOT_PRICE_BUS_ENABLED"),
    )
    chili_coinbase_strict_freshness: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_COINBASE_STRICT_FRESHNESS"),
    )
    chili_coinbase_market_data_max_age_sec: float = Field(
        default=15.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_COINBASE_MARKET_DATA_MAX_AGE_SEC"),
    )

    # Trading automation monitor: optional collapsible HUD on /trading (Phase 5).
    chili_trading_automation_hud_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_TRADING_AUTOMATION_HUD_ENABLED"),
    )

    # Momentum automation risk policy (config-backed; Phase 6 — pre-runner gates).
    chili_momentum_risk_max_daily_loss_usd: float = Field(
        default=250.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_DAILY_LOSS_USD", "CHILI_MOMENTUM_RISK_MAX_DAILY_LOSS_USD"),
    )
    chili_momentum_risk_max_loss_per_trade_usd: float = Field(
        default=50.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_LOSS_PER_TRADE_USD", "CHILI_MOMENTUM_RISK_MAX_LOSS_PER_TRADE_USD"),
    )
    chili_momentum_risk_max_concurrent_sessions: int = Field(
        default=6,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_CONCURRENT_SESSIONS"),
    )
    chili_momentum_risk_max_concurrent_live_sessions: int = Field(
        default=1,
        ge=1,
        le=20,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_CONCURRENT_LIVE_SESSIONS", "CHILI_MOMENTUM_RISK_MAX_CONCURRENT_LIVE_SESSIONS"),
    )
    chili_momentum_risk_max_concurrent_positions: int = Field(
        default=3,
        ge=1,
        le=50,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_CONCURRENT_POSITIONS"),
    )
    chili_momentum_risk_max_notional_per_trade_usd: float = Field(
        default=500.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_NOTIONAL_PER_TRADE_USD"),
    )
    chili_momentum_order_notional_guard_bps: float = Field(
        default=25.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORDER_NOTIONAL_GUARD_BPS"),
        description="Extra bps cushion applied to live market-entry ask when sizing against max notional; 0 disables.",
    )
    chili_momentum_risk_max_position_size_base: float = Field(
        default=1_000_000.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_POSITION_SIZE_BASE"),
    )
    chili_momentum_risk_max_spread_bps_paper: float = Field(
        default=28.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_PAPER"),
    )
    chili_momentum_risk_max_spread_bps_live: float = Field(
        default=12.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_SPREAD_BPS", "CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_LIVE"),
    )
    chili_momentum_risk_max_estimated_slippage_bps: float = Field(
        default=18.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_ESTIMATED_SLIPPAGE_BPS", "CHILI_MOMENTUM_RISK_MAX_ESTIMATED_SLIPPAGE_BPS"),
    )
    chili_momentum_risk_max_fee_to_target_ratio: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_FEE_TO_TARGET_RATIO"),
    )
    chili_momentum_risk_max_hold_seconds: int = Field(
        default=86_400,
        ge=60,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_HOLD_SECONDS", "CHILI_MOMENTUM_RISK_MAX_HOLD_SECONDS"),
    )
    chili_momentum_risk_cooldown_after_stopout_seconds: int = Field(
        default=300,
        ge=0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_COOLDOWN_AFTER_STOPOUT_SECONDS"),
    )
    chili_momentum_risk_cooldown_after_cancel_seconds: int = Field(
        default=60,
        ge=0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_COOLDOWN_AFTER_CANCEL_SECONDS"),
    )
    chili_momentum_risk_viability_max_age_seconds: float = Field(
        default=600.0,
        ge=30.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_VIABILITY_MAX_AGE_SECONDS"),
    )
    chili_momentum_risk_stale_market_data_max_age_sec: float = Field(
        default=30.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_STALE_MARKET_DATA_MAX_AGE_SEC"),
    )
    chili_momentum_risk_require_live_eligible: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_REQUIRE_LIVE_ELIGIBLE"),
    )
    chili_momentum_risk_require_fresh_viability: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_REQUIRE_FRESH_VIABILITY"),
    )
    chili_momentum_risk_require_strict_coinbase_freshness: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_REQUIRE_STRICT_COINBASE_FRESHNESS"),
    )
    chili_momentum_risk_disable_live_if_governance_inhibit: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_DISABLE_LIVE_IF_GOVERNANCE_INHIBIT"),
    )
    chili_momentum_risk_block_paper_when_kill_switch: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_BLOCK_PAPER_WHEN_KILL_SWITCH"),
    )
    chili_momentum_risk_auto_expire_pending_live_arm_seconds: float = Field(
        default=900.0,
        ge=60.0,
        le=86_400.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_AUTO_EXPIRE_PENDING_LIVE_ARM_SECONDS"),
    )

    # Durable venue idempotency (P0.1) — DB-backed client_order_id guard.
    # TTLs are deliberately asymmetric: crypto markets 24/7 so a shorter
    # window suffices; equities can go through a weekend before a retry
    # becomes obviously stale.
    chili_venue_idempotency_ttl_hours_crypto: float = Field(
        default=48.0,
        ge=1.0,
        le=720.0,
        validation_alias=AliasChoices("CHILI_VENUE_IDEMPOTENCY_TTL_HOURS_CRYPTO"),
    )
    chili_venue_idempotency_ttl_hours_equities: float = Field(
        default=168.0,
        ge=1.0,
        le=720.0,
        validation_alias=AliasChoices("CHILI_VENUE_IDEMPOTENCY_TTL_HOURS_EQUITIES"),
    )

    # Global daily-loss halt (P0.2) — single source of truth spanning both
    # AutoTrader v1 and momentum_neural paths. The more conservative of the
    # two limits (usd vs pct-of-equity) wins. Set pct to 0 to disable that leg.
    chili_global_max_daily_loss_usd: float = Field(
        default=300.0,
        ge=0.0,
        le=1_000_000.0,
        validation_alias=AliasChoices("CHILI_GLOBAL_MAX_DAILY_LOSS_USD"),
    )
    chili_global_max_daily_loss_pct_of_equity: float = Field(
        default=0.02,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_GLOBAL_MAX_DAILY_LOSS_PCT_OF_EQUITY"),
    )

    # Cross-process kill switch. API and scheduler run in separate processes,
    # so live paths re-read durable state instead of trusting process memory.
    chili_kill_switch_db_poll_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_KILL_SWITCH_DB_POLL_ENABLED"),
    )
    chili_kill_switch_db_poll_interval_s: float = Field(
        default=0.0,
        ge=0.0,
        le=60.0,
        validation_alias=AliasChoices("CHILI_KILL_SWITCH_DB_POLL_INTERVAL_S"),
    )
    chili_kill_switch_db_fail_closed: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_KILL_SWITCH_DB_FAIL_CLOSED"),
    )

    # Venue order rate limiter (P0.3) — token-bucket per venue wrapping
    # place_* / cancel_order. Defaults are deliberately well below each
    # venue's published cap so a reconciler retry storm can't 429-lock
    # the account. Override via env in prod if needed.
    # Robinhood's equities REST is ~60 req/min in practice; stay conservative.
    chili_venue_rate_limit_rh_orders_per_min: float = Field(
        default=20.0,
        ge=1.0,
        le=300.0,
        validation_alias=AliasChoices("CHILI_VENUE_RATE_LIMIT_RH_ORDERS_PER_MIN"),
    )
    chili_venue_rate_limit_rh_burst: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_VENUE_RATE_LIMIT_RH_BURST"),
    )
    # Coinbase Advanced Trade private endpoints are ~30 req/s per account;
    # orders specifically are lower. Stay comfortably under.
    chili_venue_rate_limit_cb_orders_per_sec: float = Field(
        default=3.0,
        ge=0.1,
        le=30.0,
        validation_alias=AliasChoices("CHILI_VENUE_RATE_LIMIT_CB_ORDERS_PER_SEC"),
    )
    chili_venue_rate_limit_cb_burst: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_VENUE_RATE_LIMIT_CB_BURST"),
    )
    chili_venue_rate_limit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_VENUE_RATE_LIMIT_ENABLED"),
    )

    # Bracket reconciler watchdog (P0.5) — opt-in background scan that fires
    # alerts when a reconciler row is older than the staleness threshold for
    # a trade that still appears to be missing its stop (or carries an
    # orphaned broker stop). Default off during Phase G so the reconciler can
    # accumulate observation data before the watchdog starts paging. Flip on
    # once the reconciler's healthy-state distribution is understood.
    chili_bracket_watchdog_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_BRACKET_WATCHDOG_ENABLED"),
    )
    # Minimum age (seconds) a non-agree reconciler observation must have
    # before the watchdog treats it as stale. Bounded low at 30s to prevent
    # alert-storms from transient mid-fill discrepancies; high at 1 hour.
    chili_bracket_watchdog_stale_after_sec: int = Field(
        default=300,
        ge=30,
        le=3600,
        validation_alias=AliasChoices("CHILI_BRACKET_WATCHDOG_STALE_AFTER_SEC"),
    )

    # Runtime feature-parity assertion (P1.4) — canary that reruns
    # ``indicator_core.compute_all_from_df`` at the moment of a live entry and
    # diffs the snap against whatever the live decision-path supplied. Default
    # off so shipping the module changes nothing until an operator flips the
    # flag. Drift rows persist to ``TradingExecutionEvent`` with
    # ``event_type='feature_parity_drift'`` — same surface as P1.2 rate-limit
    # events, so dashboards can reuse the rolling-window query pattern.
    chili_feature_parity_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_ENABLED"),
    )
    chili_autotrader_live_require_feature_parity: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_LIVE_REQUIRE_FEATURE_PARITY"),
    )
    chili_feature_parity_fail_closed_on_error: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_FAIL_CLOSED_ON_ERROR"),
    )
    # Parity enforcement mode:
    #   "disabled" — assertion is a no-op (belt-and-suspenders on top of
    #                the feature flag above).
    #   "soft"     — records drift + (optionally) alerts, never blocks.
    #   "hard"     — records drift AND blocks the entry when severity ==
    #                critical. Safe to flip once soft-mode telemetry has
    #                sized the critical_mismatch_count floor empirically.
    # Invalid values silently normalize to "soft" inside ``_resolve_settings``
    # so an operator typo can't accidentally disable the canary.
    chili_feature_parity_mode: str = Field(
        default="soft",
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_MODE"),
    )
    # Absolute tolerance for numeric-feature comparison. Pass iff
    # ``abs_delta <= epsilon_abs`` OR the relative-tolerance check passes.
    # OR semantics (not AND) — small-magnitude features like ``bb_pct`` (0..1)
    # need the absolute floor; large-magnitude features like ``price`` need
    # the relative floor. Using AND would reject both edge cases.
    chili_feature_parity_epsilon_abs: float = Field(
        default=1e-6,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_EPSILON_ABS"),
    )
    # Relative tolerance (fraction of reference value). Applied only when
    # the reference is non-zero. Default 0.5% is a middle-of-the-road floor
    # that tolerates timing jitter on a live quote without masking a real
    # feature-builder regression.
    chili_feature_parity_epsilon_rel: float = Field(
        default=0.005,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_EPSILON_REL"),
    )
    # Total-mismatch count at which severity escalates to critical. Any
    # boolean mismatch is ALWAYS critical regardless of this floor — a bool
    # flip is a semantic-contract violation, not a rounding drift.
    chili_feature_parity_critical_mismatch_count: int = Field(
        default=3,
        ge=1,
        le=64,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_CRITICAL_MISMATCH_COUNT"),
    )
    # Operator toggle to suppress warn-level alert noise during shakedown
    # without changing drift persistence. When False, only critical-severity
    # parity events fire an alert; warn rows still persist.
    chili_feature_parity_alert_on_warn: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_ALERT_ON_WARN"),
    )

    # Order state machine (P1.1) — projects broker-native status strings onto
    # a single canonical state per order, writing one transition row per
    # state change to ``trading_order_state_log``. The ``TradingExecutionEvent``
    # stream stays authoritative; this is a second-order projection used by
    # P1.2 venue-health (ack-to-fill P95), cancellation attribution, and
    # dashboards. Enabled by default; set CHILI_ORDER_STATE_MACHINE_ENABLED=false
    # only when deliberately suppressing the projection. Re-read live.
    chili_order_state_machine_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ORDER_STATE_MACHINE_ENABLED"),
    )

    # Venue health circuit breaker (P1.2) — read-only projection over the
    # ``TradingExecutionEvent`` stream plus rate-limit exhaustion events.
    # When a venue's rolling-window health is degraded, entry gates block
    # new orders on that venue (paper-switch optional). Thresholds re-read
    # live so monkeypatch/env changes apply without restart — same pattern
    # as rate_limiter / order_state_machine.
    chili_venue_health_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_ENABLED"),
    )
    # Rolling window (seconds) over which venue-health aggregates latency +
    # error-rate. 5 minutes by default — long enough to smooth per-request
    # jitter, short enough that a recovering venue re-opens quickly.
    chili_venue_health_window_sec: int = Field(
        default=300,
        ge=30,
        le=3600,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_WINDOW_SEC"),
    )
    # Minimum lifecycle samples (any event) in the window before degraded /
    # healthy classification is applied. Below this floor the venue reports
    # ``insufficient_data`` and gates pass through (fail-open).
    chili_venue_health_min_samples: int = Field(
        default=5,
        ge=1,
        le=1000,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_MIN_SAMPLES"),
    )
    # P95 ack-to-fill latency (ms) above which the venue trips degraded.
    # 5000ms is a loose bound — equities typically clear in <200ms; crypto
    # <500ms. Operator dashboards should show the live P95 so this knob can
    # be tightened against observed distribution.
    chili_venue_health_ack_to_fill_p95_ms: int = Field(
        default=5000,
        ge=100,
        le=60000,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_ACK_TO_FILL_P95_MS"),
    )
    # P95 submit-to-ack latency (ms). Lower than ack-to-fill because ack is
    # typically much faster than fill. 3000ms catches network/API issues
    # upstream of matching.
    chili_venue_health_submit_to_ack_p95_ms: int = Field(
        default=3000,
        ge=100,
        le=60000,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_SUBMIT_TO_ACK_P95_MS"),
    )
    # Error-rate threshold (fraction 0..1) — rejection + rate-limit events
    # divided by total lifecycle events in the window. 10% default ensures
    # a venue rejecting every other order trips the breaker.
    chili_venue_health_error_rate_pct: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_ERROR_RATE_PCT"),
    )
    # When True + venue is degraded + the session is live, flip the session
    # to paper mode instead of just blocking. Useful for keeping the
    # decision path alive for analysis without routing to a broken venue.
    chili_venue_health_auto_switch_to_paper: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_AUTO_SWITCH_TO_PAPER"),
    )

    # Walk-forward backtest gate (P1.3) — date-based train/embargo/test
    # window enumeration on top of the existing pattern backtest, aggregated
    # into a single pass/fail verdict threaded into the promotion gate.
    # Tri-state wiring in ``brain_apply_oos_promotion_gate``:
    #   True  → pass-through (continues to other gates)
    #   False → hard reject with status ``rejected_walk_forward``
    #   None  → pending; status ``pending_walk_forward``, allow_active=True
    # Flag defaults OFF and any value is silently ignored when the flag is
    # off — the migration safety guarantee.
    chili_walk_forward_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_ENABLED"),
    )
    # Rolling training window size per fold (days). Default 180 = ~9 months
    # of daily bars; long enough for regime-bridging patterns to train
    # meaningfully. Bounded 30..1825 (1 month .. 5 years).
    chili_walk_forward_train_days: int = Field(
        default=180,
        ge=30,
        le=1825,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_TRAIN_DAYS"),
    )
    # Held-out test window per fold (days). Default 30 ≈ 1 month — one fold
    # covers enough trading days to build a reliable win-rate estimate
    # while still fitting many folds in a reasonable history.
    chili_walk_forward_test_days: int = Field(
        default=30,
        ge=5,
        le=365,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_TEST_DAYS"),
    )
    # Anchor advancement per fold (days). When step_days < test_days,
    # tests overlap — more folds, less statistical independence. Default
    # equal to test_days for non-overlapping clean folds.
    chili_walk_forward_step_days: int = Field(
        default=30,
        ge=1,
        le=365,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_STEP_DAYS"),
    )
    # Embargo gap between train end and test start (days). Neutralizes
    # label leakage when forward-bar targets overlap the train/test
    # boundary. Default 2 days is enough for the typical 5-bar forward
    # window used by pattern imminent-alert models.
    chili_walk_forward_embargo_days: int = Field(
        default=2,
        ge=0,
        le=30,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_EMBARGO_DAYS"),
    )
    # Minimum successful folds required before the gate emits a True/False
    # verdict — fewer than this auto-fails regardless of fold outcomes so
    # a single lucky fold cannot promote a pattern.
    chili_walk_forward_min_folds: int = Field(
        default=3,
        ge=2,
        le=24,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_MIN_FOLDS"),
    )
    # Per-fold win-rate floor — a fold passes its local gate only if its
    # test-set win rate is >= this. 0.45 roughly corresponds to "a bit
    # better than coin flip after fees" for 2-bar momentum patterns.
    chili_walk_forward_min_fold_win_rate: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_MIN_FOLD_WIN_RATE"),
    )
    # Fraction of folds that must individually pass for the overall gate
    # to pass. 0.6 = 3/5 folds under defaults — a genuine majority.
    chili_walk_forward_min_pass_fraction: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_MIN_PASS_FRACTION"),
    )

    # Autopilot mutual exclusion (P0.4) — only one autopilot path may "own"
    # entries for a given symbol at a time. The primary is authoritative;
    # the non-primary is read-only/analysis until promoted. Both paths
    # check this gate before placing entry orders; exits are always allowed
    # regardless (once open, a position must be closeable). Values:
    #   "momentum_neural" | "auto_trader_v1"
    # An unknown / empty value disables primary preference (but the per-symbol
    # lease still fires, so concurrent entries on the same symbol are blocked).
    chili_autopilot_primary: str = Field(
        default="momentum_neural",
        validation_alias=AliasChoices("CHILI_AUTOPILOT_PRIMARY"),
    )
    # When True, entry attempts from the non-primary autopilot are blocked
    # even when the symbol has no active lease holder (strict primary mode).
    # When False, the non-primary may enter a symbol that has no owner, but
    # is still blocked from overlapping an existing lease holder.
    chili_autopilot_strict_primary: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOPILOT_STRICT_PRIMARY"),
    )

    # P0.5 — Bracket reconciler watchdog: alert when an open live trade's most
    # recent reconciliation kind is missing_stop / orphan_stop and the observation
    # is older than this threshold. Since RH has no native brackets, a live
    # unprotected position is a critical operator signal.
    chili_bracket_watchdog_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_BRACKET_WATCHDOG_ENABLED"),
    )
    chili_bracket_watchdog_stale_after_sec: int = Field(
        default=300,
        validation_alias=AliasChoices("CHILI_BRACKET_WATCHDOG_STALE_AFTER_SEC"),
    )

    # P1.1 — formal order state machine. When enabled, venue adapters and the
    # execution-audit event stream project broker-native statuses onto the
    # canonical DRAFT/SUBMITTING/ACK/PARTIAL/FILLED/CANCELLED/REJECTED/EXPIRED
    # states and write one row per transition to ``trading_order_state_log``.
    # Enabled by default now that execution-audit feedback depends on the
    # projection. Disabled mode is still a hard no-op: no rows are written,
    # callers receive ``reason='disabled'``.
    chili_order_state_machine_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ORDER_STATE_MACHINE_ENABLED"),
    )

    # P1.2 — venue health + circuit breaker. When enabled, ``venue_health``
    # rolls up per-venue ack-to-fill latency + error/rate-limit rates over
    # 1m/5m/1h windows and can flip a venue to "degraded" when thresholds
    # are breached. The gate blocks new entries (AutoTrader v1 + momentum
    # neural live_runner) without killing open positions. Off by default;
    # flip on once P1.1 has accumulated a week of canonical transitions.
    chili_venue_health_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_ENABLED"),
    )
    # Primary rolling window (seconds) used by ``is_venue_degraded``.
    # 300 = 5 minutes — long enough to denoise a transient spike, short
    # enough that one-off incidents don't linger past recovery.
    chili_venue_health_window_sec: int = Field(
        default=300,
        ge=60,
        le=3600,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_WINDOW_SEC"),
    )
    # Minimum sample count required before the breaker can fire. Below this
    # the venue is reported ``healthy`` regardless of thresholds so we don't
    # trip on a single unlucky ack.
    chili_venue_health_min_samples: int = Field(
        default=5,
        ge=1,
        le=500,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_MIN_SAMPLES"),
    )
    # P95 ack-to-fill latency threshold (ms) — above this the venue is
    # considered degraded. Defaults: coinbase crypto fills should clear in
    # well under 5s; robinhood equities comparable on regular market hours.
    chili_venue_health_ack_to_fill_p95_ms: int = Field(
        default=5000,
        ge=100,
        le=120000,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_ACK_TO_FILL_P95_MS"),
    )
    # P95 submit-to-ack latency threshold (ms) — catches slow broker REST.
    chili_venue_health_submit_to_ack_p95_ms: int = Field(
        default=3000,
        ge=50,
        le=60000,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_SUBMIT_TO_ACK_P95_MS"),
    )
    # Error-rate threshold as a fraction (0.0–1.0). Reject + rate_limit
    # events divided by all lifecycle events in the window. 10% = clearly
    # unhealthy; below 5% is normal noise (cancel-after-ack etc).
    chili_venue_health_error_rate_pct: float = Field(
        default=0.10,
        ge=0.01,
        le=1.0,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_ERROR_RATE_PCT"),
    )
    # When degraded, should momentum_neural live sessions auto-switch to
    # paper mode? False = block entry and stay in WATCHING_LIVE (retry on
    # next pulse). True = flip session.mode to paper so the session stays
    # productive instead of stalling. Conservative default: block only.
    chili_venue_health_auto_switch_to_paper: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_VENUE_HEALTH_AUTO_SWITCH_TO_PAPER"),
    )

    # P1.3 — date-based walk-forward backtest. Unlike a single %-holdout
    # OOS split, walk-forward slices the dataset into rolling (train,
    # test) windows so a pattern must stay profitable across multiple
    # independently-held-out periods. With an embargo day gap between
    # train-end and test-start, triple-barrier labels can't leak a
    # single-bar future return across the boundary.
    chili_walk_forward_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_ENABLED"),
    )
    # Length of each fold's train window (bars treated as days for 1d
    # data). 180d = 6 months of training → stable parameter estimates.
    chili_walk_forward_train_days: int = Field(
        default=180,
        ge=30,
        le=1825,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_TRAIN_DAYS"),
    )
    # Length of each fold's test window. 30d = 1 month of held-out
    # evaluation → 10-20 trades on a typical daily pattern.
    chili_walk_forward_test_days: int = Field(
        default=30,
        ge=5,
        le=365,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_TEST_DAYS"),
    )
    # Step between fold start dates. 30d = non-overlapping test
    # windows; <test_days means overlapping tests (bootstrap-like).
    chili_walk_forward_step_days: int = Field(
        default=30,
        ge=1,
        le=365,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_STEP_DAYS"),
    )
    # Embargo between train-end and test-start. Required for daily
    # patterns because triple-barrier labels reach ``max_bars`` forward
    # — a 2-day embargo eliminates single-bar leakage. Set to 5+ for
    # patterns with longer look-ahead (e.g. weekly exits).
    chili_walk_forward_embargo_days: int = Field(
        default=2,
        ge=0,
        le=30,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_EMBARGO_DAYS"),
    )
    # Minimum number of completed folds required before the gate can
    # fire. Fewer folds = too noisy to trust. 3 folds = minimum for any
    # real statistical claim about pattern robustness.
    chili_walk_forward_min_folds: int = Field(
        default=3,
        ge=2,
        le=24,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_MIN_FOLDS"),
    )
    # Per-fold pass threshold: a fold passes iff its test win-rate
    # meets this floor. 0.45 = baseline "not worse than coin-flip after
    # friction" for a 1:1 RR pattern.
    chili_walk_forward_min_fold_win_rate: float = Field(
        default=0.45,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_MIN_FOLD_WIN_RATE"),
    )
    # Fraction of folds that must pass the per-fold threshold for the
    # overall pattern to pass walk-forward. 0.6 = 3-of-5 / 6-of-10
    # folds must pass — tight enough to reject regime-dependent edges.
    chili_walk_forward_min_pass_fraction: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_WALK_FORWARD_MIN_PASS_FRACTION"),
    )

    # ── P1.4 — runtime feature-parity assertion at entry ────────────────────
    # Verifies at entry time that the feature vector used by the decision
    # matches what indicator_core.compute_all_from_df produces on the same
    # OHLCV frame. Catches regressions where the live path diverges from the
    # canonical backtest compute surface (missing features, rounding drift,
    # stale caches). Soft by default — logs + alerts + records to the
    # TradingExecutionEvent stream with event_type='feature_parity_drift'
    # without blocking entry. Flip to 'hard' to block critical drift.
    chili_feature_parity_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_ENABLED"),
    )
    # 'disabled' | 'soft' | 'hard'. Soft records + alerts but never blocks.
    # Hard blocks entry when severity == 'critical'. 2-week shakedown in soft
    # mode is the intended path before flipping to hard.
    chili_feature_parity_mode: str = Field(
        default="soft",
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_MODE"),
    )
    # Absolute tolerance for numeric feature comparison. Smaller-than-this
    # deltas are considered exact.
    chili_feature_parity_epsilon_abs: float = Field(
        default=1e-6,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_EPSILON_ABS"),
    )
    # Relative tolerance for numeric feature comparison. 0.005 = 0.5%.
    # Applies only when the reference value is non-zero.
    chili_feature_parity_epsilon_rel: float = Field(
        default=0.005,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_EPSILON_REL"),
    )
    # Number of feature mismatches at/above which the overall severity is
    # critical (in addition to any boolean mismatch, which is always critical).
    chili_feature_parity_critical_mismatch_count: int = Field(
        default=3,
        ge=1,
        le=64,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_CRITICAL_MISMATCH_COUNT"),
    )
    # Whether to emit alerts.dispatch_alert on warn+ severity. Off for tests;
    # operators can toggle off if alert noise exceeds tolerance during
    # shakedown.
    chili_feature_parity_alert_on_warn: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_ALERT_ON_WARN"),
    )

    # Phase 7 — simulated paper automation runner (no live orders).
    chili_momentum_paper_runner_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PAPER_RUNNER_ENABLED"),
    )
    chili_momentum_paper_runner_scheduler_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_ENABLED"),
    )
    chili_momentum_paper_runner_dev_tick_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PAPER_RUNNER_DEV_TICK_ENABLED"),
    )

    # Phase 8 — guarded live Coinbase spot runner (real orders; off by default).
    chili_momentum_live_runner_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_RUNNER_ENABLED"),
    )
    chili_momentum_live_runner_scheduler_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_ENABLED"),
    )
    chili_momentum_live_runner_dev_tick_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_RUNNER_DEV_TICK_ENABLED"),
    )
    # APScheduler interval when paper/live runner batch jobs are registered (minutes; jobs still require *_scheduler_enabled).
    chili_momentum_paper_runner_scheduler_interval_minutes: int = Field(
        default=3,
        ge=2,
        le=1440,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PAPER_RUNNER_SCHEDULER_INTERVAL_MINUTES"),
    )
    chili_momentum_live_runner_scheduler_interval_minutes: int = Field(
        default=2,
        ge=2,
        le=1440,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_INTERVAL_MINUTES"),
    )

    chili_auto_execute_stops: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTO_EXECUTE_STOPS"),
    )

    # AutoTrader v1 — pattern-imminent → rules + LLM gate → RH equities (see app/services/trading/auto_trader.py)
    chili_autotrader_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_ENABLED"),
    )
    chili_autotrader_live_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_LIVE_ENABLED"),
    )
    # f-add-paper-shadow-mode (2026-05-06): when True, every autotrader
    # live decision (placed / blocked / skipped) ALSO opens a paper-shadow
    # trade tagged with ``paper_shadow_of_alert_id``. Used to measure
    # execution-alpha-drag, provide pure-strategy pattern evidence, and
    # unstarve brain learning during low-live-placement-rate periods.
    # Default off; opt-in only. Shadow-promoted evidence may also use this
    # path while live orders are disabled so learning can keep collecting
    # samples without turning broker execution on.
    chili_autotrader_paper_shadow_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PAPER_SHADOW_ENABLED"),
    )
    chili_autotrader_paper_shadow_max_open: int = Field(
        default=AUTOTRADER_PAPER_SHADOW_DEFAULT_MAX_OPEN,
        ge=1,
        le=AUTOTRADER_PAPER_SHADOW_MAX_OPEN_CONFIG_LIMIT,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PAPER_SHADOW_MAX_OPEN"),
    )
    chili_autotrader_paper_shadow_janitor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PAPER_SHADOW_JANITOR_ENABLED"),
    )
    chili_autotrader_paper_shadow_janitor_max_age_hours: int = Field(
        default=AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_MAX_AGE_HOURS,
        ge=1,
        le=AUTOTRADER_PAPER_SHADOW_MAX_JANITOR_MAX_AGE_HOURS,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PAPER_SHADOW_JANITOR_MAX_AGE_HOURS"),
    )
    chili_autotrader_paper_shadow_janitor_buffer: int = Field(
        default=AUTOTRADER_PAPER_SHADOW_DEFAULT_JANITOR_BUFFER,
        ge=0,
        le=AUTOTRADER_PAPER_SHADOW_MAX_JANITOR_BUFFER,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PAPER_SHADOW_JANITOR_BUFFER"),
    )
    chili_autotrader_paper_shadow_capacity_evict_youngest_first: bool = Field(
        default=AUTOTRADER_PAPER_SHADOW_DEFAULT_CAPACITY_EVICT_YOUNGEST_FIRST,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAPER_SHADOW_CAPACITY_EVICT_YOUNGEST_FIRST"
        ),
    )
    # Open paper-shadow evidence for live candidates that are blocked by
    # portfolio/execution authority gates such as recert debt or venue caps,
    # plus explicitly allowlisted reject classes such as no-edge and duplicate
    # same-pattern alerts. This never places a broker order; it exists to speed
    # learning and false-negative audits without loosening live gates.
    chili_autotrader_paper_shadow_qualified_blocks_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PAPER_SHADOW_QUALIFIED_BLOCKS_ENABLED"),
    )
    chili_autotrader_paper_shadow_reject_allow_duplicate_open: bool = Field(
        default=AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_ALLOW_DUPLICATE_OPEN,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAPER_SHADOW_REJECT_ALLOW_DUPLICATE_OPEN"
        ),
    )
    chili_autotrader_paper_shadow_reject_lightweight_sizing_enabled: bool = Field(
        default=AUTOTRADER_PAPER_SHADOW_DEFAULT_REJECT_LIGHTWEIGHT_SIZING_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAPER_SHADOW_REJECT_LIGHTWEIGHT_SIZING_ENABLED"
        ),
        description=(
            "Use broker-free lightweight sizing for learning-only paper shadows "
            "created from live rejects. Disable only when reject observations "
            "must mirror full entry risk sizing."
        ),
    )
    chili_autotrader_paper_shadow_dedupe_same_alert_reason_family: bool = Field(
        default=AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_SAME_ALERT_REASON_FAMILY,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAPER_SHADOW_DEDUPE_SAME_ALERT_REASON_FAMILY"
        ),
    )
    chili_autotrader_paper_shadow_dedupe_recent_reason_family_minutes: int = Field(
        default=AUTOTRADER_PAPER_SHADOW_DEFAULT_DEDUPE_RECENT_REASON_FAMILY_MINUTES,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAPER_SHADOW_DEDUPE_RECENT_REASON_FAMILY_MINUTES"
        ),
    )
    chili_autotrader_paper_shadow_queue_pressure_suppression_floor: float = Field(
        default=AUTOTRADER_PAPER_SHADOW_QUEUE_SUPPRESSION_DEFAULT_PRESSURE,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAPER_SHADOW_QUEUE_PRESSURE_SUPPRESSION_FLOOR"
        ),
    )
    chili_autotrader_broker_reject_suppression_enabled: bool = Field(
        default=AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_BROKER_REJECT_SUPPRESSION_ENABLED"
        ),
    )
    chili_autotrader_broker_reject_suppression_minutes: int = Field(
        default=AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_MINUTES,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_BROKER_REJECT_SUPPRESSION_MINUTES"
        ),
        description=(
            "Cooldown window for suppressing repeated broker submissions with "
            "the same action fingerprint after a broker reject."
        ),
    )
    chili_autotrader_broker_reject_suppression_threshold: int = Field(
        default=AUTOTRADER_BROKER_REJECT_SUPPRESSION_DEFAULT_THRESHOLD,
        ge=1,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_BROKER_REJECT_SUPPRESSION_THRESHOLD"
        ),
    )
    # Paper-shadow evidence should be scored against the same kind of dynamic
    # position management used live, not only against the original stop/target.
    # This lightweight overlay lets autotrader-tagged PaperTrade rows react to
    # pattern-monitor risk states (exit_now / tighten_stop) before static exits.
    chili_autotrader_paper_dynamic_monitor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PAPER_DYNAMIC_MONITOR_ENABLED"),
    )
    chili_autotrader_paper_dynamic_monitor_cooldown_minutes: int = Field(
        default=AUTOTRADER_PAPER_DYNAMIC_DEFAULT_MONITOR_COOLDOWN_MINUTES,
        ge=0,
        le=AUTOTRADER_PAPER_DYNAMIC_MAX_MONITOR_COOLDOWN_MINUTES,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PAPER_DYNAMIC_MONITOR_COOLDOWN_MINUTES"),
    )
    # Managed-exit edge overlay. Some scanner brackets are intentionally wide
    # hard plans, especially on crypto, while the live/paper monitors often
    # harvest earlier MFE and tighten risk. This gate can evaluate that
    # managed geometry from directional MFE/MAE evidence, but still requires a
    # positive expected net edge and records the original full-bracket verdict.
    chili_autotrader_managed_edge_mode: str = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_MODE,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MANAGED_EDGE_MODE"),
    )
    chili_autotrader_managed_edge_asset_types: str = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_ASSET_TYPES,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MANAGED_EDGE_ASSET_TYPES"),
    )
    chili_autotrader_managed_edge_min_directional_samples: int = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_DIRECTIONAL_SAMPLES,
        ge=1,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_MANAGED_EDGE_MIN_DIRECTIONAL_SAMPLES"
        ),
    )
    chili_autotrader_managed_edge_capture_fraction: float = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_CAPTURE_FRACTION,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MANAGED_EDGE_CAPTURE_FRACTION"),
    )
    chili_autotrader_managed_edge_adverse_buffer: float = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_ADVERSE_BUFFER,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MANAGED_EDGE_ADVERSE_BUFFER"),
    )
    chili_autotrader_managed_edge_static_to_managed_reward_ratio: float = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_STATIC_TO_MANAGED_REWARD_RATIO,
        ge=1.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_MANAGED_EDGE_STATIC_TO_MANAGED_REWARD_RATIO"
        ),
    )
    chili_autotrader_managed_edge_min_reward_fraction: float = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_REWARD_FRACTION,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MANAGED_EDGE_MIN_REWARD_FRACTION"),
    )
    chili_autotrader_managed_edge_max_reward_fraction: float = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_MAX_REWARD_FRACTION,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MANAGED_EDGE_MAX_REWARD_FRACTION"),
    )
    chili_autotrader_managed_edge_min_reward_risk: float = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_REWARD_RISK,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MANAGED_EDGE_MIN_REWARD_RISK"),
    )
    chili_autotrader_managed_edge_min_expected_net_pct: float = Field(
        default=AUTOTRADER_MANAGED_EDGE_DEFAULT_MIN_EXPECTED_NET_PCT,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MANAGED_EDGE_MIN_EXPECTED_NET_PCT"),
    )
    chili_autotrader_min_expected_net_after_empirical_cost_pct: float = Field(
        default=AUTOTRADER_EDGE_DEFAULT_MIN_EXPECTED_NET_AFTER_EMPIRICAL_COST_PCT,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_MIN_EXPECTED_NET_AFTER_EMPIRICAL_COST_PCT"
        ),
    )
    chili_autotrader_stock_max_execution_stop_loss_pct: float = Field(
        default=30.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_STOCK_MAX_EXECUTION_STOP_LOSS_PCT"),
        description="Max stock stop distance the live executor may use; 0 disables the cap.",
    )
    chili_autotrader_crypto_max_execution_stop_loss_pct: float = Field(
        default=60.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_CRYPTO_MAX_EXECUTION_STOP_LOSS_PCT"),
        description="Max crypto stop distance the live executor may use; 0 disables the cap.",
    )
    chili_autotrader_options_max_execution_stop_loss_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_OPTIONS_MAX_EXECUTION_STOP_LOSS_PCT"),
        description="Max options stop distance the live executor may use; 0 disables the cap.",
    )
    chili_autotrader_directional_probability_z: float = Field(
        default=AUTOTRADER_DIRECTIONAL_PROBABILITY_DEFAULT_Z,
        ge=0.0,
        le=AUTOTRADER_DIRECTIONAL_PROBABILITY_MAX_Z,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_DIRECTIONAL_PROBABILITY_Z"),
    )
    chili_autotrader_directional_probability_max_rows: int = Field(
        default=AUTOTRADER_DIRECTIONAL_PROBABILITY_DEFAULT_MAX_ROWS,
        ge=AUTOTRADER_DIRECTIONAL_PROBABILITY_MIN_ROWS,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_DIRECTIONAL_PROBABILITY_MAX_ROWS"
        ),
    )
    # f-handler-live-drift + f-handler-execution-robustness (Phase 2
    # #8/#9, 2026-05-06): trade-close-driven observability. Both share
    # the trade-close batch size with demote/regime_ledger via
    # brain_work_trade_close_batch_size; these settings are reserved
    # for future per-handler throttling if drift/robustness become hot.
    brain_work_live_drift_batch_size: int = 2
    brain_work_execution_robustness_batch_size: int = 2
    chili_autotrader_user_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_USER_ID"),
    )
    chili_autotrader_per_trade_notional_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PER_TRADE_NOTIONAL_USD"),
        description=(
            "Deprecated explicit dollar fallback for autotrader entries. "
            "Leave at 0 so sizing comes from equity risk budget + dial."
        ),
    )
    chili_autotrader_per_trade_risk_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PER_TRADE_RISK_PCT"),
        description="Percent of effective account equity allocated before adaptive sizing overlays.",
    )
    chili_autotrader_candidate_batch_size: int = Field(
        default=AUTOTRADER_DEFAULT_CANDIDATE_BATCH_SIZE,
        ge=1,
        le=AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_CANDIDATE_BATCH_SIZE"),
        description="Maximum AutoTrader alerts processed in one tick.",
    )
    chili_autotrader_fresh_candidate_fastlane_enabled: bool = Field(
        default=AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_FRESH_CANDIDATE_FASTLANE_ENABLED"
        ),
        description=(
            "When true, very fresh imminent alerts are pulled to the front of "
            "the AutoTrader batch so execution-sensitive candidates do not sit "
            "behind older, likely-stale alerts."
        ),
    )
    chili_autotrader_fresh_candidate_fastlane_max_age_seconds: int = Field(
        default=AUTOTRADER_FRESH_CANDIDATE_FASTLANE_DEFAULT_MAX_AGE_SECONDS,
        ge=AUTOTRADER_DEFAULT_TICK_INTERVAL_SECONDS,
        le=SECONDS_PER_MINUTE * 5,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_FRESH_CANDIDATE_FASTLANE_MAX_AGE_SECONDS"
        ),
        description=(
            "Freshness window used by the AutoTrader candidate fast lane. "
            "Older unprocessed alerts remain eligible after the fresh queue."
        ),
    )
    chili_autotrader_fresh_candidate_burst_enabled: bool = Field(
        default=AUTOTRADER_FRESH_CANDIDATE_BURST_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_FRESH_CANDIDATE_BURST_ENABLED"
        ),
        description=(
            "When true, AutoTrader expands the selected batch only for the "
            "fresh-candidate fast lane, sized from the freshness window and "
            "scheduler cadence, so alert bursts are evaluated before they age "
            "into slippage without increasing live-trading eligibility."
        ),
    )
    chili_autotrader_stale_candidate_sweep_interval_seconds: int = Field(
        default=AUTOTRADER_STALE_CANDIDATE_SWEEP_DEFAULT_SECONDS,
        ge=0,
        le=AUTOTRADER_STALE_CANDIDATE_SWEEP_MAX_SECONDS,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_STALE_CANDIDATE_SWEEP_INTERVAL_SECONDS"
        ),
        description=(
            "Minimum interval between AutoTrader probes of older non-fresh "
            "imminent alerts. Fresh alerts are still checked every tick; the "
            "older backlog is sampled on cadence so stale rows do not add "
            "latency to execution-sensitive fresh entries. Set to 0 to probe "
            "older rows every tick."
        ),
    )
    chili_autotrader_non_stock_candidate_max_age_minutes: int = Field(
        default=AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES,
        ge=0,
        le=AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_MAX_MINUTES,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_NON_STOCK_CANDIDATE_MAX_AGE_MINUTES"
        ),
        description=(
            "Maximum age for non-stock pattern-imminent alerts considered by "
            "AutoTrader. Crypto/options alerts older than this should be "
            "refreshed by the scanner before any execution decision. Set to 0 "
            "to preserve the historical unbounded backlog sweep."
        ),
    )
    chili_autotrader_stock_candidate_max_age_minutes: int = Field(
        default=AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_DEFAULT_MINUTES,
        ge=0,
        le=AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_MAX_MINUTES,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_STOCK_CANDIDATE_MAX_AGE_MINUTES"),
        description=(
            "Maximum age for stock pattern-imminent alerts considered by "
            "AutoTrader while the market is open. Older stock rows remain "
            "available for monitor/evidence flows, but the scanner should "
            "refresh them before any broker execution decision. Set to 0 to "
            "preserve the historical unbounded stock backlog sweep."
        ),
    )
    chili_autotrader_candidate_price_prefetch_enabled: bool = Field(
        default=AUTOTRADER_CANDIDATE_PRICE_PREFETCH_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_CANDIDATE_PRICE_PREFETCH_ENABLED"
        ),
        description=(
            "When true, AutoTrader batch-prefetches current quotes for selected "
            "candidates and reuses them inside the rule gate, reducing per-alert "
            "market-data latency without weakening execution gates."
        ),
    )
    chili_autotrader_stock_momentum_context_gate_enabled: bool = Field(
        default=AUTOTRADER_STOCK_MOMENTUM_CONTEXT_GATE_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_STOCK_MOMENTUM_CONTEXT_GATE_ENABLED"
        ),
        description=(
            "When true, stock AutoTrader entries must carry gap-up and "
            "relative-volume evidence once the selected candidate lane is full. "
            "This is a restrictive quality gate; broker, risk, and expected-edge "
            "checks still run for candidates that pass it."
        ),
    )
    chili_autotrader_stock_momentum_context_min_queue_pressure: float = Field(
        default=AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_QUEUE_PRESSURE_DEFAULT,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_QUEUE_PRESSURE"
        ),
        description=(
            "Candidate-lane pressure required before the stock momentum-context "
            "gate activates. A full selected batch is 1.0."
        ),
    )
    chili_autotrader_stock_momentum_context_min_gap_pct: float = Field(
        default=AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_GAP_PCT_DEFAULT,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_GAP_PCT"
        ),
        description=(
            "Minimum stock gap/change percent required by the pressure-activated "
            "momentum-context gate."
        ),
    )
    chili_autotrader_stock_momentum_context_min_volume_ratio: float = Field(
        default=AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_VOLUME_RATIO_DEFAULT,
        ge=0.0,
        le=1000.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_STOCK_MOMENTUM_CONTEXT_MIN_VOLUME_RATIO"
        ),
        description=(
            "Minimum relative-volume or volume-ratio evidence required by the "
            "pressure-activated stock momentum-context gate."
        ),
    )
    chili_autotrader_cost_gate_repeat_suppression_enabled: bool = Field(
        default=AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_ENABLED"
        ),
        description=(
            "When true, AutoTrader may record a repeat cost-gate block before "
            "expensive quote/gate work when recent same-pattern evidence shows "
            "the edge is still materially below Coinbase costs."
        ),
    )
    chili_autotrader_cost_gate_repeat_suppression_minutes: int = Field(
        default=AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_DEFAULT_MINUTES,
        ge=1,
        le=AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_MAX_MINUTES,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_MINUTES"
        ),
        description=(
            "Base recent-block window. The effective window expands only with "
            "quote-prefetch misses, queue pressure, or tick-budget pressure."
        ),
    )
    chili_autotrader_cost_gate_repeat_suppression_min_edge_gap_bps: int = Field(
        default=AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_DEFAULT_MIN_EDGE_GAP_BPS,
        ge=1,
        le=2000,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_MIN_EDGE_GAP_BPS"
        ),
        description=(
            "Minimum prior cost-gate edge shortfall required before suppressing "
            "a repeat Coinbase cost-gate evaluation."
        ),
    )
    chili_autotrader_cost_gate_repeat_suppression_min_queue_pressure: float = Field(
        default=AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_DEFAULT_QUEUE_PRESSURE,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_COST_GATE_REPEAT_SUPPRESSION_MIN_QUEUE_PRESSURE"
        ),
        description=(
            "Candidate-lane pressure that allows repeat cost-gate suppression "
            "even when quote prefetch succeeded."
        ),
    )
    chili_autotrader_stock_session_defer_enabled: bool = Field(
        default=AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_ENABLED,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_STOCK_SESSION_DEFER_ENABLED"),
        description=(
            "When true, fresh stock alerts are left unconsumed while the "
            "configured stock execution session is closed, so they can be "
            "revalidated when trading reopens instead of being burned by an "
            "outside-hours skip."
        ),
    )
    chili_autotrader_stock_session_defer_max_age_hours: float = Field(
        default=AUTOTRADER_STOCK_SESSION_DEFER_DEFAULT_MAX_AGE_HOURS,
        ge=0.0,
        le=AUTOTRADER_PAPER_SHADOW_MAX_JANITOR_MAX_AGE_HOURS,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_STOCK_SESSION_DEFER_MAX_AGE_HOURS"
        ),
        description=(
            "Maximum age for an unprocessed deferred stock alert. The default "
            "tracks the directional-outcome hold horizon; older alerts are "
            "ignored by the AutoTrader candidate selector and must be refreshed "
            "by the scanner."
        ),
    )
    chili_autotrader_synergy_scale_notional_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SYNERGY_SCALE_NOTIONAL_USD"),
        description=(
            "Explicit dollar add-on for scale-ins. Leave at 0 unless the "
            "operator intentionally enables synergy sizing."
        ),
    )
    chili_autotrader_synergy_fraction: float = Field(
        default=AUTOTRADER_SYNERGY_DEFAULT_FRACTION,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SYNERGY_FRACTION"),
        description=(
            "Default scale-in fraction of the existing position when the "
            "explicit synergy add-on is left at 0."
        ),
    )
    chili_autotrader_synergy_max_notional_usd: float = Field(
        default=AUTOTRADER_SYNERGY_DEFAULT_MAX_NOTIONAL_USD,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SYNERGY_MAX_NOTIONAL_USD"),
        description=(
            "Small-dollar cap for fraction-based synergy scale-ins. Set 0 "
            "only for an intentional uncapped paper/live soak."
        ),
    )
    chili_autotrader_synergy_max_scale_ins_per_trade: int = Field(
        default=AUTOTRADER_SYNERGY_DEFAULT_MAX_SCALE_INS_PER_TRADE,
        ge=0,
        le=AUTOTRADER_SYNERGY_MAX_SCALE_INS_CONFIG_LIMIT,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SYNERGY_MAX_SCALE_INS_PER_TRADE"
        ),
        description=(
            "Maximum distinct confirming-pattern scale-ins allowed per open trade. "
            "The default is derived from the scale-in fraction and total add budget; "
            "set 0 to disable synergy scale-ins."
        ),
    )
    chili_autotrader_synergy_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SYNERGY_ENABLED"),
    )
    chili_autotrader_synergy_retry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SYNERGY_RETRY_ENABLED"),
        description=(
            "Allow spare AutoTrader batch slots to re-evaluate recent "
            "synergy_not_applicable alerts after scale-in policy changes."
        ),
    )
    chili_autotrader_synergy_retry_lookback_minutes: int = Field(
        default=AUTOTRADER_SYNERGY_RETRY_DEFAULT_LOOKBACK_MINUTES,
        ge=AUTOTRADER_SYNERGY_RETRY_MIN_LOOKBACK_MINUTES,
        le=AUTOTRADER_SYNERGY_RETRY_MAX_LOOKBACK_MINUTES,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SYNERGY_RETRY_LOOKBACK_MINUTES"
        ),
        description=(
            "Lookback window for one-shot synergy retry candidates, expressed "
            "as a named multiple of the imminent scanner cadence."
        ),
    )
    chili_autotrader_synergy_retry_max_per_tick: int = Field(
        default=AUTOTRADER_SYNERGY_RETRY_DEFAULT_MAX_PER_TICK,
        ge=0,
        le=AUTOTRADER_MAX_CANDIDATE_BATCH_SIZE,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SYNERGY_RETRY_MAX_PER_TICK"
        ),
        description="Maximum synergy retry candidates allowed to fill spare tick slots.",
    )
    chili_autotrader_daily_loss_cap_usd: float = Field(
        default=150.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_DAILY_LOSS_CAP_USD"),
    )
    chili_autotrader_daily_loss_cap_pct: float = Field(
        default=1.5,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_DAILY_LOSS_CAP_PCT"),
        description="Percent-of-proven-equity autotrader daily loss cap before falling back to the static USD cap.",
    )
    # VV — legacy global concurrency cap. Kept as the outer-safety
    # ceiling on the SUM of all open autotrader-v1 positions across all
    # lanes (equity + crypto + options). Per-lane caps live in the three
    # ``chili_autotrader_max_concurrent_<lane>`` fields below and are
    # registered in the ``strategy_parameter`` ledger so the brain can
    # adapt them. Default 60 = 3 lanes × 20 (each lane's bootstrap).
    chili_autotrader_max_concurrent: int = Field(
        default=60,
        ge=1,
        le=500,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_CONCURRENT"),
    )
    # VV — per-lane caps. These are bootstrap values; on first
    # ``passes_rule_gate`` call the rule gate registers each as a
    # ``strategy_parameter`` row (family='autotrader_concurrency',
    # key='max_concurrent_<lane>') and reads the learned current_value
    # back. Operator can hand-edit current_value via a SQL update or
    # via the Brain UI; brain learner adapts it from realized outcomes
    # when CHILI_STRATEGY_PARAMETER_LEARNING_ENABLED is on.
    chili_autotrader_max_concurrent_equity: int = Field(
        default=20,
        ge=1,
        le=200,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_CONCURRENT_EQUITY"),
    )
    chili_autotrader_max_concurrent_crypto: int = Field(
        default=20,
        ge=1,
        le=200,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_CONCURRENT_CRYPTO"),
    )
    chili_autotrader_max_concurrent_options: int = Field(
        default=20,
        ge=1,
        le=200,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_CONCURRENT_OPTIONS"),
    )
    chili_autotrader_confidence_floor: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_CONFIDENCE_FLOOR"),
    )
    chili_autotrader_min_projected_profit_pct: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MIN_PROJECTED_PROFIT_PCT"),
        description=(
            "Deprecated legacy projected-profit floor. Current entry admission "
            "uses expected net edge from reward, stop risk, probability, and TCA."
        ),
    )
    chili_autotrader_max_symbol_price_usd: float = Field(
        default=AUTOTRADER_LEGACY_MAX_SYMBOL_PRICE_DEFAULT_USD,
        ge=0.01,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_SYMBOL_PRICE_USD"),
        description=(
            "Legacy whole-share safety cap for stock entries. When "
            "CHILI_AUTOTRADER_FRACTIONAL_EQUITY_ENABLED is true, stock "
            "entries are governed by risk notional and fractional quantity "
            "normalization instead of this share-price cliff."
        ),
    )
    chili_autotrader_fractional_equity_enabled: bool = Field(
        default=AUTOTRADER_FRACTIONAL_EQUITY_DEFAULT_ENABLED,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_FRACTIONAL_EQUITY_ENABLED"),
        description=(
            "Allow stock entries to use fractional-share quantity sizing. "
            "When enabled, high-priced stocks are not blocked solely by "
            "CHILI_AUTOTRADER_MAX_SYMBOL_PRICE_USD; they still must pass "
            "positive expected edge, slippage, drawdown, lifecycle, and "
            "notional/quantity gates."
        ),
    )
    chili_autotrader_max_entry_slippage_pct: float = Field(
        default=AUTOTRADER_MAX_ENTRY_SLIPPAGE_DEFAULT_PCT,
        ge=0.0,
        le=AUTOTRADER_MAX_ENTRY_SLIPPAGE_CONFIG_LIMIT_PCT,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_ENTRY_SLIPPAGE_PCT"),
    )
    chili_autotrader_favorable_entry_drift_enabled: bool = Field(
        default=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_FAVORABLE_ENTRY_DRIFT_ENABLED"
        ),
        description=(
            "Allow stock entries to accept a bounded favorable pullback below "
            "the alert entry only after re-checking expected net edge at the "
            "current price. Adverse upward drift still uses the normal "
            "slippage block."
        ),
    )
    chili_autotrader_favorable_entry_drift_asset_types: str = Field(
        default=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_ASSET_TYPES,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_FAVORABLE_ENTRY_DRIFT_ASSET_TYPES"
        ),
    )
    chili_autotrader_favorable_entry_drift_slippage_multiple: float = Field(
        default=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_SLIPPAGE_MULTIPLE,
        ge=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MIN_SLIPPAGE_MULTIPLE,
        le=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MAX_SLIPPAGE_MULTIPLE,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_FAVORABLE_ENTRY_DRIFT_SLIPPAGE_MULTIPLE"
        ),
    )
    chili_autotrader_favorable_entry_drift_max_pct: float = Field(
        default=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_DEFAULT_MAX_PCT,
        ge=0.0,
        le=AUTOTRADER_FAVORABLE_ENTRY_DRIFT_CONFIG_LIMIT_PCT,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_FAVORABLE_ENTRY_DRIFT_MAX_PCT"
        ),
    )
    chili_autotrader_slippage_reprice_cooldown_enabled: bool = Field(
        default=AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_ENABLED"
        ),
        description=(
            "When repeated missed-entry reprices for the same pattern/ticker "
            "are non-positive EV, temporarily suppress duplicate reprice work "
            "instead of repeatedly chasing an uneconomic quote."
        ),
    )
    chili_autotrader_slippage_reprice_cooldown_minutes: int = Field(
        default=AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_MINUTES,
        ge=1,
        le=240,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_MINUTES"
        ),
    )
    chili_autotrader_slippage_reprice_cooldown_threshold: int = Field(
        default=AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_THRESHOLD,
        ge=1,
        le=25,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_THRESHOLD"
        ),
    )
    chili_autotrader_slippage_reprice_cooldown_asset_types: str = Field(
        default=AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_DEFAULT_ASSET_TYPES,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SLIPPAGE_REPRICE_COOLDOWN_ASSET_TYPES"
        ),
    )
    chili_autotrader_live_reentry_cooldown_asset_types: str = Field(
        default="stock",
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_LIVE_REENTRY_COOLDOWN_ASSET_TYPES"
        ),
        description="Comma-separated asset types protected by live same-ticker post-exit reentry cooldowns.",
    )
    chili_autotrader_live_reentry_cooldown_minutes: float = Field(
        default=30.0,
        ge=0.0,
        le=1440.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_LIVE_REENTRY_COOLDOWN_MINUTES"
        ),
        description="Cooldown after a non-stop live exit before re-entering the same ticker; 0 disables this branch.",
    )
    chili_autotrader_live_stop_reentry_cooldown_minutes: float = Field(
        default=120.0,
        ge=0.0,
        le=1440.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_LIVE_STOP_REENTRY_COOLDOWN_MINUTES"
        ),
        description="Cooldown after a stop-related live exit before re-entering the same ticker; 0 disables this branch.",
    )
    chili_autotrader_monitor_interval_seconds: int = Field(
        default=60,
        ge=5,
        le=600,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MONITOR_INTERVAL_SECONDS"),
    )
    chili_broker_position_price_monitor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_BROKER_POSITION_PRICE_MONITOR_ENABLED"),
    )
    chili_broker_position_price_monitor_interval_minutes: int = Field(
        default=5,
        ge=1,
        le=60,
        validation_alias=AliasChoices("CHILI_BROKER_POSITION_PRICE_MONITOR_INTERVAL_MINUTES"),
    )
    chili_broker_position_price_monitor_bar_lookback_minutes: int = Field(
        default=720,
        ge=5,
        le=1440,
        validation_alias=AliasChoices("CHILI_BROKER_POSITION_PRICE_MONITOR_BAR_LOOKBACK_MINUTES"),
    )

    # P0.7 — stuck-order watchdog. Auto-cancels orders that the broker
    # has acknowledged but never filled/rejected within the timeout. The
    # market timeout is short because a market order that hasn't filled
    # in minutes usually indicates a broker-side queue issue; the limit
    # timeout is longer since limits are explicitly resting orders.
    chili_stuck_order_watchdog_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_STUCK_ORDER_WATCHDOG_ENABLED"),
    )
    chili_stuck_order_watchdog_interval_seconds: int = Field(
        default=60,
        ge=15,
        le=600,
        validation_alias=AliasChoices("CHILI_STUCK_ORDER_WATCHDOG_INTERVAL_SECONDS"),
    )
    chili_stuck_order_market_timeout_seconds: int = Field(
        default=300,  # 5 minutes
        ge=30,
        le=3600,
        validation_alias=AliasChoices("CHILI_STUCK_ORDER_MARKET_TIMEOUT_SECONDS"),
    )
    chili_stuck_order_limit_timeout_seconds: int = Field(
        default=1800,  # 30 minutes
        ge=60,
        le=86400,
        validation_alias=AliasChoices("CHILI_STUCK_ORDER_LIMIT_TIMEOUT_SECONDS"),
    )
    # Coinbase AutoTrader maker-first fallback. A post-only entry at the bid
    # controls fees/slippage, but a high-edge signal should not simply vanish
    # when the bid does not fill. After the short maker window, the watchdog
    # cancels the resting maker order and submits a bounded takerable limit only
    # if the expected edge remains positive after fee + spread + safety costs.
    chili_coinbase_maker_first_fallback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_COINBASE_MAKER_FIRST_FALLBACK_ENABLED"),
    )
    chili_coinbase_maker_first_fallback_after_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        validation_alias=AliasChoices("CHILI_COINBASE_MAKER_FIRST_FALLBACK_AFTER_SECONDS"),
    )
    chili_coinbase_maker_first_min_net_after_cost_pct: float = Field(
        default=0.0,
        ge=-100.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_COINBASE_MAKER_FIRST_MIN_NET_AFTER_COST_PCT"),
    )
    chili_coinbase_maker_first_taker_price_buffer_bps: float = Field(
        default=10.0,
        ge=0.0,
        le=250.0,
        validation_alias=AliasChoices("CHILI_COINBASE_MAKER_FIRST_TAKER_PRICE_BUFFER_BPS"),
    )
    # Wide-spread taker fallback ceiling. The fallback already subtracts
    # spread from expected edge, but recent TCA shows the damage is
    # concentrated in a >200bps tail. Above this ceiling the watchdog holds
    # the maker order briefly (or cancels it after the hold window) rather
    # than replacing it with a takerable limit.
    chili_coinbase_maker_first_taker_max_spread_bps: float = Field(
        default=200.0,
        ge=0.0,
        le=10_000.0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_MAKER_FIRST_TAKER_MAX_SPREAD_BPS"
        ),
    )

    chili_coinbase_maker_only_improve_bid_ticks: int = Field(
        default=1,
        ge=0,
        le=100,
        validation_alias=AliasChoices("CHILI_COINBASE_MAKER_ONLY_IMPROVE_BID_TICKS"),
    )
    chili_coinbase_maker_first_edge_thin_hold_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_MAKER_FIRST_EDGE_THIN_HOLD_ENABLED"
        ),
    )
    chili_coinbase_maker_first_edge_thin_hold_seconds: int = Field(
        default=1800,
        ge=30,
        le=86400,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_MAKER_FIRST_EDGE_THIN_HOLD_SECONDS"
        ),
    )

    # P0.6 — execution-event lag telemetry. Measures recorded_at - event_at
    # lag on trading_execution_events; warns when the P95 lag crosses
    # warn_p95_ms and errors (and flips breach='error') at error_p95_ms.
    # Scheduler runs this on interval_seconds; disabled flag lets us kill
    # the metric without touching the scheduler.
    chili_execution_event_lag_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_EXECUTION_EVENT_LAG_ENABLED"),
    )
    chili_execution_event_lag_interval_seconds: int = Field(
        default=60,
        ge=15,
        le=600,
        validation_alias=AliasChoices("CHILI_EXECUTION_EVENT_LAG_INTERVAL_SECONDS"),
    )
    chili_execution_event_lag_lookback_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        validation_alias=AliasChoices("CHILI_EXECUTION_EVENT_LAG_LOOKBACK_SECONDS"),
    )
    chili_execution_event_lag_warn_p95_ms: float = Field(
        default=15_000.0,
        ge=100.0,
        le=600_000.0,
        validation_alias=AliasChoices("CHILI_EXECUTION_EVENT_LAG_WARN_P95_MS"),
    )
    chili_execution_event_lag_error_p95_ms: float = Field(
        default=60_000.0,
        ge=500.0,
        le=3_600_000.0,
        validation_alias=AliasChoices("CHILI_EXECUTION_EVENT_LAG_ERROR_P95_MS"),
    )
    # Upper bound on a single sample's lag before we treat it as orphan/bad-data
    # rather than legitimate lag. Without this, a single broker event with a
    # stale event_at (clock skew, late delivery, broken upstream) dominates
    # p95 and pages the operator on phantom 80h lags. Samples above this are
    # excluded from the percentile and counted in `dropped_outlier_count`.
    chili_execution_event_lag_max_sample_ms: float = Field(
        default=600_000.0,  # 10 minutes - real lag should never come close
        ge=10_000.0,
        le=86_400_000.0,
        validation_alias=AliasChoices("CHILI_EXECUTION_EVENT_LAG_MAX_SAMPLE_MS"),
    )

    # Promotion-evidence audit: scan promoted patterns for missing OOS / CPCV /
    # deflated_sharpe / promotion_gate_passed. Default audit-only (logs the
    # incomplete set, no mutation). Set chili_pattern_evidence_auto_demote=true
    # to actually demote them to 'challenged' on the next run. Reading the
    # report first is strongly recommended — Codex's 2026-04-27 audit found
    # near-total absence of evidence on the legacy promoted-status set, so
    # naive auto-demote would stop a lot of live trading at once.
    # AutoTrader entry gate: only trade alerts whose scan_pattern is in this
    # set of lifecycle stages. 2026-04-28 incident: 32 of 34 entries in 7 days
    # were on `challenged` patterns the evidence audit had just demoted - the
    # operational gate had been ignoring lifecycle_stage. Default
    # ('promoted','live') matches CLAUDE.md hard-rule. Override via env to
    # widen during recovery: CHILI_AUTOTRADER_ELIGIBLE_LIFECYCLE_STAGES=promoted,live,validated
    chili_autotrader_eligible_lifecycle_stages: str = Field(
        default="promoted,live,pilot_promoted",
        validation_alias=AliasChoices("CHILI_AUTOTRADER_ELIGIBLE_LIFECYCLE_STAGES"),
    )

    # Variant-spawn gate: refuse to fork entry/exit/tf/combo variants from
    # parent patterns with WR < 35% on >=50 trades, or that are in demoted
    # lifecycle stages. Default ON. Set false to restore the legacy variant
    # treadmill (81% of the 612-pattern audit population came from forks of
    # mostly low-edge parents).
    chili_variant_spawn_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_VARIANT_SPAWN_GATE_ENABLED"),
    )
    chili_edge_evolution_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_ENABLED"),
    )
    chili_edge_evolution_lookback_days: int = Field(
        default=7,
        ge=1,
        le=90,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_LOOKBACK_DAYS"),
    )
    chili_edge_evolution_min_rejects: int = Field(
        default=5,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_MIN_REJECTS"),
    )
    chili_edge_evolution_severe_min_rejects: int = Field(
        default=20,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_SEVERE_MIN_REJECTS"),
    )
    chili_edge_evolution_severe_avg_net_pct: float = Field(
        default=-1.0,
        ge=-100.0,
        le=0.0,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_SEVERE_AVG_NET_PCT"),
    )
    chili_edge_evolution_max_avg_net_for_child_pct: float = Field(
        default=-0.25,
        ge=-100.0,
        le=0.0,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_MAX_AVG_NET_FOR_CHILD_PCT"),
    )
    chili_edge_evolution_payoff_rescue_max_avg_net_pct: float = Field(
        default=-0.75,
        ge=-100.0,
        le=0.0,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_PAYOFF_RESCUE_MAX_AVG_NET_PCT"),
    )
    chili_edge_evolution_payoff_rescue_min_reward_risk: float = Field(
        default=2.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_PAYOFF_RESCUE_MIN_REWARD_RISK"),
    )
    chili_edge_evolution_min_payoff_samples: int = Field(
        default=5,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_MIN_PAYOFF_SAMPLES"),
    )
    chili_edge_evolution_min_reward_risk: float = Field(
        default=1.25,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_MIN_REWARD_RISK"),
    )
    chili_edge_evolution_min_directional_sample_n: float = Field(
        default=5.0,
        ge=0.0,
        le=10_000.0,
        validation_alias=AliasChoices("CHILI_EDGE_EVOLUTION_MIN_DIRECTIONAL_SAMPLE_N"),
    )

    chili_pattern_evidence_audit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_PATTERN_EVIDENCE_AUDIT_ENABLED"),
    )
    chili_pattern_evidence_auto_demote: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE"),
    )
    chili_pattern_evidence_auto_demote_dry_run: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE_DRY_RUN"),
    )

    # P0.8 — drift escalation watchdog. Alerts when the same bracket
    # intent is classified as the same non-agree kind for N consecutive
    # sweeps. Opt-in (default off) because it's new alerting surface —
    # operators should turn it on explicitly after tuning the threshold
    # for their sweep cadence.
    chili_drift_escalation_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_DRIFT_ESCALATION_ENABLED"),
    )
    chili_drift_escalation_interval_seconds: int = Field(
        default=120,
        ge=30,
        le=3600,
        validation_alias=AliasChoices("CHILI_DRIFT_ESCALATION_INTERVAL_SECONDS"),
    )
    chili_drift_escalation_min_count: int = Field(
        default=5,
        ge=2,
        le=100,
        validation_alias=AliasChoices("CHILI_DRIFT_ESCALATION_MIN_COUNT"),
    )
    chili_drift_escalation_lookback_minutes: int = Field(
        default=60,
        ge=5,
        le=1440,
        validation_alias=AliasChoices("CHILI_DRIFT_ESCALATION_LOOKBACK_MINUTES"),
    )

    # Phase G.2 — bracket writer. Top-level flag gates the module;
    # per-action flags enable individual repairs. Override via env
    # (CHILI_BRACKET_WRITER_G2_*) if you need to disable in a hurry.
    chili_bracket_writer_g2_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_BRACKET_WRITER_G2_ENABLED"),
    )
    chili_bracket_writer_g2_partial_fill_resize: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_BRACKET_WRITER_G2_PARTIAL_FILL_RESIZE"),
    )
    chili_bracket_writer_g2_place_missing_stop: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_BRACKET_WRITER_G2_PLACE_MISSING_STOP"),
    )
    # 2026-05-01 (Phase A re-enable): when an existing sell order covers
    # the entire position (held_for_sells == quantity), a SELL_STOP would
    # be rejected with "Not enough shares to sell." DEFAULT False → skip
    # the placement and preserve the existing sell. Setting True reverts
    # to the FIX 57 behavior — cancel the covering sell first, then place
    # the stop. Operator opt-in: prioritise downside protection over
    # upside lock-in for the affected positions.
    chili_bracket_writer_cancel_covering_sell: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL"),
    )
    # Round 23 - sweep-side gate that wires the G2 writer into the
    # reconciliation sweep's post-classify hook. Default OFF so the
    # Phase G.2 writer module can ship without immediately flipping the
    # repair path live. To actually repair, flip BOTH this flag AND
    # brain_live_brackets_mode="authoritative". The writer's own
    # per-action flags (above) and venue check still apply on top.
    chili_bracket_sweep_writer_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_BRACKET_SWEEP_WRITER_ENABLED"),
    )
    # audit-missing-stop-emergency-repair (2026-05-03) — additive escape
    # valve for the bracket reconciler's state_gated_skip when the intent
    # is parked at terminal_reject AND the trade is still open. With the
    # flag OFF (default) behavior is unchanged: state_gated_skip continues
    # to short-circuit. With the flag ON, the new branch fires per-intent
    # at most once per CHILI_BRACKET_TERMINAL_REJECT_REPAIR_THROTTLE_SECONDS
    # (default 6h, see bracket_reconciliation_service module-level constant).
    # Three sub-branches by broker quantity: phantom-close (qty=0), real-
    # exposure repair (qty>0, calls FIX-51 place_missing_stop), or
    # broker_unavailable skip. Any rejection by the FIX-51 path bumps the
    # throttle so the gate re-locks; manual operator action is required to
    # unstick. Operator must triage existing terminal_reject positions
    # BEFORE flipping this flag (close, manually re-arm, or accept the
    # writer's controlled retry).
    chili_bracket_missing_stop_repair_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED"),
    )
    # bracket-intent-stale-label-cleanup (2026-05-03) — additive sweep-loop
    # hook that does two things when ON:
    #   1. Mirror BrokerView.stop_order_id into bracket_intents.broker_stop_order_id
    #      (advisory cache; decision-time consumers MUST keep reading BrokerView).
    #   2. Auto-transition intent_state='terminal_reject' → 'reconciled' when
    #      classifier returns kind=agree on a subsequent sweep, with last_diff_reason
    #      'auto_reconciled_after_terminal_reject' and a CRITICAL log line.
    # Flag OFF preserves prior behavior (mark_reconciled silently fails on the
    # terminal_reject → reconciled transition because the standard state machine
    # does not allow it; the explicit auto-reconcile writer bypasses that).
    chili_bracket_intent_mirror_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_BRACKET_INTENT_MIRROR_ENABLED"),
    )
    # f-equity-reconcile-partial-list-guard (2026-05-08) — minimum number
    # of consecutive ``sync_positions_to_db`` cycles a position must be
    # missing from ``rh_tickers`` before the stale-close path may close
    # it. Default 2: one missing cycle increments the streak; the second
    # consecutive miss confirms the position is genuinely gone (not a
    # truncated broker response). Setting this to 0 disables the guard
    # without a code revert (the gate becomes a no-op since any streak
    # >= 0 always allows the close).
    chili_reconcile_partial_list_streak_min: int = Field(
        default=2,
        validation_alias=AliasChoices("CHILI_RECONCILE_PARTIAL_LIST_STREAK_MIN"),
    )
    # f-phase-e-revert-and-bracket-writer-crash-fix (2026-05-08):
    # cooldown in seconds applied to a bracket_intent after ANY
    # exception (not just broker terminal-reject) raised inside
    # place_missing_stop. The active crash loop on ADA/SOL exposed
    # that the existing terminal-reject cooldown only fires on known
    # broker-side reject codes; code bugs (e.g. IndexError inside the
    # broker SDK from get_instruments_by_symbols('ADA')[0]) did NOT
    # arm the cooldown and re-fired every 60s sweep. Default 300s.
    chili_bracket_writer_exception_cooldown_secs: int = Field(
        default=300,
        validation_alias=AliasChoices(
            "CHILI_BRACKET_WRITER_EXCEPTION_COOLDOWN_SECS"
        ),
    )
    # f-brain-phase2-producer-completion (2026-05-09) -- watchdog-style
    # mining producer wired into run_brain_work_dispatch_round. The
    # APScheduler-based brain_market_snapshots job exists at
    # trading_scheduler.py:262 but stopped firing 2026-05-05 (zero events
    # in 4 days at audit time). Rather than rebuild the scheduler stack,
    # we add a fallback emit inside the dispatch round; if the scheduler
    # is dead the dispatch hook keeps the candidate pipeline alive.
    # Disable here only if the scheduler is confirmed healthy AND
    # operator wants single-path operation.
    chili_brain_dispatch_market_snapshots_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_BRAIN_DISPATCH_MARKET_SNAPSHOTS_ENABLED"
        ),
    )
    # Minimum spacing between dispatch-round mining emits (seconds).
    # Default 900 (15min) -- matches the APScheduler job's cadence so a
    # healthy scheduler + healthy dispatch hook both producing in the
    # same minute self-dedup via the per-minute bucket key in
    # emit_market_snapshots_batch_outcome. Setting to 0 disables the
    # interval gate entirely (sweep runs every dispatch round).
    chili_brain_dispatch_market_snapshots_interval_secs: int = Field(
        default=900,
        validation_alias=AliasChoices(
            "CHILI_BRAIN_DISPATCH_MARKET_SNAPSHOTS_INTERVAL_SECS"
        ),
    )
    # f-coinbase-autotrader-enablement-phase-3-broker-selector (2026-05-09):
    # GLOBAL kill-switch for the autotrader entry path. When True, the
    # broker_selector returns venue='skip' with reason='kill_switch_global'
    # for every alert -- both venues halt. Operator-pulled lever; the
    # in-process governance.is_kill_switch_active() is the second
    # (process-local) trip layer. Multi-process visibility comes from
    # the env var (each worker reads on next round).
    chili_autotrader_kill_switch: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_KILL_SWITCH"),
    )
    # Phase 3 LIVE flag for Coinbase routing. Default OFF: when the
    # selector returns venue='coinbase', the autotrader writes a
    # shadow-log row to trading_venue_routing_log but DOES NOT call the
    # broker. Flip to True to enable real Coinbase orders. Operator
    # approval required per Phase 3 sequencing step 9.
    chili_coinbase_autotrader_live: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_COINBASE_AUTOTRADER_LIVE"),
    )
    # Coinbase live probation: even when the venue live flag is enabled,
    # require recent Coinbase-managed crypto AutoTrader exits to show clean,
    # positive realized venue evidence before paying live fees. Blocks still
    # flow into paper-shadow observation from the AutoTrader call site.
    chili_coinbase_autotrader_probation_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_AUTOTRADER_PROBATION_ENABLED"
        ),
    )
    chili_coinbase_autotrader_probation_window_days: int = Field(
        default=30,
        ge=1,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_AUTOTRADER_PROBATION_WINDOW_DAYS"
        ),
    )
    chili_coinbase_autotrader_probation_min_closed_trades: int = Field(
        default=25,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_AUTOTRADER_PROBATION_MIN_CLOSED_TRADES"
        ),
    )
    chili_coinbase_autotrader_probation_max_low_confidence_exit_rate: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_AUTOTRADER_PROBATION_MAX_LOW_CONFIDENCE_EXIT_RATE"
        ),
    )
    chili_coinbase_autotrader_probation_min_low_confidence_exits: int = Field(
        default=10,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_AUTOTRADER_PROBATION_MIN_LOW_CONFIDENCE_EXITS"
        ),
    )
    chili_coinbase_autotrader_probation_min_avg_pnl_usd: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_AUTOTRADER_PROBATION_MIN_AVG_PNL_USD"
        ),
    )
    chili_coinbase_autotrader_probation_min_payoff_ratio: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_AUTOTRADER_PROBATION_MIN_PAYOFF_RATIO"
        ),
    )
    chili_coinbase_autotrader_probation_cache_seconds: int = Field(
        default=60,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_AUTOTRADER_PROBATION_CACHE_SECONDS"
        ),
    )
    chili_broker_selector_rh_crypto_degraded_fallback_enabled: bool = Field(
        default=BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_ENABLED"
        ),
    )
    chili_broker_selector_rh_crypto_degraded_lookback_minutes: int = Field(
        default=BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_DEFAULT_LOOKBACK_MINUTES,
        ge=BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_MIN_LOOKBACK_MINUTES,
        le=BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_MAX_LOOKBACK_MINUTES,
        validation_alias=AliasChoices(
            "CHILI_BROKER_SELECTOR_RH_CRYPTO_DEGRADED_LOOKBACK_MINUTES"
        ),
    )
    chili_broker_selector_rh_crypto_degraded_min_failures: int = Field(
        default=BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_DEFAULT_MIN_FAILURES,
        ge=BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_MIN_FAILURES,
        le=BROKER_SELECTOR_RH_CRYPTO_DEGRADED_FALLBACK_MAX_FAILURES,
        validation_alias=AliasChoices(
            "CHILI_BROKER_SELECTOR_RH_CRYPTO_DEGRADED_MIN_FAILURES"
        ),
    )
    # f-coinbase-autotrader-enablement-phase-4-bracket-writer-path
    # (2026-05-09): bracket writer's Coinbase SELL stop-limit places
    # `limit_price = stop_price * (1 - buffer_pct)` so the limit
    # accepts a fill on the trigger move. Default 0.005 = 0.5% below
    # stop. Tighter than RH's stop-loss-MARKET (which fills at any
    # price) but bounded so a fast gap-down can't sell at $0. If the
    # operator wants tighter (e.g. 0.001 = 10bps below) or looser
    # (e.g. 0.02), env-override.
    chili_coinbase_stop_limit_buffer_pct: float = Field(
        default=0.005,
        validation_alias=AliasChoices("CHILI_COINBASE_STOP_LIMIT_BUFFER_PCT"),
    )
    # When Coinbase rejects an exit-market sell because a product is in
    # limit-only mode, the crypto exit monitor submits a marketable SELL
    # limit below the current price. This is an emergency flattening path,
    # so it defaults wider than bracket stops.
    chili_coinbase_exit_limit_fallback_buffer_pct: float = Field(
        default=0.01,
        validation_alias=AliasChoices("CHILI_COINBASE_EXIT_LIMIT_FALLBACK_BUFFER_PCT"),
    )
    # f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing
    # (2026-05-09): Coinbase Advanced Trade Tier 1 fees per
    # docs.cdp.coinbase.com/exchange/docs/fees:
    # 60bps taker per-side -> 120bps round-trip. The cost-aware
    # gate refuses Coinbase entries whose projected edge does not
    # clear (this fee + the safety buffer below). Operator on a
    # different tier overrides via env.
    chili_coinbase_taker_fee_bps_round_trip: int = Field(
        default=120,
        validation_alias=AliasChoices("CHILI_COINBASE_TAKER_FEE_BPS_ROUND_TRIP"),
    )
    # Cushion above the raw fee floor — covers spread + slippage
    # plus a small margin for execution drift. 30bps is conservative
    # for Tier 1 retail; tighter at higher tiers.
    chili_min_edge_safety_buffer_bps: int = Field(
        default=30,
        validation_alias=AliasChoices("CHILI_MIN_EDGE_SAFETY_BUFFER_BPS"),
    )
    # Optional per-venue notional cap (USD) for CHILI-managed Coinbase
    # autotrader exposure. 0 disables the static cap; sizing, buying power,
    # cost/edge, and portfolio gates remain active.
    chili_coinbase_max_notional_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_COINBASE_MAX_NOTIONAL_USD"),
    )
    # Optional per-venue concurrent-position cap for CHILI-managed Coinbase
    # autotrader exposure. 0 disables the static cap.
    chili_coinbase_max_concurrent_positions: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("CHILI_COINBASE_MAX_CONCURRENT_POSITIONS"),
    )
    # Phase 5K-B/C: default-OFF reader cutover for Coinbase cap truth.
    # Operators flip this through typed Settings; the cap gate does not
    # independently read a hidden environment fallback.
    chili_phase5k_coinbase_cap_use_envelopes: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES"),
    )
    # f-promotion-pipeline-rebalance Phase 1 (2026-05-09): sample-size
    # floor for the thin-evidence demote sweep. The original Phase D
    # threshold (10 trades) was wrong-sense: a pattern with 8 realized
    # trades isn't a "thin-evidence demote candidate" — its 8 trades
    # are the autotrader's 7-stage-gate-laundered noise sample, not a
    # statistically valid signal. Pattern 585 with CPCV sharpe 1.40
    # was killed by this exact path on 2026-05-09. The corrected
    # semantic: do NOT demote when n < min_realized_trades; require
    # >=N realized trades before the realized-WR signal is allowed to
    # flip lifecycle.
    chili_pattern_demote_min_realized_trades: int = Field(
        default=30,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DEMOTE_MIN_REALIZED_TRADES"
        ),
    )
    # When True (default), demote sweeps + the 02:15 PT
    # promotion_evidence_audit MUST also confirm CPCV-degrade before
    # demoting. A pattern with cpcv_median_sharpe >= 1.0 (passing
    # threshold) is protected even if its realized WR is poor or its
    # OOS evidence is incomplete — CPCV is the higher-information
    # signal and should not be overridden by gate-laundered realized
    # noise.
    chili_pattern_demote_require_cpcv_degrade: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DEMOTE_REQUIRE_CPCV_DEGRADE"
        ),
    )
    # f-evaluation-function-fix Tier A #2 (2026-05-18): payoff-ratio
    # protection in the demote-criteria gates. The 2026-05-18 audit
    # found pattern 585 (the system's only proven alpha: CPCV 1.41,
    # WR 35%, avg return 1.68%/trade, payoff ratio ~3:1) was demoted
    # by run_thin_evidence_demote -- a gate that uses WR alone. Skew-
    # driven strategies systematically score below WR floors despite
    # being positive-expectancy. When ``payoff_ratio >= floor``, the
    # pattern is protected from realized-WR-based demote regardless
    # of WR. Materialized on scan_patterns by mig 246 and refreshed
    # nightly. 1.5 chosen because at WR=0.33 (the existing floor) a
    # payoff ratio of 2.0 gives positive expectancy; 1.5 is a more
    # conservative floor that still protects skew edges. Set to a
    # very high value (e.g. 1e9) to disable the protection.
    chili_pattern_demote_payoff_ratio_floor: float = Field(
        default=1.5,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DEMOTE_PAYOFF_RATIO_FLOOR"
        ),
    )
    # Companion floor: a payoff_ratio backed by < N closed trades is
    # noise. Default 5 matches the existing realized-stats floor used
    # by compute_quality_composite_score's realized component.
    chili_pattern_demote_payoff_ratio_min_n: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DEMOTE_PAYOFF_RATIO_MIN_N"
        ),
    )
    # f-evaluation-function-fix Tier A #3 (2026-05-18): composite-score
    # sample-size floor. compute_quality_composite_score already gates
    # the realized COMPONENT at n>=5 but still produces a non-NULL
    # score from re-normalized non-realized terms when n<5. The 2026-
    # 05-16 diagnostic surfaced n=2 patterns (1215) ranked above n=86
    # pattern 585 -- noise inflating the cohort-promote landmine. This
    # floor makes the whole composite NULL when realized n<floor, so
    # the cohort-promote eligibility query simply skips those rows.
    # Default 5 matches the realized-component floor.
    chili_composite_min_realized_trades: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "CHILI_COMPOSITE_MIN_REALIZED_TRADES"
        ),
    )
    # f-position-identity-phase-4 (2026-05-18): feature flag for the
    # precise inverse-reconcile path that consults position-level fill
    # history instead of the conservative per-trade_id event_count
    # workaround. When True, broker_service.sync_positions_to_db uses
    # position_resolver.position_has_recorded_sell(position_id) as the
    # discriminator -- precise across all Trade row generations linked
    # to a position. When False (default), the existing event_count==0
    # path is used. Operator flips to True after a paper-soak window.
    chili_position_identity_phase4_authority_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "CHILI_POSITION_IDENTITY_PHASE4_AUTHORITY_ENABLED"
        ),
    )
    # f-coinbase-maker-only-routing (2026-05-19): when True, the autotrader
    # routes Coinbase BUY entries through a post_only limit order at
    # current best-bid instead of a crossing market order. Coinbase taker
    # fees are 60bps each side (120bps round-trip); maker fees are 40bps
    # or less depending on volume tier. The 2026-05-18 TCA finding showed
    # avg +102bps entry slippage on crypto, consuming ~60% of pattern 585's
    # 168bps gross edge. Maker-only routing reduces fees + adverse fills.
    # Trade-off: if the limit can't fill at best-bid (price moved up),
    # the order is REJECTED by the broker and the entry is MISSED. The
    # design assumption: missing one entry is better than paying ~100bps
    # of slippage. Default OFF for paper-soak before promotion.
    chili_coinbase_maker_only_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_MAKER_ONLY_ENABLED"
        ),
    )
    # f-stop-engine-payoff-ratio-gate (2026-05-19): payoff-ratio-aware
    # sizing scaler for the autotrader. Composes AFTER HRP / survival /
    # pilot_promoted multipliers. Reads scan_patterns.payoff_ratio +
    # payoff_ratio_n (Tier A columns from mig 246, refreshed nightly by
    # realized_stats_sync). Tiers:
    # Uses posterior-smoothed sizing (prior_ratio/prior_n below) instead
    # of raw threshold cliffs, while preserving tier labels in audit rows.
    # Default OFF. Operator flips after paper-soak comparison.
    chili_autotrader_payoff_sizing_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAYOFF_SIZING_ENABLED"
        ),
    )
    chili_autotrader_payoff_min_n: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAYOFF_MIN_N"
        ),
    )
    chili_autotrader_payoff_prior_ratio: float = Field(
        default=1.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAYOFF_PRIOR_RATIO"
        ),
    )
    chili_autotrader_payoff_prior_n: int = Field(
        default=20,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAYOFF_PRIOR_N"
        ),
    )
    chili_autotrader_payoff_min_multiplier: float = Field(
        default=0.5,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAYOFF_MIN_MULTIPLIER"
        ),
    )
    chili_autotrader_payoff_max_multiplier: float = Field(
        default=1.5,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PAYOFF_MAX_MULTIPLIER"
        ),
    )
    chili_coinbase_cost_gate_include_tca_estimates: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_COST_GATE_INCLUDE_TCA_ESTIMATES"
        ),
    )
    chili_coinbase_cost_gate_min_tca_samples: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_COST_GATE_MIN_TCA_SAMPLES"
        ),
    )
    # Robinhood has no explicit commission, but live TCA can still consume
    # the expected edge. When enough recent usable RH fills exist, require
    # projected edge to clear tail adverse entry slippage + the shared
    # safety buffer. Missing/thin evidence leaves legacy fee-free admission.
    chili_robinhood_cost_gate_include_tca_estimates: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_ROBINHOOD_COST_GATE_INCLUDE_TCA_ESTIMATES"
        ),
    )
    chili_robinhood_cost_gate_min_tca_samples: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "CHILI_ROBINHOOD_COST_GATE_MIN_TCA_SAMPLES"
        ),
    )
    chili_robinhood_cost_gate_window_days: int = Field(
        default=30,
        validation_alias=AliasChoices(
            "CHILI_ROBINHOOD_COST_GATE_WINDOW_DAYS"
        ),
    )
    # f-promotion-pipeline-rebalance Phase 2 (2026-05-09):
    # directional-correctness signal — gate-noise-free pattern eval.
    # The autotrader's 7-stage gate chain laundered pattern 585's 1284
    # imminent-alerts down to 8 realized trades; Phase 2 measures
    # directional accuracy on EVERY imminent alert (not just the gate
    # survivors). Default ON so the evaluator starts populating
    # pattern_alert_directional_outcome immediately after Phase 2
    # ships; flag-disable reverts to no eval.
    chili_pattern_directional_outcome_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_OUTCOME_ENABLED"
        ),
    )
    # Threshold for the directional_correct verdict (percent move in
    # the predicted direction within the hold window). 1.5% is a
    # standard "real move" floor for intraday/swing breakouts; the
    # snapshot is persisted on each row for audit when this is tuned.
    chili_pattern_directional_threshold_pct: float = Field(
        default=PATTERN_DIRECTIONAL_DEFAULT_THRESHOLD_PCT,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_THRESHOLD_PCT"
        ),
    )
    # Default hold window in hours when the alert row carries no
    # explicit duration estimate. 24h covers the typical
    # intraday-to-overnight breakout cycle; operator can shorten for
    # scalp-heavy regimes.
    chili_pattern_directional_default_hold_hours: int = Field(
        default=PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_DEFAULT_HOLD_HOURS"
        ),
    )
    # How far back to look for unevaluated alerts. Keeps the evaluator
    # from sweeping the entire alerts table when it first runs; one
    # week of lookback is enough to populate the rolling-30 view per
    # active pattern.
    chili_pattern_directional_max_lookback_hours: int = Field(
        default=PATTERN_DIRECTIONAL_DEFAULT_MAX_LOOKBACK_HOURS,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_MAX_LOOKBACK_HOURS"
        ),
    )
    # Per-cycle cap on alerts evaluated. Bounds OHLC fetch fan-out so
    # a single tick never overwhelms the market-data providers.
    chili_pattern_directional_max_alerts_per_run: int = Field(
        default=PATTERN_DIRECTIONAL_DEFAULT_MAX_ALERTS_PER_RUN,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_MAX_ALERTS_PER_RUN"
        ),
    )
    chili_pattern_directional_edge_debt_priority_enabled: bool = Field(
        default=PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_ENABLED"
        ),
    )
    chili_pattern_directional_edge_debt_priority_lookback_minutes: int = Field(
        default=PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_LOOKBACK_MINUTES,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_LOOKBACK_MINUTES"
        ),
    )
    chili_pattern_directional_edge_debt_priority_asset_types: str = Field(
        default=PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_ASSET_TYPES,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_ASSET_TYPES"
        ),
    )
    chili_pattern_directional_edge_debt_priority_managed_reasons: str = Field(
        default=PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_DEFAULT_MANAGED_REASONS,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DIRECTIONAL_EDGE_DEBT_PRIORITY_MANAGED_REASONS"
        ),
    )
    # f-promotion-pipeline-rebalance Phase 3 (2026-05-10):
    # shadow_promoted lifecycle stage. When True (default), patterns
    # with lifecycle_stage='shadow_promoted' are eligible for imminent
    # alerts (so the Phase 2 directional-correctness evaluator scores
    # them) but the autotrader routes their alerts to shadow-log only —
    # no broker call, no Trade row. Decouples observation from
    # execution: we measure pattern accuracy without taking on capital
    # risk during evaluation. When False, shadow_promoted patterns are
    # NOT eligible for imminent alerts AND any in-flight shadow_promoted
    # alerts that reach the autotrader fall through to the existing
    # pattern_lifecycle_not_eligible:shadow_promoted reject path
    # (pre-Phase-3 behavior). The flag is the per-phase rollback lever.
    chili_shadow_promoted_lifecycle_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_PROMOTED_LIFECYCLE_ENABLED"
        ),
    )
    # f-promotion-pipeline-rebalance Phase 4 (2026-05-10): composite
    # quality scoring + weekly cohort auto-promote. Composite score is
    # the convex combination of five normalized components:
    #   composite = w1*clip(cpcv_sharpe/2.0)
    #             + w2*clip(deflated_sharpe/1.0)
    #             + w3*(1-clip(pbo))
    #             + w4*directional_wr
    #             + w5*(1-decay)
    # — all clipped to [0,1] so composite ∈ [0,1] given weights sum to 1.
    # Decay = max(0, older_wr - newer_wr) computed from the rolling-30
    # split of pattern_alert_directional_outcome (newer-15 vs older-15);
    # patterns with rolling_sample_n < 30 are NOT eligible (no decay
    # information; they wait until enough outcomes accumulate). The
    # cohort job promotes top-N by composite score to ``shadow_promoted``
    # (Phase 3's lifecycle stage) — NOT directly to promoted/live —
    # capped at max_per_week per rolling 7-day window. Phase 4 ships
    # dormant: chili_cohort_promote_enabled defaults False until the
    # operator opts in.
    chili_cohort_promote_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_COHORT_PROMOTE_ENABLED"),
    )
    # Discovery bootstrap: the cohort job may advance top pool-relative CPCV
    # near-misses into broker-blocked ``shadow_promoted`` even when the stored
    # promotion_gate_passed flag is stale/false. This is not broker risk; it
    # only lets the imminent scanner collect directional evidence so shadow
    # vetting can decide whether the pattern deserves a pilot.
    chili_cohort_promote_bootstrap_near_miss_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_COHORT_PROMOTE_BOOTSTRAP_NEAR_MISS_ENABLED"
        ),
    )
    # Sign-based floors for the discovery bootstrap lane. These are deliberately
    # not high confidence gates; they only keep the near-miss lane from staging
    # patterns whose CPCV/DSR/PBO evidence has the wrong sign.
    chili_cohort_promote_bootstrap_min_cpcv_sharpe: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "CHILI_COHORT_PROMOTE_BOOTSTRAP_MIN_CPCV_SHARPE"
        ),
    )
    chili_cohort_promote_bootstrap_min_deflated_sharpe: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "CHILI_COHORT_PROMOTE_BOOTSTRAP_MIN_DEFLATED_SHARPE"
        ),
    )
    chili_cohort_promote_bootstrap_max_pbo: float = Field(
        default=1.0,
        validation_alias=AliasChoices("CHILI_COHORT_PROMOTE_BOOTSTRAP_MAX_PBO"),
    )
    chili_cohort_score_weight_cpcv_sharpe: float = Field(
        default=0.10,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_WEIGHT_CPCV_SHARPE"
        ),
    )
    chili_cohort_score_weight_deflated_sharpe: float = Field(
        default=0.05,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_WEIGHT_DEFLATED_SHARPE"
        ),
    )
    chili_cohort_score_weight_pbo_inverse: float = Field(
        default=0.05,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_WEIGHT_PBO_INVERSE"
        ),
    )
    chili_cohort_score_weight_directional_wr: float = Field(
        default=0.35,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_WEIGHT_DIRECTIONAL_WR"
        ),
    )
    chili_cohort_score_weight_decay_inverse: float = Field(
        default=0.10,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_WEIGHT_DECAY_INVERSE"
        ),
    )
    # f-composite-quality-reweight-realized-evidence (2026-05-16): realized-PnL
    # component weight + inputs + cohort-promote floor. New defaults sum to 1.0
    # (0.10 + 0.05 + 0.05 + 0.35 + 0.10 + 0.35). Cowork-resolved Q1/Q2/Q3.
    chili_cohort_score_weight_realized: float = Field(
        default=0.35,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_WEIGHT_REALIZED"
        ),
    )
    chili_cohort_score_realized_pnl_normalizer_pct: float = Field(
        default=0.01,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_REALIZED_PNL_NORMALIZER_PCT"
        ),
    )
    chili_cohort_score_realized_evidence_tau: float = Field(
        default=30.0,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_REALIZED_EVIDENCE_TAU"
        ),
    )
    chili_cohort_score_realized_window_days: int = Field(
        default=90,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_REALIZED_WINDOW_DAYS"
        ),
    )
    chili_cohort_score_include_autotrader_paper_dynamic: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_COHORT_SCORE_INCLUDE_AUTOTRADER_PAPER_DYNAMIC"
        ),
    )
    chili_cohort_promote_min_realized_trades_for_floor: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "CHILI_COHORT_PROMOTE_MIN_REALIZED_TRADES_FOR_FLOOR"
        ),
    )
    chili_cohort_promote_max_realized_avg_pnl_pct_negative: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "CHILI_COHORT_PROMOTE_MAX_REALIZED_AVG_PNL_PCT_NEGATIVE"
        ),
    )
    chili_cohort_promote_low_confidence_exit_rate_floor: float = Field(
        default=0.50,
        validation_alias=AliasChoices(
            "CHILI_COHORT_PROMOTE_LOW_CONFIDENCE_EXIT_RATE_FLOOR"
        ),
    )
    chili_cohort_promote_min_tca_edge_samples_for_floor: int = Field(
        default=5,
        validation_alias=AliasChoices(
            "CHILI_COHORT_PROMOTE_MIN_TCA_EDGE_SAMPLES_FOR_FLOOR"
        ),
    )
    chili_cohort_promote_tca_consumed_expected_edge_rate_floor: float = Field(
        default=0.50,
        validation_alias=AliasChoices(
            "CHILI_COHORT_PROMOTE_TCA_CONSUMED_EXPECTED_EDGE_RATE_FLOOR"
        ),
    )
    # Legacy knobs kept for env compatibility. The active cohort selector now
    # fills the adaptive roster target from chili_cpcv_target_promotion_pool_pct
    # instead of fixed top-N / weekly caps.
    chili_cohort_promote_top_n: int = Field(
        default=20,
        validation_alias=AliasChoices("CHILI_COHORT_PROMOTE_TOP_N"),
    )
    chili_cohort_promote_max_per_week: int = Field(
        default=10,
        validation_alias=AliasChoices("CHILI_COHORT_PROMOTE_MAX_PER_WEEK"),
    )
    # Alpha portfolio gate (2026-05-21): promotion quality should be a
    # diversified portfolio decision, not only a single-pattern score. The
    # gate marks stale promoted/pilot patterns for recert, ranks candidates by
    # sleeve contribution, and blocks broker-risk promotion while recert debt
    # or execution-quality uncertainty is unresolved. Shadow observation can
    # still proceed because it is broker-blocked.
    chili_alpha_portfolio_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_GATE_ENABLED"),
    )
    chili_alpha_portfolio_recert_stale_days: int = Field(
        default=30,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_RECERT_STALE_DAYS"),
    )
    chili_alpha_portfolio_min_realized_trades: int = Field(
        default=5,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MIN_REALIZED_TRADES"),
    )
    chili_alpha_portfolio_min_oos_trades: int = Field(
        default=5,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MIN_OOS_TRADES"),
    )
    chili_alpha_portfolio_min_oos_avg_return_pct: float = Field(
        default=0.0,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MIN_OOS_AVG_RETURN_PCT"),
    )
    chili_alpha_portfolio_min_oos_win_rate: float = Field(
        default=0.0,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MIN_OOS_WIN_RATE"),
    )
    chili_alpha_portfolio_min_risk_sleeves: int = Field(
        default=3,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MIN_RISK_SLEEVES"),
    )
    chili_alpha_portfolio_min_shadow_score: float = Field(
        default=0.52,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MIN_SHADOW_SCORE"),
    )
    chili_alpha_portfolio_max_shadow_total: int = Field(
        default=4,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MAX_SHADOW_TOTAL"),
    )
    chili_alpha_portfolio_max_shadow_per_sleeve: int = Field(
        default=1,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MAX_SHADOW_PER_SLEEVE"),
    )
    chili_alpha_portfolio_execution_lookback_days: int = Field(
        default=30,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_EXECUTION_LOOKBACK_DAYS"),
    )
    chili_alpha_portfolio_execution_min_samples: int = Field(
        default=10,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_EXECUTION_MIN_SAMPLES"),
    )
    chili_alpha_portfolio_execution_max_p90_slippage_pct: float = Field(
        default=0.75,
        validation_alias=AliasChoices(
            "CHILI_ALPHA_PORTFOLIO_EXECUTION_MAX_P90_SLIPPAGE_PCT"
        ),
    )
    chili_alpha_portfolio_maintenance_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MAINTENANCE_ENABLED"),
    )
    chili_alpha_portfolio_maintenance_interval_minutes: int = Field(
        default=30,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_MAINTENANCE_INTERVAL_MINUTES"),
    )
    chili_alpha_portfolio_auto_queue_recert_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_AUTO_QUEUE_RECERT_ENABLED"),
    )
    chili_alpha_portfolio_sync_realized_on_maintenance: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_SYNC_REALIZED_ON_MAINTENANCE"),
    )
    chili_alpha_portfolio_refresh_quality_on_maintenance: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_REFRESH_QUALITY_ON_MAINTENANCE"),
    )
    chili_alpha_portfolio_auto_stage_shadow_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ALPHA_PORTFOLIO_AUTO_STAGE_SHADOW_ENABLED"),
    )
    # Shadow vetting finalizer. ``shadow_promoted`` is the broker-blocked
    # observation stage; this flag lets the scheduler advance fully scored,
    # top-pool shadow patterns to normal ``promoted`` lifecycle once their
    # directional EV evidence has matured.
    chili_shadow_vetting_finalize_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_SHADOW_VETTING_FINALIZE_ENABLED"),
    )
    chili_shadow_vetting_include_paper_dynamic_outcomes: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_INCLUDE_PAPER_DYNAMIC_OUTCOMES"
        ),
    )
    # Pilot stage: broker-eligible but confidence-sized. This is the
    # non-binary ramp between broker-blocked shadow observation and full
    # promoted sizing.
    chili_pilot_promoted_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_PILOT_PROMOTED_ENABLED"),
    )
    chili_autotrader_live_requires_live_lifecycle: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_LIVE_REQUIRES_LIVE_LIFECYCLE"),
    )
    chili_autotrader_allow_pilot_promoted_live: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_ALLOW_PILOT_PROMOTED_LIVE"),
    )
    # Recert debt means the pilot has not proven the evidence surface the
    # broker-risk lane needs. Keep it observation-only unless an operator
    # deliberately reopens this older bootstrap escape hatch.
    chili_pilot_promoted_allow_bootstrap_recert_live: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_PILOT_PROMOTED_ALLOW_BOOTSTRAP_RECERT_LIVE"),
    )
    chili_autotrader_block_live_on_capital_fallback: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_BLOCK_LIVE_ON_CAPITAL_FALLBACK"),
    )
    chili_autotrader_block_live_on_recert_required: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_BLOCK_LIVE_ON_RECERT_REQUIRED"),
    )
    chili_autotrader_probation_live_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PROBATION_LIVE_ENABLED"),
    )
    chili_autotrader_probation_notional_multiplier: float = Field(
        default=AUTOTRADER_PROBATION_DEFAULT_NOTIONAL_MULTIPLIER,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PROBATION_NOTIONAL_MULTIPLIER"),
    )
    chili_autotrader_probation_max_trades_per_pattern_per_day: int = Field(
        default=AUTOTRADER_PROBATION_DEFAULT_MAX_TRADES_PER_PATTERN_PER_DAY,
        ge=0,
        le=100,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PROBATION_MAX_TRADES_PER_PATTERN_PER_DAY"
        ),
    )
    chili_autotrader_probation_max_trades_per_pattern_ticker_per_day: int = Field(
        default=AUTOTRADER_PROBATION_DEFAULT_MAX_TRADES_PER_PATTERN_TICKER_PER_DAY,
        ge=0,
        le=100,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PROBATION_MAX_TRADES_PER_PATTERN_TICKER_PER_DAY"
        ),
    )
    chili_autotrader_probation_max_trades_per_day: int = Field(
        default=AUTOTRADER_PROBATION_DEFAULT_MAX_TRADES_PER_DAY,
        ge=0,
        le=100,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PROBATION_MAX_TRADES_PER_DAY"),
    )
    chili_autotrader_probation_crypto_max_trades_per_day: int = Field(
        default=AUTOTRADER_PROBATION_DEFAULT_CRYPTO_MAX_TRADES_PER_DAY,
        ge=0,
        le=100,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PROBATION_CRYPTO_MAX_TRADES_PER_DAY"
        ),
    )
    chili_autotrader_probation_crypto_min_expected_net_pct_for_extra_quota: float = Field(
        default=AUTOTRADER_PROBATION_DEFAULT_CRYPTO_MIN_EXPECTED_NET_PCT_FOR_EXTRA_QUOTA,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_PROBATION_CRYPTO_MIN_EXPECTED_NET_PCT_FOR_EXTRA_QUOTA"
        ),
    )
    chili_autotrader_probation_min_cpcv_sharpe: float = Field(
        default=AUTOTRADER_PROBATION_DEFAULT_MIN_CPCV_SHARPE,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PROBATION_MIN_CPCV_SHARPE"),
    )
    chili_autotrader_probation_min_realized_trades: int = Field(
        default=AUTOTRADER_PROBATION_DEFAULT_MIN_REALIZED_TRADES,
        ge=0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PROBATION_MIN_REALIZED_TRADES"),
    )
    chili_autotrader_recert_signal_fastlane_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_RECERT_SIGNAL_FASTLANE_ENABLED"),
    )
    chili_autotrader_shadow_promoted_paper_observation_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SHADOW_PROMOTED_PAPER_OBSERVATION_ENABLED"),
    )
    chili_autotrader_shadow_signal_lane_observation_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SHADOW_SIGNAL_LANE_OBSERVATION_ENABLED"),
    )
    chili_autotrader_shadow_stock_fastlane_enabled: bool = Field(
        default=AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SHADOW_STOCK_FASTLANE_ENABLED"
        ),
        description=(
            "When true, stock shadow observations that already passed the "
            "normal positive-edge rule gate boost their pattern into the "
            "backtest queue. This accelerates evidence collection only; it "
            "does not make shadow-promoted patterns live-tradable."
        ),
    )
    chili_autotrader_shadow_stock_fastlane_backtest_priority: int = Field(
        default=AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_BACKTEST_PRIORITY,
        ge=1,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SHADOW_STOCK_FASTLANE_BACKTEST_PRIORITY"
        ),
    )
    chili_autotrader_shadow_stock_fastlane_min_expected_net_pct: float = Field(
        default=AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_MIN_EXPECTED_NET_PCT,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SHADOW_STOCK_FASTLANE_MIN_EXPECTED_NET_PCT"
        ),
    )
    chili_autotrader_shadow_stock_fastlane_lifecycle_stages: str = Field(
        default=AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_LIFECYCLE_STAGES,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SHADOW_STOCK_FASTLANE_LIFECYCLE_STAGES"
        ),
    )
    chili_autotrader_shadow_stock_fastlane_reboost_cooldown_minutes: float = Field(
        default=AUTOTRADER_SHADOW_STOCK_FASTLANE_DEFAULT_REBOOST_COOLDOWN_MINUTES,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SHADOW_STOCK_FASTLANE_REBOOST_COOLDOWN_MINUTES"
        ),
        description=(
            "Minimum minutes after a pattern backtest before stock shadow "
            "fastlane may re-boost the same pattern. Default follows the "
            "imminent scanner cadence so one scanner wave cannot churn the "
            "same pattern through repeated backtests."
        ),
    )
    chili_autotrader_shadow_observation_diagnostic_sizing_enabled: bool = Field(
        default=AUTOTRADER_SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_DEFAULT_ENABLED,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SHADOW_OBSERVATION_DIAGNOSTIC_SIZING_ENABLED"
        ),
        description=(
            "When true, shadow-observation-only entries run the full advisory "
            "sizing diagnostics before opening paper evidence. The default "
            "uses base risk-notional sizing so shadow learning cannot monopolize "
            "the live AutoTrader tick."
        ),
    )
    chili_autotrader_shadow_observation_evidence_notional_usd: float = Field(
        default=AUTOTRADER_SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_DEFAULT_USD,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_SHADOW_OBSERVATION_EVIDENCE_NOTIONAL_USD"
        ),
        description=(
            "Optional fixed paper-only notional for lightweight shadow "
            "observations. Leave at 0 to derive the evidence notional from "
            "assumed capital and per-trade risk percent without hitting the "
            "broker equity path."
        ),
    )
    chili_autotrader_live_require_venue_health_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_LIVE_REQUIRE_VENUE_HEALTH_ENABLED"),
    )
    chili_autotrader_rth_only: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_RTH_ONLY"),
    )
    chili_autotrader_allow_extended_hours: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_ALLOW_EXTENDED_HOURS"),
        description=(
            "Entry gate only. When true and chili_autotrader_rth_only is also "
            "true, stock entries may run during Mon-Fri US/Eastern 04:00-20:00 "
            "(pre + RTH + post) instead of RTH only. Open-position monitoring "
            "runs independently from the broker source attached to the trade."
        ),
    )
    chili_autotrader_llm_revalidation_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_LLM_REVALIDATION_ENABLED"),
    )
    chili_autotrader_llm_revalidation_skip_shadow_observation: bool = Field(
        default=AUTOTRADER_LLM_REVALIDATION_DEFAULT_SKIP_SHADOW_OBSERVATION,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_LLM_REVALIDATION_SKIP_SHADOW_OBSERVATION"
        ),
    )
    chili_autotrader_llm_revalidation_skip_options_path: bool = Field(
        default=AUTOTRADER_LLM_REVALIDATION_DEFAULT_SKIP_OPTIONS_PATH,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_LLM_REVALIDATION_SKIP_OPTIONS_PATH"
        ),
        description=(
            "When true, skip the equities-shaped LLM revalidation gate for "
            "options alerts. Option viability is handled by the deterministic "
            "option entry-quality model so premium and underlying prices are "
            "not mixed in the LLM payload."
        ),
    )
    # Task KK — gate the autotrader's crypto path. Robinhood crypto trades
    # 24/7 with no PDT regulation, so when this flag is ON the rule gate
    # accepts asset_type='crypto' alerts, skips the RTH/extended-hours
    # session check for them, and the venue adapter routes to RH's crypto
    # order endpoints. Default OFF so behavior is identical to pre-KK
    # until the operator has done a paper round-trip.
    chili_autotrader_crypto_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_CRYPTO_ENABLED"),
        description=(
            "When true, the autotrader will execute imminent-pattern entries "
            "for crypto-USD tickers via Robinhood's crypto endpoints, "
            "bypassing market-hours and PDT gates. Equity behavior unchanged."
        ),
    )
    # Task MM Phase 2 — when true, the autotrader rule gate accepts
    # alerts with asset_type='options' and routes them through the
    # RobinhoodOptionsAdapter. The alert must carry option metadata
    # (strike, expiration, option_type) in indicator_snapshot.option_meta;
    # most equity-shaped gates (price cap, slippage, projected profit)
    # are bypassed because the operator-driven entry encodes its own
    # limit price + sizing. Kill-switch / drawdown / concurrent-limit
    # still apply. Default OFF until paper round-trips validate.
    chili_autotrader_options_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_OPTIONS_ENABLED"),
        description=(
            "When true, the autotrader will execute pattern_imminent alerts "
            "with asset_type='options' via the Robinhood options adapter. "
            "Requires CHILI_OPTIONS_VENUE_ROBINHOOD_ENABLED=true at the "
            "venue layer."
        ),
    )
    # Task NN Phase 3 — substitute equity entries with options. When
    # ON, bullish equity pattern_imminent alerts get synthesized into
    # ATM call entries (~30 DTE) before the rule gate. Skip the
    # substitution if the option chain is illiquid (spread > 15%) or
    # no tradable contract exists near ATM. Requires both
    # chili_autotrader_options_enabled AND chili_options_venue_robinhood_enabled.
    chili_autotrader_options_substitute_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_OPTIONS_SUBSTITUTE_ENABLED"),
        description=(
            "When true, bullish equity entries are translated to long "
            "ATM calls instead of stock buys. Substitution is skipped "
            "(falls back to equity) when the chain is illiquid."
        ),
    )
    chili_autotrader_options_substitute_requires_underlying_positive_edge: bool = Field(
        default=AUTOTRADER_OPTIONS_SUBSTITUTE_DEFAULT_REQUIRES_UNDERLYING_POSITIVE_EDGE,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_OPTIONS_SUBSTITUTE_REQUIRES_UNDERLYING_POSITIVE_EDGE"
        ),
        description=(
            "When true, equity-to-options substitution first requires the "
            "underlying stock setup to pass expected-net-edge evaluation. "
            "This prevents expensive option-chain synthesis from bypassing "
            "the live positive-edge discipline."
        ),
    )
    # DTE target for substitution (calendar days). Default 30 for the
    # theta-vs-gamma sweet spot.
    chili_autotrader_options_substitute_dte: int = Field(
        default=30,
        ge=1,
        le=365,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_OPTIONS_SUBSTITUTE_DTE"),
    )
    # Options entry-quality gates. These bootstrap StrategyParameter rows in
    # the autotrader_options family so the brain can adapt the values after
    # realized option outcomes accumulate. Defaults are economic break-even
    # identities: reward/risk parity and non-negative expected value.
    chili_autotrader_options_min_underlying_reward_risk: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_OPTIONS_MIN_UNDERLYING_REWARD_RISK"
        ),
        description=(
            "Minimum reward/risk of the underlying target-vs-stop scenario "
            "before an equity signal may be substituted into an option."
        ),
    )
    chili_autotrader_options_min_option_reward_risk: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_OPTIONS_MIN_OPTION_REWARD_RISK"
        ),
        description=(
            "Minimum option payoff reward/risk at the underlying target and "
            "stop before an option substitution may enter."
        ),
    )
    chili_autotrader_options_min_expected_value_pct: float = Field(
        default=0.0,
        ge=-100.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_OPTIONS_MIN_EXPECTED_VALUE_PCT"
        ),
        description=(
            "Minimum expected value as a percent of option premium, using "
            "the alert confidence as the directional probability input."
        ),
    )
    chili_autotrader_options_max_contract_notional_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_OPTIONS_MAX_CONTRACT_NOTIONAL_USD"
        ),
        description=(
            "Maximum premium dollars allowed for one option contract. "
            "Set to 0 to use CHILI_AUTOTRADER_PER_TRADE_NOTIONAL_USD as "
            "the cap."
        ),
    )
    # Option-substitution miss cache. Keeps repeated no-survivor searches
    # from monopolizing the autotrader tick while preserving every quality gate.
    chili_autotrader_options_synthesis_no_survivor_cache_ttl_seconds: int = Field(
        default=AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS,
        ge=AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_MIN_TTL_SECONDS,
        le=AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_MAX_TTL_SECONDS,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_TTL_SECONDS"
        ),
        description=(
            "Seconds to suppress repeated option-substitution synthesis for "
            "the same recently rejected contract-search context. Set 0 to "
            "disable. This is an execution-throughput cache, not an entry "
            "quality override."
        ),
    )
    # Task PP Phase 5 — option-aware exit monitor. When ON, the scheduler
    # ticks the options_exit_pass which closes open option Trade rows
    # on three triggers: DTE threshold (default 7d), premium stop-loss
    # (default 50% drop), premium take-profit (default 100% gain).
    # Decoupled from chili_autotrader_options_enabled so the operator
    # can flip the entry path on without flipping the exit monitor on
    # (manual close mode), and vice versa.
    chili_autotrader_options_exit_monitor_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_OPTIONS_EXIT_MONITOR_ENABLED"),
    )
    chili_autotrader_options_exit_dte: int = Field(
        default=7,
        ge=0,
        le=180,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_OPTIONS_EXIT_DTE"),
        description="DTE threshold below which open options auto-close.",
    )
    chili_autotrader_options_exit_stop_pct: float = Field(
        default=50.0,
        ge=1.0,
        le=99.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_OPTIONS_EXIT_STOP_PCT"),
        description="Premium drop %% below entry that triggers stop-loss exit.",
    )
    chili_autotrader_options_exit_tp_pct: float = Field(
        default=100.0,
        ge=1.0,
        le=10000.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_OPTIONS_EXIT_TP_PCT"),
        description="Premium gain %% above entry that triggers take-profit exit.",
    )
    chili_autotrader_assumed_capital_usd: float = Field(
        default=25_000.0,
        ge=100.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_ASSUMED_CAPITAL_USD"),
    )
    chili_autotrader_tick_interval_seconds: int = Field(
        default=AUTOTRADER_SCHEDULER_TICK_INTERVAL_DEFAULT_SECONDS,
        ge=5,
        le=120,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_TICK_INTERVAL_SECONDS"),
    )

    # XX — concurrency safety. ``max_instances`` lets a slow tick coexist
    # with subsequent fast ticks (per-alert advisory locks prevent races on
    # the same alert; audit-row check is the second line of defense).
    # ``tick_max_seconds`` is the hard wall-clock budget enforced by the
    # outer wrapper in ``_run_auto_trader_tick_job`` — a tick exceeding it
    # is abandoned (its worker continues until its socket times out, but
    # the scheduler slot is freed). ``misfire_grace_s`` lets a missed
    # schedule still fire if it's only a few seconds late instead of
    # being dropped entirely.
    chili_autotrader_tick_max_instances: int = Field(
        default=1,
        ge=1,
        le=10,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_TICK_MAX_INSTANCES"),
    )
    chili_autotrader_tick_max_seconds: int = Field(
        default=AUTOTRADER_DEFAULT_TICK_MAX_SECONDS,
        ge=AUTOTRADER_MIN_TICK_MAX_SECONDS,
        le=AUTOTRADER_MAX_TICK_MAX_SECONDS,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_TICK_MAX_SECONDS"),
    )
    chili_autotrader_tick_misfire_grace_s: int = Field(
        default=30,
        ge=0,
        le=300,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_TICK_MISFIRE_GRACE_S"),
    )

    # AAA -- janitor threshold for terminating leaked autotrader
    # advisory-lock holders. When XX outer wall-clock budget abandons a
    # hung worker thread, the thread DB session stays alive holding the
    # lock; the janitor (runs at the start of every tick) terminates
    # sessions stuck "idle in transaction" older than this threshold.
    # Default 120s -- well past 45s tick budget so legitimate slow ticks
    # are never killed.
    chili_autotrader_leak_cleanup_threshold_s: int = Field(
        default=120,
        ge=60,
        le=900,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_LEAK_CLEANUP_THRESHOLD_S"),
    )

    # HHH -- crypto exit monitor flag. Default on so the entry/exit
    # halves of KK ship together.
    chili_autotrader_crypto_exit_monitor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_CRYPTO_EXIT_MONITOR_ENABLED"),
    )

    chili_autotrader_crypto_exit_missing_qty_backoff_seconds: int = Field(
        default=CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_SECONDS,
        ge=CRYPTO_EXIT_MISSING_QTY_BACKOFF_MIN_SECONDS,
        le=CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_SECONDS,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_CRYPTO_EXIT_MISSING_QTY_BACKOFF_SECONDS"
        ),
    )
    chili_autotrader_crypto_exit_missing_qty_backoff_start_streak: int = Field(
        default=CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_START_STREAK,
        ge=1,
        le=CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_START_STREAK,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_CRYPTO_EXIT_MISSING_QTY_BACKOFF_START_STREAK"
        ),
    )

    # YY — drawdown breaker scope. When True (default), the breaker only
    # measures P&L from CHILI-placed trades (auto_trader_version IS NOT
    # NULL or management_scope='auto_trader_v1'). Pre-CHILI manual
    # positions are invisible to the breaker. Set False to revert to the
    # legacy "all trades count" behavior.
    chili_breaker_scope_autotrader_only: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_BREAKER_SCOPE_AUTOTRADER_ONLY"),
    )

    # f-phase3-stop-bleed D1 — empirical monthly drawdown breaker
    # (CHILI-attributed / pattern tier).
    # RENAMED 2026-05-16 by f-portfolio-vs-pattern-breaker-separation D4:
    # chili_monthly_dd_breaker_enabled → chili_pattern_dd_breaker_enabled
    # to disambiguate from the new portfolio tier. AliasChoices keeps the
    # legacy env var honored for one release; the legacy alias will be
    # removed in a follow-up brief once operators confirm migration.
    # Default OFF until walk-forward shows it would have tripped on/around
    # 2026-04-22 (the cumulative-PnL trough date from the 2026-05-15 audit).
    # When ON, ``check_drawdown_breaker`` computes a Gaussian lower-bound on
    # 30-day realized PnL from the trailing 180d of CHILI-attributed history
    # and trips when actual 30d PnL falls below it. No fallback dollar value
    # (see COWORK_ADVISOR_BRIEF §2.6); when history is <30d the check skips
    # with a logged warning.
    chili_pattern_dd_breaker_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DD_BREAKER_ENABLED",
            "CHILI_MONTHLY_DD_BREAKER_ENABLED",  # legacy alias (one-release deprecation)
        ),
    )
    # K — sigma multiplier for the Gaussian lower-bound. 2.0 = 95% one-sided;
    # 3.0 = ~99.7%. Tighter K means the breaker is less likely to false-trip
    # but more likely to miss a real bleed; looser K is the inverse.
    chili_pattern_dd_breaker_lower_bound_sigmas: float = Field(
        default=2.0,
        ge=0.5,
        le=5.0,
        validation_alias=AliasChoices(
            "CHILI_PATTERN_DD_BREAKER_LOWER_BOUND_SIGMAS",
            "CHILI_MONTHLY_DD_BREAKER_LOWER_BOUND_SIGMAS",  # legacy
        ),
    )

    # f-portfolio-vs-pattern-breaker-separation — portfolio-tier drawdown
    # breaker. Gates EVERY entry path (CHILI-attributed, no_pattern, manual,
    # reconcile-inferred) at the venue-adapter boundary against an
    # all-closed PnL distribution. Default-OFF. Lives next to the pattern
    # tier; the two are independent (D5 in the brief) and each gates only
    # what its trip signal can act on. The pattern tier gates
    # CHILI-attributed entries from the autotrader; the portfolio tier
    # gates every BUY entry from the venue adapters regardless of source.
    chili_portfolio_dd_breaker_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_PORTFOLIO_DD_BREAKER_ENABLED"),
    )
    # 7-day shadow-soak gate. When enabled=True AND live=False the breaker
    # computes the "would have tripped" decision, persists a shadow row to
    # trading_risk_state (regime='portfolio_breaker_shadow'), and logs a
    # structured INFO line — but DOES NOT block entries. Flip to True
    # after the operator reviews the shadow-log output and confirms the
    # tier is calibrated correctly.
    chili_portfolio_dd_breaker_live: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_PORTFOLIO_DD_BREAKER_LIVE"),
    )
    # K — sigma multiplier for the portfolio-tier Gaussian lower-bound.
    # Default 2.0 matches the pattern tier initially; the two tune
    # independently because their distributions differ — all-closed is
    # wider than CHILI-attributed in today's data per the 2026-05-15
    # quant audit.
    chili_portfolio_dd_breaker_lower_bound_sigmas: float = Field(
        default=2.0,
        ge=0.5,
        le=5.0,
        validation_alias=AliasChoices(
            "CHILI_PORTFOLIO_DD_BREAKER_LOWER_BOUND_SIGMAS",
        ),
    )
    # Knob to silence shadow-log emission if the daily volume becomes
    # noisy (e.g. the breaker would-have-tripped multiple times/day and
    # the log lines mask other signals). Default-ON when the tier is
    # enabled; operator flips False to silence. Does NOT affect live
    # blocking behavior — only the shadow-log row + INFO line.
    chili_portfolio_dd_breaker_shadow_log_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_PORTFOLIO_DD_BREAKER_SHADOW_LOG_ENABLED",
        ),
    )

    # f-phase3-stop-bleed D4 — Coinbase placement pre-flight cash check.
    # The 2026-05-15 audit's last-7d rejection histogram shows 830
    # ``broker:Insufficient balance`` errors -- we lose race conditions
    # between our buying_power resolver and the placement call. A local
    # pre-flight refuses the call when our cached buying_power is already
    # below the order's required notional (broker is still final check).
    # Fee slack and the stale-cache tolerance are settings-sourced (no
    # magic constants -- COWORK_ADVISOR_BRIEF §2.6).
    chili_coinbase_preflight_fee_slack_bps: float = Field(
        default=50.0,
        ge=0.0,
        le=500.0,
        validation_alias=AliasChoices("CHILI_COINBASE_PREFLIGHT_FEE_SLACK_BPS"),
    )
    chili_coinbase_preflight_max_stale_seconds: float = Field(
        default=5.0,
        ge=0.0,
        le=300.0,
        validation_alias=AliasChoices("CHILI_COINBASE_PREFLIGHT_MAX_STALE_SECONDS"),
    )

    # Phase B (tech-debt): TTL cache on broker-equity lookups so a flapping
    # broker does not amplify into a per-tick retry storm. When enabled, the
    # first call per ``chili_autotrader_broker_equity_cache_ttl_seconds`` window
    # hits the broker; subsequent calls in-window return the cached equity
    # tagged ``cache:fresh``. When the broker is unreachable and a prior
    # successful value exists, the cache serves it tagged ``cache:stale``
    # (up to ``chili_autotrader_broker_equity_cache_max_stale_seconds``) so
    # the sizing logic degrades gracefully instead of collapsing to the env
    # default. Defaults disabled — flip to true after a paper-mode soak.
    chili_autotrader_broker_equity_cache_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_BROKER_EQUITY_CACHE_ENABLED"),
    )
    chili_autotrader_broker_equity_cache_ttl_seconds: int = Field(
        default=300,  # 5 min fresh window
        ge=10,
        le=3600,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_BROKER_EQUITY_CACHE_TTL_SECONDS"),
    )
    chili_autotrader_broker_equity_cache_max_stale_seconds: int = Field(
        default=900,  # serve stale up to 15 min during a broker outage
        ge=0,
        le=7200,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_BROKER_EQUITY_CACHE_MAX_STALE_SECONDS"),
    )

    brain_market_snapshot_scheduler_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_MARKET_SNAPSHOT_SCHEDULER_ENABLED"),
    )
    brain_market_snapshot_interval_minutes: int = Field(
        default=15,
        ge=5,
        validation_alias=AliasChoices("BRAIN_MARKET_SNAPSHOT_INTERVAL_MINUTES"),
    )

    # Pattern backtests (backtesting.py): spread = constant bid/ask friction as fraction of price.
    # Combined proxy for half-spread + slippage (e.g. 0.0002 â‰ˆ 2 bps per side order of magnitude).
    backtest_spread: float = 0.0002
    backtest_commission: float = 0.001
    # Hold out the last fraction of bars for out-of-sample metrics when training/evaluating patterns.
    brain_oos_holdout_fraction: float = 0.25
    brain_oos_gate_enabled: bool = True
    brain_oos_min_win_rate_pct: float = 42.0
    brain_oos_max_is_oos_gap_pct: float = 38.0
    brain_oos_min_evaluated_tickers: int = 3
    brain_oos_min_aggregate_trades: int = 15
    # Optional stricter OOS gates (None = use brain_oos_min_win_rate_pct / gap only). Applied in
    # brain_oos_gate_kwargs_for_pattern when timeframe, asset_class, or hypothesis_family matches.
    brain_oos_min_win_rate_pct_short_tf: Optional[float] = None
    brain_oos_min_win_rate_pct_crypto: Optional[float] = None
    brain_oos_min_win_rate_pct_high_vol_family: Optional[float] = None
    brain_oos_min_oos_trades_short_tf: Optional[int] = None
    brain_oos_min_oos_trades_crypto: Optional[int] = None
    brain_oos_min_oos_trades_high_vol_family: Optional[int] = None

    # Per learning-cycle resource caps (miners). Zero cap = unlimited for that dimension.
    brain_budget_ohlcv_per_cycle: int = 280
    brain_budget_miner_rows_per_cycle: int = 100000
    brain_budget_pattern_injects_per_cycle: int = 32
    brain_budget_miner_error_trip: int = 5

    # Secondary miners: high-vol regime hypothesis (crypto 15m) â€” additive to compression intraday miner.
    brain_high_vol_miner_enabled: bool = True
    # Spawn ScanPattern from miner stats; must still pass normal backtest/OOS gates.
    # Q1.T8 follow-up: investigated and reclassified Category 4 -> Category 1
    # (diagnostic-safe-to-flip). The bridge enqueues at most one candidate pattern
    # per cycle ("Brain miner: BB squeeze prescreen (15m)") which then has to earn
    # promotion through the same OOS gates as any other candidate. See
    # docs/FEATURE_FLAG_AUDIT.md > "Resolved: brain_miner_scanpattern_bridge_enabled".
    brain_miner_scanpattern_bridge_enabled: bool = True

    # Benchmark walk-forward (SPY/QQQ-style) after hypothesis test; optional extra promotion gate.
    brain_bench_walk_forward_enabled: bool = True
    brain_bench_walk_forward_gate_enabled: bool = True
    brain_bench_tickers: str = "SPY,QQQ"
    brain_bench_period: str = "10y"
    brain_bench_interval: str = "1d"
    brain_bench_n_windows: int = 8
    brain_bench_min_bars_per_window: int = 35
    brain_bench_min_positive_fold_ratio: float = 0.375
    # Cost-stress benchmark: multiply spread/commission for a second walk-forward eval (stored on bench JSON).
    brain_bench_cost_stress_spread_mult: float = 2.0
    brain_bench_cost_stress_commission_mult: float = 1.5
    # When True, stress eval must also pass passes_gate (stress uses mults above; code bumps 1.0/1.0 to 2.0/1.5 if needed).
    brain_bench_require_stress_pass: bool = False

    # OOS gates beyond win rate (None = off). Enabling thins promotions; set in .env only on purpose.
    brain_min_trades_for_promotion: int = 30
    brain_oos_min_expectancy_pct: Optional[float] = None
    brain_oos_min_profit_factor: Optional[float] = None
    # Extra holdout fractions on the same OHLCV window (comma-separated); empty = primary holdout only.
    brain_oos_robustness_extra_fractions: str = ""
    # If True, min OOS win rate must hold for every evaluated extra holdout (per ticker), not only primary.
    brain_oos_require_robustness_wr_above_gate: bool = False
    # Bootstrap on vector of per-ticker OOS win rates (0 = disabled).
    brain_oos_bootstrap_iterations: int = 500
    # Reject promotion when bootstrap CI lower bound for mean OOS WR is below this (None = skip).
    brain_oos_bootstrap_ci_min_wr: Optional[float] = 0.42

    # Edge-vs-luck (v1 weak-null permutations) for OOS-gated repeatable-edge patterns only.
    brain_edge_evidence_enabled: bool = True
    brain_edge_evidence_gate_enabled: bool = True
    brain_edge_evidence_permutations: int = 400
    brain_edge_evidence_seed: int = 42
    brain_edge_evidence_max_is_perm_p: Optional[float] = None
    brain_edge_evidence_max_oos_perm_p: float = 0.20
    brain_edge_evidence_max_wf_perm_p: float = 0.25
    brain_edge_evidence_require_wf_when_available: bool = False
    brain_edge_evidence_fdr_enabled: bool = True
    brain_edge_evidence_fdr_q: float = 0.10

    # Phase 2 research hygiene (repeatable-edge lane): slice burn ledger + optional stability probe.
    brain_selection_bias_enabled: bool = True
    brain_parameter_stability_enabled: bool = True
    brain_parameter_stability_seed: int = 123
    brain_parameter_stability_ticker_subset_size: int = 2
    brain_parameter_stability_max_variant_evals: int = 6
    brain_parameter_stability_neighbor_rel_tol: float = 0.12
    brain_parameter_stability_neighbor_abs_floor: float = 40.0
    brain_phase2_hygiene_nudge_enabled: bool = True

    # Two-tier queue: cheap prescreen then full backtest (final OOS gate unchanged).
    brain_queue_prescreen_enabled: bool = True
    brain_queue_prescreen_tickers: int = 6
    # Daily prescreen job (America/Los_Angeles); persists candidates for scan step.
    brain_prescreen_scheduler_enabled: bool = True
    # Full market scan after prescreen (America/Los_Angeles); populates trading_scans.
    brain_daily_market_scan_scheduler_enabled: bool = True
    brain_prescreen_internal_max_per_kind: int = 40
    brain_prescreen_max_total: int = 3000
    brain_queue_prescreen_period: str = "3mo"
    brain_queue_prescreen_min_win_rate_pct: float = 45.0
    # Queue ``smart_backtest_insight`` normally samples only ``brain_queue_target_tickers`` random tickers
    # and marks the pattern tested for ~``brain_retest_interval_days`` — stored evidence rows for other
    # tickers were never refreshed. When True, prepend tickers whose stored rows look stale or under-traded.
    brain_queue_priority_stored_refresh: bool = True
    brain_queue_stored_refresh_max_tickers: int = 40
    brain_queue_stored_stale_trade_cap: int = 2  # refresh if stored trade_count <= this (captures 0–2 trade rows)
    brain_queue_stored_stale_days: int = 14  # refresh if ran_at older than this (UTC)

    # Live vs research: downgrade patterns when realized win rate lags research OOS materially.
    brain_live_depromotion_enabled: bool = True
    brain_live_depromotion_min_closed_trades: int = 8
    brain_live_depromotion_max_gap_pct: float = 25.0

    # Live/paper drift vs research baseline (repeatable-edge promoted/live only).
    brain_live_drift_window_days: int = 120
    brain_live_drift_live_min_primary: int = 8
    brain_live_drift_min_trades: int = 12
    brain_live_drift_baseline_p0_low: float = 0.05
    brain_live_drift_baseline_p0_high: float = 0.95
    brain_live_drift_warning_delta_pp: float = 8.0
    brain_live_drift_critical_delta_pp: float = 18.0
    brain_live_drift_strong_p_like: float = 0.02
    brain_live_drift_confidence_nudge_enabled: bool = True
    brain_live_drift_confidence_mult_healthy: float = 1.0
    brain_live_drift_confidence_mult_warning: float = 0.94
    brain_live_drift_confidence_mult_critical: float = 0.88
    brain_live_drift_confidence_floor: float = 0.1
    brain_live_drift_confidence_cap: float = 0.95
    brain_live_drift_auto_challenged_enabled: bool = True
    brain_live_drift_auto_challenged_max_p_like: float = 0.02
    brain_live_drift_v2_enabled: bool = True
    brain_live_drift_shadow_mode: bool = False
    brain_live_drift_v2_warn_expectancy_ratio: float = 0.7
    brain_live_drift_v2_critical_expectancy_ratio: float = 0.4
    brain_live_drift_v2_warn_profit_factor: float = 1.0
    brain_live_drift_v2_critical_profit_factor: float = 0.8
    brain_live_drift_v2_warn_slippage_bps: float = 25.0
    brain_live_drift_v2_critical_slippage_bps: float = 45.0

    # Execution robustness from linked Trade rows (repeatable-edge promoted/live).
    brain_execution_robustness_window_days: int = 120
    brain_execution_robustness_min_orders: int = 5
    brain_execution_robustness_warn_fill_rate: float = 0.65
    brain_execution_robustness_critical_fill_rate: float = 0.45
    brain_execution_robustness_warn_slippage_bps: float = 35.0
    brain_execution_robustness_critical_slippage_bps: float = 65.0
    brain_execution_robustness_live_not_recommended: bool = True
    brain_execution_robustness_flag_weak_truth_live: bool = True
    brain_execution_robustness_hard_block_live_enabled: bool = False
    brain_execution_robustness_v2_enabled: bool = True
    brain_execution_robustness_shadow_mode: bool = True
    brain_execution_robustness_v2_live_not_recommended: bool = False
    brain_execution_robustness_v2_hard_block_live_enabled: bool = False

    # When a pattern is promoted, initialize paper_book_json for optional shadow tracking.
    brain_paper_book_on_promotion: bool = False

    # OHLCV quality: log warnings from assess_ohlcv_bar_quality; strict can skip miner rows (future hook).
    brain_bar_quality_strict: bool = False
    brain_bar_quality_max_gap_bars: int = 5

    # Data retention policy (days). Used by data_retention.py scheduler job.
    brain_retention_snapshot_days: int = 180
    brain_retention_batch_job_days: int = 90
    brain_retention_event_days: int = 120
    brain_retention_alert_days: int = 90
    brain_retention_backtest_days: int = 180
    brain_retention_prediction_days: int = 30
    brain_retention_cycle_run_days: int = 180
    brain_retention_integration_event_days: int = 90
    brain_retention_prescreen_days: int = 90
    brain_retention_pattern_trade_days: int = 365
    brain_retention_proposal_days: int = 90
    brain_retention_paper_trade_days: int = 180
    brain_retention_hypothesis_days: int = 180
    brain_retention_breakout_alert_days: int = 180
    brain_retention_exit_parity_backtest_days: int = 7
    brain_retention_exit_parity_live_days: int = 30
    brain_retention_exit_parity_delete_batch_size: int = 50_000
    brain_retention_bracket_reconciliation_days: int = 30
    brain_retention_execution_event_days: int = 180
    brain_retention_fast_snapshot_days: int = 30
    brain_retention_fast_orderbook_days: int = 3
    brain_retention_fast_alert_days: int = 14
    brain_retention_fast_execution_days: int = 30
    brain_retention_fast_exit_days: int = 90
    brain_retention_fast_delete_batch_size: int = 50_000
    brain_retention_fast_partition_maintenance_enabled: bool = False
    brain_retention_fast_partition_days_ahead: int = 7
    brain_retention_fast_partition_max_default_bytes: int = 1_000_000_000
    brain_retention_fast_drop_partitions_enabled: bool = True

    # Portfolio: max simultaneous open longs per coarse sector (0 = disabled).
    brain_max_open_per_sector: int = 0
    brain_max_correlated_positions: int = 0
    brain_allocator_enabled: bool = True
    brain_allocator_shadow_mode: bool = True
    brain_allocator_live_soft_block_enabled: bool = False
    brain_allocator_live_hard_block_enabled: bool = False
    brain_allocator_incumbent_score_margin: float = 0.08
    brain_allocator_max_active_risk_items: int = Field(
        default=0,
        validation_alias=AliasChoices("CHILI_BRAIN_ALLOCATOR_MAX_ACTIVE_RISK_ITEMS"),
        description="Max open trades + active live automation sessions before allocator blocks; 0 disables.",
    )
    brain_allocator_max_live_notional_usd: float = Field(
        default=0.0,
        validation_alias=AliasChoices("CHILI_BRAIN_ALLOCATOR_MAX_LIVE_NOTIONAL_USD"),
        description="Max estimated live notional after a new allocation; 0 disables.",
    )
    brain_allocator_max_same_family_live_sessions: int = Field(
        default=0,
        validation_alias=AliasChoices("CHILI_BRAIN_ALLOCATOR_MAX_SAME_FAMILY_LIVE_SESSIONS"),
        description="Max active live automation sessions per strategy/hypothesis family; 0 disables.",
    )
    chili_cash_deployment_min_closed_evidence: int = Field(
        default=5,
        ge=0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_MIN_CLOSED_EVIDENCE"),
        description="Minimum closed paper/live evidence rows before cash-deployment ranking can mark a pattern live-deployable.",
    )
    chili_cash_deployment_max_brier_score: float = Field(
        default=0.28,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_MAX_BRIER_SCORE"),
        description="Probability calibration ceiling for live-deployable cash-deployment candidates.",
    )
    chili_cash_deployment_max_abs_paper_live_gap_pct: float = Field(
        default=3.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_MAX_ABS_PAPER_LIVE_GAP_PCT"),
        description="Absolute paper-vs-live EV gap above which cash deployment stays calibration-blocked.",
    )
    chili_cash_deployment_equity_cost_pct: float = Field(
        default=0.05,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_EQUITY_COST_PCT"),
        description="Conservative equity execution-cost drag used by the cash-deployment diagnostics.",
    )
    chili_cash_deployment_crypto_cost_pct: float = Field(
        default=0.25,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_CRYPTO_COST_PCT"),
        description="Conservative crypto execution-cost drag used by the cash-deployment diagnostics.",
    )
    chili_cash_deployment_options_cost_pct: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_OPTIONS_COST_PCT"),
        description="Conservative option premium/spread execution-cost drag used by the cash-deployment diagnostics.",
    )
    chili_cash_deployment_unknown_asset_cost_pct: float = Field(
        default=0.35,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_UNKNOWN_ASSET_COST_PCT"),
        description="Fallback execution-cost drag for cash-deployment rows whose asset class is not yet explicit.",
    )
    chili_cash_deployment_slippage_miss_penalty_pct: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_SLIPPAGE_MISS_PENALTY_PCT"),
        description="Extra EV drag applied when recent positive candidates missed entry on slippage.",
    )
    chili_cash_deployment_broker_reject_penalty_pct: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CASH_DEPLOYMENT_BROKER_REJECT_PENALTY_PCT"),
        description="Extra EV drag applied to patterns with recent broker rejects; hard execution blockers still prevent live deployability.",
    )

    # Decision ledger + net-expectancy allocator (momentum autopilot / brain). Live enforcement OFF by default.
    brain_enable_decision_ledger: bool = Field(default=True, validation_alias=AliasChoices("CHILI_BRAIN_ENABLE_DECISION_LEDGER"))
    brain_decision_packet_required_for_runners: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_DECISION_PACKET_REQUIRED_FOR_RUNNERS")
    )
    brain_decision_packet_required_for_proposals: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_DECISION_PACKET_REQUIRED_FOR_PROPOSALS")
    )
    brain_expectancy_allocator_shadow_mode: bool = Field(
        default=False, validation_alias=AliasChoices("CHILI_BRAIN_EXPECTANCY_ALLOCATOR_SHADOW_MODE")
    )
    brain_opportunity_board_decision_packets_enabled: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_OPPORTUNITY_BOARD_DECISION_PACKETS_ENABLED")
    )
    brain_alert_decision_packets_enabled: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_ALERT_DECISION_PACKETS_ENABLED")
    )
    brain_enable_execution_realism: bool = Field(default=True, validation_alias=AliasChoices("CHILI_BRAIN_ENABLE_EXECUTION_REALISM"))
    brain_enable_capacity_governor: bool = Field(default=True, validation_alias=AliasChoices("CHILI_BRAIN_ENABLE_CAPACITY_GOVERNOR"))
    brain_enable_deployment_ladder: bool = Field(default=True, validation_alias=AliasChoices("CHILI_BRAIN_ENABLE_DEPLOYMENT_LADDER"))
    brain_minimum_net_expectancy_to_trade: float = Field(
        default=0.0, validation_alias=AliasChoices("CHILI_BRAIN_MINIMUM_NET_EXPECTANCY_TO_TRADE")
    )
    brain_enforce_net_expectancy_paper: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_ENFORCE_NET_EXPECTANCY_PAPER")
    )
    brain_enforce_net_expectancy_live: bool = Field(
        default=False, validation_alias=AliasChoices("CHILI_BRAIN_ENFORCE_NET_EXPECTANCY_LIVE")
    )
    brain_capacity_hard_block_paper: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_CAPACITY_HARD_BLOCK_PAPER")
    )
    brain_capacity_hard_block_live: bool = Field(
        default=False, validation_alias=AliasChoices("CHILI_BRAIN_CAPACITY_HARD_BLOCK_LIVE")
    )
    brain_paper_deployment_enforcement: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_PAPER_DEPLOYMENT_ENFORCEMENT")
    )
    brain_live_deployment_enforcement: bool = Field(
        default=False, validation_alias=AliasChoices("CHILI_BRAIN_LIVE_DEPLOYMENT_ENFORCEMENT")
    )
    brain_max_adv_notional_pct: float = Field(
        default=0.25,
        validation_alias=AliasChoices("CHILI_BRAIN_MAX_ADV_NOTIONAL_PCT"),
        description="Max position notional as fraction of crude ADV USD proxy (0 disables)",
    )
    brain_peer_candidate_sessions_max: int = Field(default=4, validation_alias=AliasChoices("CHILI_BRAIN_PEER_CANDIDATE_SESSIONS_MAX"))
    brain_deployment_promote_min_paper_trades: int = Field(default=3, validation_alias=AliasChoices("CHILI_BRAIN_DEPLOYMENT_PROMOTE_MIN_PAPER_TRADES"))
    brain_deployment_degrade_drawdown_pct: float = Field(default=8.0, validation_alias=AliasChoices("CHILI_BRAIN_DEPLOYMENT_DEGRADE_DRAWDOWN_PCT"))
    brain_deployment_degrade_slippage_bps: float = Field(default=35.0, validation_alias=AliasChoices("CHILI_BRAIN_DEPLOYMENT_DEGRADE_SLIPPAGE_BPS"))
    brain_deployment_degrade_missed_fill_rate: float = Field(default=0.35, validation_alias=AliasChoices("CHILI_BRAIN_DEPLOYMENT_DEGRADE_MISSED_FILL_RATE"))
    brain_deployment_degrade_negative_expectancy_rolls: int = Field(default=3, validation_alias=AliasChoices("CHILI_BRAIN_DEPLOYMENT_DEGRADE_NEGATIVE_EXPECTANCY_ROLLS"))

    # Imminent ScanPattern breakout alerts (scheduler + pattern_imminent_alerts).
    pattern_imminent_alert_enabled: bool = True
    pattern_imminent_max_eta_hours: float = 4.0
    # Loosened defaults: many ScanPatterns reference indicators absent from the swing snapshot;
    # with only 1â€“2 evaluable conditions, a high ratio floor produced zero candidates forever.
    pattern_imminent_min_readiness: float = 0.58
    pattern_imminent_readiness_cap: float = 0.995
    pattern_imminent_max_per_run: int = 12
    pattern_imminent_cooldown_hours: float = 3.0
    # R33 (2026-04-30): crypto markets are 24/7 and tighter-coupled to news/whales,
    # so a 3h cooldown wastes intraday opportunity. Default to 0.5h (30min) for
    # crypto tickers; equity stays at 3h. _cooldown_active is asset-class-aware.
    pattern_imminent_cooldown_hours_crypto: float = 0.5
    pattern_imminent_max_tickers_per_run: int = 160
    pattern_imminent_scope_tickers_cap: int = 32
    pattern_imminent_evaluable_ratio_floor: float = 0.35
    pattern_imminent_eta_scale_k: float = 1.5
    # Imminent: main Telegram uses stricter coverage than board shortcut path.
    pattern_imminent_min_feature_coverage_main: float = 0.45
    pattern_imminent_min_composite_main: float = 0.42
    pattern_imminent_allow_evaluable_shortcut: bool = True
    pattern_imminent_max_per_ticker_per_run: int = 2
    pattern_imminent_max_per_pattern_per_run: int = 3
    pattern_imminent_shadow_observation_enabled: bool = True
    pattern_imminent_shadow_reserve_per_run: int = 4
    pattern_imminent_shadow_extra_per_run: int = 4
    pattern_imminent_shadow_max_per_ticker_per_run: int = 2
    pattern_imminent_shadow_max_per_pattern_per_run: int = 2
    pattern_imminent_shadow_cooldown_hours: float = 1.0
    pattern_imminent_shadow_cooldown_hours_crypto: float = 0.25
    # Keep broker-blocked shadow observation useful without letting one
    # recently rejected negative-edge pattern monopolize the scanner's shadow
    # slots. This does not demote the pattern or affect promoted/live alerts.
    pattern_imminent_shadow_poor_edge_cooldown_enabled: bool = True
    pattern_imminent_shadow_poor_edge_lookback_hours: float = (
        PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_LOOKBACK_HOURS
    )
    pattern_imminent_shadow_poor_edge_min_rejects: int = (
        PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MIN_REJECTS
    )
    pattern_imminent_shadow_poor_edge_max_avg_return_pct: float = (
        PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MAX_AVG_RETURN_PCT
    )
    pattern_imminent_shadow_poor_edge_expected_net_enabled: bool = True
    pattern_imminent_shadow_poor_edge_max_avg_expected_net_pct: float = (
        PATTERN_IMMINENT_SHADOW_POOR_EDGE_DEFAULT_MAX_AVG_EXPECTED_NET_PCT
    )
    # Activity throughput: for crypto imminent scans, spend latency on symbols
    # that the live Coinbase spot venue can actually execute. If Coinbase
    # universe metadata is unavailable, the scanner fails open.
    pattern_imminent_filter_crypto_to_coinbase_spot: bool = True
    pattern_imminent_coinbase_spot_filter_ttl_seconds: int = (
        PATTERN_IMMINENT_COINBASE_SPOT_FILTER_DEFAULT_TTL_SECONDS
    )
    # Repeated OHLCV/integrity failures should not monopolize every fast-scan
    # minute. This is an abstention cooldown only; a skipped score cannot
    # create a candidate and therefore cannot weaken live-entry gates.
    pattern_imminent_score_failure_cooldown_enabled: bool = True
    pattern_imminent_score_failure_cooldown_minutes: float = (
        PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_COOLDOWN_MINUTES
    )
    pattern_imminent_score_failure_min_failures: int = (
        PATTERN_IMMINENT_SCORE_FAILURE_DEFAULT_MIN_FAILURES
    )
    pattern_imminent_score_time_budget_seconds: float = (
        PATTERN_IMMINENT_SCORE_DEFAULT_TIME_BUDGET_SECONDS
    )
    pattern_imminent_max_tickers_per_pattern: int = (
        PATTERN_IMMINENT_DEFAULT_MAX_TICKERS_PER_PATTERN
    )
    pattern_imminent_suppressed_diagnostic_limit: int = (
        PATTERN_IMMINENT_DEFAULT_SUPPRESSED_DIAGNOSTIC_LIMIT
    )
    pattern_imminent_missing_indicator_sample_limit: int = (
        PATTERN_IMMINENT_DEFAULT_MISSING_INDICATOR_SAMPLE_LIMIT
    )
    pattern_imminent_readiness_near_miss_limit: int = (
        PATTERN_IMMINENT_DEFAULT_READINESS_NEAR_MISS_LIMIT
    )
    # Close readiness misses can enter paper/shadow observation, never live
    # broker flow. This increases measured learning samples without weakening
    # promoted/live entry gates.
    pattern_imminent_shadow_near_miss_enabled: bool = True
    pattern_imminent_shadow_near_miss_max_gap: float = (
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_MAX_GAP
    )
    pattern_imminent_shadow_near_miss_adaptive_enabled: bool = (
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_ENABLED
    )
    pattern_imminent_shadow_near_miss_adaptive_max_per_run: int = (
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_MAX_PER_RUN
    )
    pattern_imminent_shadow_near_miss_adaptive_min_readiness_fraction: float = (
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_ADAPTIVE_MIN_READINESS_FRACTION
    )
    pattern_imminent_shadow_near_miss_lifecycle_stages: str = (
        PATTERN_IMMINENT_DEFAULT_SHADOW_NEAR_MISS_LIFECYCLE_STAGES
    )
    # Outside regular US equity hours, promoted/live stock patterns can still
    # emit paper/shadow observation rows for learning and next-session prep.
    # The dedicated signal lane keeps them out of live broker entry flow.
    pattern_imminent_offsession_stock_shadow_enabled: bool = (
        PATTERN_IMMINENT_OFFSESSION_STOCK_SHADOW_DEFAULT_ENABLED
    )
    # Hard failed recert debt should not spend main live-alert capacity, but
    # can still produce paper/shadow evidence for future requalification.
    pattern_imminent_hard_recert_shadow_enabled: bool = True
    pattern_imminent_hard_recert_shadow_lifecycle_stages: str = (
        PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_LIFECYCLE_STAGES
    )
    pattern_imminent_hard_recert_shadow_reasons: str = (
        PATTERN_IMMINENT_DEFAULT_HARD_RECERT_SHADOW_REASONS
    )
    pattern_imminent_ticker_rotation_enabled: bool = True
    pattern_imminent_ticker_rotation_window_minutes: int = (
        PATTERN_IMMINENT_DEFAULT_TICKER_ROTATION_WINDOW_MINUTES
    )
    pattern_imminent_ticker_rotation_explore_tickers: int = (
        PATTERN_IMMINENT_DEFAULT_TICKER_ROTATION_EXPLORE_TICKERS
    )
    pattern_imminent_open_position_deflection_enabled: bool = (
        PATTERN_IMMINENT_OPEN_POSITION_DEFLECTION_DEFAULT_ENABLED
    )
    pattern_imminent_research_mode: bool = False
    pattern_imminent_research_nearmiss_log: bool = False
    pattern_imminent_debug_dry_run: bool = False
    pattern_imminent_use_prescreener_universe: bool = True
    pattern_imminent_use_predictions_universe: bool = True
    pattern_imminent_use_scanner_universe: bool = True
    pattern_imminent_max_prescreener_tickers: int = 80
    pattern_imminent_max_prediction_tickers: int = 40
    pattern_imminent_max_scanner_tickers: int = 50

    # Opportunity board + shared composite weights (see opportunity_scoring.py).
    opportunity_board_stale_seconds: int = 180
    # Board-only cost caps (imminent Telegram path unchanged). Reduce live timeouts.
    opportunity_board_max_universe_cap: int = 80
    opportunity_board_max_tickers_per_pattern: int = 10
    opportunity_board_max_ticker_scores_per_request: int = 360
    opportunity_board_max_scanner_fallback: int = 6
    opportunity_board_max_prescreener_fallback: int = 8
    opportunity_board_scanner_fallback_min_score_b: float = 6.5
    # Read-only inspect API: optional Bearer; empty = session-only (non-guest).
    trading_inspect_bearer_secret: str = Field(
        default="",
        validation_alias=AliasChoices("CHILI_TRADING_INSPECT_SECRET", "TRADING_INSPECT_BEARER_SECRET"),
    )
    opportunity_tier_a_min_composite: float = 0.48
    opportunity_tier_a_min_coverage: float = 0.5
    opportunity_tier_b_min_composite: float = 0.38
    opportunity_tier_b_min_coverage: float = 0.35
    opportunity_tier_b_max_eta_hours: float = 4.0
    opportunity_tier_c_min_composite: float = 0.28
    opportunity_max_tier_a: int = 3
    opportunity_max_tier_b: int = 5
    opportunity_max_tier_c: int = 8
    opportunity_max_tier_d: int = 12
    opportunity_weight_readiness: float = 0.28
    opportunity_weight_coverage: float = 0.22
    opportunity_weight_pattern_quality: float = 0.22
    opportunity_weight_risk_reward: float = 0.13
    opportunity_weight_eta: float = 0.15

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

    @field_validator("staging_database_url")
    @classmethod
    def _optional_postgres_staging_url(cls, v: str) -> str:
        url = (v or "").strip()
        if not url:
            return ""
        lowered = url.lower()
        if not (
            lowered.startswith("postgresql://")
            or lowered.startswith("postgresql+psycopg2://")
            or lowered.startswith("postgresql+psycopg://")
        ):
            raise ValueError(
                "STAGING_DATABASE_URL must be a PostgreSQL URL or empty. See docs/STAGING_DATABASE.md."
            )
        return url

    @field_validator("chili_llm_cost_mode")
    @classmethod
    def _llm_cost_mode(cls, v: str) -> str:
        mode = (v or "shadow").strip().lower()
        if mode not in {"shadow", "enforce"}:
            raise ValueError("CHILI_LLM_COST_MODE must be 'shadow' or 'enforce'")
        return mode

    @property
    def primary_api_key(self) -> str:
        """Primary LLM key: LLM_API_KEY or OPENAI_API_KEY."""
        return self.llm_api_key or self.openai_api_key or ""

    @property
    def premium_api_key_resolved(self) -> str:
        """Premium key: PREMIUM_API_KEY or primary for vision fallback."""
        return self.premium_api_key or self.primary_api_key or ""


def get_active_profile_info() -> dict[str, Any]:
    """Return the active profile name and its preset keys."""
    name = settings.brain_config_profile
    profile = CONFIG_PROFILES.get(name, {})
    return {"profile": name, "overrides": dict(profile)}


# Load once at import
settings = Settings()
