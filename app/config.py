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
AUTOTRADER_CANDIDATE_PRICE_PREFETCH_BATCH_TIMEOUT_DEFAULT_SECONDS = 2.0
AUTOTRADER_CANDIDATE_PRICE_PREFETCH_BATCH_TIMEOUT_MIN_SECONDS = 0.1
AUTOTRADER_CANDIDATE_PRICE_PREFETCH_BATCH_TIMEOUT_MAX_SECONDS = 5.0
AUTOTRADER_CANDIDATE_SELECT_TIMEOUT_DEFAULT_FRACTION = 0.15
AUTOTRADER_CANDIDATE_SELECT_TIMEOUT_MIN_MS = 1000
AUTOTRADER_CANDIDATE_SELECT_TIMEOUT_MAX_MS = 2500
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

    # ── Frontier code-generation tier (opt-in) ───────────────────────────
    # When a frontier provider key is present AND chili_code_frontier_enabled
    # is True, CHILI routes its code-generation gateway purposes (plan / edit /
    # create / diagnose / pr-repair / review) to a frontier model. This runs
    # CHILI's full coding harness — worktree isolation, anti-hallucination diff
    # validation, the test-repair loop, and the review gate — on a frontier
    # brain instead of the local Groq/OpenAI cascade. Any frontier failure
    # (auth, rate-limit, error) falls back to the local cascade, so enabling it
    # never makes the code path worse than today. Inert by default: with no key
    # or the flag off, behavior is byte-identical to the existing cascade.
    # Defaults target Anthropic's OpenAI-compatible endpoint + Claude Opus 4.8;
    # point frontier_base_url / frontier_model at any OpenAI-compatible frontier
    # (e.g. OpenAI's strongest coding model) to use a different provider.
    frontier_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "ANTHROPIC_API_KEY", "FRONTIER_API_KEY", "CHILI_FRONTIER_API_KEY"
        ),
    )
    frontier_base_url: str = Field(
        default="https://api.anthropic.com/v1",
        validation_alias=AliasChoices("FRONTIER_BASE_URL", "CHILI_FRONTIER_BASE_URL"),
    )
    frontier_model: str = Field(
        default="claude-opus-4-8",
        validation_alias=AliasChoices("FRONTIER_MODEL", "CHILI_FRONTIER_MODEL"),
    )
    chili_code_frontier_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_CODE_FRONTIER_ENABLED"),
    )
    # Output-token headroom for the code-generation purposes (plan / create /
    # edit). ONE documented knob: frontier coding models need room to emit
    # whole files and multi-file plans; the historical hardcoded 1500-3000
    # caps amputated diffs mid-hunk and truncated plan JSON.
    chili_code_gen_max_tokens: int = Field(
        default=16384,
        validation_alias=AliasChoices("CHILI_CODE_GEN_MAX_TOKENS"),
    )

    # ── Local-first code generation (free tier zero: own GPU) ────────────
    # When on, code purposes route to the local Ollama coder FIRST; any
    # failure or weak reply falls through the standard cascade (free Groq
    # 70B → paid tiers), so quality is preserved while the default code
    # brain costs nothing. Premium/frontier becomes opt-in escalation, not
    # the default. Resolution order in the gateway: explicit per-purpose
    # JSON override > local (this flag) > frontier flag.
    chili_code_local_first: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_CODE_LOCAL_FIRST"),
    )
    chili_code_local_model: str = Field(
        default="qwen2.5-coder:7b",
        validation_alias=AliasChoices("CHILI_CODE_LOCAL_MODEL"),
    )

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
    broker_login_ttl_seconds: int = Field(
        default=3600,
        ge=1,
        validation_alias=AliasChoices("BROKER_LOGIN_TTL_SECONDS"),
    )
    broker_cache_ttl_seconds: int = Field(
        default=300,
        ge=0,
        validation_alias=AliasChoices("BROKER_CACHE_TTL_SECONDS"),
    )
    broker_order_poll_timeout: int = Field(
        default=30,
        ge=1,
        validation_alias=AliasChoices("BROKER_ORDER_POLL_TIMEOUT"),
    )
    broker_order_poll_interval: float = Field(
        default=2.0,
        gt=0.0,
        validation_alias=AliasChoices("BROKER_ORDER_POLL_INTERVAL"),
    )
    broker_challenge_poll_timeout: int = Field(
        default=15,
        ge=1,
        validation_alias=AliasChoices("BROKER_CHALLENGE_POLL_TIMEOUT"),
    )
    broker_reconcile_confirm_seconds: int = Field(
        default=300,
        ge=0,
        validation_alias=AliasChoices("BROKER_RECONCILE_CONFIRM_SECONDS"),
    )

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
    brain_queue_pattern_walltime_seconds: float = Field(
        default=900.0,
        ge=0.0,
        le=86_400.0,
        validation_alias=AliasChoices(
            "BRAIN_QUEUE_PATTERN_WALLTIME_SECONDS",
            "CHILI_BACKTEST_QUEUE_PATTERN_WALLTIME_SECONDS",
        ),
    )
    brain_queue_pattern_soft_deadline_fraction: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "BRAIN_QUEUE_PATTERN_SOFT_DEADLINE_FRACTION",
            "CHILI_BACKTEST_QUEUE_PATTERN_SOFT_DEADLINE_FRACTION",
        ),
    )
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
    brain_work_mine_lease_seconds: int = Field(
        default=3600,
        ge=900,
        le=21600,
        validation_alias=AliasChoices(
            "BRAIN_WORK_MINE_LEASE_SECONDS",
            "CHILI_BRAIN_WORK_MINE_LEASE_SECONDS",
        ),
    )
    brain_work_max_attempts_default: int = 5
    brain_work_retry_base_seconds: int = 30
    brain_work_retry_multiplier: int = 2
    brain_work_dead_letter_recovery_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_WORK_DEAD_LETTER_RECOVERY_ENABLED"),
    )
    brain_work_dead_letter_recovery_limit: int = Field(
        default=8,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_WORK_DEAD_LETTER_RECOVERY_LIMIT"),
    )
    brain_work_dead_letter_recovery_max_per_event: int = Field(
        default=3,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_WORK_DEAD_LETTER_RECOVERY_MAX_PER_EVENT"),
    )
    brain_work_dead_letter_recovery_delay_seconds: int = Field(
        default=10,
        ge=0,
        le=86_400 * 30,
        validation_alias=AliasChoices("BRAIN_WORK_DEAD_LETTER_RECOVERY_DELAY_SECONDS"),
    )
    brain_work_dead_letter_recovery_cap_reset_delay_seconds: int = Field(
        default=3600,
        ge=0,
        le=86_400 * 30,
        validation_alias=AliasChoices(
            "BRAIN_WORK_DEAD_LETTER_RECOVERY_CAP_RESET_DELAY_SECONDS"
        ),
    )
    brain_work_dead_letter_recovery_max_cap_resets: int = Field(
        default=2,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices(
            "BRAIN_WORK_DEAD_LETTER_RECOVERY_MAX_CAP_RESETS"
        ),
    )
    brain_work_dead_letter_reuse_dedupe_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_WORK_DEAD_LETTER_REUSE_DEDUPE_ENABLED"),
    )
    # When True, run_learning_cycle skips in-cycle queue drain; brain-worker work-ledger batch owns it.
    # Requires brain_work_ledger table to exist; set False to drain queue in-cycle.
    brain_work_delegate_queue_from_cycle: bool = False
    # Per-handler dispatch budgets (ledger round processes execution_feedback_digest before backtests).
    brain_work_exec_feedback_batch_size: int = 3
    brain_work_exec_feedback_debounce_seconds: int = 45
    brain_work_edge_reliability_batch_size: int = Field(
        default=4,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_WORK_EDGE_RELIABILITY_BATCH_SIZE"),
    )
    brain_work_recert_rescue_batch_size: int = Field(
        default=2,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_WORK_RECERT_RESCUE_BATCH_SIZE"),
    )
    brain_work_exit_variant_batch_size: int = Field(
        default=2,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_WORK_EXIT_VARIANT_BATCH_SIZE"),
    )
    brain_work_provenance_batch_size: int = Field(
        default=1,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_WORK_PROVENANCE_BATCH_SIZE"),
    )
    brain_work_time_decay_exit_variant_sweep_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_SWEEP_ENABLED"),
    )
    brain_work_time_decay_exit_variant_sweep_lookback_hours: float = Field(
        default=48.0,
        ge=1.0,
        le=24.0 * 30.0,
        validation_alias=AliasChoices(
            "BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_SWEEP_LOOKBACK_HOURS"
        ),
    )
    brain_work_time_decay_exit_variant_sweep_limit: int = Field(
        default=25,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_SWEEP_LIMIT"),
    )
    brain_work_time_decay_exit_variant_min_losses: int = Field(
        default=2,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_WORK_TIME_DECAY_EXIT_VARIANT_MIN_LOSSES"),
    )
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
    # Provider-aware I/O concurrency (network fetches sized to each provider's
    # rate budget, NOT the CPU budget). None -> adaptive default per provider.
    # Coinbase public OHLCV is 429-prone; keep it gentle (defaults to the
    # fast-path's proven snapshot concurrency). yfinance is fragile + globally
    # paced. brain_io_fanout_ceiling caps heterogeneous I/O fan-outs (AI context,
    # prescreener) — independent multi-source calls bound by task count, not CPU.
    coinbase_fetch_concurrency: int | None = None
    yfinance_fetch_concurrency: int | None = None
    brain_io_fanout_ceiling: int | None = None
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
    brain_net_edge_execution_drag_max_rows: int = Field(
        default=200,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices("BRAIN_NET_EDGE_EXECUTION_DRAG_MAX_ROWS"),
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
    brain_risk_min_streak_loss_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("BRAIN_RISK_MIN_STREAK_LOSS_PCT"),
    )
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

    # Equity-native pattern miner (2026-06-04). Equity counterpart of the
    # crypto miner: mines candidate patterns from the indicator signatures of
    # profitable equity winners (live + paper-shadow, sourced from the linked
    # breakout alert). Generates research candidates only, which still pass
    # certification + operator promotion before any live capital.
    # See app/services/trading/equity_pattern_miner.py.
    # Activated 2026-06-05: validated live (209 winners / 168 signatures found,
    # spawns deduped variants for signatures with >=3 winners); runs every 6
    # brain cycles, bounded to <=10 variants/run.
    brain_equity_miner_enabled: bool = True
    brain_equity_miner_lookback_days: int = 90  # equity trades sparser than crypto
    brain_equity_miner_min_winners_per_signature: int = 3
    brain_equity_miner_max_variants_per_run: int = 10

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
    brain_capital_reweight_total_capital_default: float = Field(
        default=100_000.0,
        ge=0.0,
        validation_alias=AliasChoices("BRAIN_CAPITAL_REWEIGHT_TOTAL_CAPITAL_DEFAULT"),
    )

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
    brain_mining_min_samples: int = Field(
        default=20,
        ge=1,
        le=100_000,
        validation_alias=AliasChoices("BRAIN_MINING_MIN_SAMPLES"),
    )
    brain_mining_min_win_rate: float = Field(
        default=0.58,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("BRAIN_MINING_MIN_WIN_RATE"),
    )
    brain_mining_emit_scan_patterns: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_MINING_EMIT_SCAN_PATTERNS"),
    )
    brain_mining_use_v2_promotion: bool = Field(
        default=True,
        validation_alias=AliasChoices("BRAIN_MINING_USE_V2_PROMOTION"),
    )
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
    # Realized-aware edge prior: when shrinking the regime-conditioned hit rate
    # for the expected-edge gate, use the pattern's own well-sampled overall
    # realized win rate as the empirical-Bayes prior (capped at the regime
    # sample) instead of a neutral 0.5, so a noisy regime cell cannot bury a
    # proven pattern's edge. Losers (low realized WR) stay below break-even.
    # Default ON (no dark flags); set False to revert to the neutral prior.
    chili_edge_realized_aware_prior_enabled: bool = True
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
    # 2026-06-05: realized-EV CLEAN WINDOW (instrumentation floor). The live
    # execution system churned heavily through early 2026 (constantly-changing
    # algo-trader, execution discrepancies, gate/quality drift), so realized
    # PnL before this date is NOT apples-to-apples with current behaviour and
    # must not be treated as a trustworthy demote signal. The demote pass judges
    # promoted patterns on their REPRESENTATIVE post-floor clean realized EV
    # only; patterns whose post-floor evidence is too thin (data-starved, e.g.
    # equity) or unrepresentative are KEPT, never demoted on pre-floor churn.
    # This is an instrumentation floor, NOT applied to the promotion gate (post-
    # floor supply is too thin to graduate on — flooring promotion would starve
    # it, equity especially). Tune the floor as post-floor data accumulates.
    chili_realized_ev_clean_window_since: str = "2026-05-22"
    chili_realized_ev_clean_window_min_trades: int = 5
    chili_realized_ev_clean_window_min_days: int = 5
    # 2026-06-05 (rank by realized PnL): once realized EV is trustworthy (clean
    # exits, clean corrected_*/raw_realized_* columns, no backtest/mining bleed), a
    # pattern PROVING itself on clean realized PnL graduates on that evidence even
    # when its backtest CPCV/OOS gates disagree -- live realized PnL is the
    # higher-information signal for a pattern that has actually traded. The
    # realized-PnL promotion pass promotes active, not-yet-promoted patterns that
    # pass the clean realized-EV gate AND clear a meaningful realized-edge floor,
    # ranked by realized average return, capped per run. The kill switch / drawdown
    # breaker still gate the actual trade at execution time.
    chili_realized_pnl_promotion_enabled: bool = True
    chili_realized_pnl_promotion_min_trades: int = 8
    chili_realized_pnl_promotion_min_avg_return_pct: float = 0.5
    chili_realized_pnl_promotion_max_per_run: int = 10
    # 2026-06-05 backtest<->live parity: the backtest charges the system's OWN
    # MEASURED realized round-trip execution cost per asset class (incl. venue
    # fees) instead of hardcoded spread/commission floors, so a pattern whose edge
    # does not survive its real execution cost no longer backtests positive. The
    # only tunable is this sample-size guard (a statistical floor, NOT a cost
    # number): an asset class needs at least this many measured observations
    # before its derived cost is used; below it, the legacy fallback applies.
    chili_backtest_cost_min_measured_samples: int = 8
    # 2026-06-05 entry-cost parity (Fix 2/4): the live entry-edge cost now uses the
    # MEASURED median (not P90) per-ticker spread+slippage, floored by the same
    # measured asset-class round-trip cost the backtest charges (incl. venue fees;
    # closes the cold-start zero-cost hole), then self-corrects via the measured
    # realized-vs-expected gap. Knobs below are control/statistical params (NOT cost
    # numbers): the p90 buffer weight defaults to 0 (pure median); the feedback is
    # bounded (max bps), gated by a min-observation count, over a lookback window.
    chili_entry_cost_p90_buffer_weight: float = 0.0
    chili_venue_truth_feedback_max_bps: float = 50.0
    chili_venue_truth_feedback_min_obs: int = 5
    chili_venue_truth_feedback_lookback_days: int = 30
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
    chili_memory_watcher_interval_s: int = Field(
        default=300,
        ge=1,
        validation_alias=AliasChoices("CHILI_MEMORY_WATCHER_INTERVAL_S"),
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
    # FIX-16 (B3) — NOTIONAL-CEILING DOUBLE-HAIRCUT. When True (default), the per-trade
    # notional ceiling that clips the risk-first quantity reverts to a PURE liquidity/BP
    # cap (the equity-relative notional cap) — the variant-performance size multiplier is
    # NO LONGER folded into the ceiling (where, being DOWN-only in [0.3,1.0], it silently
    # cut realized dollar-risk to 8-47% of designed while never being able to add shares —
    # "arithmetic theater"). Instead perf scales the per-trade RISK BUDGET once (surfaced
    # as allocation["performance_size_mult"] and applied in the live/paper runner's
    # _eff_max_loss composition under the SAME base*3.0 clamp). OFF => byte-identical legacy
    # (perf multiplies the notional ceiling; performance_size_mult surfaced as 1.0 so the
    # runner never double-applies). The #769 max-loss circuit + structural stop are
    # untouched. (feedback_adaptive_no_magic, feedback_no_dark_flags, feedback_evolve_not_devolve)
    chili_momentum_notional_pure_liquidity_cap_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NOTIONAL_PURE_LIQUIDITY_CAP_ENABLED"),
        description="FIX-16 (B3): True (default) => the per-trade notional ceiling is a PURE liquidity/BP cap (equity-relative), and the variant-performance multiplier scales the RISK BUDGET once instead of double-haircutting the ceiling. OFF => byte-identical legacy (perf folded into the ceiling).",
    )
    # Block entries when family×regime×session history is clearly negative (queries DB).
    chili_momentum_family_regime_prefilter_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FAMILY_REGIME_PREFILTER_ENABLED"),
    )
    # FILL_OUTCOME_LOG (mig308) — WRITE-ONLY per-broker-fill ledger for the live
    # momentum lane (one row per real fill leg). Stage-1 logger only; reconcile +
    # reporting authority-flip + replay consumer are gated as a separate stage.
    # KILL-SWITCH default OFF: when False the writer returns BEFORE any DB work or
    # broker read — byte-identical, zero new SQL. Live-mode-only (paper/non-live =>
    # zero rows). Fail-open + savepoint-isolated so it can never poison a trade txn.
    chili_momentum_fill_log_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FILL_LOG_ENABLED"),
        description="Kill-switch: True => the momentum live lane records one momentum_fill_outcomes row per real broker fill leg (entry/exit/partial/scale-out). Default OFF (no writes, byte-identical). Write-only Stage-1.",
    )
    chili_momentum_recycle_entry_state_reset_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RECYCLE_ENTRY_STATE_RESET_ENABLED"),
        description="SAFETY: True (default) => on the COOLDOWN->WATCHING_LIVE recycle the live runner clears the prior trade's entry-order / position lifecycle state (entry_order_id, entry_order_ids_all, entry_orders_resolved, entry_submitted, position, + per-trade exit/scale/pyramid/anticipation/micropullback/stop/halt markers) so the recycled watcher starts as a CLEAN watcher with no entry order to re-poll. Fixes the duplicate-fill root cause (AREC sid 9331: a recycled watcher re-adopted its OWN filled entry order -> phantom 2x long + stuck live_bailout spin). OFF => the recycle is byte-identical to the legacy behavior (state retained). Identity / cooldown / trade_cycles / cumulative PnL+fees / discipline counters always persist.",
    )
    chili_momentum_fake_catalyst_guard_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FAKE_CATALYST_GUARD_ENABLED"),
        description="Down-weight unverified / hacked-PR / unsolicited-buyout headlines so fabricated catalysts don't drive selection or sizing.",
    )
    chili_momentum_adv_ceiling_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADV_CEILING_ENABLED"),
        description="Prefer LOW average-daily-volume names (the no-market-maker edge) by penalizing names above an adaptive ADV ceiling.",
    )
    chili_momentum_float_rotation_tilt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLOAT_ROTATION_TILT_ENABLED"),
        description="Volume/float rotation sustainability tilt (>=~5x EOD): reward names rotating their float multiple times as a fuel-remaining signal.",
    )
    # MECHANIZE-RVOL-PILLAR (2026-07-07): the premarket scanner payload carries NO rvol field,
    # so ross_tick_scalp_evidence_ok's rvol pillar is ALWAYS None premarket -> genuine explosive
    # gappers (heavy float rotation, no rvol key) are blocked (~146/182 recovered on merit).
    # Derive a premarket-scale-correct rvol-equivalent from FLOAT ROTATION = shares_traded/float
    # (shares_traded = raw volume if present else dollar_volume/price) and stamp it under the key
    # the gate already reads (intraday_cumulative_rvol). GUARDED (stamp only when it would ADMIT,
    # rvol_equiv >= min_rvol) => monotonic, never demotes a change-solo admit. Default-ON (no dark
    # flags); OFF => key stays unset => byte-identical. (feedback_adaptive_no_magic)
    chili_momentum_premarket_rvol_pillar_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_RVOL_PILLAR_ENABLED"),
        description="Mechanize the RVOL pillar premarket: derive an rvol-equivalent from the already-available float rotation (shares_traded/float) and stamp execution_readiness_json.extra.ross_signals[sym].intraday_cumulative_rvol so ross_tick_scalp_evidence_ok grades the rvol pillar on merit for genuine premarket gappers. Fail-closed (no stamp when dv/price/float missing), guarded (stamp only when rvol_equiv>=min_rvol => monotonic), additive. False = byte-identical (key stays None).",
    )
    chili_momentum_premarket_rvol_rotation_base: float = Field(
        default=0.20,
        gt=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_RVOL_ROTATION_BASE"),
        description="The ONE documented base: the float-rotation value that maps to the gate min_rvol (5.0). rvol_equiv = min_rvol * rotation / base. 0.20 ~= p50-p55 of the blocked-gapper rotation distribution (a median-participation gapper clears the pillar; a large-float non-mover reads ~0 and stays blocked). Lower => more permissive, higher => stricter.",
    )
    # FIX-19(d): flipped default False -> True. The fixed 1800bps pullback ceiling sailed
    # under real 1308/1365bps depths (it never bit), so the adaptive Ross retrace ceiling is
    # now ON by default. It is calm-ONLY tightening + ATR%-WIDENING (explosive/volatile names
    # KEEP the deeper tolerance, hard-capped 0.75), so it CANNOT re-introduce the documented
    # explosive-name 0-fills regression — it only tightens calm names toward Ross's ~50%-of-
    # prior-candle. (feedback_adaptive_no_magic, feedback_no_dark_flags)
    chili_momentum_adaptive_pullback_depth_ceiling_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_PULLBACK_DEPTH_CEILING_ENABLED"),
        description="Adaptive Ross retrace ceiling on the bull-flag IMPULSE-relative axis. FIX-19(d): default flipped to ON (the fixed 1800bps ceiling never bit under real ~1300bps depths). ON ⇒ CALM-only tightening toward Ross's ~50%-of-prior-candle (calm atr_pct≈0.01 ⇒ ceiling≈0.515, tighter than the current 0.70) while WIDENING with the name's own ATR% so EXPLOSIVE/volatile names keep the deeper tolerance (atr_pct≈0.05 ⇒ ≈0.575; hard-capped 0.75). Reuses the single documented base (_VOL_SHALLOW_BASE/_VOL_SHALLOW_ATR_MULT) — no fixed per-name magic. OFF ⇒ byte-identical to the current vol-aware flag ceiling. THE TRAP: tightening WITHOUT adaptation cuts explosive-name entries (the documented 0-fills regression — explosive names normally pull deeper), so this is calm-ONLY + ATR%-widening.",
    )
    # ── Ross re-audit SELECTION tilts (each kill-switched DEFAULT-OFF = byte-identical) ──
    # Four MEASURED selection-side tilts from the 2026-06-26 Warrior-courses re-audit. Each is a
    # minority RE-RANK that NEVER dominates the explosive RVOL/change/float core and can NEVER
    # fabricate an entry (selection/eligibility/arming only). Default OFF ⇒ no sub-score is stamped
    # and no pillar weight is folded ⇒ ranking is byte-identical to the deployed image.
    chili_momentum_float_overrotation_fix_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLOAT_OVERROTATION_FIX_ENABLED"),
        description="Ross SS101 EXHAUSTION fix: the deployed float-rotation sub-score rewards higher projected rotation MONOTONICALLY, but EXCESSIVE rotation (over-rotated, especially midday/late session) is an exhaustion NEGATIVE, not bullish. When ON, the float_rotation_pct sub-score contributes positively up to a healthy threshold, then PENALISES projected rotation above it, AND de-weights the penalty severity by how much of the morning session has elapsed (the over-rotation read is muted at the open, strongest late). MEASURED re-rank only (never a veto, equity-only). OFF ⇒ the legacy monotone sub-score is used ⇒ byte-identical.",
    )
    chili_momentum_float_overrotation_threshold: float = Field(
        default=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLOAT_OVERROTATION_THRESHOLD"),
        description="The ONE documented knob for the over-rotation fix: projected-rotation-at-EOD (float turns) at/below this stays bullish; ABOVE it is treated as float-exhaustion and penalised (de-rated below the saturation peak). Reference float-multiple, not a hard cutoff (the within-batch percentile still orders names). Adaptive in spirit — a generalised Ross 'healthy rotation' ceiling.",
    )
    chili_momentum_float_overrotation_session_minute: float = Field(
        default=120.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLOAT_OVERROTATION_SESSION_MINUTE"),
        description="Minutes after the regular open (09:30 ET) at which the over-rotation penalty reaches FULL strength (e.g. 120 = ~11:30, late-morning). Before this the penalty ramps in linearly from the open (early float burn is normal/bullish); at/after it the exhaustion read is fully applied. The ONE documented session-clock knob.",
    )
    chili_momentum_daily_200ema_room_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAILY_200EMA_ROOM_ENABLED"),
        description="Wire the dead-computed daily 200-SMA room (daily_levels.dist_to_sma_200_atr — signed daily-ATR units, + above / − below) as a MEASURED selection tilt: reward CLEAR room above the daily 200MA (clean sky, no overhead macro resistance), penalise names pinned to / just under the 200MA from below (buying into resistance). Folded into daily_structure_pct alongside the other daily tilts (no viability.py change). OFF ⇒ dist_to_sma_200_atr stays discarded ⇒ byte-identical.",
    )
    chili_momentum_daily_200ema_clear_room_atr: float = Field(
        default=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAILY_200EMA_CLEAR_ROOM_ATR"),
        description="The ONE documented knob for the 200-EMA room tilt: daily-ATR units of room ABOVE the 200MA at which the name is treated as fully 'clear sky' (max reward). Pinned/below-200MA reads ramp toward the de-rate. Adaptive (ATR-relative), not a fixed $.",
    )
    chili_momentum_news_pr_cadence_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NEWS_PR_CADENCE_ENABLED"),
        description="Ross PR-clock cadence: PRs drop on the top/bottom of the hour premarket (7:00/7:30/8:00/8:30 ET). When ON, a CATALYST name (present in the news set) gets a small additional selection LEAN-IN during a PR window — but ONLY in premarket hours, ET. Outside the windows the name is NEUTRAL (no boost). Requires the news-catalyst pillar to be stamping; a pure time-of-day gate on top of it. OFF ⇒ no cadence boost ⇒ byte-identical.",
    )
    chili_momentum_news_pr_cadence_hours: str = Field(
        default="4:00-4:45,5:00-5:45,6:00-6:45,7:00-7:50,8:00-8:50,9:25-9:35",
        validation_alias=AliasChoices("CHILI_MOMENTUM_NEWS_PR_CADENCE_HOURS"),
        description="The ONE documented knob for the PR-cadence windows: comma-separated HH:MM-HH:MM ranges in ET (premarket) during which catalyst names get the cadence lean-in. Default now covers the TOP + BOTTOM of EVERY hour Ross watches across the full premarket (4:00-9:30 ET) — each window spans the :00 top-of-hour drop through the :35-:45 bottom-of-hour drop, plus a 9:25-9:35 pre-open window. (GAP 0 re-audit: the old 7:00-7:30,8:00-8:30 default under-filled the PR clock.) Parser (_parse_cadence_windows) handles arbitrary windows fail-safe.",
    )
    chili_momentum_price_sweetspot_tilt_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PRICE_SWEETSPOT_TILT_ENABLED"),
        description="Ross $3-10 SWEET-SPOT preference (NOT a hard cut — the $1-20 price-band gate is untouched): a MEASURED selection tilt that BOOSTS names in the $3-10 high-conviction band and mildly de-rates names outside it but still within the broad band. Folded as a composable price_band pillar onto the active Ross weight-set (score_universe self-renormalises). OFF ⇒ no sub-score is stamped and no pillar is folded ⇒ byte-identical.",
    )
    chili_momentum_price_sweetspot_min: float = Field(
        default=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PRICE_SWEETSPOT_MIN"),
        description="Lower bound of the Ross high-conviction sweet-spot ($3). Names at/above this and at/below the max get the full boost; below ramps down. Documented reference, not a hard gate.",
    )
    chili_momentum_price_sweetspot_max: float = Field(
        default=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PRICE_SWEETSPOT_MAX"),
        description="Upper bound of the Ross high-conviction sweet-spot ($10). Above this (but within the $1-20 band) ramps down toward the de-rate. Documented reference, not a hard gate.",
    )
    chili_momentum_explosive_scoring_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_SCORING_ENABLED"),
        description="3-layer EXPLOSIVE scorer for score_universe (fixes the score-compression bug where a non-explosive mega-cap out-ranked a +400%/15,000x-RVOL rocket). Replaces the compensatory linear-percentile blend with: (1) a lexicographic explosiveness TIER (batch-median multiples — non-compensatory outer sort key), (2) a magnitude-preserving log-min-max multiplicative explosive CORE (rvol_norm^0.6 * mom_norm^0.4) x bounded quality modifier from the secondary pillars, (3) raw-rvol tiebreak. Batch-relative / no magic numbers; fail-OPEN (missing rvol/change degrades to tier 0, never crashes, never vetoes — selection re-rank only). Flag OFF ⇒ byte-identical to the legacy blend.",
    )
    # 2026-07-04 — RECOVERY-GAP DOWN-RANK (NO MAGIC). A "leading gainer" whose big % is measured off a
    # CRUSHED prior close (the prior session collapsed) is a backside-fade recovery, NOT a fresh
    # breakout — Ross lost -$394.89 on exactly this (TC 07-01). The scanner emits chg_vs_prev_high_pct
    # (the honest change vs the name's OWN prior-day HIGH); ross_momentum CAPS the momentum pillar at
    # that value — a purely adaptive down-rank off the name's own price history, no threshold/constant.
    # A genuine breakout (at/above its prior high) is untouched; a recovery under its prior high is
    # down-ranked to its true progress. OFF ⇒ byte-identical.
    chili_momentum_recovery_gap_dampen_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RECOVERY_GAP_DAMPEN_ENABLED"),
        description="2026-07-04: cap the momentum pillar at chg_vs_prev_high_pct (change vs the name's own prior-day HIGH) so a leading-gainer whose % is inflated by a crushed prior close (recovery-gap fade trap, e.g. TC 07-01) is down-ranked to its true progress. Adaptive, no magic number; a genuine breakout at/above its prior high is untouched. OFF ⇒ byte-identical.",
    )
    chili_momentum_ross_rvol_feed_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ROSS_RVOL_FEED_ENABLED"),
        description="FIX A: un-zero the starved ws_ignition Ross scorer. (A1) the ignition feeder threads the REAL intraday RVOL it captured from the screen snapshot into ross_signals.vol_ratio, so an igniting mover reaches the explosive CORE with rvol present instead of None. (A2) when a name genuinely has no rvol, the explosive core degrades to a BOUNDED momentum-only score (capped below a confirmed rvol+mom mover) instead of 0.0 — so the viability tilt no longer penalises every explosive mover toward the floor. SELECTION-ONLY (never touches an entry decision). OFF => byte-identical (old rvol-None -> core 0.0 path).",
    )
    chili_momentum_squeeze_fuel_tilt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_FUEL_TILT_ENABLED"),
        description="SQUEEZE-FUEL selection tilt (Ross SS101 #2): SOFT within-batch BOOST for squeeze-prone names (high short-interest %% + high cost-to-borrow, via Ortex) and a small DE-RATE for very-low-CTB / easy-to-borrow names (free shares, shorts attack the pop). Ortex fetch gated to top-N explosive low-float candidates + cached 12h. Equity-only; flag-off OR Ortex absent/error ⇒ byte-identical.",
    )
    chili_momentum_news_catalyst_weight_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NEWS_CATALYST_WEIGHT_ENABLED"),
        description="NEWS-CATALYST selection pillar (the 🔥 on Ross's scanner — the 4th Ross pillar that was a STUB). Maps each symbol's REAL Polygon/Benzinga catalyst GRADE (the strong/weak/fake/all sets the pipeline already computes from headlines — no new fetch) to a [0,1] news_catalyst_pct sub-score and FOLDS a MEASURED 0.10-weight pillar onto the active Ross weight-set (score_universe self-renormalises). Strong (FDA/trial/M&A/beat) BOOSTS; weak (dilution/compliance) and fake (unverified/hacked-PR) DE-RATE; news a minority RE-RANK only — float/RVOL/change stay primary. GRACEFUL: a name with NO news data is NEUTRAL (pillar omitted, never penalised or rejected for lack of news). Ships DEFAULT-OFF (operator confirms the catalyst feed's live amplitude before tilting). OFF ⇒ the sub-score is never stamped and the pillar is never folded ⇒ BYTE-IDENTICAL ranking.",
    )
    chili_momentum_squeeze_fuel_top_n: int = Field(
        default=12, ge=0, le=60,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_FUEL_TOP_N"),
        description="Credit-frugality gate: only the top-N explosive low-float candidates (by Ross score, after the explosive floor) get an Ortex short-mechanics fetch. Keeps the Trader plan (1,000 credits/mo, 1 req/s) within budget. 0 ⇒ no fetch.",
    )
    chili_momentum_theme_crowded_substitute_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THEME_CROWDED_SUBSTITUTE_ENABLED"),
        description="CROWDED-TAPE CATALYST-SUBSTITUTE (selection-rank hold for Ross sympathy movers). In the news-catalyst pillar a name in NONE of the catalyst sets reads news_catalyst_pct=None (NEUTRAL in isolation), but its strong-catalyst peers carry a high news percentile and out-rank it on the same RVOL+momentum core, so a catalyst-LESS crowded-tape / high-RVOL THEME name (a Ross sympathy mover) is demoted purely on P1-catalyst absence. When ON, a genuine keyword-THEME member (a sympathy peer of a real leader, from theme_detector.theme_sympathy_symbols) that is ALSO a CROWDED high-RVOL name (its OWN within-batch RVOL percentile >= the crowded floor, default top ~30%) earns a partial news-substitute sub-score FLOORED onto news_catalyst_pct — ramping the 0.5 neutral midpoint up to AT MOST the present/ungraded reference (0.60, below the 0.90 strong grade), so a real GRADED catalyst leader still out-ranks it and a NON-mover crowded tape (low RVOL) earns nothing. A FLOOR applied ONLY to no-own-catalyst names (never lowers a graded name), adaptive (own within-batch RVOL percentile — no magic absolute), bounded, selection-only re-rank — never a veto, never an entry/exit/sizing change. Equity-only. Gated on BOTH the news pillar AND this flag; either OFF / no theme cluster / below the RVOL tail ⇒ BYTE-IDENTICAL ranking.",
    )
    chili_momentum_theme_crowded_rvol_floor_pctl: float = Field(
        default=0.70, ge=0.0, le=0.999,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THEME_CROWDED_RVOL_FLOOR_PCTL"),
        description="The within-batch RVOL percentile FLOOR at/above which a keyword-theme member starts earning the crowded-tape catalyst-substitute (0.70 = top ~30% by relative volume = a genuinely CROWDED tape, not a quiet name that merely shares a headline keyword). The substitute sub-score ramps the 0.5 neutral midpoint -> the present grade as the RVOL percentile goes this floor -> 1.0. The ONE documented base for the crowded-tape credit.",
    )
    chili_momentum_squeeze_quality_floor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_QUALITY_FLOOR_ENABLED"),
        description="SQUEEZE-FUEL ADAPTIVE QUALITY-FLOOR (crowded-tape sympathy-name rank hold). In the explosive scorer the continuous score is core*(0.5+0.5*quality_blend); the news-catalyst (P1) and squeeze_fuel pillars BOTH live inside the bounded quality_blend, so a catalyst-LESS but high-RVOL, fillable, strongly squeeze-fueled SYMPATHY name (e.g. NPT — 119M sh, no own news) gets demoted out of the armed top-N when its P1 reads weak/zero, because strong squeeze fuel is just averaged into the same ±50%% modifier and cannot lift it back. When ON, a name in the TOP squeeze-fuel percentile of the batch FLOORS its quality modifier (ramp 0.80->1.0 pctl => floor up to 0.90; a FLOOR via max(), never an additive boost), so a strong squeeze + fillability HOLDS the name in the armed set despite a weak/zero P1 — P1-absence alone no longer demotes a high-RVOL fillable squeezer. Bounded (< the 1.0 ceiling; the RVOL+momentum core still dominates ordering), adaptive (the name's OWN within-batch squeeze percentile — no magic SI/CTB cutoff), selection-only re-rank (never a veto, never an entry/exit change). Below the tail / no squeeze data / flag OFF ⇒ floor 0.0 ⇒ BYTE-IDENTICAL explosive ordering.",
    )
    # ── P4 SQUEEZE-SCORE DEEPENING — ENTRY size-up + EXIT squeeze-aware-hold ──
    # Extend the SAME squeeze_fuel score (already a SELECTION tilt) to two downstream uses, each
    # driven SOLELY by the name's OWN within-batch squeeze PERCENTILE (squeeze_fuel_rank_pct). One
    # documented base each; default ON (no dark flags) and OFF / un-armed ⇒ BYTE-IDENTICAL.
    chili_momentum_squeeze_entry_sizeup_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_ENTRY_SIZEUP_ENABLED"),
        description="P4(1) ENTRY SIZE-UP: a name in the TOP within-batch squeeze percentile (its OWN squeeze_fuel_rank_pct, from Ortex SI%+CTB) whose tape AGREES (live OFI>0) AND whose news AGREES (strong-catalyst member) scales the per-trade RISK BUDGET UP by a bounded percentile-driven multiplier in [1.0, max_mult]. Composes under the SAME 3x clamp + hard max_notional ceiling + max-loss circuit (NEVER past any cap, NEVER a veto). Equity-only (crypto has no borrow data ⇒ no rank ⇒ 1.0). OFF / any gate failing ⇒ 1.0 byte-identical.",
    )
    chili_momentum_squeeze_entry_top_pctl: float = Field(
        default=0.80, ge=0.0, le=0.999,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_ENTRY_TOP_PCTL"),
        description="P4(1): the within-batch squeeze percentile FLOOR at/above which the entry size-up arms (0.80 = top quintile). The multiplier ramps 1.0->max_mult as rank goes this floor->1.0. The ONE documented base for the entry lever.",
    )
    chili_momentum_squeeze_entry_max_mult: float = Field(
        default=1.50, ge=1.0, le=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_ENTRY_MAX_MULT"),
        description="P4(1): the BOUNDED upper cap of the entry risk-budget multiplier (reached only at squeeze rank == 1.0 with tape + news agreeing). The ONE documented cap; the 3x combined clamp + max_notional ceiling still bound it.",
    )
    chili_momentum_squeeze_exit_hold_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_EXIT_HOLD_ENABLED"),
        description="P4(2) SQUEEZE-AWARE HOLD: a name in the EXTREME within-batch squeeze tail WIDENS the smart-hold / volnorm RIDE candidate band (raise the trail k by a bounded percentile factor) so a fueled runner extends further. INVARIANT-A SAFE — widens the CANDIDATE band BEFORE placement; a wider band only lowers the trail candidate (composed through max(stop, be, candidate)), NEVER loosens a placed stop. OFF / below the tail ⇒ 1.0 byte-identical.",
    )
    chili_momentum_squeeze_exit_tail_pctl: float = Field(
        default=0.90, ge=0.0, le=0.999,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_EXIT_TAIL_PCTL"),
        description="P4(2): the within-batch squeeze percentile FLOOR at/above which the exit band-widen arms (0.90 = extreme top decile). The widen factor ramps 1.0->max_widen as rank goes this floor->1.0. The ONE documented base for the exit lever.",
    )
    chili_momentum_squeeze_exit_max_widen: float = Field(
        default=1.50, ge=1.0, le=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SQUEEZE_EXIT_MAX_WIDEN"),
        description="P4(2): the BOUNDED upper cap of the RIDE-band widen factor (reached only at squeeze rank == 1.0). The ONE documented cap; the trail's own vol-floor + max_dist clamp + INVARIANT-A still bound the resulting stop.",
    )
    chili_momentum_gap_geometry_tilt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_GAP_GEOMETRY_TILT_ENABLED"),
        description="Unfilled-gap to-the-penny trigger + clear-sky room: tilt entries where price triggers at the gap edge with open space overhead.",
    )
    chili_momentum_red_rejection_derate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RED_REJECTION_DERATE_ENABLED"),
        description="De-rate a level with a daily history of red upper-wick rejections (repeated sellers defending the same price).",
    )
    chili_momentum_blue_sky_recent_ipo_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BLUE_SKY_RECENT_IPO_ENABLED"),
        description="All-time-high breakout boost gated to recent-IPO names (<2yr history) where there is no overhead supply.",
    )
    # ── P0: blue-sky ENTRY trigger + overhead-supply veto (daily context INTO entries) ──
    chili_momentum_blue_sky_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BLUE_SKY_ENTRY_ENABLED"),
        description="P0 dedicated ENTRY trigger: fire the break of a NEW multi-period/all-time high with NO overhead resistance (clear sky >= the room-ATR floor) + volume confirm. OFF => the entry path stays daily-blind (byte-identical: no daily context is read, no trigger fires).",
    )
    chili_momentum_blue_sky_entry_min_room_atr: float = Field(
        default=1.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BLUE_SKY_ENTRY_MIN_ROOM_ATR"),
        description="The ONE documented knob for the blue-sky entry: the nearest overhead-supply level must sit at least this many DAILY-ATR units above the break (clear-room floor) for the trigger to fire genuine clear sky. Adaptive (ATR-relative), not a fixed $.",
    )
    chili_momentum_bull_flag_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BULL_FLAG_ENTRY_ENABLED"),
        description="Ross BULL FLAG (SS101 #012) ALL-DAY entry: 1-3 green-candle impulse, then a 2-3 candle DEEPER pullback (50-70% retrace -- DEEPER than the shallow first_pullback cap, riding the 9-EMA band) that holds, then FIRE the first candle to break the prior pullback swing high. DISTINCT from first_pullback (shallow only) and deep_reclaim (morning-only). Reuses the anti-chase guards (explosive/first-pullback/backside/L2/overhead) + volume-profile (light-pull dry-up + high-vol-red distribution veto). Ship DARK (default FALSE -- NEW + never-run; operator ramps). OFF => no-op, byte-identical. docs/DESIGN/MOMENTUM_LANE.md",
    )
    chili_momentum_overhead_veto_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERHEAD_VETO_ENABLED"),
        description="P0 overhead-supply veto/derate: for ANY breakout entry, if trapped supply (prior swing high / unfilled gap / red-rejection cluster) sits within the veto-ATR floor overhead, VETO the entry (don't buy into a ceiling). OFF => breakout entries stay daily-blind = byte-identical.",
    )
    chili_momentum_overhead_veto_atr: float = Field(
        default=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERHEAD_VETO_ATR"),
        description="The ONE documented knob for the overhead veto: a breakout whose nearest overhead-supply level sits within this many DAILY-ATR units is vetoed (a wall the price must fight through). Adaptive (ATR-relative). A true blue-sky/clear-room break (room beyond this) passes.",
    )
    chili_momentum_reverse_split_recency_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REVERSE_SPLIT_RECENCY_ENABLED"),
        description="Recent (<~1mo) reverse split + real news = low-float-squeeze BOOST (un-penalize the reduced share count).",
    )
    chili_momentum_reverse_split_recency_days: int = Field(
        default=30,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REVERSE_SPLIT_RECENCY_DAYS"),
        description="The ONE documented base for the SS101 reverse-split squeeze: a reverse split must have executed within this many days (~Ross's '<1mo') to count as a FRESH low-float reset. Clamped 1..120.",
    )
    chili_momentum_private_placement_sign_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PRIVATE_PLACEMENT_SIGN_ENABLED"),
        description="Private placement at/above market is bullish: split out of the weak-catalyst de-boost so above-market raises are not penalized.",
    )
    chili_momentum_iceberg_add_probe_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ICEBERG_ADD_PROBE_ENABLED"),
        description="Per-add iceberg/hidden-seller probe: compare filled-through vs displayed ask to detect a hidden seller before each scale-in add.",
    )
    # ── ORDER-PATH ITEM 1: ANTICIPATION STARTER (probe-then-add entry) ───────────────
    # DEFAULT OFF (NEW + order-path: the agentic rail had a duplicate-fill + stranded-
    # naked-long history). When ON, the live entry is split into a small PROBE leg (a
    # fraction of the intended qty) submitted on the pivot break; the REMAINDER is added
    # only after the position confirms (fill seen) via the EXISTING pyramid/scale-in add
    # machinery (which already carries the full veto chain + dedupe-safe client_order_id +
    # broker_order_id recording + orphan reconciliation). OFF => the single-leg entry path
    # is byte-identical (no probe split, qty unchanged, no remainder add). docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_anticipation_starter_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ANTICIPATION_STARTER_ENABLED"),
        description="ANTICIPATION STARTER (probe-then-add): split the live entry into a small PROBE leg on the pivot break, then ADD the remainder after the position confirms — REUSING the existing pyramid/scale-in add path (full veto chain, dedupe-safe per-leg client_order_id, broker_order_id recorded + orphan-reconciled to the parent). NEW order-path => ships DEFAULT-OFF, do not enable without soak (the agentic rail's duplicate-fill / stranded-naked-long history). OFF => single-leg entry byte-identical (no split, no remainder add).",
    )
    chili_momentum_anticipation_probe_fraction: float = Field(
        default=0.25, ge=0.05, le=0.95,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ANTICIPATION_PROBE_FRACTION"),
        description="The ONE documented knob for the anticipation starter: the fraction of the risk-first entry quantity submitted as the initial PROBE leg (the remainder is added on confirmation via the pyramid path). Clamped 0.05..0.95. Only consulted when chili_momentum_anticipation_starter_enabled.",
    )
    # ── ORDER-PATH ITEM 2: ORDER CHUNKING (split parent into venue blocks) ───────────
    # DEFAULT OFF (NEW + order-path). When ON, a ChunkingVenueAdapter WRAPPER intercepts
    # place_limit_order_gtc and splits the parent order into N equal blocks, each with a
    # FRESH client_order_id (so the venue accepts them as distinct orders) and each
    # broker_order_id collected for reconciliation. For a small CASH account the benefit
    # is marginal (N× spread/commission). OFF => the wrapper is not inserted; the base
    # adapter is returned and every place_*_order is byte-identical to the single order.
    chili_momentum_order_chunking_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORDER_CHUNKING_ENABLED"),
        description="ORDER CHUNKING: a venue-adapter WRAPPER that splits a parent place_limit_order_gtc into N equal blocks for queue priority, each with a fresh client_order_id and a collected broker_order_id (dedupe/reconcile preserved). Multiplies broker_order_ids => ships DEFAULT-OFF; do NOT enable until dedupe/reconcile safety is proven on the agentic rail (marginal benefit for a small cash account). OFF => the wrapper is not inserted and the base adapter is returned byte-identical.",
    )
    chili_momentum_order_chunking_blocks: int = Field(
        default=1, ge=1, le=10,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORDER_CHUNKING_BLOCKS"),
        description="The ONE documented knob for order chunking: how many equal blocks to split the parent order into (1 = no split, byte-identical even with the wrapper inserted). Clamped 1..10. Only consulted when chili_momentum_order_chunking_enabled.",
    )
    # ── BEHAVIORAL ITEM 3: GREEN-DAY GRADUATION (consecutive-green size multiplier) ──
    # DEFAULT OFF. A size-multiplier gate (NOT a hard live-block): after a consecutive
    # green-day streak (realized daily PnL > 0, ET calendar, auto-derived from history)
    # the per-trade risk basis is scaled UP a bounded amount. OFF => multiplier is always
    # 1.0 (byte-identical sizing). Operator can size-down Monday validation by disabling.
    chili_momentum_green_day_graduation_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_GREEN_DAY_GRADUATION_ENABLED"),
        description="GREEN-DAY GRADUATION: graduate to bigger size ONLY after a consecutive green-day streak (realized daily PnL > 0, bucketed by ET calendar day, auto-derived from MomentumAutomationOutcome history — no scattered magic). A bounded UPWARD size multiplier on the per-trade risk basis (composes into the existing 3x combined-multiplier ceiling), applied at entry-quantity compute time — NOT a veto, never blocks an entry. DEFAULT-ON (no dark flags); self-gating — multiplier stays 1.0 until a real green-day streak exists, so it scales AS expectancy proves out, never before. Kill-switch via env=0 => multiplier always 1.0 (byte-identical sizing).",
    )
    chili_momentum_green_day_step_per_day: float = Field(
        default=0.1, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_GREEN_DAY_STEP_PER_DAY"),
        description="The per-extra-green-day size step for green-day graduation: multiplier = 1.0 + step * max(0, consecutive_green_days - 1), capped at chili_momentum_green_day_max_multiplier. Default 0.1 (day-2 => 1.1x, day-3 => 1.2x ...). Clamped 0..1.",
    )
    chili_momentum_green_day_max_multiplier: float = Field(
        default=2.0, ge=1.0, le=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_GREEN_DAY_MAX_MULTIPLIER"),
        description="The hard ceiling on the green-day graduation multiplier (the streak can never size the lane above this). Clamped 1.0..3.0. Default 2.0.",
    )
    chili_momentum_green_day_lookback_days: int = Field(
        default=30, ge=1, le=120,
        validation_alias=AliasChoices("CHILI_MOMENTUM_GREEN_DAY_LOOKBACK_DAYS"),
        description="How many ET calendar days back to scan when counting the consecutive green-day streak for graduation. Clamped 1..120. Default 30.",
    )
    # ── CATALYST-CONVICTION SIZE MULTIPLIER (Ross E2 grade → size) ────────────────────
    # DEFAULT OFF. A bounded UPWARD size multiplier on the per-trade risk basis driven by
    # the DEPLOYED catalyst grade (the SAME strong/weak/fake news accessors the lane already
    # uses — no new feed): a STRONG, credible catalyst (FDA/trial/M&A/contract/beat) is a real
    # reason a low-float runs, so Ross "earns the size" on conviction. Mirrors green-day
    # graduation: mult = clamp(1.0 + step * grade_rank, 1.0, max_multiplier); STRONG => rank>0
    # (boost), weak/fake/none => rank 0 (1.0, no boost — a catalyst only ADDS, no-news shrink
    # is handled elsewhere). Composes MULTIPLICATIVELY into the runner's 3x combined-multiplier
    # ceiling (auto-contained) + the downstream hard notional ceiling — NEVER a veto, never
    # blocks/shrinks an entry. OFF => multiplier always 1.0 (byte-identical sizing).
    chili_momentum_catalyst_conviction_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_CONVICTION_ENABLED"),
        description="CATALYST-CONVICTION SIZE: graduate to bigger size when the name carries a STRONG, credible catalyst (the deployed strong/weak/fake news grade — no new feed). A bounded UPWARD multiplier on the per-trade risk basis (composes into the existing 3x combined-multiplier ceiling + the hard notional ceiling), applied at entry-quantity compute time — NOT a veto, never blocks/shrinks an entry. Ships DEFAULT-OFF. OFF => multiplier always 1.0 (byte-identical sizing).",
    )
    chili_momentum_catalyst_conviction_step: float = Field(
        default=0.15, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_CONVICTION_STEP"),
        description="The per-grade-rank size step for catalyst conviction: multiplier = 1.0 + step * grade_rank, capped at chili_momentum_catalyst_conviction_max_multiplier. STRONG catalyst => rank 3 (=> 1.0 + 0.15*3 = 1.45, clamped to max). weak/fake/none => rank 0 => 1.0. Clamped 0..1.",
    )
    chili_momentum_catalyst_conviction_max_multiplier: float = Field(
        default=1.5, ge=1.0, le=2.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_CONVICTION_MAX_MULTIPLIER"),
        description="The hard ceiling on the catalyst-conviction size multiplier (a catalyst can never size the lane above this). Conservative default 1.5; clamped 1.0..2.0 so it stays well under the runner's 3x combined-multiplier ceiling.",
    )
    chili_momentum_kelly_conviction_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_KELLY_CONVICTION_ENABLED"),
        description="P5 — FRACTIONAL-KELLY TRIPLE-CONFLUENCE SIZE-UP: when squeeze-fuel AND OFI AND a STRONG news catalyst ALL agree, scale the per-trade risk basis UP by a bounded HALF-KELLY multiplier off the blended conviction percentile (no magic win-rate). Composes into the runner's 3x ceiling + hard notional ceiling + the unchanged #769 max-loss circuit. NEVER a veto/shrink (>=1.0). Default ON (no dark flags); OFF / any-leg-missing / error => 1.0 (byte-identical).",
    )
    chili_momentum_kelly_conviction_max_multiplier: float = Field(
        default=1.5, ge=1.0, le=2.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_KELLY_CONVICTION_MAX_MULTIPLIER"),
        description="The ONE documented cap — the HALF-KELLY ceiling on the triple-confluence size-up. Conservative default 1.5; stays under the runner's 3x clamp, and the #769 circuit still bounds the realized worst case. Clamped 1.0..2.0.",
    )
    chili_momentum_kelly_conviction_gain: float = Field(
        default=1.0, ge=0.0, le=5.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_KELLY_CONVICTION_GAIN"),
        description="Linear gain mapping the half-Kelly fraction f_half in [0,0.5] onto the multiplier span: mult = clamp(1 + gain*f_half, 1.0, max_multiplier). Bounded by max_multiplier regardless.",
    )
    chili_momentum_kelly_conviction_w_squeeze: float = Field(
        default=0.4, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_KELLY_CONVICTION_W_SQUEEZE"),
        description="Weight of the squeeze-fuel pillar in the blended conviction percentile (normalized by the weight sum).",
    )
    chili_momentum_kelly_conviction_w_ofi: float = Field(
        default=0.4, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_KELLY_CONVICTION_W_OFI"),
        description="Weight of the OFI pillar in the blended conviction percentile (normalized by the weight sum).",
    )
    chili_momentum_kelly_conviction_w_news: float = Field(
        default=0.2, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_KELLY_CONVICTION_W_NEWS"),
        description="Weight of the news-catalyst pillar in the blended conviction percentile (normalized by the weight sum). News is the binary permission gate.",
    )
    # ── ADDITIVE ITEM 4: PROCESS-OVER-PROFITS SCORE (logged rule-adherence) ──────────
    # DEFAULT OFF. A LOGGED-ONLY rule-adherence score (entered-on-trigger / honored-stop /
    # no-chase) distinct from realized PnL. Read-only journaling — NEVER gates, re-sizes,
    # or vetoes an entry. OFF => the score is not computed and nothing is logged.
    chili_momentum_process_score_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PROCESS_SCORE_ENABLED"),
        description="PROCESS-OVER-PROFITS SCORE: a logged rule-adherence metric over closed sessions (entered-on-trigger / honored-stop / no-chase, via outcome_labels classes) distinct from realized PnL. Read-only journaling surface — NEVER gates, re-sizes, or vetoes trading. OFF => the score is not computed / not logged (byte-identical).",
    )
    chili_momentum_process_score_window: int = Field(
        default=30, ge=1, le=500,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PROCESS_SCORE_WINDOW"),
        description="Rolling window of recent REAL ENTERED closed trades scored by the process-over-profits rule-adherence metric. Read-only journaling only. Clamped 1..500.",
    )
    # ── ADDITIVE ITEM 5: OVERHEAD-SUPPLY CEILING (selection de-weight tilt) ──────────
    # DEFAULT OFF. A bounded SELECTION tilt (like the other ross_momentum pillars) that
    # de-weights a name approaching a prior huge-VOLUME doji / round-trip overhead level
    # from below. A re-rank only — NEVER an entry gate, NEVER removes a name from the pool.
    # OFF => the sub-score is not stamped and the pillar is absent from the blend (byte-identical).
    chili_momentum_overhead_supply_tilt_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERHEAD_SUPPLY_TILT_ENABLED"),
        description="OVERHEAD-SUPPLY CEILING (selection tilt): de-weight a name approaching a prior huge-VOLUME doji / round-trip overhead level from below (trapped supply ahead). A composable 0.10-weight FIFTH pillar folded onto the active Ross weight-set (score_universe self-renormalises), percentile-ranked within the batch — a name far below an overhead level scores HIGH (1.0), one AT/above scores LOW (0.0). A re-rank tilt ONLY: it can never block a fill or remove a name from the candidate pool (operator can still manually arm a de-weighted name). DISTINCT from chili_momentum_overhead_veto_enabled (which is an ENTRY veto). OFF => the sub-score is never stamped and the pillar is never folded => BYTE-IDENTICAL ranking.",
    )
    chili_momentum_overhead_supply_clear_room_atr: float = Field(
        default=1.5, ge=0.1, le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERHEAD_SUPPLY_CLEAR_ROOM_ATR"),
        description="The ONE documented base for the overhead-supply selection tilt: daily-ATR room to the nearest overhead level at/above which a name reads ~1.0 (clear sky, max reward); pinned at the level reads 0.0 (max de-weight). The within-batch PERCENTILE of the sub-score is what actually orders names (adaptive), so this only shapes the raw curve. Clamped 0.1..10.",
    )
    # ── ADDITIVE ITEM 6: METRICS SURFACE (operator journaling KPIs) ──────────────────
    # DEFAULT OFF. A read-only reporting surface (accuracy% + profit-loss ratio + green-day
    # streak) over already-computed trade data. No trading impact whatsoever. OFF => the
    # surface returns empty dicts / is not called.
    chili_momentum_challenge_metrics_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CHALLENGE_METRICS_ENABLED"),
        description="METRICS SURFACE: a read-only operator-journaling surface exposing accuracy% (rule-adherence) + profit-loss ratio + consecutive-green-day streak over already-computed closed-session data. A reporting function only — NO trading impact (never gates, sizes, or vetoes). OFF => the surface returns empty dicts / is not called (byte-identical).",
    )
    chili_momentum_challenge_metrics_window: int = Field(
        default=50, ge=1, le=500,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CHALLENGE_METRICS_WINDOW"),
        description="Window of recent closed sessions the challenge-metrics surface aggregates accuracy% + profit-loss ratio over. Read-only journaling only. Clamped 1..500.",
    )
    chili_momentum_adv_ceiling_ref_shares: float = Field(
        default=10_000_000.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADV_CEILING_REF_SHARES"),
        description="The ONE documented base for the ADV-ceiling soft re-rank: Ross's <10M-shares average-daily-volume reference FLOOR. The live ceiling is the MAX of this base and the batch ADV percentile (adaptive), so high-ADV names get a soft rank discount without a hard drop. Reversible via chili_momentum_adv_ceiling_enabled.",
    )
    chili_momentum_flush_dip_buy_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLUSH_DIP_BUY_ENABLED"),
        description="Enable the algo-flush V-bounce dip-buy entry trigger (buy the reclaim after a fast flush).",
    )
    chili_momentum_red_vol_exhaustion_veto_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RED_VOL_EXHAUSTION_VETO_ENABLED"),
        description="Veto a breakout bar that closes RED on max session volume (climactic exhaustion / failed break).",
    )
    chili_momentum_thick_tape_veto_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THICK_TAPE_VETO_ENABLED"),
        description="Veto/discount high-cumulative-volume-with-no-net-progress tape (distribution / churn into supply).",
    )
    chili_momentum_curl_detector_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CURL_DETECTOR_ENABLED"),
        description="Rounding-bottom (curl) continuation selection signal — favor names curling back up off a base.",
    )
    chili_momentum_opening_bell_suppression_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OPENING_BELL_SUPPRESSION_ENABLED"),
        description="Suppress FRESH triggers in the first ~2 min after the RTH open (avoid opening-auction whipsaw).",
    )
    chili_momentum_opening_bell_suppress_base_min: float = Field(
        default=2.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OPENING_BELL_SUPPRESS_BASE_MIN"),
        description="The ONE documented base for opening-bell suppression: minutes after the 09:30 ET open during which a FRESH equity trigger is held. Adaptively WIDENED (up to ~2x) by the opener's own day-range/ATR volatility. Reversible via chili_momentum_opening_bell_suppression_enabled.",
    )
    chili_momentum_bid_prop_min_samples: int = Field(
        default=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BID_PROP_MIN_SAMPLES"),
        description="Bid-prop confirmer: minimum L1 tape samples required to evaluate; below this the confirmer FAILS OPEN (never blocks a break on thin/absent tape). Reversible via chili_momentum_bid_prop_confirmer_enabled.",
    )
    chili_momentum_bid_prop_max_samples: int = Field(
        default=8,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BID_PROP_MAX_SAMPLES"),
        description="Bid-prop confirmer: number of most-recent L1 tape samples examined for the non-decreasing-bid / spread-at-or-below-median backing check.",
    )
    chili_momentum_nonmonotonic_volume_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NONMONOTONIC_VOLUME_ENABLED"),
        description="Inverted-U volume preference — favor mid-range RVOL; treat too-high volume as choppy/exhausted, not better.",
    )
    chili_momentum_daily_trade_count_budget_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAILY_TRADE_COUNT_BUDGET_ENABLED"),
        description="Adaptive per-day A+ entry-count ceiling — cap the number of fresh entries per session (discipline / overtrading guard).",
    )
    # A1(b) (Ross CLRO-lesson 2026-07-02): TOP-RANK EXEMPTION for the trade-count budget.
    # Ross spent 3 trades on ONE name (CLRO) for +$8,917 while CHILI's 5/5 FIFO budget denied
    # 98 CLRO candidates (used on B-names). Episode-counting alone still yields 5/5 on a churny
    # day, so the #1 freshness-valid live-eligible mover whose score >= today's within-day p90
    # gets its OWN episode sub-budget = the SAME base — the CLRO-class name is never blocked
    # while B-names churn the ceiling. FAIL-CLOSED: unreadable rank / not-#1 / below-p90 => no
    # exemption (the ceiling stands). Default-ON; kill-switch
    # CHILI_MOMENTUM_TRADE_BUDGET_TOP_RANK_EXEMPT_ENABLED=0.
    chili_momentum_trade_budget_top_rank_exempt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TRADE_BUDGET_TOP_RANK_EXEMPT_ENABLED"),
        description="A1(b): the #1 freshness-valid live-eligible symbol (score >= within-day p90) gets its OWN episode sub-budget = the same base when the trade-count ceiling is reached. Fail-closed on unreadable rank. OFF => the ceiling is a hard block (no exemption).",
    )
    # A2 (Ross CLRO-lesson 2026-07-02): QUALITY-RANKED RISK-ENVELOPE DISPLACEMENT. On 07-02
    # two dying IPW losers pinned the whole 3%-of-equity aggregate risk envelope (726 blocks)
    # through CLRO's curl. When the aggregate-open-risk cap blocks a TOP-RANKED candidate (A1's
    # top-rank predicate), enqueue a stop-TIGHTEN on the largest at-risk open position to that
    # position's OWN already-computed most-defensive trail candidate (never an invented
    # breakeven; INVARIANT-A compose max(candidate, current) — stops only tighten). THIS tick
    # still blocks (no simultaneous act); the position's next tick applies the tighten and the
    # freed envelope admits the NEXT candidate tick. FAIL-CLOSED everywhere: no candidate level
    # / frees < planned / non-top-ranked => plain block, byte-identical to today. Default-ON;
    # kill-switch CHILI_MOMENTUM_RISK_ENVELOPE_DISPLACEMENT_ENABLED=0.
    # NOTE (sequencing): commit 0276285 (basis-independent slots) is NOT yet in main 888198e —
    # A2 composes with the CURRENT main aggregate-risk accounting; reconcile when 0276285 lands.
    chili_momentum_risk_envelope_displacement_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_ENVELOPE_DISPLACEMENT_ENABLED"),
        description="A2: when the aggregate-open-risk cap blocks a TOP-RANKED candidate, enqueue a stop-TIGHTEN on the largest at-risk open position to its own most-defensive trail candidate (INVARIANT-A max(candidate,current)); this tick still blocks, the next admits against the freed envelope. Fail-closed: no candidate level / frees < planned / non-top-ranked => plain block. OFF => byte-identical.",
    )
    # A4 (Ross CLRO-lesson 2026-07-02): MID-MOVE ELIGIBILITY-FLIP RE-SCORE. On 07-02 621x
    # "Not live-eligible per neural viability" covered Ross's ENTIRE CLRO window; the flip
    # landed ~60-90s after the top. main already has the tape-delta ignite feeder, but it only
    # fires on >=10%/5min crossers — CLRO's SLOW 15-min curl (~2.7%/5min) slipped under it. A4
    # closes that hole: at a viability block whose ONLY failing checks are live_eligible /
    # viability_freshness, when the session is ARMED and its own tick evidence shows running-up
    # continuation (signed OFI level>0 AND slope>=0), invoke the SAME single-symbol re-score the
    # tape-delta feeder uses (run_momentum_neural_tick, freshness_ts=now), rate-limited per
    # symbol to the adaptive tape cadence (clamp of tape_inter_row_gap_p50). FAIL-CLOSED: a
    # re-score error => the block stands; never force eligibility. Default-ON; kill-switch
    # CHILI_MOMENTUM_ELIGIBILITY_BLOCK_RESCORE_ENABLED=0.
    chili_momentum_eligibility_block_rescore_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ELIGIBILITY_BLOCK_RESCORE_ENABLED"),
        description="A4: at an eligibility-only viability block on an ARMED, running-up session, invoke the same single-symbol re-score the tape-delta feeder uses (freshness_ts=now), rate-limited per symbol to the adaptive tape cadence. Fail-closed: re-score error => block stands; never force eligibility. OFF => byte-identical (no re-score).",
    )
    chili_momentum_prior_day_pnl_damper_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PRIOR_DAY_PNL_DAMPER_ENABLED"),
        description="Size-DOWN the session after an outlier prior-day PnL (revert toward baseline risk after a big win/loss).",
    )
    # FIX-17 — DAY-OPEN RISK RAMP. Today the red-day reducer cuts size only AFTER a loss has
    # already landed (IPW -$137: the FIRST trades consumed 2.4x what later trades were then
    # allowed). Ramp the FIRST N real entries of the ET day: they share an adaptive fraction
    # of the day's risk envelope (size-DOWN), climbing back to full by entry N OR the moment
    # the day's realized start goes GREEN (then the cushion ladder takes over). N and the
    # fraction TILT off the recent daily-PnL volatility (reuse _prior_session_pnl_over_equity
    # + the daily-loss-cap machinery). Sizing-only — exits untouched. Fail-OPEN to full size
    # when history is unavailable. OFF => byte-identical (mult 1.0).
    # (feedback_adaptive_no_magic, feedback_no_dark_flags)
    chili_momentum_day_open_risk_ramp_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAY_OPEN_RISK_RAMP_ENABLED"),
        description="FIX-17: True (default) => the first N real entries of the ET day share an adaptive fraction of the day's risk envelope (size-DOWN, ramps to full by entry N or a green realized start). Sizing-only; exits untouched. OFF => 1.0.",
    )
    # ONE documented base: the size fraction the FIRST entry of the day gets (before the
    # volatility tilt). Reference point = a FLOOR the ramp climbs FROM, not a ceiling.
    chili_momentum_day_open_ramp_fraction_base: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAY_OPEN_RAMP_FRACTION_BASE"),
    )
    # ONE documented base: how many of the day's first entries the ramp spans (before the
    # volatility tilt). The ramp releases at entry N (or earlier on a green start).
    chili_momentum_day_open_ramp_entries_base: int = Field(
        default=3,
        ge=1,
        le=20,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAY_OPEN_RAMP_ENTRIES_BASE"),
    )
    chili_momentum_vwap_reclaim_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VWAP_RECLAIM_ENABLED"),
        description="Enable the sub-VWAP-then-reclaim entry trigger (buy the reclaim of VWAP after trading below it).",
    )
    chili_momentum_vwap_reclaim_min_below_bars: int = Field(
        default=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VWAP_RECLAIM_MIN_BELOW_BARS"),
        description="VWAP-reclaim trigger: minimum consecutive prior bars that must have closed BELOW VWAP before a current-bar reclaim counts (the SCAL101 K).",
    )
    chili_momentum_vwap_reclaim_vol_mult: float = Field(
        default=1.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VWAP_RECLAIM_VOL_MULT"),
        description="VWAP-reclaim trigger: rel-volume floor on the reclaim bar (conviction, not a drift back over VWAP).",
    )
    # ── BATCH B (FIX 1): STICKY BACK-SIDE BENCH (Ross front/back-side discipline). The
    # per-tick front_side_state / _detect_back_side vetoes recompute backside EACH tick, so
    # once a name rolls over midday CHILI re-arms it on every MACD pivot — chasing a dead,
    # rolled-over top. Ross BENCHES the name for the rest of the move once it is on the back
    # side. When ON, a CONFIRMED session backside (front_side_state.is_backside) latches a
    # session-level bench marker so the name is NOT re-armed each tick. MANDATORY un-bench:
    # a GENUINE NEW HIGH (live tick / completed-bar HOD above the benched-at HOD) clears the
    # bench — a name that truly resumes a new leg CAN still trade (never a permanent ban).
    # KILL-SWITCH: False -> the marker is never set/read -> byte-identical to today.
    chili_momentum_sticky_backside_bench_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STICKY_BACKSIDE_BENCH_ENABLED"),
        description="BATCH B FIX 1: once a name CONFIRMS the session back side, latch a session-level bench so it is NOT re-armed each tick (vs the per-tick recompute that chases a rolled-over top). MANDATORY fresh-HOD un-bench (a genuine new high clears the bench — never a permanent ban). KILL-SWITCH: False -> byte-identical.",
    )
    # ── FIX D: BACKSIDE VWAP-RECLAIM EXCEPTION ──────────────────────────────────────────
    # The decisive w0av0u3qy replay showed the below-VWAP backside bench ATE the SDOT (44x)
    # and ILLR (14x) early pushes — names that DIPPED below VWAP for a tick but were RECLAIMING
    # it from below with upward momentum (exactly the reclaims the killed E1 backside-veto used
    # to over-veto). A genuine-backside FADE (a name still FALLING away below VWAP) must STAY
    # benched; only an active RECLAIM-from-below is un-benched. The exception applies ONLY when
    # the bench reason is `below_vwap` (not already_faded / chasing_top) and the current price
    # has crossed BACK above VWAP (within the existing chili_momentum_entry_vwap_hold_buffer
    # tolerance) AND is rising vs the prior bar (positive reclaim direction). Every downstream
    # capital-protection gate still runs (max-loss circuit, structural stop, the 4 chase-guards,
    # the per-tick tape-required/extension vetoes). Heavily instrumented (live_entry_backside_
    # vwap_reclaim_exception counterfactual) so the operator can read the un-bench rate + PnL
    # delta and flip off if net-negative. KILL-SWITCH: False -> byte-identical (the exception is
    # never evaluated, the below_vwap bench latches exactly as before). Default ON per operator
    # style with all backstops intact.
    chili_momentum_backside_vwap_reclaim_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BACKSIDE_VWAP_RECLAIM_ENABLED"),
        description="FIX D: exception to the below-VWAP backside bench — do NOT bench a name that is RECLAIMING VWAP from below with upward momentum (price back above VWAP within chili_momentum_entry_vwap_hold_buffer AND rising vs the prior bar). A name still FALLING below VWAP stays benched. Reuses the existing vwap_hold_buffer (one documented base). KILL-SWITCH: False -> byte-identical (the below_vwap bench latches as before).",
    )
    # WAVE-4 ITEM-5: JEM STICKY-BENCH VWAP-RECLAIM UN-BENCH. FIX D above only declines to
    # LATCH a fresh below_vwap bench; once a name is ALREADY benched (any latch reason), the
    # ONLY un-bench was a genuine NEW HIGH above the benched-at HOD. JEM (2026-07-02) benched,
    # then at 12:50 reclaimed VWAP from below (8.97->9.06) into the 9.0->9.7 leg — never a new
    # HOD, so it stayed permanently benched and the whole leg was missed. This adds a SECOND
    # un-bench: a genuine fresh CROSS-from-below of session VWAP (prior completed close BELOW
    # VWAP*(1-buffer) AND current px AT/ABOVE VWAP) clears the bench for ALL latch reasons.
    # It is a CROSS (a state change from below to above), NOT a level test alone — a level test
    # would un-bench into the 13:24 dump where price merely hovered near VWAP; the cross
    # preserves that veto while catching the 12:50 reclaim. Reuses the SAME vwap_hold_buffer.
    # Every downstream capital-protection gate still runs. KILL-SWITCH: False -> byte-identical
    # (a benched name un-benches only on a new high, exactly as before).
    chili_momentum_backside_bench_reclaim_unbench_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BACKSIDE_BENCH_RECLAIM_UNBENCH_ENABLED"),
        description="WAVE-4 ITEM-5: un-bench an ALREADY-benched name on a genuine fresh CROSS-from-below of session VWAP (prior completed close < VWAP*(1-buffer) AND current px >= VWAP) — a CROSS (state change), NOT a level test (a level test would un-bench into a hover-then-dump; the cross catches the 12:50 JEM reclaim into the 9.0->9.7 leg while preserving the 13:24-dump veto). Applies to ALL latch reasons; reuses chili_momentum_entry_vwap_hold_buffer. KILL-SWITCH: False -> byte-identical (a benched name un-benches only on a new high).",
    )
    # ── BATCH B (FIX 2): HOT-TAPE WICK-RECLAIM entry (HVM101 #008, the extreme-volatility
    # variant of VWAP-reclaim). In HOT/parabolic tape ONLY: a huge rejection candle (large
    # upper wick, high range) -> immediate low-volume flush -> the next bar(s) retrace ~R of
    # the wick on rate-of-change -> re-enter into the wick; stop below the wick low. INVALID
    # on slow/cold recoveries (the hot-tape gate is MANDATORY: it reuses is_explosive_mover /
    # the RVOL signals). Returns the shared (ok, reason, debug) with pullback_high/pullback_low
    # so the runner stop/sizing machinery is reused (no new sizing path).
    # KILL-SWITCH: False -> the trigger never fires -> byte-identical to today.
    chili_momentum_wick_reclaim_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WICK_RECLAIM_ENTRY_ENABLED"),
        description="BATCH B FIX 2: hot-tape-only wick-reclaim entry — re-enter the retrace into a big upper-wick rejection candle after a low-volume flush, on a strong, EXPLOSIVE (high RVOL/ATR) name only. Stop below the wick low; shared (ok, reason, debug). KILL-SWITCH: False -> byte-identical.",
    )
    # The ONE documented knob for the wick-reclaim wick-size floor: the rejection candle's
    # UPPER wick must be at least this fraction of the bar RANGE to count as a big-wick
    # rejection. Adaptive on TOP of this: the bar must also be an outsized-range bar relative
    # to the name's own ATR%, so the absolute floor only guards the thin/degenerate case.
    chili_momentum_wick_reclaim_min_wick_frac: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WICK_RECLAIM_MIN_WICK_FRAC"),
        description="BATCH B FIX 2: minimum UPPER-wick fraction of the rejection bar's range for the wick-reclaim trigger (the one documented wick-size base; the bar must ALSO be outsized vs the name's own ATR%). Reversible via chili_momentum_wick_reclaim_entry_enabled.",
    )
    # The ONE documented knob for the retrace-into-the-wick depth: the reclaim must recover at
    # least this fraction of the rejection wick (from the flush low back up toward the wick
    # high). ~0.4 = the HVM101 ~40% retrace-on-rate-of-change. Adaptive: measured as a fraction
    # of the wick itself (the name's own bar geometry), so it carries no fixed-price magnitude.
    chili_momentum_wick_reclaim_min_retrace_frac: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WICK_RECLAIM_MIN_RETRACE_FRAC"),
        description="BATCH B FIX 2: minimum fraction of the rejection wick the reclaim must recover (HVM101 ~40% retrace) for the wick-reclaim trigger to fire. Reversible via chili_momentum_wick_reclaim_entry_enabled.",
    )
    # ── slow-recovery bar-count gate (HVM101 #008) ──────────────────────────────────
    # Ross: a wick rejection must RECOVER within 1-3 bars; the 4th bar only counts when
    # the tape is "really showing a lot of price action" (a high-rate-of-change, drying-up
    # flush); 5-6+ bars = a slow trickle = "more times than not invalid" = it CONFIRMS the
    # rejection, not a reclaim. wick_reclaim_confirmation already COMPUTES the bar offset
    # (cur - rej_idx) but never gated on it, so a slow trickle wrongly fired. This gate
    # REJECTS that invalid slow-recovery case only — a pure quality filter, never loosens
    # an existing wick-reclaim guard. KILL-SWITCH default OFF => byte-identical.
    chili_momentum_wick_reclaim_slow_recovery_gate_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WICK_RECLAIM_SLOW_RECOVERY_GATE_ENABLED"),
        description="HVM101 #008: reject wick-reclaim recoveries that took too many bars (a slow trickle = invalid). Quality filter (REJECTS only, never loosens a guard). KILL-SWITCH: False -> byte-identical.",
    )
    # The ONE documented bar-count base for the slow-recovery gate. Ross accepts 1-3 bars
    # outright; the 4th bar only on strong price-action (the flush-receding / drying-up
    # proof already computed in the trigger). 5-6+ bars are rejected. 4 mirrors the
    # VWAP-reclaim K+1 look-back yardstick (one consistent lane window, no new magic).
    chili_momentum_wick_reclaim_max_recovery_bars: int = Field(
        default=4,
        ge=1,
        le=12,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WICK_RECLAIM_MAX_RECOVERY_BARS"),
        description="HVM101 #008: max bars the wick-reclaim recovery may take (the one documented base; 1-3 fire, the 4th bar fires only on strong drying-up price-action, 5-6+ are rejected). Gated by chili_momentum_wick_reclaim_slow_recovery_gate_enabled.",
    )
    chili_momentum_bid_prop_confirmer_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BID_PROP_CONFIRMER_ENABLED"),
        description="Confirm a break only when the best-bid steps up / spread tightens (bid-propping microstructure confirmer).",
    )
    chili_momentum_live_eligible_max_spread_bps: float = Field(
        default=300.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_ELIGIBLE_MAX_SPREAD_BPS"),
        description="The SINGLE documented live-eligibility spread ceiling (bps). A spread WIDER than this disqualifies a name from LIVE entry (truly toxic / broken-halted quote). At/below it the wide spread only DERATES the viability score — the explosive low-float movers the Ross lane targets (~40-90bps) stay live-eligible and are entered with marketable-limit/maker orders that cross the spread. 0 = no spread disqualification (rely on the liquidity floor). Replaces the old hard 12/25bps disqualify that silently blocked every squeeze (1,495 'Not live-eligible' entry blocks 2026-06-25).",
    )
    # LEVER 1 — LIVE-ELIGIBILITY WIN-WIN. The day-monster (UPC +476%, Ross +$35k) scored
    # CHILI's #1 (0.755) but live_eligible=FALSE blocked it -> $0. Two leaks blanket-block a
    # GENUINE explosive Ross-class mover from LIVE while it is exactly what the lane exists to
    # trade: (a) the vol_regime==extreme blanket block, and (b) the A-setup quality floor
    # failing CLOSED when the rvol DATUM is merely MISSING (None) rather than affirmatively
    # low (UPC live: float=563K low-float OK, change=+10% OK, but rvol=None -> 1,520 rejects
    # 2026-06-29). When ON (default True): a name that clears the lane's EXISTING explosiveness
    # floor (below_explosive_floor — low-float + change, fail-open on absent rvol) AND is
    # product_tradable AND has a spread within the win-win ceiling stays LIVE-eligible on
    # extreme-vol/missing-rvol ALONE, and is flagged for RISK-BOUNDED sizing (the live_runner
    # extreme-vol size-down lever sizes it DOWN so worst-case loss is bounded the SAME as a
    # normal trade — the max-loss circuit #769 + structural/vol-floored stop + risk-first qty
    # are reused, no new sizing invented). THE WIN-WIN INVARIANT: a name that is extreme-vol
    # but NOT a genuine explosive mover (affirmatively below the explosiveness floor, OR
    # untradeable, OR toxic spread > ceiling, OR missing/zero float) is STILL gated. OFF =>
    # the prior blanket extreme-vol block + None-rvol fail-closed are byte-identical.
    chili_momentum_live_eligible_allow_extreme_explosive: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_ELIGIBLE_ALLOW_EXTREME_EXPLOSIVE"),
        description="LEVER 1 win-win: keep a GENUINE explosive Ross-class mover (clears the existing below_explosive_floor low-float+change floor, product_tradable, spread within the live ceiling) LIVE-eligible even on extreme-vol or a merely-MISSING rvol datum, and mark it for RISK-BOUNDED size-down (worst-case loss bounded the same as a normal trade via the reused max-loss circuit + vol-floored/structural stop + risk-first qty). A name that is NOT a genuine mover (affirmatively below the floor, untradeable, toxic spread, or unconfirmed float) is STILL gated. OFF = byte-identical (prior extreme-vol blanket block + None-rvol fail-closed).",
    )
    # LEVER 1 — the extreme-vol RISK-BOUNDED size-down fraction. When an extreme-vol genuine
    # mover is admitted live (above), its per-trade risk budget is multiplied by this fraction
    # so the worst-case dollar loss is bounded as conservatively as (or tighter than) a normal
    # trade despite the wider vol. ONE documented adaptive base (0.5 = half-risk, the same
    # conservative end the lane already uses for ask-heavy/overnight size-downs); composes
    # multiplicatively under the SAME 3x clamp + hard max_notional ceiling as every other
    # size-down lever, so it can NEVER push notional past any cap and is NEVER a veto. The
    # max-loss circuit, structural/vol-floored stop, and daily-loss breaker are untouched.
    chili_momentum_extreme_vol_risk_bounded_fraction: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXTREME_VOL_RISK_BOUNDED_FRACTION"),
        description="LEVER 1 risk-bounded size-down: per-trade risk-budget multiplier for an extreme-vol genuine mover admitted live (default 0.5 = half-risk). Composes under the same 3x clamp + hard max_notional ceiling as the other size-down levers; never a veto. ONE documented base.",
    )
    # ── THIN/TOXIC-SPREAD SQUEEZE CARVE-OUT (2026-06-29) ──────────────────────
    # A genuine TOP squeeze-fuel + high-RVOL mover at a ~4.6% (460bps) spread is a
    # FALSE decline at the 300bps live-eligibility ceiling (viability.py): the
    # marketable-LIMIT entry + notional guard + risk-first sizing already bound the
    # toxic-fill downside the zero-fills fix solved. These flags convert the BINARY
    # 300bps decline into a bounded SIZE-DOWN admission for ONLY the top within-batch
    # squeeze-percentile high-RVOL names: raise the ceiling EM/squeeze-scaled, mark
    # them extreme_vol_risk_bounded (reuses the LEVER-1 size-down), and clamp the
    # per-trade dollar loss to a HARD fraction of the normal base. ORDINARY names keep
    # the flat decline (zero-fills protection). flag OFF ⇒ byte-identical.
    chili_momentum_thin_spread_squeeze_lane_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THIN_SPREAD_SQUEEZE_LANE_ENABLED"),
        description="Master kill-switch for the thin/toxic-spread squeeze carve-out: convert the binary 300bps live-eligibility decline into a bounded EM/squeeze-percentile-scaled SIZE-DOWN admission for ONLY top-squeeze high-RVOL names. OFF ⇒ byte-identical binary decline + no hard loss cap. Equity-only.",
    )
    chili_momentum_thin_spread_squeeze_top_pctl: float = Field(
        default=0.80, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THIN_SPREAD_SQUEEZE_TOP_PCTL"),
        description="Within-batch squeeze_fuel_rank_pct floor for the carve-out (matches the existing squeeze entry size-up top_pctl). A name must sit at/above this within-batch squeeze percentile (AND clear the explosive RVOL floor AND the explosiveness floor) before its toxic-spread ceiling is widened. Adaptive — no magic SI/CTB cutoff.",
    )
    chili_momentum_thin_spread_ceiling_squeeze_slope: float = Field(
        default=1.0, ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THIN_SPREAD_CEILING_SQUEEZE_SLOPE"),
        description="Slope of the squeeze-scaled spread ceiling: admitted_ceiling = live_eligible_max_spread_bps * (1 + slope*squeeze_excess), squeeze_excess = clamp((sq_rank-top_pctl)/(1-top_pctl),0,1). At top_pctl excess=0 ⇒ base ceiling (no relax); at rank=1.0 ⇒ base*(1+slope). Hard-capped by the abs broken-quote ceiling (1500bps).",
    )
    chili_momentum_thin_spread_hard_loss_fraction: float = Field(
        default=0.5, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THIN_SPREAD_HARD_LOSS_FRACTION"),
        description="HARD per-trade dollar-loss cap for a carve-out-admitted wide-spread trade, as a fraction of the normal base max-loss-per-trade (default 0.5 = half). Applied as a min()-only clamp on _eff_max_loss ON TOP of the extreme_vol size-down; never raises risk, never vetoes. The #769 max-loss circuit + structural stop + daily breaker are untouched.",
    )
    # ADAPTIVE stale-quote (stale_bbo) window — scale the freshness ceiling to the NAME's own
    # trade cadence instead of a fixed 15s clock (operator 2026-06-25 "gawin mong adaptive").
    # max_age = clamp(cadence_mult * avg_inter-trade-interval, floor, ceiling). A name printing
    # every ~20s is fresh at ~60s; a halted/quiet name (no recent ticks) stays at the floor and
    # is correctly stale. ONE documented knob (cadence_mult) + safety bounds.
    chili_momentum_quote_freshness_cadence_mult: float = Field(
        default=3.0,
        ge=1.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_QUOTE_FRESHNESS_CADENCE_MULT"),
        description="K: the adaptive stale-quote window = K x the name's avg inter-trade interval (over the last 120s of iqfeed_trade_ticks), clamped to [floor, ceiling]. The one documented knob; higher = more tolerant of slow-but-live names.",
    )
    chili_momentum_quote_freshness_floor_seconds: float = Field(
        default=15.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_QUOTE_FRESHNESS_FLOOR_SECONDS"),
        description="Lower bound on the adaptive stale-quote window (never tighter than the venue base). Fast-printing + halted/quiet names land here.",
    )
    chili_momentum_quote_freshness_ceiling_seconds: float = Field(
        default=120.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_QUOTE_FRESHNESS_CEILING_SECONDS"),
        description="Upper bound on the adaptive stale-quote window — caps how old a quote can be and still count as fresh for a slow-trading name (safety vs trading on a truly stale price).",
    )
    # FIX B1 — extended-hours-aware stale-quote window. Pre/post-market movers trade at a
    # much slower cadence; the regular-hours ceiling perpetually flags them stale so the
    # entry trigger never gets a turn (stale_bbo peaks 16:00-19:00 ET). When ON, EXTENDED
    # HOURS raise the adaptive-window CEILING (the cadence-scaled window may now stretch
    # further for a slow-but-LIVE name), while the conservative FLOOR for genuinely
    # no-tick / halted names is unchanged. OFF => byte-identical (regular ceiling always).
    chili_momentum_ext_hours_quote_age_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXT_HOURS_QUOTE_AGE_ENABLED"),
        description="FIX B1: during pre/post-market, widen the adaptive stale-quote CEILING using the name's own inter-trade cadence so a slow-but-live extended-hours mover isn't perpetually flagged stale. Floor for halted/no-tick names is unchanged. OFF => byte-identical.",
    )
    chili_momentum_ext_hours_quote_ceiling_seconds: float = Field(
        default=300.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXT_HOURS_QUOTE_CEILING_SECONDS"),
        description="FIX B1: the adaptive stale-quote window CEILING used during extended hours (replaces the regular-hours ceiling only when ext-hours widening is enabled). Still a hard cap — a truly stale ext-hours quote past this is still stale.",
    )
    # FIX B2 — entry-quote secondary-source refetch. When the primary entry tick is STALE
    # (not invalid), refetch the BBO ONCE from the documented market-data priority
    # (Massive WS -> Polygon -> RH MCP get_equity_quotes) and re-run the SAME validation
    # before emitting live_blocked_by_risk. Validation is NOT weakened: invalid_bbo
    # (ask<bid, mid/bid/ask<=0) still hard-blocks; only the SOURCE of the quote changes.
    # OFF => byte-identical (no refetch; the primary stale verdict stands).
    chili_momentum_entry_quote_refetch_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_QUOTE_REFETCH_ENABLED"),
        description="FIX B2: on a STALE (not invalid) entry tick, refetch the BBO once from the secondary market-data chain (Massive WS -> Polygon -> RH MCP) and re-run the same validation before blocking. invalid_bbo still hard-blocks. OFF => byte-identical.",
    )
    # IQFEED-L1-FIRST BBO (d473331 lineage: "wire IQFeed into the entry gate"). The bridge already
    # MIRRORS tick-level IQFeed L1 into momentum_nbbo_spread_tape (source='iqfeed_l1') — this flag is
    # the READ side: the equity get_best_bid_ask reads the FRESHEST iqfeed_l1 tape row FIRST (recency-
    # gated by the same adaptive floor as the stale-quote window), and only falls through to Massive WS
    # -> robin_stocks -> Legend when that row is absent or older than the floor. IQFeed L1 prints
    # 1-2s-fresh @ 100s+ ticks/min on real movers, vs the WS quote that lagged 10-270s and false-blocked
    # wide-spread names on stale_bbo. Default ON (fixes over-rejection; reversible). KILL-SWITCH: False =>
    # current Massive-WS-first behavior, byte-identical (the IQFeed read is never attempted).
    chili_momentum_entry_gate_iqfeed_bbo_first: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_GATE_IQFEED_BBO_FIRST"),
        description="IQFeed-L1-first BBO: equity get_best_bid_ask reads the freshest momentum_nbbo_spread_tape source='iqfeed_l1' row FIRST (recency-gated by chili_momentum_quote_freshness_floor_seconds), falling through to Massive WS -> robin_stocks -> Legend when absent/stale. OFF => Massive-first, byte-identical.",
    )
    # BROKER-TRUTH RECONCILIATION (mig309) — TWO decoupled flags (write-then-verify-then-read):
    #
    #   chili_momentum_broker_truth_reconciliation_enabled — gates the WRITE pass
    #     (reconcile_momentum_outcomes_to_broker_truth). When ON the pass matches CLOSED
    #     momentum sessions to broker fills and stamps the authoritative broker_* label
    #     columns + divergence audit. ADDITIVE: never touches legacy realized_pnl_usd /
    #     return_bps. When OFF: pass is a no-op, zero new SQL, byte-identical.
    #
    #   chili_momentum_broker_truth_label_enabled — gates the learning READ switch via
    #     authoritative_label_for_outcome(). When OFF (default) the accessor returns the
    #     LEGACY label byte-for-byte (no behavior change). When ON it returns the broker-true
    #     label for reconciled rows and EXCLUDES (is_reconciled=False) any unreconciled row
    #     so the trainer drops it (never a fabricated $0). ⚠️ Flipping the READ flag ON also
    #     changes the daily-loss-cap / profit-giveback GATE inputs (risk_evaluator reads the
    #     same field) — it is a TRADING-BEHAVIOR change to soak deploy-when-flat, NOT a pure
    #     data relabel. Operator flips READ only after inspecting the divergence distribution
    #     + day-net cross-check from the WRITE pass.
    chili_momentum_broker_truth_reconciliation_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BROKER_TRUTH_RECONCILIATION_ENABLED"),
        description="Kill-switch: True => the reconcile pass writes the authoritative broker-truth label columns on momentum_automation_outcomes (additive, never overwrites legacy fields). Default OFF (pass is a no-op).",
    )
    chili_momentum_broker_truth_label_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BROKER_TRUTH_LABEL_ENABLED"),
        description="Kill-switch: True => learning consumers read the broker-true label via authoritative_label_for_outcome (reconciled rows use broker PnL/bps; unreconciled are EXCLUDED). Default OFF (legacy field, byte-identical). Flipping ON changes daily-loss/giveback gate inputs — soak deploy-when-flat.",
    )
    # Replay v3 R1 — append the live_eligible TIME-SERIES to momentum_viability_history
    # (mig311) on every viability write, so FUTURE replays read the exact recorded
    # flicker instead of reconstructing it. Default ON: it is cheap (one extra INSERT per
    # viability tick), risk-free (append-only observability, never touches the live
    # viability decision), and fail-open (a history-write error is swallowed and never
    # blocks the live viability upsert). Kill-switch =0 to disable the append entirely
    # (byte-identical to pre-R1: zero history rows written, the viability upsert unchanged).
    chili_momentum_viability_history_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VIABILITY_HISTORY_ENABLED"),
        description="Replay v3 R1: True (default) => append the live_eligible time-series to momentum_viability_history on each viability write (perfect future replay fidelity). Cheap, append-only, fail-open. =0 disables the append (byte-identical, zero history rows).",
    )
    # ── Extended-hours trading window (Ross trades the pre-market gap-and-go) ──
    # The momentum equity lane is tradeable from premarket_start → afterhours_end ET.
    # Regular session (9:30–16:00 ET) is a fixed exchange fact in market_profile.py;
    # these two settings are the ONLY tunable bounds. Ross streams 7:00am ET — that's
    # the documented pre-market default. To DISABLE pre-market, set start to "09:30";
    # to disable after-hours, set end to "16:00" (the window itself is the control —
    # there is no separate on/off flag). Orders placed outside RTH are flagged
    # extended_hours so the venue routes them correctly (Alpaca DAY+ext, RH override).
    chili_momentum_premarket_start_et: str = Field(
        default="04:00",
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_START_ET"),
        description="Premarket entry-window open (ET). 04:00 = the US extended-session open Ross trades from; the data-session is derived as this minus selection_prep_lead. (Was 07:00; aligns the code default with the deployed value.) Set 09:30 to collapse premarket to RTH-only.",
    )
    chili_momentum_afterhours_end_et: str = Field(
        default="20:00",
        validation_alias=AliasChoices("CHILI_MOMENTUM_AFTERHOURS_END_ET"),
    )
    # Premarket tick-break confirmation (the CUPR fix): in premarket (thin, whipsaw,
    # NO L2) a tick poking 1¢ through the pullback high is a false-pop shake-out
    # entry (CUPR: bought 4.07 on a failed pop → −15% stop → THEN +92%). Require an
    # ATR-derived THRUST buffer so a real break fires, not a chop wick. RTH + crypto
    # unchanged. ONE adaptive knob (atr_mult); buffer = atr_pct·mult·level.
    chili_momentum_premarket_tickbreak_confirm: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_TICKBREAK_CONFIRM"),
        description="Gate premarket tick-break entries on an ATR thrust buffer (the CUPR false-pop guard). Premarket-only; RTH + crypto byte-unchanged. Only gates an entry (INVARIANT-A-safe). 0 = old behavior (fire on any 1¢ poke).",
    )
    chili_momentum_premarket_tickbreak_atr_mult: float = Field(
        default=0.10,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_TICKBREAK_ATR_MULT"),
        description="The single adaptive base knob for the premarket tick-break buffer: required clearance above the level = atr_pct · this · level (equity-relative; auto-scales as ATR thickens into RTH).",
    )
    chili_momentum_premarket_tickbreak_floor_bps: float = Field(
        default=100.0, ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_TICKBREAK_FLOOR_BPS"),
        description="Minimum premarket tick-break clearance (bps over the level) regardless of ATR. At the START of a premarket explosion the historical-bar ATR is LOW, so the ATR buffer alone is too thin to reject the false-pop (CUPR cleared a ~0.05-ATR buffer). The buffer = max(atr_pct·mult, floor). 0 = ATR-only (old behavior).",
    )
    # Selection/data must be WARM before the entry window opens (operator
    # 2026-06-11, twice): the data-session open is DERIVED as entry start minus
    # this lead (never later than the exchange's 04:00 ET extended open) — the
    # movers traded at window-open develop before it.
    chili_momentum_selection_prep_lead_min: int = Field(
        default=60,
        ge=0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SELECTION_PREP_LEAD_MIN"),
        description="Minutes the data/selection window leads the entry window (data open = entry start − lead).",
    )
    chili_momentum_premarket_change_fallback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_CHANGE_FALLBACK_ENABLED"),
        description="Premarket: when the snapshot's vendor todaysChangePerc is null, derive change% from today's open (else prevDay close) → live premarket price, so already-printing gappers enter the universe/viability board by ~04:00 ET (warm by the derived 03:00-03:45 prep window) instead of ~09:40 ET. Mirrors the proven nbbo_tape fallback; fail-closed (no usable base → dropped). RTH byte-unchanged (vendor field populated RTH → never consulted). 0 = old behavior.",
    )
    chili_momentum_premarket_gap_full_universe_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_GAP_FULL_UNIVERSE_ENABLED"),
        description="Equity Ross lane: when the equity-viability-refresh hands its already-SCREENED universe to the premarket-gap scan, size the scan's output cap to that screened pool (so EVERY screened gapper is scored into viability) instead of the fixed top-15-by-raw-gap-magnitude. Fixes fresh-catalyst mid-gap runners (low-float +7% name on an 8AM catalyst, e.g. QUCY) being truncated out by already-extended +200% gappers and thus never getting a fresh viability score → never armable. The downstream Ross percentile re-rank (+ the bridge top-30 cap) still makes the real selection; this only stops the premature magnitude truncation. Adaptive (cap = screened-universe size, no new magic number). 0 = old fixed top-15 cap (the broad default-universe sweep is byte-unchanged either way).",
    )
    # ── UNCAPPED universe + WS ignition (surface EVERY explosive mover, fast) ─────
    # ROOT CAUSE (verified live 2026-06-15): the day's biggest movers never enter the
    # SCORED universe (momentum_symbol_viability) — so the lane can't even consider
    # them. Two drop points: (1) the top-50 count cap in universe.build_equity_universe
    # truncated 296 screened movers to 50 (CUPR +125% faded on pos_in_range and ranked
    # out), and (2) the EMA9 continuation gate in scan_momentum_continuation emits NO
    # signal for a VERTICAL name (RGNT +498% is nowhere near its EMA9) so it never gets
    # a fresh per-symbol viability row even though build_equity_universe selects it.
    # FIX: uncap the universe (the adaptive screen + a DB-safety hard ceiling are the
    # only bounds — no top-N quality cap) and add an additive WS ignition scorer that
    # scores a name DIRECTLY into viability the instant a price-bus tick shows it
    # igniting (bypassing the EMA9 continuation gate). Both flag-gated; OFF ⇒ byte-
    # identical to current (top-50 + scheduled-only). Adaptive / no-magic: ONE base
    # FLOOR knob (chili_momentum_ignition_min_pct); the hard ceiling is a DB backstop,
    # NOT a quality cap. See docs/DESIGN/MOMENTUM_LANE.md.
    chili_momentum_universe_uncapped_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_UNIVERSE_UNCAPPED_ENABLED"),
        description="Surface EVERY screen-passing mover into the scored universe instead of truncating to the top-50 by the freshness×move rank. The adaptive price/$-volume/change screen + the hard ceiling (DB-safety) are the only bounds; the downstream Ross percentile re-rank + the bridge chunking still make the real selection. Fixes the day's biggest movers (e.g. CUPR +125%) being ranked out of the candidate pool and thus never getting a viability row. 0 = old top-50 count cap (byte-identical to current).",
    )
    chili_momentum_universe_hard_ceiling: int = Field(
        default=1500,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_UNIVERSE_HARD_CEILING"),
        description="DB-safety backstop for the uncapped universe — the absolute max number of screen-passers surfaced per build (so a runaway snapshot can't flood viability). This is NOT a quality cap (the adaptive screen does the real selection); it exists only to bound the row count. Only consulted when chili_momentum_universe_uncapped_enabled is on.",
    )
    chili_momentum_ws_ignition_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WS_IGNITION_ENABLED"),
        description="Enable the additive WS ignition scorer: subscribe the (uncapped) equity universe on the price bus and, the instant a tick shows a name igniting (intraday move% ≥ the ignition floor), score it DIRECTLY into momentum_symbol_viability — bypassing the EMA9 continuation gate that emits nothing for a vertical name (e.g. RGNT +498% nowhere near its EMA9). The scheduled 5-min batch builder + legacy pattern lane are unchanged; this path is purely additive. 0 = scheduled-only (no WS ignition; byte-identical to current).",
    )
    # CAPTURE-G3 (2026-07-03): Gate-0 subscription latency. The IQFeed trade/depth bridges
    # (host processes) subscribe symbols by POLLING the armed-sessions + eligible-mover viability
    # board on a ~20s refresh, so a symbol that FIRST ignites only reaches the bridge after its
    # viability row is written AND the next refresh — a ~2.7-min blind window on a sub-2-min
    # squeeze (VWAV 2026-06-30: the whole 5->9.75 leg was un-taped). When ON, the ws-ignition
    # first-alert writes a hint to momentum_bridge_subscribe_requests (a NON-trading coordination
    # table) the instant a name crosses a Ross axis; the bridge fast-polls that table and
    # subscribes immediately (first-alert -> subscribed in seconds). OFF ⇒ no hint write ⇒ the
    # bridge is byte-identical to the poll-only cadence.
    chili_momentum_bridge_subscribe_on_alert_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BRIDGE_SUBSCRIBE_ON_ALERT_ENABLED"),
        description="CAPTURE-G3: event-driven subscribe-on-first-alert for the IQFeed bridges. True (default) = on first ignition/alert, write a subscribe hint to momentum_bridge_subscribe_requests so the host bridge fast-polls it and subscribes the symbol immediately (closes the ~2.7-min Gate-0 blind window on sub-2-min squeezes like VWAV). False = no hint write; the bridge falls back to its ~20s poll cadence (byte-identical to current).",
    )
    chili_momentum_ross_equity_universe_required: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ROSS_EQUITY_UNIVERSE_REQUIRED"),
        description=(
            "Require live equity momentum entries on the Ross/Robinhood lane to prove "
            "they belong to the Ross small-cap active-mover universe. This is an "
            "entry-time invariant, not a candidate-cap increase."
        ),
    )
    chili_momentum_ignition_min_pct: float = Field(
        default=3.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_IGNITION_MIN_PCT"),
        description="The single adaptive FLOOR knob for the WS ignition scorer: the minimum intraday move% (live price vs today's open / prev-close) for a tick to be treated as an ignition worth scoring into viability. A FLOOR / reference point, not a ceiling — the downstream Ross percentile re-rank does the real selection above it. Sized to drop dead tape while still catching the real igniters early.",
    )
    # ── S1 EVENT-DRIVEN FEEDER (docs/DESIGN/MOMENTUM_ENGINE.md §1, §5) ────────────
    # A cold new explosive mover today waits up to ~300s for the next viability-refresh
    # batch (_run_equity_viability_refresh_job, ~half the 600s freshness gate) before it
    # can earn a viability row and arm. These flags add an EVENT-DRIVEN feeder so the
    # instant the tape (IQFeed→momentum_nbbo_spread_tape, ~1s) shows a name crossing ANY
    # Ross axis, it is scored straight into viability (freshness_ts=now) via the SAME
    # single-symbol run_momentum_neural_tick path the ignition loop uses — target ~5-15s.
    # The 300s batch stays as the backstop. Every flag OFF ⇒ byte-identical to the batch-
    # only deployed path.
    chili_momentum_event_select_primary_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EVENT_SELECT_PRIMARY_ENABLED"),
        description="MASTER switch for the S1 event-driven selection feeder (docs/DESIGN/MOMENTUM_ENGINE.md §1/§5). When ON, the tape-delta ignite job is REGISTERED and the ignition-loop tick gate uses the basis-complete _ross_threshold_crossed (RVOL OR gap OR move% crosses a Ross floor) instead of the move%-only floor — so a cold new explosive mover reaches live_eligible in ~5-15s instead of waiting ~300s for the next viability-refresh batch. OFF ⇒ the job is NOT registered and the ignition loop keeps its current move%-only gate (byte-identical to the deployed batch-only path). The 300s batch always runs as the backstop.",
    )
    chili_momentum_tape_delta_ignite_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TAPE_DELTA_IGNITE_ENABLED"),
        description="Enable the price-bus-INDEPENDENT tape-delta ignite job: an interval job that reads ONLY tape rows newer than an in-process high-water mark (incremental delta, not a full rescan), applies _ross_threshold_crossed, and scores each crosser into viability via the single-symbol run_momentum_neural_tick path (freshness_ts=now). It is the live winner when the price bus is down (the deployed state). OFF (with the master flag still ON) ⇒ the registered job is a no-op return (byte-identical-off). Only consulted when chili_momentum_event_select_primary_enabled is ON.",
    )
    chili_momentum_tape_delta_min_seconds: float = Field(
        default=5.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TAPE_DELTA_MIN_SECONDS"),
        description="The ONE documented adaptive FLOOR (seconds) for the tape-delta ignite job's cadence. The job runs at clamp(p50 tape inter-row gap, this floor, 15s) — adaptive to how fast the tape is actually filling, never below this floor (so it can't hammer the DB on a dense tape) and never above 15s (so a cold igniter is caught within one cadence). A floor / reference point, not a fixed clock.",
    )
    # ── HOT-MOVER RE-CATCH + sub-$1 explosive exemption (the NEXR late-surge miss) ─
    # ROOT CAUSE (verified live 2026-06-24): a name that FADED midday (low pos_in_range)
    # then SURGED late ranks #51+ in build_equity_universe's freshness×move sort, is
    # TRUNCATED out of the candidate pool (top-50 / hard-ceiling), stops getting a fresh
    # viability row, goes stale in 10min, and is fresh-gated OUT of arming — even though
    # it scored eligible (NEXR +106%, scored 0.580, NEVER armed; sub-$1 part of the day
    # so price_min=1.0 also excluded it). The cure is to KEEP the genuinely-explosive
    # name rescored (so freshness follows automatically), WITHOUT loosening the staleness
    # gate and WITHOUT flooding the lane with penny junk.
    # FIX (conservative, two additive guards, both flag-gated, OFF ⇒ byte-identical):
    #  (A) GUARANTEE-INCLUDE the top hot movers (high RVOL AND big %-move AND ≥$-vol
    #      floor) BEFORE the truncation cap, even if pos_in_range ranks them past the
    #      cap — so a faded-then-resurging runner stays in the rescoring set. Bounded by
    #      ONE knob (the guaranteed-slot count); the RVOL/$-vol/%-move quality bar is NOT
    #      relaxed (no junk). The normal freshness×move ranking is unchanged — this only
    #      ADDS the hot-mover guarantee on top.
    #  (B) let an EXPLOSIVE sub-$1 name (same RVOL + %-move + $-vol bar) pass the
    #      price_min floor (Ross trades sub-$1 runners; NEXR ran $0.95→$1.18). Guarded,
    #      not a blanket floor removal — ONLY a name that clears the hot-mover bar is
    #      exempted; ordinary sub-$1 penny tape is still dropped.
    # Adaptive / no-magic: the RVOL + %-move bars are within-batch high-percentiles (the
    # batch decides what "genuinely explosive" means today) clamped to documented floors;
    # the $-vol floor reuses the profile's existing min_dollar_volume. No re-import
    # shadowing (settings read once, lazily). See docs/DESIGN/MOMENTUM_LANE.md.
    chili_momentum_hot_mover_recatch_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_MOVER_RECATCH_ENABLED"),
        description="Guarantee-include the top genuinely-explosive hot movers (high RVOL AND big %-move AND ≥ the profile $-vol floor) in build_equity_universe BEFORE the top-N / hard-ceiling truncation, so a name that FADED midday then SURGED late (ranks #51+ on freshness×move, e.g. NEXR +106%) stays in the rescoring set and its viability stays fresh enough to arm. Bounded by chili_momentum_hot_mover_recatch_slots; the RVOL/$-vol/%-move quality bar is NOT relaxed (no penny-junk flood) and the normal ranking is unchanged (this only ADDS guaranteed slots on top). 0 = byte-identical to the freshness×move-ranked truncation.",
    )
    chili_momentum_hot_mover_recatch_slots: int = Field(
        default=15,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_MOVER_RECATCH_SLOTS"),
        description="The ONE documented bound for the hot-mover re-catch: the max number of genuinely-explosive hot movers guaranteed into the universe ahead of the truncation cap (ranked by RVOL×move among the hot set). A small bound so a faded-then-resurging runner is re-caught without the guarantee itself becoming an unbounded second universe. Only consulted when chili_momentum_hot_mover_recatch_enabled is on.",
    )
    chili_momentum_hot_mover_rvol_floor: float = Field(
        default=5.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_MOVER_RVOL_FLOOR"),
        description="The single documented RVOL FLOOR (today share-volume ÷ prevDay share-volume) for the hot-mover quality bar — Ross's ≥5× relative-volume reference. A name must clear MAX(this floor, the batch RVOL high-percentile) AND the %-move bar AND the $-vol floor to qualify as a guaranteed hot mover or for the sub-$1 exemption. A FLOOR (the batch percentile can only lift it), so missing/degenerate prevDay volume never fabricates an explosive name (fail-closed: no usable RVOL ⇒ not a hot mover).",
    )
    chili_momentum_hot_mover_change_floor: float = Field(
        default=20.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_MOVER_CHANGE_FLOOR"),
        description="The single documented %-move FLOOR for the hot-mover quality bar — a 'genuinely explosive RIGHT NOW' threshold well above the universe's modest min_change_pct in-play floor. A name must clear MAX(this floor, the batch change high-percentile) (plus the RVOL + $-vol bars) to be guaranteed-included or sub-$1-exempted. A FLOOR; the within-batch percentile can only raise it so a quiet day can't flood the guarantee.",
    )
    chili_momentum_hot_mover_subdollar_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_MOVER_SUBDOLLAR_ENABLED"),
        description="Sub-$1 exemption: let an EXPLOSIVE sub-$1 name (clears the SAME RVOL + %-move + $-vol hot-mover bar) pass the profile's price_min floor (Ross trades sub-$1 runners; NEXR ran $0.95→$1.18). Guarded, NOT a blanket floor removal — ordinary sub-$1 penny tape that fails the hot-mover bar is still dropped. Requires chili_momentum_hot_mover_recatch_enabled. 0 = the price_min floor is enforced for every name (byte-identical).",
    )
    # ── Daily-chart context (the multi-timeframe layer Ross STARTS with) ─────────
    # Adds a 5th SELECTION pillar (daily_structure, 10% weight) from
    # daily_levels.compute_daily_context — break ABOVE a major daily level + room to
    # the next level + a SOFT broader-trend minority input. It RE-RANKS the candidate
    # pool toward clean daily breakouts; it can NEVER block a fill (the entry gate is
    # untouched). A news-gap spike breaking a level scores HIGH (the CUPR guarantee).
    # OFF ⇒ the selection is byte-identical (the liquidity-biased weights). Equities
    # only; the daily fetch is cached (600s) on the viability-refresh pass.
    chili_momentum_daily_context_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAILY_CONTEXT_ENABLED"),
        description="Enable the daily-chart selection tilt (5th pillar daily_structure, 10% weight; equities). RE-RANKS toward clean daily breakouts, never blocks a fill. 0 = selection byte-identical (liquidity-biased weights).",
    )
    # ATTENTION-LEADERSHIP selection pillar (2026-06-22 Ross study — the TRUE winner/loser
    # separator). Ranks each EQUITY mover by its amplitude share+rank of the live mover-field
    # (the dominant leader holds + squeezes; followers round-trip). A re-rank pillar (never a
    # veto → no winner-kill, breadth kept). 0 = selection byte-identical (liquidity-biased weights).
    chili_momentum_attention_leadership_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ATTENTION_LEADERSHIP_ENABLED"),
        description="Enable the attention-leadership selection pillar (amplitude share+rank over the full mover field, +dormant->explosive vol; equities). RE-RANKS toward the dominant leader, never blocks a fill. 0 = byte-identical.",
    )
    chili_momentum_use_real_float: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_USE_REAL_FLOAT"),
        description="Feed the low-float pillar the REAL share count (Polygon reference share_class_shares_outstanding) instead of the market_cap $-proxy; names without it use a consistent market_cap/price share-count estimate (never mixes units). Kill-switch: 0 = the pillar keeps the market_cap proxy (selection byte-identical).",
    )
    # FLOAT BACKFILL (anti-flicker; equities). Share FLOAT is STATIC (does not change
    # intraday), so a known value is a CONSTANT — never stale dynamic data. The scanner
    # bridge forwards a ROTATING subset of movers per cycle, so a symbol absent from THIS
    # tick's ross_signals never gets float-enriched and its persisted
    # execution_readiness_json.extra.ross_signals[sym].float_shares flickers to None — which
    # the fail-closed A-setup quality floor wrongly rejects. When the current cycle can't
    # resolve a real float, BACKFILL from the last-known value (prior persisted
    # momentum_symbol_viability row for the symbol). Only FILLS a missing float — NEVER
    # overwrites a fresh real float, NEVER fabricates for a never-seen symbol (stays None =>
    # fail-closed reject = correct). 0 = byte-identical (no backfill).
    chili_momentum_float_persistence_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLOAT_PERSISTENCE_ENABLED"),
        description="Backfill a missing float_shares from the last-known persisted momentum_symbol_viability row (share float is STATIC, so a known value is a constant — anti-flicker for the fail-closed A-setup quality floor). Equities only; fills missing float only, never overwrites a fresh real float, never fabricates for a never-seen symbol. 0 = byte-identical (no backfill).",
    )
    chili_momentum_daily_lookback_days: int = Field(
        default=20,
        ge=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAILY_LOOKBACK_DAYS"),
        description="The ONE base structural knob for the daily-context layer: the swing-high/low window + daily-ATR period (days). Everything else derives from the daily ATR (equity-relative).",
    )
    # P1 SECOND-DAY / MULTI-DAY CONTINUATION CONTEXT (selection re-rank tilt; equities). Folds
    # into the existing daily_structure sub-score (compute_daily_context, reusing the P0 daily
    # df — no new fetch): BOOST a clean DAY-2 holding above the prior-day high/close, DERATE
    # day-3+ (exhaustion). A re-rank tilt, never a hard gate (a day-1 news spike still scores
    # HIGH — the CUPR guarantee). OFF => daily_structure_pct byte-identical (no boost/derate).
    chili_momentum_second_day_context_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SECOND_DAY_CONTEXT_ENABLED"),
        description="Kill-switch for the P1 second-day/multi-day continuation selection tilt (equities). false = no day-2 boost, no day-3+ derate; the run/level fields are still surfaced for audit but daily_structure_pct is byte-identical.",
    )
    chili_momentum_symbol_freshness_tilt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SYMBOL_FRESHNESS_TILT_ENABLED"),
        description="A5 (clro-p1) two-signed symbol-freshness SELECTION tilt (equities). Folds a bounded re-rank into daily_structure_pct: FRESH (no recent explosive daily move AND low prior trailing-20d $vol percentile vs the symbol's own trailing year) => positive tilt; STALE (a recent explosive daily move that FADED back below its pre-move base) => negative tilt. TILT only, never a veto; fail-open-to-neutral on thin/NaN history. false => daily_structure_pct byte-identical.",
    )
    # ── Halt awareness (Ross low-floats halt constantly: LULD circuit breakers) ──
    # A trading HALT is observable as a SUSTAINED quote freeze: the stale_bbo gate
    # already blocks single stale ticks; this many CONSECUTIVE stale-quote ticks on
    # an armed equity marks a suspected halt (vs a one-tick data blip). Sized in
    # ticks so it self-scales with the runner cadence.
    chili_momentum_halt_stale_ticks: int = Field(
        default=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_STALE_TICKS"),
    )
    # After a suspected halt RESUMES (quotes fresh again), block new ENTRIES for this
    # cooldown — the post-resume whipsaw window where price discovery is violent
    # (KMRK 2026-06-10 resumed through $6.81→$3.01→$5.13→$4.35→$3.33; the lane bought
    # the middle of it). Watching continues so structure rebuilds; only entry waits.
    chili_momentum_halt_resume_cooldown_seconds: float = Field(
        default=120.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_RESUME_COOLDOWN_SECONDS"),
    )
    # Window after a halt RESUMES in which the specialized halt_resume_dip entry
    # pattern owns the tape (Ross 2026-06-10 DSY: "it drops and on the resumption I
    # bought the dip"). The dip trigger demands dip+hold+reclaim STRUCTURE — strictly
    # stronger evidence than the generic pullback-break — so it may enter inside the
    # whipsaw cooldown above. Past the window the normal trigger ladder owns entries.
    chili_momentum_halt_resume_dip_window_seconds: float = Field(
        default=600.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_RESUME_DIP_WINDOW_SECONDS"),
    )
    # GAP 1 (Warrior re-audit) — HALT-CHAIN RISK GATE. A name that keeps halting UP
    # again and again (a "halt chain") is climbing the LULD ladder — each successive
    # limit-up halt-resume long is statistically later/riskier (the move is more
    # extended, the unwind is sharper). When ON, the lane tracks a PER-SYMBOL
    # consecutive halt-UP count (reset on a new session/day or on a halt-down resume)
    # and, once the count reaches chili_momentum_halt_chain_block_count, BLOCKS the
    # halt-resume-dip long entirely (and de-weights size as it climbs toward the block).
    # RISK-REDUCING ONLY: it can turn a would-fire into a no-fire / smaller, never the
    # reverse; it NEVER touches exits or any other gate. Default OFF ⇒ the counter is
    # never read ⇒ byte-identical. docs/STRATEGY/CC_REPORTS/2026-06-26_warrior-courses-reaudit.md
    chili_momentum_halt_chain_risk_gate_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_CHAIN_RISK_GATE_ENABLED"),
        description="GAP 1: when ON, track a PER-SYMBOL consecutive halt-UP count; once it reaches chili_momentum_halt_chain_block_count, BLOCK the halt-resume-dip long (and size down as it climbs). Risk-reducing only (block/de-weight); never loosens any gate or touches exits. OFF ⇒ byte-identical.",
    )
    chili_momentum_halt_chain_block_count: int = Field(
        default=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_CHAIN_BLOCK_COUNT"),
        description="GAP 1: the consecutive halt-UP count at/above which the halt-resume-dip long is BLOCKED (Ross watches ~3 halts up before the move is too extended to chase the resumption). Below it the entry is de-weighted toward the block. Only consulted when chili_momentum_halt_chain_risk_gate_enabled is ON.",
    )
    # GAP 2 (Warrior re-audit) — HALT-RESUMPTION PRICE-DIRECTION conviction. When a
    # name halts, capture the halt_level (the price at the moment the halt was detected).
    # On resume, compare the resumption open vs that halt_level: opens HIGHER (gap-up
    # resume) = bullish conviction → a small size BOOST on the halt-resume-dip long;
    # opens LOWER = caution → a size PENALTY. The deployed halt_resume_dip_trigger reads
    # ONLY post-resume bars and never the halt_level — this wires the halt_level + the
    # resumption-direction read as a CONVICTION MODIFIER (annotation in the debug dict;
    # live applies it to entry size). Default OFF ⇒ no halt_level is read / no modifier
    # is emitted ⇒ byte-identical.
    chili_momentum_halt_resumption_direction_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_RESUMPTION_DIRECTION_ENABLED"),
        description="GAP 2: capture halt_level (price at the halt) and compare resumption_open vs it. Resumes HIGHER = bullish (small size boost); resumes LOWER = caution (size penalty). Conviction modifier on the halt-resume-dip long only; never loosens a gate. OFF ⇒ byte-identical.",
    )
    chili_momentum_halt_resumption_boost_frac: float = Field(
        default=0.15,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_RESUMPTION_BOOST_FRAC"),
        description="GAP 2: the fractional size adjustment magnitude for the resumption-direction conviction modifier (e.g. 0.15 ⇒ +15% on a bullish gap-up resume, −15% on a caution lower resume). Equity-relative multiplier on the existing structural size, not a fixed $; bounded [0,0.5]. Only consulted when chili_momentum_halt_resumption_direction_enabled is ON.",
    )
    # GAP 3 (Warrior re-audit) — FALSE-HALT RESUMPTION REVERSAL avoid. A limit-UP halt
    # that resumes WEAK — the first post-resume bar OPENS BELOW the halt_level (the price
    # it halted at) — is a FALSE halt: the limit-up move did not hold through the auction
    # and the resumption is a fade, not a continuation. When ON, the halt-resume-dip long
    # is AVOIDED in that case (shares the halt_level + resumption read with GAP 2). Pure
    # risk-reduction: it can only ADD a no-fire reason; it never enables an entry. Default
    # OFF ⇒ the resumption_open-vs-halt_level check is never made ⇒ byte-identical.
    chili_momentum_false_halt_avoid_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FALSE_HALT_AVOID_ENABLED"),
        description="GAP 3: a limit-UP halt that resumes WEAK (first post-resume bar opens below halt_level) = a FALSE halt → AVOID the halt-resume-dip long. Shares halt_level + resumption read with GAP 2. Risk-reducing only (adds a no-fire); never enables an entry. OFF ⇒ byte-identical.",
    )
    # ── R6 (WAVE-4 ITEM-1): PRINT-RECENCY halt inference (an INDEPENDENT second path) ──
    # The quote-freshness halt inference (_register_stale_quote_tick ⇒ stale_bbo streak)
    # STARVED since 2026-06-26: a secondary BBO refetch stamps FRESH meta on cached quotes,
    # so stale_bbo never returns → suspected_halt went 602/day → 0 and halt_resume_dip has
    # NEVER fired. A real LULD halt still stops the PRINTS (trade tape) even when the quote
    # meta looks fresh. When ON, for a WATCHED/HELD symbol with a fresh NBBO session, if the
    # trade tape (iqfeed_trade_ticks) shows NO PRINTS for an ADAPTIVE window while the market
    # is open AND the name was RECENTLY ACTIVE, mark a suspected halt (the SAME downstream
    # flags the quote path sets). FAIL-CLOSED: no tape data / not recently active ⇒ NO
    # inference (never false-halt a quiet name). The existing false_halt_avoid guard still
    # applies at the resume read. OFF ⇒ this path is never consulted ⇒ byte-identical.
    chili_momentum_halt_print_recency_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_PRINT_RECENCY_ENABLED"),
        description="R6: independent PRINT-RECENCY halt inference. For a watched/held symbol with a fresh NBBO session, if the trade tape shows no prints for an adaptive window (multiple of the recent median inter-print gap; floor ~30s) while open AND the name was recently active, mark suspected-halt (same downstream flags as the quote path). Fail-closed: no tape / not recently active ⇒ no inference. OFF ⇒ byte-identical.",
    )
    # The no-print window = this multiple of the symbol's recent MEDIAN inter-print gap
    # (adaptive: a name that normally prints every 2s halts far faster than one printing
    # every 20s). Bounded below by the absolute floor so a dense name still needs a real gap.
    chili_momentum_halt_print_gap_multiple: float = Field(
        default=8.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_PRINT_GAP_MULTIPLE"),
        description="R6: the no-print halt window = this multiple of the symbol's recent median inter-print gap (adaptive). Bounded below by chili_momentum_halt_print_gap_floor_seconds. Only consulted when chili_momentum_halt_print_recency_enabled is ON.",
    )
    chili_momentum_halt_print_gap_floor_seconds: float = Field(
        default=30.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_PRINT_GAP_FLOOR_SECONDS"),
        description="R6: absolute floor (seconds) for the no-print halt window — a dense name must still be silent this long before a print-recency halt is inferred. Prevents a one-tick tape blip from false-halting.",
    )
    # "Recently active" = the name printed at least one trade within this lookback BEFORE the
    # silence began (the fail-closed activity requirement: a quiet never-active name must not
    # be inferred as halted — it was never trading). Window sized generously vs the gap.
    chili_momentum_halt_print_recent_active_seconds: float = Field(
        default=300.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_PRINT_RECENT_ACTIVE_SECONDS"),
        description="R6: 'recently active' lookback (seconds) — the name must have printed at least chili_momentum_halt_print_recent_active_min_prints trades within this window (ending at the last print) to be eligible for a print-recency halt inference. Fail-closed: a never-active quiet name is never inferred as halted.",
    )
    chili_momentum_halt_print_recent_active_min_prints: int = Field(
        default=5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_PRINT_RECENT_ACTIVE_MIN_PRINTS"),
        description="R6: minimum prints within the recent-active window required to treat the name as recently active (activity floor). Below it ⇒ no print-recency halt inference (fail-closed).",
    )
    # HOT-tape regime floor: this many simultaneous LULD-scale movers (>=30% day
    # move among the bridge's scanned candidates) flips the catalyst tilt to the
    # no-news read (Ross 2026-06-10: hot days = no-news foreign small caps run;
    # news names fade). Normal days (0-1 big movers) keep the news boost.
    chili_momentum_hot_tape_min_big_movers: int = Field(
        default=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_TAPE_MIN_BIG_MOVERS"),
    )
    # ARM-time tape freshness: a candidate without an NBBO tape row this
    # recent is not actually trading in this session (2026-06-12: quiet
    # mid-caps with hours-old quotes consumed slots and sat stale_bbo while
    # the real movers ran). 3 min = 3 missed 1-min sampler beats. 0 disables.
    chili_momentum_arm_tape_freshness_max_sec: float = Field(
        default=180.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ARM_TAPE_FRESHNESS_MAX_SEC"),
    )
    # Crypto stands down while the US equity session is OPEN (premarket ->
    # 16:00 ET close) and resumes automatically after the close (operator
    # directive 2026-06-12, SpaceX morning: crypto arms were consuming live
    # slots during the premarket equity tape). Live arming only; paper
    # shadows unaffected.
    chili_momentum_crypto_pause_during_us_session: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CRYPTO_PAUSE_DURING_US_SESSION"),
    )
    # ACTIVE EVENT THEME keywords (comma-separated; empty = none). Names whose fresh
    # headlines match keep their catalyst boost even in a HOT tape (only generic
    # news is neutralized) — e.g. "space,satellite,rocket,orbit,launch,aerospace,
    # spacex" for the SpaceX IPO window (June 10-13 2026). Operator-set per event.
    chili_momentum_event_theme_keywords: str = Field(
        default="",
        validation_alias=AliasChoices("CHILI_MOMENTUM_EVENT_THEME_KEYWORDS"),
    )
    # AUTO HOT-THEME detector: the tape rotates themes faster than the operator can re-type
    # the event-theme list (FDA-day biotech cluster, crypto-treasury PR run, tariff sympathy
    # wave). When ON, theme_catalyst_symbols UNIONS the operator-set keywords with the
    # catalyst keywords recurring across the recent BIG movers (read off the SAME deployed
    # strong/weak keyword machinery + the gainers feed), so a NEW name carrying a hot-theme
    # keyword inherits the theme boost without operator action. The operator-set keywords are
    # ALWAYS honored (auto only ADDS). Selection tilt only; fail-open (no movers/feed => no
    # auto theme). KILL-SWITCH: False -> operator-set-only, byte-identical. (catalyst.py)
    chili_momentum_auto_theme_detection_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_THEME_DETECTION_ENABLED"),
        description="Auto-detect HOT catalyst themes from recent big movers and union them into the event-theme boost. Operator-set keywords always honored; selection tilt only; fail-open. KILL-SWITCH: False -> operator-set-only, byte-identical.",
    )
    # AUTO HOT-THEME documented bases (override only to tune; defaults match catalyst.py).
    # Recurrence: a keyword in >= this many recent big movers is a real theme (2 = leader +
    # >=1 peer; one hot name is noise). Window: rolling freshness (minutes) for the movers +
    # their headlines (defaults to the news-catalyst freshness window; own knob so a theme can
    # run wider than a single fill). Big-mover floor: the explosive %% gain a mover must clear
    # to vote (defaults to the HOT-tape LULD-scale move pct — the floor the lane already trusts).
    chili_momentum_auto_theme_recurrence_min: int = Field(
        default=2,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_THEME_RECURRENCE_MIN"),
        description="Auto hot-theme: a catalyst keyword must recur across >= this many recent big movers to count as HOT (2 = leader + >=1 peer).",
    )
    chili_momentum_auto_theme_window_min: int = Field(
        default=120,
        ge=15,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_THEME_WINDOW_MIN"),
        description="Auto hot-theme: rolling freshness window (minutes) for the recent big movers + their headlines. Default 120 = the news-catalyst freshness window.",
    )
    chili_momentum_auto_theme_big_mover_pct: float = Field(
        default=30.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_THEME_BIG_MOVER_PCT"),
        description="Auto hot-theme: a mover must clear this %% day-gain to vote for a hot theme. Default 30 = the HOT-tape LULD-scale 'big move' floor.",
    )
    # ── FIX E: PER-TICKER catalyst-news pass for the IN-PLAY movers ─────────────────────
    # Root cause (decisive w0av0u3qy probe in the LIVE container): 0/11 Ross names were
    # catalyst/theme-tagged because the catalyst accessors read the GLOBAL Polygon news
    # firehose (/v2/reference/news sorted desc, limit ~200) — on a busy tape the low-float
    # micro-caps Ross trades get BURIED under large-cap news and never appear, even when
    # Polygon DOES carry a fresh headline for them (verified per-ticker: ILLR/NXTT/NVCT/SDOT/
    # SKYQ each have news the firehose omitted; FISN genuinely has none). This adds a PER-
    # TICKER news pass for the names the lane is ACTUALLY arming and UNIONS the fresh hits into
    # the catalyst set, so a real catalyst on a low-float actually tags. Selection TILT only
    # (rides the existing catalyst path); never a gate, never a penalty; freshness still
    # enforced (a stale headline never tags — that residual provider lag is the honest feed
    # constraint, NOT a code bug). Bounded per pass. KILL-SWITCH: False -> firehose-only,
    # byte-identical. Default ON per operator style; the catalyst tilt is additive so a
    # mis-tag can only mildly boost rank, never bypass a capital-protection gate.
    chili_momentum_catalyst_tagging_repair_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_TAGGING_REPAIR_ENABLED"),
        description="FIX E: query Polygon news PER IN-PLAY-MOVER ticker (not just the global firehose) and UNION the fresh hits into the catalyst/strong/weak/fake/theme sets, so a low-float Ross mover with a real catalyst actually gets tagged (the firehose buries micro-caps under large-cap news). Freshness still enforced. Selection tilt only; fail-open. KILL-SWITCH: False -> firehose-only, byte-identical.",
    )
    chili_momentum_catalyst_repair_max_tickers: int = Field(
        default=40,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_REPAIR_MAX_TICKERS"),
        description="FIX E: max in-play mover tickers to run the per-ticker catalyst-news pass on per viability pass (one HTTP call each; the lane's in-play set is small). The one documented bound.",
    )
    # E7: THEME / SYMPATHY detector (the 1000%-mover lever). When a LEADER squeezes on a
    # catalyst, same-THEME names run too (STI->ASTC). Complements the SIC-sector sympathy
    # tilt with a SHARED-CATALYST-KEYWORD axis: cluster the batch's movers by a salient
    # keyword shared across their fresh headlines; if the cluster has a genuine leader
    # (top gainer clears the floor) and >= min_cluster members, the NON-leader peers get
    # a SMALL additive viability boost. Soft + additive (never a gate, never a penalty);
    # equity-only; fail-open on thin news. KILL-SWITCH: False -> byte-identical.
    # (theme_detector.py)
    chili_momentum_theme_sympathy_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THEME_SYMPATHY_ENABLED"),
        description="E7: boost same-theme (shared-catalyst-keyword) sympathy peers of a hot leader. Soft additive tilt, equity-only, fail-open. KILL-SWITCH: False -> byte-identical.",
    )
    # E7 documented bases (override only to tune; defaults match theme_detector.py).
    chili_momentum_theme_leader_floor_pct: float = Field(
        default=15.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THEME_LEADER_FLOOR_PCT"),
        description="E7: the top mover in a keyword theme cluster must clear this %% to count as a genuine squeeze leader.",
    )
    chili_momentum_theme_min_cluster: int = Field(
        default=2,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THEME_MIN_CLUSTER"),
        description="E7: minimum movers sharing a catalyst keyword (leader + peers) to count as a real theme.",
    )
    chili_momentum_theme_sympathy_boost: float = Field(
        default=0.05,
        ge=0.0,
        le=0.20,
        validation_alias=AliasChoices("CHILI_MOMENTUM_THEME_SYMPATHY_BOOST"),
        description="E7: additive viability boost for a theme sympathy peer (small — a secondary corroborator).",
    )
    # Ross "Running Up" feeder (the 2026-06-11 SKYQ gap): the viability batch ranks
    # DAY-change movers, so a name bursting NOW from a flat day never refreshes and
    # can never arm. The NBBO tape already samples Ross-universe names every minute —
    # lift symbols whose mid rose >= min_pct over the lookback into the refresh
    # batch (bounded by max_symbols; every downstream gate still applies).
    chili_momentum_running_up_lookback_min: float = Field(
        default=5.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUNNING_UP_LOOKBACK_MIN"),
    )
    chili_momentum_running_up_min_pct: float = Field(
        default=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUNNING_UP_MIN_PCT"),
    )
    chili_momentum_running_up_max_symbols: int = Field(
        default=6,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUNNING_UP_MAX_SYMBOLS"),
    )
    # SHAKE-OUT churn guards (tick-speed entries re-trigger in seconds): a symbol
    # with this many losing live trades today is done for the DAY (Ross's 2-strike
    # walk-away), and any losing trade sits the symbol out for the cooldown below.
    chili_momentum_symbol_max_daily_stopouts: int = Field(
        default=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SYMBOL_MAX_DAILY_STOPOUTS"),
    )
    chili_momentum_symbol_loss_cooldown_min: float = Field(
        default=5.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SYMBOL_LOSS_COOLDOWN_MIN"),
    )
    # ADAPTIVE POST-LOSS COOLDOWN (2026-06-16, Ross-discipline / the CCTG re-entry):
    # CCTG took a −159bps scratch then re-armed 11min later into a −892bps bailout —
    # inside neither the FIXED 5-min cooldown nor the 2-strike block. A hard bailout
    # should sit a name out FAR longer than a small scratch. The cooldown grows with
    # the loss the tape actually delivered (return_bps, already persisted) — derived
    # from data, NOT a scattered magic number. EQUITY-only (crypto keeps its fixed
    # base + reap_cooldown). The existing _loss_cooldown_min stays THE documented base.
    chili_momentum_loss_cooldown_adaptive_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LOSS_COOLDOWN_ADAPTIVE_ENABLED"),
        description="Kill-switch: False => byte-identical fixed-base post-loss cooldown.",
    )
    chili_momentum_loss_cooldown_bps_per_min: float = Field(
        default=500.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LOSS_COOLDOWN_BPS_PER_MIN"),
        description="THE adaptive base knob: minutes added per this many bps of realized loss, on top of the fixed base. <=0 => no scaling (fixed base).",
    )
    chili_momentum_loss_cooldown_max_base_mult: float = Field(
        default=4.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LOSS_COOLDOWN_MAX_BASE_MULT"),
        description="Irreducible SAFETY clamp (not a tuning surface): max cooldown = this x base, so a data glitch can never freeze a name for hours.",
    )
    # A CONFIGURED broker disconnected this long raises a loud ops alarm (websocket
    # broadcast + critical log). The RH refresh token died silently for ~7 weeks
    # (2026-04-19 -> 06-10) with only info-level log spam — never again.
    chili_broker_disconnect_alarm_minutes: float = Field(
        default=15.0,
        validation_alias=AliasChoices("CHILI_BROKER_DISCONNECT_ALARM_MINUTES"),
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
    chili_mesh_critical_alert_cooldown_seconds: int = Field(
        default=15 * 60,
        ge=0,
        le=86_400,
        validation_alias=AliasChoices("CHILI_MESH_CRITICAL_ALERT_COOLDOWN_SECONDS"),
    )
    mesh_daily_llm_cap: int = Field(
        default=50,
        ge=0,
        le=500,
        validation_alias=AliasChoices("MESH_DAILY_LLM_CAP", "CHILI_MESH_DAILY_LLM_CAP"),
    )
    mesh_teacher_queue_pressure_block_fraction: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "MESH_TEACHER_QUEUE_PRESSURE_BLOCK_FRACTION",
            "CHILI_MESH_TEACHER_QUEUE_PRESSURE_BLOCK_FRACTION",
        ),
    )

    # Robinhood spot venue adapter (execution layer; equities via robin_stocks).
    chili_robinhood_spot_adapter_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_SPOT_ADAPTER_ENABLED"),
    )
    # Robinhood Agentic Trading MCP rail — officially-sanctioned execution endpoint
    # (isolated Agentic account). See docs/DESIGN/ROBINHOOD_AGENTIC_MCP.md.
    # Activation switch is token-presence (a real dependency, not a default-OFF dark flag);
    # the bearer token comes from CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN or the token file below.
    chili_robinhood_agentic_mcp_endpoint: str = Field(
        default="",  # empty -> client default (https://agent.robinhood.com/mcp/trading)
        validation_alias=AliasChoices("CHILI_ROBINHOOD_AGENTIC_MCP_ENDPOINT"),
    )
    chili_robinhood_agentic_mcp_token_file: str = Field(
        default="",
        validation_alias=AliasChoices("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN_FILE"),
    )
    chili_robinhood_agentic_mcp_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_AGENTIC_MCP_TIMEOUT_SECONDS"),
    )
    # Optional JSON map of capability -> real MCP tool name, set after introspection
    # (e.g. '{"place_order":"submit_equity_order"}'). Empty -> capability keyword matching.
    chili_robinhood_agentic_mcp_tool_map: str = Field(
        default="",
        validation_alias=AliasChoices("CHILI_ROBINHOOD_AGENTIC_MCP_TOOL_MAP"),
    )
    # The dedicated isolated Agentic account number every order is PINNED to. Empty ->
    # the adapter raises no_agentic_account rather than ever using the brain's account
    # (structurally impossible to hit the main portfolio). Set to 674153143 to activate.
    chili_robinhood_agentic_mcp_account_number: str = Field(
        default="",
        validation_alias=AliasChoices("CHILI_ROBINHOOD_AGENTIC_MCP_ACCOUNT_NUMBER"),
    )
    # When True (default), every order previews via review_equity_order first and
    # aborts on a HARD pre-trade alert (conservative: soft alerts pass, fail-open).
    chili_robinhood_agentic_mcp_review_before_place: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_AGENTIC_MCP_REVIEW_BEFORE_PLACE"),
    )
    # STEP-D #14: a frequent scheduler job calls the RH Agentic rail's is_enabled() (which
    # runs ensure_authable + refreshes the token cache) so the auth cache never goes COLD at
    # the open — the RH-dark-at-open flap class (dark 74 min over today's best window). OFF
    # => no keep-warm probe (the cache warms lazily on first entry, the old behavior).
    chili_robinhood_agentic_probe_keepwarm_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ROBINHOOD_AGENTIC_PROBE_KEEPWARM_ENABLED"),
        description="Keep the RH Agentic rail's auth cache warm via a lightweight periodic is_enabled()/auth probe wired into a frequent scheduler job, so the cache never goes cold at the open.",
    )
    # Which rail equities route to: "robinhood_spot" (default, unofficial robin_stocks) or
    # "robinhood_agentic_mcp" (sanctioned rail; trades the isolated Agentic account). A
    # conscious account-routing choice — only takes effect when a token is also present.
    chili_equity_execution_rail: str = Field(
        default="robinhood_spot",
        validation_alias=AliasChoices("CHILI_EQUITY_EXECUTION_RAIL"),
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
    # WS WATCHDOG (2026-06-16): the Coinbase SDK silently drops the L2 feed on a socket
    # flap (on_close never fires) — it stayed dead 65min until a manual restart. The
    # drain job polls watchdog_check() to detect staleness + force a clean reconnect.
    chili_coinbase_ws_watchdog_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_COINBASE_WS_WATCHDOG_ENABLED"),
        description="Kill-switch: False => no auto-reconnect (manual restart on a dead feed).",
    )
    chili_coinbase_ws_watchdog_stale_s: float = Field(
        default=45.0,
        ge=5.0,
        validation_alias=AliasChoices("CHILI_COINBASE_WS_WATCHDOG_STALE_S"),
        description="Force a WS reconnect when no l2 message has arrived for this many seconds.",
    )
    chili_coinbase_ws_watchdog_min_reconnect_interval_s: float = Field(
        default=30.0,
        ge=5.0,
        validation_alias=AliasChoices("CHILI_COINBASE_WS_WATCHDOG_MIN_RECONNECT_INTERVAL_S"),
        description="Rate-limit: minimum seconds between watchdog force-reconnects (anti-storm).",
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
        default=10,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_CONCURRENT_SESSIONS"),
    )
    chili_momentum_risk_max_concurrent_live_sessions: int = Field(
        default=5,
        ge=1,
        le=20,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_CONCURRENT_LIVE_SESSIONS", "CHILI_MOMENTUM_RISK_MAX_CONCURRENT_LIVE_SESSIONS"),
    )
    chili_momentum_risk_max_concurrent_positions: int = Field(
        default=5,
        ge=1,
        le=50,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_CONCURRENT_POSITIONS"),
    )
    # ── decouple_watching (concurrency conversion lever) ────────────────────
    # MASTER KILL-SWITCH. false = legacy single live-session cap (byte-identical
    # to today: watchers + holders share one risk-budget cap, so the lane watches
    # only ~5-15 names). true = watchers governed by the watch-FANOUT cap (zero
    # risk), the risk-budget cap charges only OPEN POSITIONS.
    # CHUNK 2 (engine core, 2026-06-28): flipped DEFAULT True now that the atomic
    # shape-aware fill-cap + fill-burst test land below — a flat/stuck broker-zero
    # session holds ZERO aggregate_open_risk_usd so it can no longer block a real
    # entry. Set False to revert to the legacy count-based single-cap path
    # (byte-identical). docs/DESIGN/MOMENTUM_ENGINE.md §2 / Phase 4.
    chili_momentum_decouple_watching_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DECOUPLE_WATCHING_ENABLED"),
    )
    # ── ATOMIC SHAPE-AWARE RISK-BUDGET ADMISSION (CHUNK 2 engine core) ───────
    # MASTER for the new admission governor. true (default) = the advisory-locked
    # fill boundary in live_runner ADMITS iff (aggregate_open_risk_usd + this
    # candidate's ACTUAL (entry-stop)*fill_qty) <= the equity-relative aggregate
    # budget — a CONTINUOUS dollars-at-risk gate that replaces the slot COUNT as
    # the primary governor (the count, effective_position_cap, stays as a misconfig
    # BACKSTOP). The budget FRACTION REUSES chili_momentum_max_aggregate_risk_pct_of_
    # equity (no new magic number). Shape-aware: 10 tight-stop scalps admit where 10
    # wide-stop trades would not, for the same dollar budget. Computed INSIDE the
    # per-(user,lane) advisory lock so a fill-burst cannot pass two against a stale
    # aggregate. false ⇒ the exact legacy count check (byte-identical).
    # docs/DESIGN/MOMENTUM_ENGINE.md §2 / Phase 4.
    chili_momentum_atomic_risk_budget_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ATOMIC_RISK_BUDGET_ENABLED"),
    )
    # ── FILL-BOUNDARY BREAKER RE-CHECK (CHUNK 2 safety completion) ───────────
    # true (default) = re-check the three FINANCIAL breakers (per-broker daily-loss,
    # portfolio drawdown, profit-giveback halt) at the live_runner fill boundary,
    # INSIDE the advisory lock, atomically with the risk-budget admission — closing
    # the gap that default-ON decouple_watching widens: a watcher armed while the day
    # was green can persist across ticks, trigger, and submit an entry AFTER the day
    # breaches (the breakers are only checked in auto_arm's arm-pass guards, not at the
    # fill boundary). On any breach -> BLOCK the entry (do NOT place the order), emit
    # live_entry_blocked_by_breaker, stay WATCHING (retry next tick once the breaker
    # clears). The kill-switch is already re-checked in the runner; this ADDS the three
    # financial breakers beside it. false ⇒ no boundary re-check (byte-identical to the
    # current path). Reuses the SAME governance helpers auto_arm uses (no reimplementation).
    chili_momentum_fill_boundary_breaker_recheck_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FILL_BOUNDARY_BREAKER_RECHECK_ENABLED"),
    )
    # Max simultaneous WATCHERS (pre-fill, $0 risk) when decoupled. REST-safe
    # ceiling 20 without the WS-quote re-route; default 15 = today's runner-list
    # limit. This is the FALLBACK / hard upper bound when the adaptive fanout
    # (below) is disabled or the field/equity basis is unavailable.
    chili_momentum_watch_fanout_max: int = Field(
        default=15,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WATCH_FANOUT_MAX"),
    )
    # ── ADAPTIVE watch-fanout (CHUNK 2) ─────────────────────────────────────
    # true (default) = derive the watcher cap from the live field (how many names
    # are actually igniting) rather than a flat 15 — a machine's edge is BREADTH,
    # so when 40 names are moving the lane should watch more than when 3 are.
    # The cap = clamp(live_eligible_field_size, base_floor, watch_fanout_max):
    # it floats UP with the field but never past watch_fanout_max (the documented
    # per-tick processing-cost ceiling) nor below the floor below. Watchers are
    # FREE ($0 risk); only the atomic risk-budget governs real admission. false ⇒
    # the flat chili_momentum_watch_fanout_max (byte-identical).
    chili_momentum_watch_fanout_adaptive_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WATCH_FANOUT_ADAPTIVE_ENABLED"),
    )
    # The single documented FLOOR for the adaptive watch-fanout: never watch fewer
    # than this many names even on a quiet field (so the lane is always primed for
    # the first igniter). The cap floats between this floor and watch_fanout_max.
    chili_momentum_watch_fanout_floor: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WATCH_FANOUT_FLOOR"),
    )
    # ── CHUNK 3 — S4 FAST EXECUTOR + RAIL GOVERNOR ──────────────────────────
    # Turn engine ADMISSIONS into fills in a few RTTs (not the 2–15s tick-coupled
    # latency) AND bound the rail rate so multi-admission cannot flood / 429 the
    # broker (the execution-flooding risk Chunk 2 introduced by removing the slot
    # count). Three default-ON, kill-switched levers. Flag-OFF for ALL THREE ⇒
    # byte-identical to the deployed order path. docs/DESIGN/MOMENTUM_ENGINE.md §3.
    #
    # A. INLINE MICRO-REPEG. true (default) = run the bounded entry repegs WITHIN
    # the same tick (re-reading the live ask each iter) instead of one repeg per
    # external WS tick. EVERY existing bound is preserved: the cumulative-spread
    # ceiling (_entry_repeg_price), the risk-first re-size on each repeg, the
    # max-repeg counter (chili_momentum_entry_max_repegs), the equity/fresh-quote
    # gate. "3 repegs over 6–45s" -> "3 repegs over ~3 RTTs." false ⇒ the current
    # one-repeg-per-tick behavior (byte-identical).
    chili_momentum_entry_inline_repeg_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_INLINE_REPEG_ENABLED"),
    )
    # Inline-repeg inter-iteration delay is ADAPTIVE (no magic clock): it waits the
    # smaller of (the measured rail RTT) and (a fraction of the name's expected per-bar
    # move time). This is the upper BOUND on that adaptive wait — a single documented
    # ceiling so a stuck RTT measurement can never spin the inline loop tightly.
    chili_momentum_entry_inline_repeg_max_delay_s: float = Field(
        default=0.75,
        ge=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_INLINE_REPEG_MAX_DELAY_S"),
    )
    # B. FAST ACK-POLL. true (default) = after submit, poll get_order to confirm the
    # fill WITHOUT waiting for the next external tick — interval = the measured RTT
    # widening geometrically, total window bounded by the EXISTING rest-bars * interval
    # backstop (chili_momentum_entry_max_rest_bars). On confirm, adopt immediately via
    # the SAME adopt path. false ⇒ the current tick-coupled confirm (byte-identical).
    chili_momentum_entry_fast_poll_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FAST_POLL_ENABLED"),
    )
    # Fast-poll seed interval (s) when no RTT has been measured yet, and the geometric
    # widening factor. The TOTAL poll window is bounded by rest_bars * entry_interval
    # (already configured) — these only shape the within-window cadence. One documented
    # conservative seed; the cadence then rides the measured RTT.
    chili_momentum_entry_fast_poll_seed_interval_s: float = Field(
        default=0.25,
        ge=0.01,
        le=5.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FAST_POLL_SEED_INTERVAL_S"),
    )
    chili_momentum_entry_fast_poll_widen_factor: float = Field(
        default=1.6,
        ge=1.0,
        le=4.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FAST_POLL_WIDEN_FACTOR"),
    )
    # Hard cap on fast-poll iterations within a tick (belt-and-suspenders bound beside
    # the wall-clock window) so a misconfigured RTT can never busy-poll the rail.
    chili_momentum_entry_fast_poll_max_iters: int = Field(
        default=12,
        ge=1,
        le=200,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FAST_POLL_MAX_ITERS"),
    )
    # IDLE-IN-TRANSACTION GUARD: hard ceiling (seconds) on the TOTAL in-tick fast-poll
    # wall-clock. tick_live_session holds a SELECT ... FOR UPDATE row lock for the whole
    # call, so any in-tick sleep pins a DB connection in an open transaction; this bounds
    # worst-case lock-hold to single-digit seconds (the geometric widen to max_iters could
    # otherwise sleep ~100s+ at a 5m interval). The fast-poll is a latency optimizer — an
    # unfilled order still falls through to the event-driven cancel/repeg path next tick.
    chili_momentum_entry_fast_poll_max_wall_s: float = Field(
        default=5.0,
        ge=0.0,
        le=30.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FAST_POLL_MAX_WALL_S"),
    )
    # C. THE ADAPTIVE RAIL RATE GOVERNOR (the load-bearing new safety component). true
    # (default) = a process-local token bucket shared by ALL lane rail calls (order
    # PLACES and get_order POLLS — get_order is a LIST endpoint on the SAME budget) so
    # multi-admission cannot flood / 429 the broker. The rate SELF-DISCOVERS: it WIDENS
    # on a run of successes and HALVES on a 429 / rate-limit push-back. When the bucket
    # is empty a call WAITS briefly (max_wait_s) then DEFERS to the next tick (never a
    # silent drop — it logs). false ⇒ no governor in the loop (byte-identical).
    chili_momentum_entry_placement_governor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_PLACEMENT_GOVERNOR_ENABLED"),
    )
    # Governor knobs — the ONE documented CONSERVATIVE starting bound is start_rps
    # (~2 rail calls/s, well under any plausible broker per-account budget); the rate
    # then self-discovers between min_rps and max_rps. burst = bucket capacity. The
    # remaining knobs shape the adaptive widen/halve. No fixed steady-state RPS magic.
    chili_momentum_rail_governor_start_rps: float = Field(
        default=2.0,
        ge=0.01,
        le=100.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RAIL_GOVERNOR_START_RPS"),
    )
    chili_momentum_rail_governor_min_rps: float = Field(
        default=0.25,
        ge=0.001,
        le=100.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RAIL_GOVERNOR_MIN_RPS"),
    )
    chili_momentum_rail_governor_max_rps: float = Field(
        default=20.0,
        ge=0.01,
        le=1000.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RAIL_GOVERNOR_MAX_RPS"),
    )
    chili_momentum_rail_governor_burst: float = Field(
        default=4.0,
        ge=1.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RAIL_GOVERNOR_BURST"),
    )
    chili_momentum_rail_governor_max_wait_s: float = Field(
        default=1.5,
        ge=0.0,
        le=30.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RAIL_GOVERNOR_MAX_WAIT_S"),
    )
    chili_momentum_rail_governor_widen_after_successes: int = Field(
        default=8,
        ge=1,
        le=1000,
        validation_alias=AliasChoices(
            "CHILI_MOMENTUM_RAIL_GOVERNOR_WIDEN_AFTER_SUCCESSES"
        ),
    )
    chili_momentum_rail_governor_widen_factor: float = Field(
        default=1.25,
        ge=1.0,
        le=4.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RAIL_GOVERNOR_WIDEN_FACTOR"),
    )
    chili_momentum_rail_governor_halve_factor: float = Field(
        default=0.5,
        ge=0.001,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RAIL_GOVERNOR_HALVE_FACTOR"),
    )
    # Hard operator backstop on OPEN POSITIONS; adaptive risk-budget N (≤15) binds
    # first (reference numbers are ceilings, not the active value).
    chili_momentum_max_open_positions_ceiling: int = Field(
        default=20,
        ge=1,
        le=50,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_OPEN_POSITIONS_CEILING"),
    )
    # Crypto correlated-dump SUPER-bucket: max simultaneous OPEN crypto (-USD)
    # positions across ALL coins (one BTC-led dump hits everything). NOT per-coin.
    chili_momentum_max_open_positions_per_correlation_bucket: int = Field(
        default=4,
        ge=1,
        le=50,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_OPEN_POSITIONS_PER_CORRELATION_BUCKET"),
    )
    # Crypto pre-entry DOLLAR backstop: cap aggregate open-crypto-risk (entry→stop
    # $) at this fraction of equity (the equity aggregate_open_risk_cap excludes
    # crypto, so this is the crypto lane's only dollar-precise correlation guard).
    chili_momentum_max_aggregate_crypto_risk_pct_of_equity: float = Field(
        default=0.07,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_AGGREGATE_CRYPTO_RISK_PCT_OF_EQUITY"),
    )
    # Age (s) after which a momentum-lane advisory lock held by an idle-in-transaction
    # backend is treated as orphaned (force-killed worker) and reaped by the once-per-
    # batch janitor. Generous vs a normal tick so legitimate slow ticks aren't killed.
    chili_momentum_lane_leak_cleanup_threshold_s: int = Field(
        default=120,
        ge=60,
        le=3600,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LANE_LEAK_CLEANUP_THRESHOLD_S"),
    )
    # Adaptive concurrency: the number of live slots = the simultaneous-open-risk BUDGET
    # RATIO. N = clamp(round(this_fraction / loss_fraction_of_equity), max_concurrent_live_
    # sessions, 15) — i.e. how many per-trade risks fit in the budget. With loss_fraction
    # 0.01, this 0.10 => 10 slots. INDEPENDENT of account size/margin (growth scales per-
    # trade SIZE, not the count — so a 2x buying-power basis does NOT also double the slots).
    # Worst-case simultaneous loss <= this_fraction * basis. A 06-08 sweep showed a fixed 5
    # left ~$2.9k of winners on the table while >=8 captured them. 0 disables (fixed cap).
    chili_momentum_risk_concurrent_open_risk_fraction: float = Field(
        default=0.10,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_CONCURRENT_OPEN_RISK_FRACTION"),
    )
    chili_momentum_risk_max_notional_per_trade_usd: float = Field(
        default=500.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_NOTIONAL_PER_TRADE_USD"),
    )
    # Equity-relative per-trade notional cap: a fraction of ACCOUNT EQUITY (not a
    # fixed $). Frozen at session admission; scales up as equity grows and DOWN in
    # drawdown. The cap above is the fixed-$ FALLBACK when equity is unavailable.
    # This single fraction is the documented per-trade size risk-appetite knob.
    # NOTE: per-trade SIZE is risk-first (qty = max_loss / stop_distance); this is the
    # upper NOTIONAL ceiling on that. 0.15 -> trades are sized by the ~1% equity loss cap,
    # capped at 15% of equity. (A brief 0.03/~$300 experiment was reverted — it shrank
    # positions below the intended risk-first size.)
    chili_momentum_risk_notional_fraction_of_equity: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_NOTIONAL_FRACTION_OF_EQUITY"),
    )
    # Liquidity-ceiling sizing (the scaling enabler): cap per-trade notional at this
    # fraction of the NAME's daily dollar-volume, so the position never exceeds what can
    # be EXITED cleanly (Ross's "can't move 500k shares in 1-2 min"). At a small account
    # the equity notional cap binds (unchanged); as the account COMPOUNDS this binds on
    # thin names so CHILI scales only as far as each name's liquidity allows. ~1% of daily
    # $-vol ~= a few min of exitable volume. 0 disables (fail-open). (SCALING_ENGINE.md)
    chili_momentum_risk_liquidity_participation_fraction: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_LIQUIDITY_PARTICIPATION_FRACTION"),
    )
    # Equity-relative per-trade MAX-LOSS cap: a fraction of ACCOUNT EQUITY (not a
    # fixed $). Frozen at admission; scales with equity. The fixed loss cap is the
    # FALLBACK when equity is unavailable. Single documented per-trade RISK knob.
    chili_momentum_risk_loss_fraction_of_equity: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_LOSS_FRACTION_OF_EQUITY"),
    )
    # Equity-relative DAILY-LOSS circuit-breaker: a fraction of ACCOUNT EQUITY (not
    # a fixed $). Evaluated live so the breaker adapts to current equity. The fixed
    # daily-loss cap is the FALLBACK when equity is unavailable. One documented knob.
    chili_momentum_risk_daily_loss_fraction_of_equity: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_DAILY_LOSS_FRACTION_OF_EQUITY"),
    )
    # SIZING BASIS: use account BUYING POWER (margin-inclusive) rather than just settled
    # cash/equity as the base for the equity-relative caps above, so the lane utilizes
    # available margin. When True (default) the per-venue basis is buying_power (falling
    # back to equity if unavailable); set False to size off settled equity only. NOTE: all
    # the *_fraction_of_equity caps (notional, per-trade loss, daily-loss) then scale off
    # buying power — bigger buying power => bigger size AND bigger risk (margin amplifies
    # both). At a near-cash account buying_power ~= equity so the effect is small.
    chili_momentum_risk_size_use_buying_power: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_SIZE_USE_BUYING_POWER"),
    )
    # Extra MARGIN MULTIPLE on the buying-power sizing basis. The robin_stocks API
    # under-reports the displayed Gold/Reg-T margin buying power — it returns the ~1x base
    # ($11,276) in every buying_power field, while the app shows the 2x margin ($22,551 =
    # 2 * $11,276). This multiple recovers the account's ACTUAL margin buying power. Code
    # default 1.0 (no extra leverage — safe for cash/unknown accounts); the operator's live
    # env sets it per their margin (e.g. 2.0 = 2x Gold margin). WARNING: this multiplies
    # SIZE and RISK across ALL equity-relative caps (notional, per-trade loss, daily-loss).
    chili_momentum_risk_buying_power_margin_multiple: float = Field(
        default=1.0,
        ge=1.0,
        le=4.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_BUYING_POWER_MARGIN_MULTIPLE"),
    )
    # Ross-style PROFIT-GIVEBACK session halt (the upside mirror of the daily-loss
    # breaker). Once today's realized PnL has PEAKED at a meaningful, equity-relative
    # green AND has since given back this FRACTION of that peak, the momentum LIVE lane
    # STOPS arming for the rest of the daily window — Ross's rule to lock in a green day
    # instead of round-tripping it back to flat/red ("I give back 50% of my profits once
    # I reach a certain threshold... easier to remember half than 40%",
    # warriortrading.com/7-day-trading-rules). This giveback fraction is the SINGLE
    # documented knob; the activation threshold is equity-relative (it reuses the
    # equity-relative daily-loss-cap magnitude — no second fixed-$ number: a green day
    # worth protecting is, by symmetry, one that exceeds the day's max tolerable red).
    # 0 disables. docs/DESIGN/MOMENTUM_LANE.md [[feedback_adaptive_no_magic]]
    chili_momentum_profit_giveback_fraction: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PROFIT_GIVEBACK_FRACTION"),
    )
    # Spike guard for the equity-relative per-trade caps above. A frozen per-trade cap
    # may not exceed this MULTIPLE of its rolling median across recent same-venue
    # admissions. A transient bad equity read (e.g. a Coinbase get_portfolio spike)
    # otherwise inflates BOTH per-trade caps at once, releasing the notional ceiling
    # and 4-6x-ing size + risk (FIDA/KAIO oversized trades = ~60% of the halting
    # daily loss, 2026-06-06). The rolling median is the derived center; this multiple
    # is the single documented HEADROOM knob — legitimate equity growth trails the
    # median so only sudden >Nx jumps clamp.
    # docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md
    chili_momentum_risk_cap_max_median_multiple: float = Field(
        default=2.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_CAP_MAX_MEDIAN_MULTIPLE"),
    )
    # Window (count of recent same-venue admitted sessions) whose frozen per-trade caps
    # form the rolling median for the spike guard above. Wide enough that a handful of
    # spiked admissions cannot move the median (median resists outliers). One knob.
    chili_momentum_risk_cap_median_lookback: int = Field(
        default=40,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_CAP_MEDIAN_LOOKBACK"),
    )
    # ALPACA PAPER (2026-07-07): skip the rolling-median per-trade cap spike-guard for the alpaca
    # paper lane. That guard clamps a cap to 2x the recent same-venue median to catch a BAD equity
    # READ inflating size — but the Alpaca paper account read (~$100k eq / ~$400k BP) is authoritative
    # and its recent-cap history is contaminated by the pre-fix wrong-basis era (~$1.9k Coinbase), so
    # the median under-clamps a legit $400k account to ~$1k. Default ON (paper = fake money; the
    # equity-relative cap + BP + max-loss still bound size). Set False to restore the guard.
    chili_momentum_alpaca_skip_cap_median_guard: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ALPACA_SKIP_CAP_MEDIAN_GUARD"),
    )
    # Reward:risk multiple — the TARGET is set this many x the actual stop distance
    # (Ross-style 2:1 floor; the per-instrument/regime learner can raise it). Fixes
    # the old ~1.3-1.5:1 that sat below Ross's strict 2:1. One documented R:R knob.
    chili_momentum_risk_reward_risk_ratio: float = Field(
        default=2.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_REWARD_RISK_RATIO"),
    )
    # Ross asymmetric exit: fraction of the ORIGINAL position sold into the FIRST
    # (2:1) target — Ross "sell 1/2 into strength". The balance becomes the RUNNER:
    # its stop moves to breakeven (entry) and trails up the next structural/ATR level.
    # This ONE documented knob is the only number in the asymmetric exit; breakeven
    # is derived (= entry) and the runner trail is derived (chandelier off the frozen
    # entry ATR x stop_atr_mult). Ross = 1/2 (0.5) on the risk-2:1 rule, up to 0.75 on
    # the micro-pullback; default 0.5 keeps the largest runner (most tail capture).
    # The learner can raise/lower it per family. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_scale_out_fraction: float = Field(
        default=0.5,
        gt=0.0,
        lt=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SCALE_OUT_FRACTION"),
    )
    chili_momentum_mfe_shadow_logging_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MFE_SHADOW_LOGGING_ENABLED"),
        description="SHADOW-LOG the realized Maximum-Favorable-Excursion (MFE_R) + first-target-R per closed momentum trade (event momentum_mfe_realized), keyed by setup family, to accumulate the tape's OWN excursion distribution. This is the DATA the no-magic exit-target calibration needs (a percentile of realized MFE replaces the fixed rr_cap=6 / room_capture=0.5 magic; see exit_calibration.py). Always keep ON (it feeds the live target). OFF ⇒ no emit; the target then can only use whatever distribution already exists.",
    )
    chili_momentum_mfe_target_live_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MFE_TARGET_LIVE_ENABLED"),
        description="APPLY the DATA-DERIVED first-partial target LIVE (default ON — no dark flag): the actual first-scale R:R = a percentile of the setup family's realized-MFE distribution, SHRUNK toward the plan's base R:R until _min_samples accumulate. With 0 samples it IS the base R:R (byte-identical to today's plan floor) and adapts UP per family as MFE accumulates — replacing the fixed rr_cap=6 / room_capture=0.5 magic realized-HOD lift. The round-number pull-in still snaps it to structure. Emits momentum_mfe_target_applied for audit. Kill-switch =0 ⇒ restore the magic adaptive lift (instant rollback). The shrinkage-toward-prior IS the safety net (López de Prado / fractional-shrinkage): it never diverges from the current behavior faster than real data justifies.",
    )
    chili_momentum_mfe_shadow_target_percentile: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MFE_SHADOW_TARGET_PERCENTILE"),
        description="The ONE documented base for the DATA-DERIVED first-partial target: the fraction of the realized-MFE distribution to aim the first scale at (per setup family). 0.6 = target the 60th-pctile favorable run. SHADOW-computed at entry and logged beside the live magic target (momentum_shadow_first_target) — no behavior change until proven. Everything else is the tape's own realized excursion.",
    )
    chili_momentum_mfe_shadow_min_samples: int = Field(
        default=30,
        ge=1,
        le=500,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MFE_SHADOW_MIN_SAMPLES"),
        description="Small-sample shrinkage floor for the data-derived exit target: blend the realized-MFE percentile toward the current magic R:R (a robust PRIOR) with weight min(1, n_samples/this) until this many closed trades accumulate per setup family. Guards against overfitting a thin excursion sample (the operator's 'no brittle magic' + robustness ask).",
    )
    # DESIGN #3 — ADAPTIVE PROFIT TARGETS. A fixed 2:1 first target caps a +400% low-float
    # monster at 2R when its realized intraday room (ATR-to-HOD) is 6-10R, and the fixed 0.5
    # scale-out dumps half the runner too early on a high-vol name. Make BOTH adaptive to the
    # name's OWN realized intraday range (percentile/range-derived, no magic absolute):
    #   * first-target R:R lifts toward the proven HOD room in R-units (room_capture * room_R),
    #     clamped [base_rr, rr_cap]; self-corrects vs a wider vol-floored stop (room_R has risk
    #     in the denominator).
    #   * scale-out fraction tilts SMALLER (bigger runner) when realized vol is high, LARGER
    #     when it compresses; centered at the median so a typical name is byte-identical.
    # Flag-off OR no realized_high OR median vol ⇒ every path returns the base ⇒ byte-identical.
    chili_momentum_adaptive_target_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_TARGET_ENABLED"),
        description="DESIGN #3 master kill-switch (adaptive first-target R:R + vol-aware scale-out). OFF ⇒ adaptive_first_target_reward_risk and the scale-out tilt are byte-identical no-ops.",
    )
    chili_momentum_adaptive_target_room_capture: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_TARGET_ROOM_CAPTURE"),
        description="DESIGN #3 ONE documented base: fraction of the name's realized HOD room (in R) the FIRST target aims at. rr_eff = clamp(max(base_rr, capture*room_R), base_rr, rr_cap). 0.5 = aim at half the proven travel.",
    )
    chili_momentum_adaptive_target_rr_cap: float = Field(
        default=6.0,
        ge=2.0,
        le=20.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_TARGET_RR_CAP"),
        description="DESIGN #3 upper bound on the adaptive first-target R:R so a vertical blow-off can't push the target absurdly far. Clamped up to the base floor internally so it can never sit below base_rr.",
    )
    chili_momentum_adaptive_scale_vol_tilt: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_SCALE_VOL_TILT"),
        description="DESIGN #3 ONE documented base: magnitude of the scale-out vol tilt. frac_eff = base_frac * (1 - tilt*(vol_pctl-0.5)*2). At vol_pctl=1.0 (max vol) the fraction is cut by `tilt`.",
    )
    chili_momentum_adaptive_scale_vol_ref_pct: float = Field(
        default=0.05,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_SCALE_VOL_REF_PCT"),
        description="DESIGN #3 reference intraday range (Ross ~5% small-cap opening range) at which realized vol maps to the median percentile (0.5). Promotes the existing hardcoded 0.05 to ONE named knob.",
    )
    # Shake-out fix: the stop must clear at least this fraction of the live 15m
    # expected-move so it sits OUTSIDE intraday noise (KAIO: 72bps stop / 400bps
    # move got shaken out, then hit target). Risk-first sizing trims qty to keep
    # $risk constant. ONE documented knob; the move itself is the live ATR.
    chili_momentum_risk_stop_vol_floor_mult: float = Field(
        default=0.5,
        ge=0.0,
        le=5.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_STOP_VOL_FLOOR_MULT"),
    )
    # E5: how strongly a news catalyst (earnings) tilts equity viability — Ross's
    # 4th selection pillar. Additive boost for catalyst names; no penalty otherwise.
    chili_momentum_catalyst_viability_tilt: float = Field(
        default=0.10,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_VIABILITY_TILT"),
    )
    # ── Ross course-study P1 edges (E1 backside veto, E3 explosive floor, E2 catalyst
    # grading). Each is ADDITIVE + has its OWN kill-switch (default True per the
    # no-dark-flags MO). Flag-off OR input-absent ⇒ byte-identical to prior behavior. ──
    #
    # E1 — BACKSIDE VETO (Ross gap: front-side vs back-side of a move). CHILI computes
    # the session-anchored front_side_state (#798) but it was UNWIRED in the entry path.
    # When ON and the session frame AFFIRMATIVELY reads backside (below VWAP / faded past
    # the retrace veto / chasing an extended top), VETO the otherwise-valid pullback break.
    # Front-side, unknown, or thin data ⇒ NO change (fail-open). Distinct from the
    # point-in-time MACD/EMA _detect_back_side gate (rollover), which still runs.
    chili_momentum_backside_veto_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BACKSIDE_VETO_ENABLED"),
        description="E1: veto an entry when the SESSION-anchored front_side_state reads backside (post-peak/declining lifecycle). Fail-open on unknown/thin data. KILL-SWITCH: False -> byte-identical. KILLED 2026-06-25, proven net-negative: replay showed this blunt session-level veto over-vetoed valid below-VWAP RECLAIM winners (-$78/7d), so it is killed live (env=0). Code default flipped True->False to match the live kill and remove the branch-divergence hazard (an env-less deploy must NOT silently re-enable a proven-negative veto). The finer-grained below-VWAP lifecycle is instead enforced per-setup via front_side_state inside each entry gate (cup/wedge/bull_flag/ma_vwap/etc.), not by this global blunt veto.",
    )
    # ADAPTIVE FRONT-SIDE STRENGTH (the continuous successor to the killed binary E1 backside
    # veto). Instead of a hard below-VWAP/extended cut, a CONTINUOUS strength score (Kaufman
    # Efficiency-Ratio spine + VWAP-dist / day-range / OFI level+slope / signed-tape) maps to an
    # entry SIZE-TILT multiplier in [size_floor, 1.0] + a soft, non-terminal defer — never a hard
    # veto. A clean first-push (high ER, rising OFI) sizes FULL; a falling-knife sizes DOWN to the
    # floor / soft-defers; the below-VWAP-RECLAIM winner E1 over-vetoed scores HIGH and gets full
    # size. ON by default (no-dark-flags); flag OFF or stale tape ⇒ mult 1.0, defer off ⇒
    # byte-identical. The score is pure; the caller supplies live OFI/tape + own-distribution
    # percentiles, so there is no cross-name magic. size_floor is the ONE documented base.
    chili_momentum_frontside_adaptive_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FRONTSIDE_ADAPTIVE_ENABLED"),
        description="Adaptive front-side strength: map a continuous ER-spine strength score to an entry SIZE-TILT mult [size_floor,1.0] + soft defer (NEVER a hard veto; the successor to the killed binary E1 backside veto). OFF or stale tape ⇒ mult 1.0, defer off ⇒ byte-identical. Default ON.",
    )
    chili_momentum_frontside_size_floor: float = Field(
        default=0.25,
        ge=0.05,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FRONTSIDE_SIZE_FLOOR"),
        description="The minimum entry SIZE-TILT multiplier the weakest admitted front-side still trades (never 0 ⇒ no hard veto; meta-labeling-as-sizing). ONE documented base. Default 0.25, band [0.05,1.0].",
    )
    chili_momentum_frontside_defer_pctile: float = Field(
        default=0.15,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FRONTSIDE_DEFER_PCTILE"),
        description="Own-distribution strength percentile below which the entry SOFT-DEFERS (non-terminal re-poll, bounded) instead of admitting at the floor. Default 0.15 (p15). 0 ⇒ defer disabled. Band [0,0.5].",
    )
    # ── ROSS RISK GAP 1 — SIZE-DOWN INTO THE 200MA / OVERHEAD RESISTANCE ──────────────
    # Ross cuts share size approaching the daily 200MA from below / into clear overhead.
    # A continuous size-DOWN multiplier in [floor, 1.0] keyed on the signed daily-ATR
    # distance to the 200MA (and the nearest overhead resistance): full size with lots of
    # room, ramping DOWN (smoothstep over an ATR band) as price approaches the wall from
    # below. SIZE-DOWN ONLY (never sizes up). Composes as one more bounded _safe_mult factor
    # in the runner's _eff_max_loss product. OFF / missing distance ⇒ mult 1.0 (byte-identical,
    # fail-open). docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_daily_room_size_down_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAILY_ROOM_SIZE_DOWN_ENABLED"),
        description="GAP 1: continuous SIZE-DOWN as price approaches the daily 200MA / overhead resistance from below (signed daily-ATR distance). Risk-reducing only; OFF / missing distance ⇒ mult 1.0 (byte-identical). Default ON.",
    )
    chili_momentum_daily_room_band_atr: float = Field(
        default=2.0,
        gt=0.0,
        le=20.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAILY_ROOM_BAND_ATR"),
        description="GAP 1: the daily-ATR distance band over which the 200MA/resistance size-down ramps (smoothstep). At >= band ATR of room ⇒ full size; AT the wall ⇒ floor. ONE documented base. Default 2.0 daily-ATR.",
    )
    chili_momentum_daily_room_size_floor: float = Field(
        default=0.4,
        ge=0.05,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DAILY_ROOM_SIZE_FLOOR"),
        description="GAP 1: minimum size multiplier right at / into the 200MA-or-resistance wall (never zeros the order ⇒ no hard veto). ONE documented base. Default 0.4, band [0.05,1.0].",
    )
    # ── ROSS RISK GAP 2 — RED-INTRADAY SIZE-DOWN (cushion ladder, down side) ──────────
    # Ross trades SMALLER when down on the day. The cushion ladder sizes UP after green but
    # never DOWN when red intraday. A continuous size-DOWN multiplier in [floor, 1.0] keyed
    # on the day's REALIZED P&L (deeper red ⇒ smaller), measured as a fraction of the day's
    # risk budget (units of the per-trade loss cap) so it is self-relative/adaptive (no fixed
    # $). SIZE-DOWN ONLY; green/flat ⇒ 1.0. OFF ⇒ byte-identical. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_red_intraday_size_down_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RED_INTRADAY_SIZE_DOWN_ENABLED"),
        description="GAP 2: continuous SIZE-DOWN when down on the day (red intraday realized P&L); deeper red ⇒ smaller, floored. Risk-reducing only; green/flat / OFF ⇒ mult 1.0 (byte-identical). Default ON.",
    )
    chili_momentum_red_intraday_full_down_units: float = Field(
        default=2.0,
        gt=0.0,
        le=20.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RED_INTRADAY_FULL_DOWN_UNITS"),
        description="GAP 2: how many units of the per-trade loss budget of intraday RED reaches the size floor (linear ramp). Down ~2x the per-trade risk ⇒ floor. ONE documented base. Default 2.0.",
    )
    chili_momentum_red_intraday_size_floor: float = Field(
        default=0.4,
        ge=0.05,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RED_INTRADAY_SIZE_FLOOR"),
        description="GAP 2: minimum size multiplier when deep red intraday (never zeros the order). ONE documented base. Default 0.4, band [0.05,1.0].",
    )
    # ── COMBINED SIZE-DOWN FLOOR for a genuine front-side A-setup ─────────────────────
    # The _eff_max_loss multiplier product (live_runner) has a combined CEILING (base x 3.0)
    # but NO combined FLOOR — unbounded MULTIPLICATIVE STACKING of the ~23 size-DOWN
    # multipliers (e.g. daily_room 0.40 x midday-sched 0.50 = 0.20) can crush a REAL A-setup's
    # per-trade risk far below the equity-relative base (CUPR: $122 base -> $24, ~0.18% risk
    # instead of 1%). This FLOOR RAISES a stacked-down budget back toward the base ONLY for a
    # confirmed front-side A-setup (above-VWAP + forward OFI + viability cleared its family
    # floor). It can ONLY RAISE toward base, NEVER above it (it multiplies base by the floor
    # fraction <= 1.0 and only when the realized aggregate is BELOW the floor) — so the floor
    # is equity-relative by construction and dollar-risk stays risk-FIRST and <= base (1%
    # equity). Fail-CLOSED: a non-A-setup keeps today's stacked size-down. The combined x3.0
    # ceiling, the #769 max-loss circuit, the structural/vol-floored stop, the notional ceiling,
    # and the per-broker daily-loss cap are ALL untouched. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_combined_size_floor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_COMBINED_SIZE_FLOOR_ENABLED"),
        description="Combined SIZE-DOWN FLOOR: for a genuine front-side A-setup (above-VWAP + forward OFI + viability cleared its family floor), RAISE a stacked-down per-trade budget back toward the equity-relative base so multiplicatively stacked size-down multipliers can't crush a real A-setup's risk far below base. Risk-FIRST: can ONLY raise toward base, NEVER above it. Fail-CLOSED (non-A-setup keeps stacked size-down). KILL-SWITCH: False -> byte-identical. Default ON.",
    )
    chili_momentum_combined_size_down_floor: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_COMBINED_SIZE_DOWN_FLOOR"),
        description="Combined size-down FLOOR fraction: a genuine front-side A-setup never sizes below this fraction of the equity-relative base per-trade risk (it FLOORS the realized aggregate of all size-down multipliers). A documented FLOOR (not a ceiling), equity-relative by construction since it multiplies base_max_loss. ONE documented base. Default 0.5 (never below half the equity-relative base), band [0.0,1.0]. Superseded by the conviction-derived floor when chili_momentum_asetup_conviction_size_enabled is ON (this becomes the OFF fallback).",
    )
    chili_momentum_asetup_conviction_size_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ASETUP_CONVICTION_SIZE_ENABLED"),
        description="NO-MAGIC A-setup sizing (replaces the fixed 0.5 combined-size-down floor): the floor = the setup's CONVICTION = clamp((viability - A_setup_floor) / (1 - A_setup_floor), 0, 1) — how far the viability sits ABOVE the A-setup floor toward its max. A TOP-conviction A-setup (viability -> 1.0) gets ~FULL base risk, so the risk-first qty binds on the pre-existing 15%-of-equity notional cap — i.e. a high-quality trade sizes to ~15% of cash (the operator's stated risk-appetite), while a marginal A-setup keeps a smaller floor. NO new magic number (the A-setup gate, the [0,1] viability, and the 15% notional cap all pre-exist). LIFT-ONLY (the _combined_mult<floor gate raises, never cuts) ⇒ monotonic vs the stacked size-down, never a regression on marginal setups. Kill-switch =0 ⇒ the fixed chili_momentum_combined_size_down_floor (instant rollback).",
    )
    chili_momentum_conviction_frontside_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CONVICTION_FRONTSIDE_GATE_ENABLED"),
        description="Gate the A-setup CONVICTION size-up by the ENTRY's front-side strength (frontside_mult), not just the NAME's viability. Viability scores the NAME (DXF and JEM both ~0.90); frontside_mult scores THIS ENTRY (extension/OFI/tape/Kaufman-ER). Without this gate the viability lift OVERRIDES the front-side size-down that already correctly shrank a weak/extended top-buy — measured DXF/CLRO 06-30/07-02: frontside_mult 0.36-0.75 (correctly weak) sized down to combined_mult 0.03-0.21, but the 0.80 viability lift blew them back up 3-23x into a fade. The gate multiplies the conviction floor by frontside_mult so the size-up needs BOTH a high-quality NAME and a high-quality ENTRY (edge = name x entry): JEM (frontside_mult ~1.0) is unchanged; DXF (x0.75)/CLRO (x0.36) size down. OFF ⇒ the viability lift stands alone (byte-identical). Requires chili_momentum_asetup_conviction_size_enabled.",
    )
    # ── ROSS RISK GAP 3 — ACCOUNT-WIDE CONSECUTIVE-LOSS ARM HALT (tilt rule) ──────────
    # Ross's tilt rule: 2-3 reds in a row = walk away. The streak dial only de-SIZES (never
    # halts), the count day-blocks are PER-SYMBOL, and the account-wide halts are DOLLAR-based
    # — so N small losses across N tickers trip no halt (death by a thousand papercuts). After
    # N consecutive realized LOSSES across ALL symbols/families (count resets on a win or a new
    # ET day), HALT new ARMING (open positions still manage + exit normally). Risk-reducing,
    # reversible. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_consecutive_loss_halt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CONSECUTIVE_LOSS_HALT_ENABLED"),
        description="GAP 3: HALT new ARMING after N consecutive account-wide realized losses (resets on a win / new ET day). Halts arming only — never blocks exits/management of open positions. OFF ⇒ never halts (byte-identical). Default ON.",
    )
    chili_momentum_consecutive_loss_halt_count: int = Field(
        default=4,
        ge=2,
        le=20,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CONSECUTIVE_LOSS_HALT_COUNT"),
        description="GAP 3: the consecutive account-wide realized-loss count that halts new arming. ONE documented base (Ross's 2-3-red tilt rule, set conservative). Default 4, band [2,20].",
    )
    # E3 — EXPLOSIVE-FLOOR HARD GATE. Selection ranks by within-batch PERCENTILE, so on a
    # dull tape the best-of-a-dull-batch ranks #1 and arms a non-explosive name. Ross's
    # stated floors (RVOL >= ~5x AND day-change >= ~10%) are absolute, not relative. When
    # ON, an EQUITY entry must clear BOTH floors at the entry tick regardless of its rank.
    # Crypto (24h semantics differ) is exempt; missing data ⇒ fail-open (never blocks).
    chili_momentum_explosive_floor_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_FLOOR_ENABLED"),
        description="E3: hard entry gate — an equity name must clear absolute RVOL + day-change floors (on top of percentile rank) to be live-eligible. Crypto/missing-data exempt. KILL-SWITCH: False -> byte-identical.",
    )
    # Documented absolute floors for E3 (Ross's stated minima; FLOORS the system may raise,
    # never ceilings — a 50x-RVOL / +200% name is MORE eligible). One source for each, no
    # scattered magic. Mirror ross_momentum.ROSS_ELIGIBILITY_* defaults so the entry-tick
    # gate and the selection-time filter agree.
    chili_momentum_explosive_floor_rvol: float = Field(
        default=5.0,
        ge=0.0,
        le=100.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_FLOOR_RVOL"),
        description="E3: absolute relative-volume floor (Ross's '5x minimum') an equity must clear at entry to be a live setup.",
    )
    chili_momentum_explosive_floor_change_pct: float = Field(
        default=10.0,
        ge=0.0,
        le=500.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_FLOOR_CHANGE_PCT"),
        description="E3: absolute day-change %% floor (Ross's 'never buy what isn't already moving') an equity must clear at entry to be a live setup.",
    )
    # ── BATCH A: HOD-break / flat-top BREAKOUT entry + setup-selector (Ross gap: CHILI
    # has ZERO breakout entries — every trigger is pullback/dip/reclaim, so a straight-up
    # PARABOLIC HOD runner that never pulls back to the 9-EMA produces NO fills (SHPH +86%
    # armed 8x, 0 entries). Ross buys the HOD break verbatim, SS101 #011: "buying the high
    # of day... get in a couple cents underneath that level to anticipate the break". These
    # detect a CONSOLIDATION/BASE under the HOD (a flag right under the high, NOT a vertical
    # spike) and fire on the break to a new high with volume + tick-thrust confirmation.
    # ANTI-CHASE: the break fires ONLY off a tested base; a backside / rolled-over top
    # (front_side_state / _detect_back_side) or an over-extended vertical (the existing
    # extension veto) is skipped. KILL-SWITCH each: OFF -> dip-only behavior byte-identical.
    chili_momentum_hod_break_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOD_BREAK_ENTRY_ENABLED"),
        description="Batch A: HOD/new-high BREAKOUT entry — detect a consolidation BASE holding a tight range just under the day high, then FIRE on the break to a new HOD with a volume spike + tick-thrust confirmation; stop below the consolidation low. ANTI-CHASE: requires the base (a tested break, never a vertical blow-off), and is vetoed on a backside/rolled-over top or an over-extended (above-9-EMA/VWAP) extension. KILL-SWITCH: False -> the trigger is never tried -> byte-identical dip-only ladder.",
    )
    chili_momentum_flat_top_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLAT_TOP_ENTRY_ENABLED"),
        description="Batch A: FLAT-TOP consolidation breakout — a parameterization of the HOD break requiring 2-3 taps (topping tails) at a FLAT resistance level, then FIRE on the break; stop = consolidation low; whole/half-dollar round-number context from the existing grid. Same anti-chase guards as the HOD break. KILL-SWITCH: False -> the flat-top variant is never tried -> byte-identical.",
    )
    chili_momentum_setup_selector_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SETUP_SELECTOR_ENABLED"),
        description="Batch A: setup-selector — when >=2 triggers (a dip-family fire AND the new breakout fire) are eligible on the SAME bar, choose the one with the best structural reward:risk (via stop_target_prices) instead of first-clears-gates. KILL-SWITCH: False -> the first trigger that fires wins (the legacy ladder order) -> byte-identical.",
    )
    # The ONE documented adaptive knob for the consolidation BASE width: the base is "tight"
    # when its high-to-low range is within this multiple x the instrument's ATR (a calm name
    # keeps a tight base, a volatile small-cap is allowed a proportionally wider one). No
    # fixed cents — derived from ATR. A base wider than this is a sloppy chop, not a flag.
    chili_momentum_hod_base_atr_mult: float = Field(
        default=1.5,
        gt=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOD_BASE_ATR_MULT"),
        description="Batch A: the consolidation BASE is 'tight enough' to be a flag (not chop) when its high-low range <= this x ATR (ATR-relative, no fixed cents). The base must also hold within this fraction of the day-high band. ONE documented base knob for the HOD/flat-top consolidation detection.",
    )
    # Number of recent completed bars that form the consolidation base under the HOD. The
    # ONE documented base for the base-window length (a flag is a handful of bars, not a
    # long grind). Bounded so a degenerate config can't read the whole frame as a base.
    chili_momentum_hod_base_bars: int = Field(
        default=4,
        ge=2,
        le=12,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOD_BASE_BARS"),
        description="Batch A: number of recent completed bars that must form the tight consolidation base just under the HOD before the break fires. ONE documented base-window knob.",
    )
    # ── BATCH D: opening-range breakout (ORB) + red-to-green + micro-pullback-primary ──
    # The remaining entry gaps from the Ross course audit. ORB = break of the first-N-min
    # opening range (a session-time-windowed breakout). RED-TO-GREEN = a name trading below
    # the session open that reclaims the open with a bottoming-tail reversal. MICRO-PRIMARY
    # = the 1-candle shallow-flag micro-pullback as an INITIAL entry (not just a re-load),
    # hot-tape-gated like the wick-reclaim so it cannot over-fire on slow names. Each is
    # independently kill-switched; OFF -> the trigger is never tried -> byte-identical.
    chili_momentum_orb_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORB_ENTRY_ENABLED"),
        description="Batch D: OPENING-RANGE BREAKOUT — define the opening range (high/low of the first N completed bars after the session open) and FIRE on a break above the OR-high with volume confirm; entry = OR-high break, stop = OR-low. Valid ONLY within the first ~30-60 min after the open (a session-time window). No lookahead (the OR is built from COMPLETED bars only; the live tick is the only intrabar use). KILL-SWITCH: False -> the trigger is never tried -> byte-identical.",
    )
    chili_momentum_orb_minutes: int = Field(
        default=5,
        ge=1,
        le=60,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORB_MINUTES"),
        description="Batch D: the ONE documented knob for the opening-range LENGTH (minutes of completed bars after the session open whose high/low define the OR). The bar count is DERIVED from this and the entry bar interval (e.g. 5 min / 1m bars = 5 bars; 5 min / 15s bars = 20 bars). Default 5 (Ross's first-5-min OR).",
    )
    chili_momentum_orb_window_minutes: float = Field(
        default=60.0,
        gt=0.0,
        le=180.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORB_WINDOW_MINUTES"),
        description="Batch D: the ORB is only valid within this many minutes AFTER the session open (past it the rest of the ladder owns the tape). ONE documented session-window knob; default 60 min.",
    )
    chili_momentum_red_to_green_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RED_TO_GREEN_ENTRY_ENABLED"),
        description="Batch D: RED-TO-GREEN — a name trading RED (below the session OPEN level) that RECLAIMS the open with a bottoming-tail/reversal bar + volume; entry = the open-level reclaim, stop = the red (session) low. Reuses the bottoming-tail + dipbuy reversal machinery. KILL-SWITCH: False -> the trigger is never tried -> byte-identical.",
    )
    chili_momentum_bottom_reversal_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BOTTOM_REVERSAL_ENTRY_ENABLED"),
        description="SS101 #019 BOTTOM REVERSAL — a series of N consecutive RED candles on elevated volume, then the FIRST candle to CLOSE GREEN is the counter-trend confirmation; entry = the green-candle close (or the break above its high on live price), stop = the recent red-series low (the structural pivot). Optional doji/bottoming-tail at the low = exhaustion confirmer (recorded, never required). Reuses the backside + L2 anti-chase vetoes + the tick-break contract. DISTINCT from red_to_green (no session-open tie; enters on the green bar itself, not a reclaim), double-bottom (no two-low structure), first-pullback (counter-trend not continuation), deep-reclaim (no EMA req, all day). KILL-SWITCH: False (DEFAULT — new + never-run, ship dark) -> the trigger is never tried -> byte-identical.",
    )
    chili_momentum_bottom_reversal_min_red: int = Field(
        default=2,
        ge=2,
        le=20,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BOTTOM_REVERSAL_MIN_RED"),
        description="SS101 #019: minimum count of CONSECUTIVE RED candles immediately preceding the first green close before a bottom reversal can fire. ONE documented noise-defense base; Ross's floor is 2, 3-5 recommended for noise rejection.",
    )
    chili_momentum_bottom_reversal_volume_spike_multiple: float = Field(
        default=1.5,
        gt=0.0,
        le=20.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BOTTOM_REVERSAL_VOLUME_SPIKE_MULTIPLE"),
        description="SS101 #019: the green confirmation bar's RVOL (volume_ratio) must be at least this multiple for a real reclaim (not a dead-tape green dribble). ONE documented volume-confirm base; default 1.5x.",
    )
    chili_momentum_bottom_reversal_velocity_floor_atr_mult: float = Field(
        default=0.0,
        ge=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BOTTOM_REVERSAL_VELOCITY_FLOOR_ATR_MULT"),
        description="SS101 #019 OPTIONAL 'jackknife' sharp-V refinement: require the red-series flush to be a STEEP V (a violent algo-stop-run that snaps back), not a slow grind down. Reuses the _dip_velocity yardstick: the flush per-bar % drop must be >= this multiple of the name's OWN ATR% (a drop is 'steep' when the per-bar move exceeds ~1 ATR%, so base ~1.0). 0.0 = OFF = byte-identical current behavior (no velocity gate); >0 requires the sharp V. fail-OPEN on missing ATR%/degenerate data.",
    )
    chili_momentum_micro_pullback_primary_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICRO_PULLBACK_PRIMARY_ENABLED"),
        description="Batch D: MICRO-PULLBACK AS PRIMARY — fire the 1-candle shallow micro-pullback flag as an INITIAL entry (not just a post-fill re-load), GATED to HOT/explosive tape (the same _is_hot_tape RVOL/ATR floors as the wick-reclaim) so it does not over-fire on slow names. Reuses micro_pullback_reentry_detect's shelf/dip geometry; entry = the micro-break, stop = the micro-pullback low. KILL-SWITCH: False -> the trigger is never tried -> byte-identical.",
    )
    # ── SS101 #014: MOVING-AVERAGE / VWAP PULLBACK (cooler-market EMA-cascade dip-buy) ──
    # NEW + never-run -> ship DARK (default False); the operator ramps it. The cooler-market
    # grinder case the shallow first-pullback (too-shallow) and the morning-only deep-reclaim
    # cannot address: a DEEPER all-day pull to the 9/20-EMA cascade that grinds sideways (no
    # clean flag/ABCD), bought on the EMA reclaim. Adaptive ATR-relative geometry; one
    # documented base per knob. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_ma_vwap_pullback_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MA_VWAP_PULLBACK_ENABLED"),
        description="SS101 #014: MOVING-AVERAGE / VWAP PULLBACK — after an impulse (3+ green candles) the name pulls back 2+ bars into a SIDEWAYS consolidation grinding along the moving averages (DEEPER than the shallow first-pullback allows; may touch the 9-EMA then the 20-EMA, possibly the VWAP), then FIRES on the reclaim of the 9-EMA (primary support) or, if the 9 broke, the 20-EMA (secondary); entry = the reclaimed EMA level, stop = the pullback RETRACEMENT LOW. The cooler-market grinder dip-buy that other gates fail to form (no clean flag/ABCD). Reuses the anti-chase machinery (extension guard, collapse cap, backside + L2 vetoes, overhead veto via select_best_setup). KILL-SWITCH (ship DARK): False -> the trigger is never tried -> byte-identical.",
    )
    chili_momentum_ma_vwap_impulse_bars: int = Field(
        default=3,
        ge=2,
        le=10,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MA_VWAP_IMPULSE_BARS"),
        description="SS101 #014: the ONE documented base for the INITIAL IMPULSE length — how many consecutive GREEN candles must precede the consolidation for the move to be a real leg up worth buying the dip of. Default 3 (Ross's '3+ green candles').",
    )
    chili_momentum_ma_vwap_consolidation_bars: int = Field(
        default=2,
        ge=2,
        le=10,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MA_VWAP_CONSOLIDATION_BARS"),
        description="SS101 #014: the ONE documented base for the PULLBACK/consolidation length — how many recent bars (up to the forming bar) must grind sideways near/below the moving averages before the reclaim. The pullback retracement low across these bars is the structural stop. Default 2 (Ross's '2+ bars').",
    )
    chili_momentum_ma_vwap_vol_mult: float = Field(
        default=1.5,
        gt=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MA_VWAP_VOL_MULT"),
        description="SS101 #014: the ONE documented base for the ELEVATED-VOLUME floor on the EMA-reclaim bar (conviction of the bounce, not a drift back to the averages) as a multiple of the rolling average volume. Default 1.5x (shares the lane's vwap-reclaim vol-mult yardstick).",
    )
    # ── BATCH C: ABCD (SS101 #013) + double-bottom (swing-pivot scanner) ──────────────
    # Lower hit-rate than the breakout/pullback families (the audit said defer) — built
    # to COMPLETE the playbook, each independently kill-switched. The ATR pivot filter +
    # the per-pattern hold/no-new-low conditions are how CHOP is NOT read as structure.
    chili_momentum_abcd_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ABCD_ENTRY_ENABLED"),
        description="Batch C: ABCD entry (SS101 #013) — from the ATR-filtered swing pivots, A = an impulse-up leg, B = the pullback low after A, C = a SECOND pullback low that HOLDS above the prior structure (no new low below B), then fire on D = the break above the B->C swing high with a volume confirm; entry = the B-high break level, stop = the C-low structural low (shared pullback_high/pullback_low keys). NOISE DEFENSE: the ATR pivot filter + the no-new-low hold + a _collapse_cap depth gate. KILL-SWITCH: False -> the trigger is never tried -> byte-identical.",
    )
    chili_momentum_double_bottom_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DOUBLE_BOTTOM_ENTRY_ENABLED"),
        description="Batch C: double-bottom entry — two swing lows at ~the same support level (within an ATR-derived band), the second printing a bottoming-tail reversal and HOLDING (no new low below the first), then fire on the break above the intervening swing high; entry = the neckline break level, stop = below the double-bottom low (shared pullback_high/pullback_low keys). NOISE DEFENSE: the ATR pivot filter + the ATR-derived equal-lows band + the second-low bottoming tail. KILL-SWITCH: False -> the trigger is never tried -> byte-identical.",
    )
    chili_momentum_inverse_head_shoulders_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_INVERSE_HEAD_SHOULDERS_ENTRY_ENABLED"),
        description="Batch C: inverse (inverted) head-and-shoulders entry (SS101 #017; Ross: 'I do trade the inverted head and shoulders'). From the ATR-filtered swing lows: left-shoulder low -> head (a LOWER low + recovery high) -> right-shoulder (a HIGHER-low HOLD above the head + high), neckline = the MINIMUM of the two shoulder highs. Fire on the break above the neckline with a volume confirm; entry = the neckline break level (pullback_high), stop = the HEAD low = the structural support of the pattern (pullback_low). NOISE DEFENSE: the ATR pivot filter + the head-below-both-shoulders ordering + the right-shoulder-above-head hold + a _collapse_cap depth gate on the shoulder retraces. DISTINCT from double-bottom (three pivots + shoulder-hold + neckline=min(shoulder-highs), not two equal lows). ANTI-CHASE: the four shared breakout chase-guards — NOT-BACKSIDE/NOT-BELOW-VWAP (_detect_back_side + front_side_state, fail-CLOSED on a thin frame), NOT-PARABOLIC (_hod_extension_ok), L2 hidden-seller veto, and TAPE REQUIRED + FAIL-CLOSED (tape_confirms_hold). WAVE-4 R4: default flipped False->True — a proven filler with the hardened 4-chase-guard suite (test_momentum_setup_guard_parity). KILL-SWITCH: False -> the trigger is never tried -> byte-identical.",
    )
    chili_momentum_cup_and_handle_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CUP_AND_HANDLE_ENTRY_ENABLED"),
        description="Batch C: cup-and-handle entry (SS101 #016; Ross: 'formed by a double top that then doesn't totally fail ... buy here for this breakout, using the low of the handle as support'). From the ATR-filtered swing highs: CUP = two swing HIGHS at ~the same level (within the double-bottom ATR band, applied to resistance) within ~15-20 bars (the double-top rim); HANDLE = a SHALLOW pullback (1-3 completed bars) after the second top, capped by the SAME vol-aware shallow tolerance first_pullback uses + the _collapse_cap, holding above the 9-EMA (vol-aware wick tolerance). Fire on the first NEW HIGH above the cup rim with a volume surge; entry = the cup-rim/double-top peak (pullback_high), stop = the HANDLE LOW (pullback_low — 'using the low of the handle as support'). DISTINCT from first_pullback (REQUIRES the double-top rim first, not any impulse), ABCD (2-tops+handle, not a 4-swing C-low-hold coil), double-bottom (two HIGHS at resistance, not two lows at support), deep_reclaim (ALL-DAY + shallow-only, not morning-only deeper), flat_top (rounded double-top + a SEPARATE handle phase, not one repeatedly-tested level). ANTI-CHASE (parity with wedge/hod_break — the gatekeeper's chase-safety bar): structural guards (ATR pivot filter + equal-highs band + shallow handle cap + _collapse_cap + 9-EMA hold) PLUS the four shared breakout chase-guards — NOT-BACKSIDE/NOT-BELOW-VWAP (_detect_back_side + front_side_state, fail-CLOSED on a thin frame), NOT-PARABOLIC (_hod_extension_ok vs the 9-EMA AND VWAP), L2 hidden-seller veto, and TAPE REQUIRED + FAIL-CLOSED (tape_confirms_hold gates BOTH the tick-break and completed-bar fire — depends on chili_momentum_tape_hold_entry_enabled; OFF there => cup never fires). Structural stop = the handle low. WAVE-4 R4: default flipped False->True — a proven filler with the hardened 4-chase-guard suite (test_momentum_cup_and_handle, test_momentum_setup_guard_parity). KILL-SWITCH: False -> the trigger is never tried -> byte-identical.",
    )
    # The ONE documented adaptive knob for the swing-pivot SCANNER WINDOW: a bar is a
    # confirmed swing high/low when it is the local extreme over +/- this many neighbors
    # (so the last `half_window` bars are not yet confirmable pivots). Shared by both
    # Batch-C triggers. Bounded so a degenerate config can't read the whole frame.
    chili_momentum_swing_pivot_half_window: int = Field(
        default=2,
        ge=1,
        le=10,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SWING_PIVOT_HALF_WINDOW"),
        description="Batch C: swing-pivot scanner half-window — a bar is a confirmed swing high/low when it is the local extreme over +/- this many neighbor bars. The ONE documented pivot-window base knob (shared by ABCD + double-bottom).",
    )
    # The ONE documented adaptive knob for the ATR pivot-NOISE filter: a pivot is ignored
    # unless its prominence (vertical move off its flanking opposite extreme within the
    # window) is at least this fraction of ATR. THIS is the guard that stops chop being
    # mistaken for structure — bigger = stricter (only larger swings count as pivots).
    chili_momentum_swing_pivot_atr_noise_frac: float = Field(
        default=0.5,
        ge=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SWING_PIVOT_ATR_NOISE_FRAC"),
        description="Batch C: swing-pivot ATR-noise filter — a pivot is ignored unless its prominence (vertical move off its flanking opposite extreme) is >= this fraction of ATR. THE chop-rejection knob (ATR-relative, no fixed cents); 0 disables the filter. Shared by ABCD + double-bottom.",
    )
    # The ONE documented adaptive knob for the double-bottom EQUAL-LOWS band: the two
    # swing lows count as "the same support" when they are within this multiple x ATR of
    # each other (ATR-derived, no fixed cents). Bigger = looser equal-lows tolerance.
    chili_momentum_double_bottom_band_atr_mult: float = Field(
        default=0.6,
        ge=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DOUBLE_BOTTOM_BAND_ATR_MULT"),
        description="Batch C: double-bottom equal-lows band — the two swing lows are 'at the same support' when within this multiple x ATR of each other (ATR-derived, no fixed cents). ONE documented band knob.",
    )
    # The ONE documented adaptive knob for the cup-and-handle CUP WIDTH: the two tops (the
    # double-top rim) must be within this many bars of each other to read as a single cup
    # (Ross's "~10-20 bar" double-top lookback). Bigger = a wider cup is allowed.
    chili_momentum_cup_and_handle_lookback_bars: int = Field(
        default=20,
        ge=2,
        le=120,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CUP_AND_HANDLE_LOOKBACK_BARS"),
        description="Batch C: cup-and-handle cup-width ceiling — the two swing highs (the double-top rim) must be within this many bars of each other to read as one cup. ONE documented cup-width base knob.",
    )
    # The ONE documented knob for the cup-and-handle HANDLE LENGTH: the handle is the shallow
    # pullback AFTER the second top — at most this many completed bars before the breaking bar
    # (Ross's "1-3 bar" handle). Bigger = a longer handle is tolerated.
    chili_momentum_cup_and_handle_max_handle_bars: int = Field(
        default=3,
        ge=1,
        le=20,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CUP_AND_HANDLE_MAX_HANDLE_BARS"),
        description="Batch C: cup-and-handle handle length — the handle is at most this many completed bars (the shallow pullback after the second top, before the breaking bar). ONE documented handle-length base knob.",
    )
    chili_momentum_cup_handle_anticipatory_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CUP_HANDLE_ANTICIPATORY_ENABLED"),
        description="A6 (clro-p1) cup-and-handle TOPPING-TAIL ANTICIPATORY early-fire variant. When BOTH rim-high bars are topping tails (is_topping_tail), allow an EARLY fire on a live uptick through handle_low x (1 + the SAME min_reclaim_bps base tick_scalp uses) AND the existing volume-surge leg — Ross: 'jumped in a little early to anticipate the breakthrough … these were BOTH topping tails … got in as volume started to pick up.' Stop unchanged (handle low). ALL existing guards (backside/front-side, extension, L2 seller veto, tape-required) stay ahead of the fire. Fail-closed on unreadable rim-bar wick geometry => rim-break only. false => byte-identical to the rim-break-only path.",
    )
    # E2 — CATALYST GRADING + WEAK HARD GATE. weak_catalyst_symbols() (dilution/compliance/
    # legal) existed only as a soft viability de-boost; it never gated the arm queue. Ross
    # DISTRUSTS weak catalysts (fade predictors) and favors STRONG (FDA/M&A/contract). When
    # ON: a weak-catalyst equity is SUPPRESSED at selection (dropped from live eligibility)
    # and a strong-catalyst name is BOOSTED; MEDIUM stays neutral. Absent news feed / crypto
    # ⇒ no-op (fail-open).
    chili_momentum_catalyst_grade_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_GRADE_GATE_ENABLED"),
        description="E2: grade catalysts — suppress weak-catalyst (dilution/compliance/legal) equities from live eligibility and boost strong-catalyst (FDA/M&A/contract) names; medium neutral. Absent feed/crypto -> no-op. KILL-SWITCH: False -> byte-identical.",
    )
    chili_momentum_catalyst_action_grading_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_ACTION_GRADING_ENABLED"),
        description="Ross-batch2 QUCY-vs-ILLR lesson: grade a STRONG catalyst higher/lower by HEADLINE VERB QUALITY + DOLLAR AMOUNT. Completed-action verbs (acquires/signed/definitive agreement/awarded) BOOST; tentative/pursuit verbs (approves pursuit/explores/letter of intent) DE-BOOST (the +24%-fade class); a headline dollar amount ($400M) ADDS a boost scaled adaptively vs market cap (else >= $100M strong / >= $10M moderate). Fail-closed: no verb/dollar signal -> unchanged. KILL-SWITCH: False -> byte-identical.",
    )
    # A9 (Ross CLRO-lesson 2026-07-02): MERGER-class reliability notch. Ross at [04:43]:
    # "merger agreements can work. They don't always." Split a MERGER sub-class out of the
    # completed-action grader + emit the class label on EVERY graded headline NOW
    # (instrumentation first). The per-class reliability MULTIPLIER is ADAPTIVE from our own
    # labeled follow-through history once >= N samples exist, else EXACTLY 1.0 — so behavior
    # is byte-identical to today until the class has enough labeled outcomes to earn a
    # weight. FAIL direction: no history => 1.0. Default-ON; kill-switch
    # CHILI_MOMENTUM_CATALYST_CLASS_RELIABILITY_ENABLED=0.
    chili_momentum_catalyst_class_reliability_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_CLASS_RELIABILITY_ENABLED"),
        description="A9: split a MERGER sub-class out of the completed-action grader + emit the class label on every graded headline. Per-class reliability multiplier is ADAPTIVE from labeled history once >= N samples, else EXACTLY 1.0 (identical until trained). OFF => label still emitted, multiplier always 1.0.",
    )
    # ONE documented base: minimum labeled per-class samples before an adaptive reliability
    # multiplier is trusted (below this the class multiplier is exactly 1.0). A FLOOR, not a
    # magic tuning ceiling.
    chili_momentum_catalyst_class_reliability_min_samples: int = Field(
        default=20,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CATALYST_CLASS_RELIABILITY_MIN_SAMPLES"),
        description="A9: minimum labeled per-class outcomes before the adaptive reliability multiplier departs from 1.0. ONE documented base (floor).",
    )
    # A10 (Ross CLRO-lesson 2026-07-02): OWN-HEADLINE DILUTION-HISTORY MEMORY (WHLR serial-
    # diluter class). Ross at [04:15]: "many secondary offerings, many reverse splits … I've
    # written that one off." No corp-actions vendor exists — but catalyst.weak_catalyst_symbols
    # already flags dilution symbols daily, so we persist OUR OWN observations (mig312,
    # momentum_dilution_history) and a symbol flagged on >= K distinct days in the trailing
    # window (K ADAPTIVE relative to the flag-frequency distribution) earns a DECAYING selection
    # derate — never a hard ban (the fresh reverse-split-squeeze carve-out must still win). No
    # history => no derate. Default-ON; kill-switch CHILI_MOMENTUM_DILUTION_HISTORY_DERATE_ENABLED=0.
    chili_momentum_dilution_history_derate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DILUTION_HISTORY_DERATE_ENABLED"),
        description="A10: persist each day's dilution/weak-flagged symbols (momentum_dilution_history) and apply a DECAYING selection derate to serial diluters (flagged on >= adaptive-K distinct days in the trailing window). Never a hard ban — the fresh reverse-split-squeeze carve-out still wins. No history => no derate. false => no persist read/derate (byte-identical).",
    )
    # ONE documented base for A10: the trailing window (calendar days) over which distinct
    # dilution-flag days are counted. K is ADAPTIVE within this window (relative to the observed
    # flag-frequency distribution); this window is the single irreducible base.
    chili_momentum_dilution_history_window_days: int = Field(
        default=90,
        ge=7,
        le=365,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DILUTION_HISTORY_WINDOW_DAYS"),
        description="A10: trailing window (calendar days) over which distinct dilution-flag DAYS are counted per symbol. The ONE documented base; K (the min distinct-day count that earns a derate) is adaptive within this window.",
    )
    # Entry trigger mode: "hybrid" (Ross pullback-break on 1m/5m, momentum_volume
    # fallback), "pullback_break" (pullback only), or "momentum_volume" (legacy 15m).
    chili_momentum_entry_trigger_mode: str = Field(
        default="hybrid",
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_TRIGGER_MODE"),
    )
    # Timeframe for the Ross pullback-break trigger. DURABLE DEFAULT = "1m" (the scalp clock).
    # WAVE-4 ITEM-0: flipped 5m->1m so a future dropped env pin can't silently regress to the
    # 5m clock — that was THE −$137 bug (2026-07-02: both IPW trades bench on a 1m frame; on 5m
    # they armed below-VWAP and cost −$136.93). The live env pin (CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL=1m)
    # and this default now MATCH, so the binding-assert manifest (FIX-9) expects "1m" both ways.
    chili_momentum_pullback_entry_interval: str = Field(
        default="1m",
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_ENTRY_INTERVAL"),
    )
    # ── Tick-scalp one-shot latch: placeability gate (STEP-B #12) ──
    # The tick first-pullback trigger is one-shot (state["fired"] latches so a single
    # reclaim can't re-buy the same leg). The bug: it CONSUMED the shot even when NO order
    # could be placed (blocked clock / dark adapter / unpassable spread) — USDE fired ×1,149
    # while blocked, WHLR/DSY/JEM/CETX stranded. With this ON, the reclaim is only CONSUMED
    # when the caller confirms the order is actually placeable; a fired-but-no-order tick
    # REARMS (does not latch) so the very next placeable tick can fire — bounded by the
    # per-day rearm cap below. OFF => legacy behavior (consume on reclaim regardless).
    chili_momentum_tick_scalp_placeability_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TICK_SCALP_PLACEABILITY_GATE_ENABLED"),
        description="Consume the tick-scalp one-shot latch ONLY when an order is actually placeable; a fired-but-no-order tick REARMS instead of latching (bounded by the per-day rearm cap). Fixes the blocked-while-fired strand (USDE ×1,149).",
    )
    chili_momentum_tick_scalp_max_rearms_per_day: int = Field(
        default=8,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TICK_SCALP_MAX_REARMS_PER_DAY"),
        description="Max fired-but-no-order REARMS per symbol/day before the tick-scalp latch consumes anyway (stops an unbounded blocked-spin). The one documented base; a FLOOR the caller can raise per-symbol.",
    )
    # ── Ross RECENT (post-book) entry-quality refinements (docs/DESIGN/MOMENTUM_LANE.md §8) ──
    # #1 Break-AND-retest: don't buy the raw first break (it wicks out / reverses);
    # wait for the break, a shallow retest of the broken level, and a hold+reclaim.
    chili_momentum_pullback_require_retest: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_REQUIRE_RETEST"),
        description="Require break+retest+hold of the pullback high before entry, not the raw first break.",
    )
    chili_momentum_pullback_retest_tolerance: float = Field(
        default=0.002,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_RETEST_TOLERANCE"),
        description="Fraction band around the broken level that still counts as a retest/hold (0.002 = 20 bps).",
    )
    chili_momentum_pullback_retest_lookback_bars: int = Field(
        default=4,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_RETEST_LOOKBACK_BARS"),
        description="Bars reserved after the consolidation base for the break+retest+reclaim sequence.",
    )
    # Deep-retrace RECLAIM entry (the 2026-06-11 EDHL gap): when the retrace was too
    # deep for the flag checks, Ross waits for price to RECLAIM the 9-EMA, hold it,
    # and buys the first break of the recovery swing high. Stop = the reclaim
    # consolidation low (never the far dip low). All other yardsticks are reused
    # (collapse cap = the halt-resume dip cap; EMA band, runaway volume floor).
    chili_momentum_deep_reclaim_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DEEP_RECLAIM_ENABLED"),
        description="Allow re-entry on a deep retrace once price reclaims the 9-EMA and breaks the recovery swing high.",
    )
    chili_momentum_reclaim_confirm_bars: int = Field(
        default=2,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RECLAIM_CONFIRM_BARS"),
        description="Completed bars that must CLOSE holding the 9-EMA band after a deep dip before the reclaim arms (Ross: reclaim it and HOLD it).",
    )
    chili_momentum_reclaim_max_hours_after_open: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RECLAIM_MAX_HOURS_AFTER_OPEN"),
        description="Deep reclaims arm only until this many hours after the 9:30 ET open (Ross: 'by 10:30 I'm done'); A/B-validated — morning reclaims paid (EDHL/LASE), afternoon ones bled (SPHL/GCDT/DBGI 06-10).",
    )
    # ── Dip-buy (Ross "first reversal off the dip") evolution of deep_reclaim ─────
    # Today's deep_reclaim waits for the RECOVERY swing-high break (held>=2 bars +
    # break) = entering well off the dip low (a chase). Ross instead buys NEAR the
    # dip on the FIRST candle to tick its OWN pullback-bar high, with a stop just
    # under the dip low — EARLIER. This ADDITIVE branch (tried before the recovery
    # path, falls through byte-identically on any decline) fires only behind a
    # 3-signal AND gate that separates a buyable dip from a falling knife:
    #  (1) rising trend (VWAP-proxy slope>0) + intact HH/HL + first-pullback + clear
    #      runway, (2) volume DRY-UP on the dip then volume RETURN on the trigger,
    #  (3) first reversal new-high off the dip bar (green close). Stop = the dip-low
    # anchor (the authoritative vol-floor layer widens it; INVARIANT A lives there).
    # ONE adaptive base = the VWAP-slope lookback; the rest are Ross-discipline
    # floors. Only helps the BUYABLE-DEPTH class (<=25% dips, the collapse cap runs
    # first); >25% collapses stay (correctly) rejected. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_deep_reclaim_dipbuy_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DEEP_RECLAIM_DIPBUY_ENABLED"),
        description="Buy the FIRST reversal off the dip (earlier than the recovery-high reclaim) when the 3-signal gate passes. KILL-SWITCH: False -> byte-identical to the current deep_reclaim.",
    )
    chili_momentum_deep_reclaim_dipbuy_vwap_lookback: int = Field(
        default=12,
        ge=4,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DEEP_RECLAIM_DIPBUY_VWAP_LOOKBACK"),
        description="THE single adaptive base: bars over which the VWAP-proxy slope + the HH/HL structure anchor are measured (research 10-15-bar trend window).",
    )
    chili_momentum_deep_reclaim_dipbuy_dryup_ratio: float = Field(
        default=0.85,
        gt=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DEEP_RECLAIM_DIPBUY_DRYUP_RATIO"),
        description="Dip-bar mean volume must be < this x the prior trend-push mean volume (volume DRY-UP). A floor the system can tighten.",
    )
    chili_momentum_deep_reclaim_dipbuy_pullback_bars: int = Field(
        default=3,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DEEP_RECLAIM_DIPBUY_PULLBACK_BARS"),
        description="Shallowness guard: the dip must be a SHALLOW 2-3 red-candle pullback (this many bars from peak to dip), not a long grind down.",
    )
    chili_momentum_deep_reclaim_dipbuy_stop_buffer_bps: float = Field(
        default=10.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DEEP_RECLAIM_DIPBUY_STOP_BUFFER_BPS"),
        description="Stop sits this many bps under the dip low (ATR-relative max with 0.25xATR%; bps not cents = class-aware). The vol-floor layer widens it if too tight.",
    )
    chili_momentum_deep_reclaim_collapse_cap_mult: float = Field(
        default=1.6,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DEEP_RECLAIM_COLLAPSE_CAP_MULT"),
        description="Deep-reclaim dip-buy: a dip deeper than the (adaptive) collapse cap is normally a breakdown, but if price has ALREADY reclaimed back within tol of the run-high it was BOUGHT, not a collapse (Ross's halt-resume dip-buy; WNW 2026-06-16). Allow such RECLAIMED dips up to this multiple of the collapse cap (1.0 = off/old behavior). Bounded so a true -60% collapse is still rejected even if it bounced.",
    )
    # ── Ross FIRST-PULLBACK entry (the EARLIEST, most aggressive momentum entry) ──
    # Ross buys the FIRST 1m candle to make a new high after the FIRST shallow pullback
    # off a confirmed impulse (he caught JRSH this way for +$21k). CHILI's existing
    # retest/deep-reclaim ladder enters structurally LATER (on JRSH its only setup fired
    # at 09:26 during the collapse → a loss). This ADDITIVE branch (in entry_gates.
    # first_pullback_break, tried alongside the ladder; a FIRE wins, an ARM tick-watches,
    # a PASS falls through byte-identically) fires near the resumption of the move. CHOP
    # is the dominant risk — the explosive-name + first-pullback-only + depth guards (all
    # reusing existing yardsticks: the RVOL floor, _is_first_pullback, the dipbuy depth
    # cap) are the defense; do NOT loosen them. This is a REAL risk change (aggressive
    # entry), REPLAY-VALIDATED before live. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_entry_first_pullback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FIRST_PULLBACK_ENABLED"),
        description="Enable Ross's first-pullback entry (first new-high after the first shallow pullback off an impulse) alongside the retest/deep-reclaim ladder. KILL-SWITCH: False -> byte-identical to the current ladder.",
    )
    chili_momentum_first_pullback_interval: str = Field(
        default="15s",
        validation_alias=AliasChoices("CHILI_MOMENTUM_FIRST_PULLBACK_INTERVAL"),
        description="THE base timeframe knob for the first-pullback structure ('15s' — paired with micropull-enabled so Ross's ~120s micro-pullback->new-high geometry runs on the tick-built 15s micro-bars, sub-minute). A 5m bar structurally collapses it; '1m' detects the break a bar-close late. CAPTURE-G1(a) 2026-07-03: flipped 1m->15s (with chili_momentum_micropull_enabled True) — SVRE 2026-06-30 replay-verified: the micro path reaches waiting_for_break at 12:45:25Z with pullback_high=6.89 (Ross's exact 6.98 entry), where the 1m path stays pullback_too_deep the whole window. BYTE-IDENTICAL on the non-micro (1m/5m) runner: the first-pullback branch only activates when entry_interval == this interval, so a 15s value simply skips the branch on a 1m/5m df exactly as a mismatched interval did before.",
    )
    # 15s MICRO-PULLBACK (2026-06-15, operator "1m too slow for our style"): Ross's
    # ~120s micro-pullback happens INSIDE a 1m bar, so a 1m trigger detects the
    # break a bar-close late. When enabled, the live entry path builds a 15s
    # micro-bar df from the densified tick tape and runs the first-pullback trigger
    # on it — sub-minute entry. SUPERSET/FAIL-SAFE: where only 1-min snapshots
    # exist the resampler yields <2 micro-bars → the trigger naturally no-fires →
    # the path falls back to the existing 1m bars (byte-identical). The
    # micro-pullback is the MOST aggressive entry: the existing chop guards
    # (_dipbuy_tick_thrust_ok, premarket-confirm) still apply. Default OFF until
    # replay-proven. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_micropull_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULL_ENABLED"),
        description="Run the first-pullback entry on tick-built 15s micro-bars (sub-minute entry). FAIL-SAFE: insufficient tick density ⇒ _build_micro_bar_df returns None ⇒ fall back to 1m (byte-identical) — a thin/sparse name can NEVER fabricate 15s bars that arm a junk break. CAPTURE-G1(a) 2026-07-03: flipped False->True (paired with chili_momentum_first_pullback_interval='15s' so the first-pullback ARM engages on the micro-frame). SVRE 2026-06-30 replay-verified: the micro path reaches waiting_for_break at 12:45:25Z with pullback_high=6.89 (Ross's exact 6.98 entry) where the 1m path stays pullback_too_deep the whole window; the extended_verticality anti-chase guard still correctly takes over on the later +6%/s vertical.",
    )
    chili_momentum_micropull_bar_seconds: int = Field(
        default=15, ge=5, le=30,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULL_BAR_SECONDS"),
        description="THE single base knob for micro-bar width (seconds) — the tick tape is bucketed into OHLC bars of this size for the sub-minute first-pullback trigger.",
    )
    # WAVE-4 ITEM-7 F2: when the micro-bar build (from the in-DB tick tape) raises, RETRY
    # ONCE on a FRESH short-lived SessionLocal before falling back — the tape is in-DB, so a
    # transient/stale session error must NOT silently drop the micro frame to the 1m/5m path.
    # OFF ⇒ the legacy single-attempt swallow (byte-identical). Paired with the F1 log/meta.
    chili_momentum_micro_fallback_1m_from_ticks_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICRO_FALLBACK_1M_FROM_TICKS_ENABLED"),
        description="WAVE-4 ITEM-7 F2: on a micro-bar build error, retry ONCE on a fresh short-lived SessionLocal (the tape is in-DB) before falling back — never silently degrade the micro frame on a transient session error. OFF ⇒ legacy single-attempt (byte-identical).",
    )
    # Pending-entry lifecycle is EVENT-DRIVEN (cancel on setup invalidation /
    # limit left behind), not clock-driven — this is only the BACKSTOP: a
    # submitted entry limit must not outlive the bar evidence that produced it,
    # measured in entry-interval BARS (same pattern as breakout_bailout_max_bars;
    # no free seconds). 2 bars @1m = 120s, which also outwaits RH's ~13s
    # "unconfirmed" review that killed the CPSH/SNDG submits at the old 10s window.
    chili_momentum_entry_max_rest_bars: float = Field(
        default=2.0,
        ge=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_MAX_REST_BARS"),
        description="Backstop: entry-interval bars a submitted entry limit may rest before cancel + re-watch (invalidation/runaway cancels fire first, event-driven).",
    )
    # SPREAD STABILITY (2026-06-11 INDP): one clean BBO instant inside a hostile
    # flickering spread regime passed the gate; the MEDIAN of the recent tape is
    # the market. Window in entry-interval BARS (derived); fails open below the
    # sample floor.
    chili_momentum_spread_stability_window_bars: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_STABILITY_WINDOW_BARS"),
        description="Entry-interval bars of tape whose MEDIAN spread must also pass the adaptive max (0 disables).",
    )
    # Cushion-adaptive runner trail (Ross day-4 2026-06-11) — band knobs: floor
    # when no cushion, ceiling once the position+day bank the trade's own
    # reward:risk plan. SWEPT 2026-06-11 on the two-day tape: FLAT 500/500 won
    # decisively (+$939 vs +$533 for 500/1000 — the cushion ramp saturated too
    # fast and gave winners back), so defaults ship FLAT at 500. The band
    # machinery stays for the weekly refit from live capture ratios.
    chili_momentum_trail_floor_bps: float = Field(
        default=500.0,
        ge=50.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TRAIL_FLOOR_BPS"),
    )
    chili_momentum_trail_ceiling_bps: float = Field(
        default=500.0,
        ge=50.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TRAIL_CEILING_BPS"),
    )
    # LEVER 2A — MATH-VERIFIED adaptive vol-normalized runner trail. The frozen-ATR trail
    # snapshots entry_stop_atr_pct AT ENTRY and never refreshes it, so a runner whose realized
    # vol collapses after the breakout keeps an entry-sized (too-wide) trail and bleeds the move
    # back (the live ASTC/DCOY/LI/AMPX/TMC "breaks don't hold, -$17" leak). When ON (default True):
    # the runner-trail WIDTH is re-derived from LIVE tape realized vol scaled to the holding
    # horizon (rv_hold = rv_live*sqrt(N), trail = clamp(k*rv_hold, floor, 0.15)), floored OUTSIDE
    # the bid/ask bounce (Roll effective spread) so a healthy pullback isn't shaken out, and
    # applied to the MICRO-PRICE high-water mark. INVARIANT-A is preserved: new_stop = max(current,
    # breakeven, candidate) — ratchet-only, NEVER loosens/nulls the structural or breakeven stop;
    # the structural stop, breakeven-after-partial, max-loss circuit #769, and the climax exits
    # (tape_accel_reversal / ofi_exhaustion_lock) are all untouched. OFF (or a thin tape that yields
    # < 2 grid returns) ⇒ the frozen-ATR width path is byte-identical.
    chili_momentum_volnorm_trail_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VOLNORM_TRAIL_ENABLED"),
        description="LEVER 2A: re-derive the runner-trail WIDTH from LIVE tape realized vol scaled to the holding horizon (vs the frozen entry ATR), floored outside the bid/ask bounce, on the micro-price HWM. Ratchet-only (INVARIANT-A) — never loosens the structural/breakeven stop. OFF or thin tape ⇒ frozen-ATR width byte-identical.",
    )
    # LEVER 2A — the trail tightness multiplier k on rv_hold. ONE documented base (1.3); literature
    # range [1.1, 1.7] (k<1 would put the stop inside the holding-horizon vol band → whipsaw; k too
    # high gives back too much). Clamped to the [1.1, 1.7] band. The trail width is k*rv_hold,
    # clamped between the live vol-floor (reused) and the existing 0.15 ceiling.
    chili_momentum_volnorm_trail_k: float = Field(
        default=1.3,
        ge=1.1,
        le=1.7,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VOLNORM_TRAIL_K"),
        description="LEVER 2A tightness multiplier k on rv_hold (trail_dist = clamp(k*rv_hold, floor, max_dist)). Default 1.3, band [1.1,1.7]. This is the BASE width; DESIGN#2 widens it ADAPTIVELY toward the chandelier-literature optimum via the maturity factor below.",
    )
    # DESIGN#2 — ADAPTIVE TRAIL-WIDTH MATURITY WIDEN. The 2A base k (1.3 = ~1.3-sigma over the
    # holding horizon) trails too TIGHT vs the chandelier-ATR literature (profit factor peaks
    # near ~3x ATR; ~2x over-tightens and shakes runners out — the ~40%-of-MFE-capture leak).
    # Rather than a flat magic 3x, widen the EFFECTIVE k by a bounded factor in [1.0, max_widen]
    # driven by TWO real live signals (no magic absolute): the live realized-vol regime
    # (rv_step vs the entry vol-floor) AND move MATURITY (the OFI level/slope already computed by
    # _live_flow_slope) — WIDE early in a fresh, fed trend (OFI level>0 ∧ slope>=0), decaying to
    # 1.0 as flow rolls over so the existing RIDE-LOCK LOCK/HARD bands tighten unimpeded.
    # INVARIANT-A SAFE: a wider band only LOWERS the trail candidate, composed through
    # max(stop, breakeven, candidate) — it can only decline to ratchet as hard, NEVER loosens a
    # placed stop. OFF / thin flow ⇒ factor 1.0 ⇒ byte-identical.
    chili_momentum_volnorm_trail_maturity_widen_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VOLNORM_TRAIL_MATURITY_WIDEN_ENABLED"),
        description="DESIGN#2: widen the 2A trail k ADAPTIVELY (vol-regime x move-maturity, [1.0,max_widen]) toward the chandelier ~3x optimum early in a fresh trend, decaying to 1.0 near exhaustion. Ratchet-only (INVARIANT-A). OFF or thin flow ⇒ byte-identical.",
    )
    # DESIGN#2 — the maturity widen CEILING (one documented cap). Effective k reaches
    # base_k * max_widen at its widest: 1.3 * 2.0 = 2.6 (within the chandelier 2-3x optimum band,
    # short of the 3x exhaustion edge). Clamped [1.0, 3.0].
    chili_momentum_volnorm_trail_maturity_max_widen: float = Field(
        default=2.0,
        ge=1.0,
        le=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VOLNORM_TRAIL_MATURITY_MAX_WIDEN"),
        description="DESIGN#2 maturity widen ceiling on the 2A k. Effective k tops out at base_k*max_widen (1.3*2.0=2.6, inside the chandelier 2-3x optimum band). Default 2.0, band [1.0,3.0].",
    )
    # DESIGN#2 — the trail-distance CEILING as a fraction of price (was the hard-coded 0.15 in
    # volnorm_trail_dist_pct). Raised to a knob so the WIDENED band is actually reachable; the
    # widened candidate is still clamped here so a vol spike can't run the trail unbounded.
    chili_momentum_volnorm_trail_max_dist_pct: float = Field(
        default=0.20,
        ge=0.05,
        le=0.40,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VOLNORM_TRAIL_MAX_DIST_PCT"),
        description="DESIGN#2 trail-distance ceiling (fraction of price) for the 2A vol-norm trail. Was a hard 0.15; raised to 0.20 so the maturity-widened band is reachable. Clamped [0.05,0.40].",
    )
    # LEVER 2A — the realized-vol tape lookback (seconds). Longer than the 15s OFI window so the
    # EWMA has enough event-grid steps; the EWMA half-life is derived as window/(2*grid).
    chili_momentum_volnorm_trail_window_s: float = Field(
        default=90.0,
        ge=15.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VOLNORM_TRAIL_WINDOW_S"),
        description="LEVER 2A realized-vol tape lookback (s). EWMA half-life derived as window/(2*grid_secs). Default 90s.",
    )
    # LEVER 2A — the event-time sub-sample grid (seconds). Sub-sampling raw ticks to a ~1-5s grid is
    # the DENOISING step (collapses bid-ask-bounce micro-noise out of the per-tick return series).
    chili_momentum_volnorm_trail_grid_secs: float = Field(
        default=2.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VOLNORM_TRAIL_GRID_SECS"),
        description="LEVER 2A event-time sub-sample grid (s) for denoising the realized-vol return series. Default 2.0; 0 = per-tick (no sub-sample).",
    )
    # LEVER 2B — VELOCITY/PERSISTENCE RIDE-LOCK on top of the 2A vol-norm trail. The live
    # entries that DO fire (ASTC/DCOY/LI/AMPX/TMC, 8 round-trips, -$17) bail because the
    # breaks do not HOLD: the 2A trail is correctly-sized but still mechanical, so a runner
    # gets tightened out on a healthy mid-thrust pullback, and a true climax tops a full
    # candle before the candle-shaped exits print. 2B reads the DENOISED order flow (OFI
    # LEVEL + its EWMA SLOPE = the 1st derivative on the event-time series — NOT the raw
    # 2nd-derivative signed_accel, which is noise-amplifying per the math verification) plus
    # the tick_rate, and modulates the trail band by REGIME:
    #   RIDE  — while signed-flow/OFI-slope > 0 AND tick_rate >= entry_tick_rate*persist_frac,
    #           the runner is in a PERSISTENT thrust: hold the 2A band WIDE (do NOT mechanically
    #           tighten) so the move extends. (Never loosens an EXISTING stop — INVARIANT-A.)
    #   LOCK  — when the OFI-slope/flow ROLLS OVER (turns negative) NEAR the HWM, COLLAPSE the
    #           band to a tight giveback: sell into strength at the climax, BEFORE a full bar
    #           prints (faster than the topping-tail candle exits).
    #   HARD  — strong-negative flow WITH sellers lifting through the micro-price ⇒ a tighter
    #           climax-lock still (the most decisive distribution read).
    # ALL ratchet-only (INVARIANT-A: the returned stop can only TIGHTEN, never loosen/null the
    # structural, breakeven, vol-norm, or climax-exit stop — it composes via max() at the call
    # site exactly like ofi_exhaustion_lock / tape_accel_reversal_exit). Buy/sell aggressor
    # classification reuses the EXISTING Lee-Ready tick/quote rule (_aggressor_imbalance). The
    # max-loss circuit #769, structural stop, breakeven, and daily-loss breaker are UNTOUCHED.
    # OFF (or thin/missing tape) ⇒ the 2A vol-norm trail alone, byte-identical.
    chili_momentum_velocity_persistence_exit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VELOCITY_PERSISTENCE_EXIT_ENABLED"),
        description="LEVER 2B: velocity/persistence RIDE-LOCK on the 2A vol-norm trail using DENOISED OFI level + EWMA SLOPE (1st derivative) + tick_rate. RIDE holds the band wide while flow persists; LOCK collapses it to a tight giveback when flow rolls over near the HWM (sell into strength before a full candle prints); HARD-EXIT on strong-negative flow + sellers lifting through micro-price. Ratchet-only (INVARIANT-A); never loosens any stop or removes a protection gate. OFF or thin tape ⇒ 2A trail alone, byte-identical.",
    )
    # LEVER 2B — persistence fraction: the runner stays in RIDE only while the LIVE tick_rate
    # holds at >= entry_tick_rate * persist_frac (the thrust is still being fed). ONE documented
    # seed (0.6 = the move's pace may ebb to 60% of the entry pace and still be "persistent";
    # below it the thrust is fading ⇒ the band reverts to the 2A mechanical width). A/B-tunable;
    # clamped [0.1, 1.0]. Reuses the entry tick_rate the tape already computes (no new datum).
    chili_momentum_velocity_persist_frac: float = Field(
        default=0.6,
        ge=0.1,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VELOCITY_PERSIST_FRAC"),
        description="LEVER 2B persistence fraction: stay in RIDE while live tick_rate >= entry_tick_rate*persist_frac. Default 0.6, band [0.1,1.0]. A/B-tunable seed.",
    )
    # LEVER 2B — the OFI-slope EWMA half-life (in event-GRID steps). The denoised OFI LEVEL is
    # the per-grid-bucket aggressor imbalance; its EWMA SLOPE (consecutive-EWMA difference) is the
    # rollover signal. ONE documented seed (4.0 grid steps ≈ the recent ~window/2 dominates at the
    # 2A grid); shorter ⇒ snappier/noisier rollover, longer ⇒ smoother/laggier. Clamped >= 1.0.
    chili_momentum_velocity_ofi_slope_half_life: float = Field(
        default=4.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VELOCITY_OFI_SLOPE_HALF_LIFE"),
        description="LEVER 2B OFI-slope EWMA half-life in event-grid steps (the rollover signal is the EWMA-OFI-level 1st derivative). Default 4.0; >=1.0. A/B-tunable seed.",
    )
    # Paper quote sanity (2026-06-12 ROBO: a failed quote fetch fabricated a
    # $100 placeholder that "filled" a $0.022 token's exit at $99.84 = +$555k of
    # fiction): a mid that jumps beyond this fraction vs the session's own last
    # mid in ONE tick is quarantined (tick skipped). 0 disables.
    chili_momentum_paper_quote_jump_guard_frac: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PAPER_QUOTE_JUMP_GUARD_FRAC"),
    )
    # Alpaca twin soak (2026-06-12, ALPACA_LANE "same-name A/B"): every equity
    # armed live on Robinhood also arms a twin on alpaca_spot (REAL order
    # lifecycle, Alpaca PAPER endpoint = fake money). Fill-quality diff decides
    # the venue migration. Fake-money outcomes/risk excluded from real accounting.
    chili_momentum_alpaca_twin_arm_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ALPACA_TWIN_ARM_ENABLED"),
    )
    # Paper shadow mass (2026-06-11): probed eligibles that lose the single live
    # slot are armed in PAPER (free sample data; 3 paper sessions EVER vs 718
    # live = tuning on anecdotes). Bounded by the concurrent cap below.
    chili_momentum_paper_shadow_arm_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PAPER_SHADOW_ARM_ENABLED"),
    )
    chili_momentum_paper_shadow_max_sessions: int = Field(
        default=40,
        ge=0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PAPER_SHADOW_MAX_SESSIONS"),
    )
    # Aggregate open at-risk cap (correlation guard, 2026-06-11: three
    # "independent" losses were ONE regime trade trebled): the SUM of
    # entry-to-stop risk across open live equity momentum positions may not
    # exceed this fraction of equity. 3% = three concurrent full-risk (1%)
    # positions; breakeven-locked winners contribute zero.
    chili_momentum_max_aggregate_risk_pct_of_equity: float = Field(
        default=0.03,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_AGGREGATE_RISK_PCT_OF_EQUITY"),
    )
    # Nightly replay regression tripwire: rerun today through the replay engine
    # on tonight's code and diff vs live actuals (catch behavior drift the
    # evening before the next open, not during it).
    chili_momentum_replay_regression_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_REGRESSION_ENABLED"),
    )
    # Replay selection->entry alignment (2026-06-15): the replay's as-of arming
    # mirrors the live auto_arm's fresh-impulse discipline — a FADED 24h mover is
    # not pinned to a watch slot unless its trigger is FIRING (a firing break is
    # always valid). Reuses the SAME ``intraday_impulse_freshness`` helper the live
    # auto_arm calls (parity by construction; no lookahead, completed-bars-only).
    # ON by default = faithful; set =0 to restore the prior viability-rank-only
    # arming (the reversible knob). docs/STRATEGY replay-lab convergence.
    chili_momentum_replay_freshness_filter_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_FRESHNESS_FILTER_ENABLED"),
    )
    # Replay feature-capture (2026-06-23): when ON, the replay attaches a lookahead-free
    # entry-moment FEATURE VECTOR to each trade record (front_side_state, OFI/micro,
    # spread/atr/rr geometry, entry-gate dbg, context flags) alongside run_r — the labeled
    # dataset for the winner/loser DISCRIMINATOR search. DEFAULT-OFF: when off, _feat is
    # None and trade records (pnl/cum/fills) are byte-identical to today (replay invariant).
    chili_momentum_replay_capture_features: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_CAPTURE_FEATURES"),
    )
    # REPLAY FIDELITY V2 (2026-06-28): the replay's absolute $ were ~6x-overstated =
    # 2.24x OVER-FILL x 2.71x OVER-SIZE (diagnosis wf4wdtntt). When ON, replay_v2 (a)
    # SIZES each entry through the SAME live ~18-dial de-risk stack (cushion / green-day /
    # streak / per-name de-risk) the runner applies — computed against the REPLAY's own
    # running simulated state, via the SAME live callables (no hardcoded multipliers) —
    # and applies the governor under-fill; (b) replaces the deterministic auto-fill with a
    # marketable-LIMIT FILL-OR-REJECT (spread-ceiling reject + ack-window through-trade
    # confirmation + a per-minute fill-admission token bucket mirroring the rail governor);
    # (c) emits a result["day_pnl_band"] confidence interval over the irreducible tail.
    # DEFAULT-OFF => byte-identical to current HEAD (an md5-of-trades parity check guards
    # it). REPLAY-ONLY: this flag is read ONLY inside replay_v2.py; it never touches any
    # live-trading code path. docs/STRATEGY replay-lab convergence.
    chili_momentum_replay_fidelity_v2: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_FIDELITY_V2"),
    )
    # REPLAY ENGINE-ON (2026-06-28): A/B the live momentum ENGINE offline, INDEPENDENT of
    # the fidelity flag. When ON, replay_v2 swaps (A) the fixed MAX_OPEN_CONCURRENT slot
    # cap for the engine's shape-aware admit_by_aggregate_risk (running aggregate (entry-
    # stop)*qty vs chili_momentum_max_aggregate_risk_pct_of_equity x equity) and (B) the
    # fixed MAX_SLOTS watch cap for adaptive_watch_fanout(field_size). Both read the SAME
    # live settings the runner reads (no replay-only constants). HONEST: on the 06/22-26
    # data the slot cap rarely binds, so this barely moves the trade-SET — it is for
    # engine-on/off A/B fidelity, NOT reliability. DEFAULT-OFF => byte-identical. REPLAY-
    # ONLY. docs/DESIGN/MOMENTUM_ENGINE.md.
    chili_momentum_replay_engine_on: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_ENGINE_ON"),
    )
    # REPLAY RECORDED-FILLS CONSUMER (2026-06-28, "RECORD don't derive"): for armed_source
    # ='live' replays ONLY, when ON, the replay stops DERIVING fills from the tape for names
    # the live lane actually armed and instead consumes the RECORDED broker truth in
    # momentum_fill_outcomes (one row per fill leg; collapsed into per-(session_id, leg_seq)
    # round-trips). A live-armed name that the recorded truth shows FILLED is emitted with its
    # RECORDED entry/exit/spread/$/qty (why='recorded_live'); a live-armed name with NO recorded
    # fill (live cancelled it pre-entry) is DROPPED (trace gate_fail:live_cancelled). This makes
    # the live-armed fill-SET exactly match what live traded (06-24: the derived model fires 25
    # distinct names but live only filled 9). Names the replay arms that live NEVER armed (pure
    # counterfactual) keep the existing DERIVED tape model (why suffix ':counterfactual'), so the
    # engine's own selection is still exercised. INDEPENDENT of chili_momentum_replay_fidelity_v2
    # (the operator can flip either alone). DEFAULT-OFF => byte-identical to current HEAD (no
    # recorded-fill load, derived model for every name). REPLAY-ONLY: read ONLY inside replay_v2.py;
    # it never touches any live-trading code path. docs/STRATEGY replay-lab convergence.
    chili_momentum_replay_recorded_fills_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_RECORDED_FILLS_ENABLED"),
    )
    # REPLAY PRINTS-BASED FILL MODEL (2026-06-28, STEP 2 of the version-agnostic backtest;
    # docs/DESIGN/VERSION_AGNOSTIC_BACKTEST.md): the quote-touch fill OVER-fills (a marketable
    # LIMIT is "filled" the instant a quote touches it — BEEM predicted 29/34 fills vs live's
    # 1/34) because QUOTES cannot see executions. The TRADE PRINTS (iqfeed_trade_ticks: price/
    # size/observed_at) CAN: a real execution AT/THROUGH the limit is direct evidence shares
    # traded. When ON, replay_v2 REPLACES the quote-touch fill (min(limit,max(bid,mid))) with a
    # prints_fill_decision: cumulate participation*size of through-prints in [t0-review_latency,
    # t0+ack_window], fill = max(0, min(qty, cum_size - queue_ahead)), fill_vwap = the size-
    # weighted print price over the filling slice (bounded [bid, limit]); >=qty => FILL,
    # 0<filled<qty => PARTIAL (emit + cancel remainder; below min-size => CANCEL), 0 =>
    # CANCEL (trace gate_fail:prints_no_fill, the trade is dropped). Each trade is tagged
    # source=prints_fill (resolved against real prints) vs quote_fallback (no prints / degraded
    # queue => the existing quote model, low-confidence flagged). Version-AGNOSTIC: nothing
    # reads momentum_fill_outcomes — any version's (sym,limit,qty,t0) is scored against the
    # immutable recorded prints. DEFAULT-OFF => the EXACT current quote fill (md5-of-trades
    # byte-identical to HEAD). REPLAY-ONLY: read ONLY inside replay_v2.py.
    chili_momentum_replay_prints_fill_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_PRINTS_FILL_ENABLED"),
    )
    # PRINTS-FILL REVIEW-LATENCY MULTIPLIER (adaptive, NO constant latency): the prints-fill
    # window opens at t0 - review_latency, where review_latency is DERIVED — the median of the
    # lane's OWN recorded (live_entry_submitted.ts - live_entry_candidate_detected.ts) latencies
    # for the day (fallback = the name's inter-trade print cadence when no events exist). This
    # multiplier scales that derived latency (1.0 = use it as measured; >1 widens the review
    # lookback, <1 tightens it) so the operator can stress the fill model WITHOUT hardcoding a
    # latency. REPLAY-ONLY; only consulted when chili_momentum_replay_prints_fill_enabled is ON.
    chili_momentum_replay_review_latency_k: float = Field(
        default=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_REVIEW_LATENCY_K"),
    )
    # LIVE feature-capture (2026-06-23): when ON, the live runner records the SAME
    # lookahead-free entry-feature vector (shared entry_features.capture_entry_features)
    # onto the session's live-exec blob at the entry fill, so outcome_extract reads it for
    # mode==live and the meta-label dataset GROWS from real trades (today it's PAPER-ONLY ->
    # empty for live). READ-ONLY + POST-transition + best-effort (a capture error is logged
    # and skipped; it can NEVER affect the fill/management). Kill-switch -> instant per-sha
    # rollback. Default-ON: the operator wants data flowing; the capture is side-effect-free.
    chili_momentum_live_capture_features: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_CAPTURE_FEATURES"),
    )
    # META-LABEL DE-RATE: size DOWN a low-edge/loser-profile entry per the adaptive, regime-aware
    # meta-label model (evidence-scaled -> INERT until it earns confidence; NEVER a veto). Default-ON;
    # =0 -> byte-identical (multiplier 1.0). Instant per-sha rollback.
    chili_momentum_meta_label_derate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_META_LABEL_DERATE_ENABLED"),
    )
    # The ONE documented irreducible base (operator: "irreducible base = ONE documented setting"):
    # the smallest size fraction the meta-label de-rate may shrink an entry to. NEVER 0 -> never a
    # veto, so a rare below-VWAP explosive winner is sized-down at worst, never killed.
    chili_momentum_meta_label_min_size: float = Field(
        default=0.4, ge=0.05, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_META_LABEL_MIN_SIZE"),
    )
    # DATA-SNOOPING-CORRECTED FEATURE SCREEN: prune the meta-label's spurious feature columns
    # (CALIB-BY-DayRestrict — marginal-preserving within-day permutation null + BY-FDR + empirical
    # self-calibration + protected-tail union + tail-monotone revert). Keep-all-DOMINANT: INERT at
    # today's n (byte-identical to the all-feature ridge) and self-activates only when type-I error
    # is provably controlled as data grows. Default-ON; =0 -> screen bypassed entirely (keep all).
    chili_momentum_meta_label_feature_screen_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_META_LABEL_FEATURE_SCREEN_ENABLED"),
    )
    chili_momentum_spread_stability_min_samples: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_STABILITY_MIN_SAMPLES"),
        description="Minimum tape samples in the window before the stability gate may block (below = fail open).",
    )
    # Volume spike required on the break/reclaim bar (a FLOOR, not a magic cutoff).
    chili_momentum_pullback_volume_spike_multiple: float = Field(
        default=1.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_VOLUME_SPIKE_MULTIPLE"),
        description="Min relative-volume on the trigger bar (current bar vol / trailing average).",
    )
    # #3 Sustaining-volume gate (the ESTR guardrail): at the entry TICK the move must
    # still be carried by volume (recent rel-vol above the floor), not a faded 24h mover.
    chili_momentum_entry_require_sustained_volume: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_REQUIRE_SUSTAINED_VOLUME"),
        description="Reject entries where recent rel-vol has faded below the floor at entry time.",
    )
    chili_momentum_entry_sustained_rvol_floor: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_SUSTAINED_RVOL_FLOOR"),
        description="Min mean rel-vol over the sustain window (1.0 = still at its own trailing average; a FLOOR).",
    )
    chili_momentum_entry_sustain_lookback_bars: int = Field(
        default=5,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_SUSTAIN_LOOKBACK_BARS"),
        description="Bars over which sustained rel-vol is averaged at the entry tick.",
    )
    # CAPTURE-G2 (2026-07-03): the sustained-volume gate averages rel-vol over the last N
    # bars, which INCLUDES the quiet coil bars preceding a dry-coil premarket break (the JEM
    # 2026-06-30 12:59Z $46k class). The first break tick's 5-bar mean rvol is therefore < 1.0
    # BY CONSTRUCTION, so the gate rejects the ONLY catchable window even as the name explodes
    # — the setup class is structurally untradeable. EXEMPT the sustain gate when the break
    # bar's OWN forming-bar rvol is exploding (>= chili_momentum_explosive_floor_rvol; the
    # break bar proves volume is exploding NOW, exactly what the mean would show once the coil
    # rolls off). The ESTR faded-24h-mover guardrail stays intact (a genuine low-volume drift,
    # break bar NOT exploding, is still blocked); fail-CLOSED (unreadable break-bar rvol ⇒ no
    # exemption ⇒ current behavior). Deep-reclaim bounces are never exempted (dead-cat guard).
    chili_momentum_sustained_volume_coil_exempt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SUSTAINED_VOLUME_COIL_EXEMPT_ENABLED"),
        description="CAPTURE-G2: exempt the sustained-volume (faded-mover) gate on the break tick via two OR-ed fail-closed paths: (A) the BREAK BAR's own forming-bar rvol is exploding (>= chili_momentum_explosive_floor_rvol), or (B) the sustain mean recomputed EXCLUDING identified low-range coil bars clears the floor. So a dry-coil premarket break (JEM 06-30 class) whose coil-inclusive 5-bar mean is mathematically depressed by the quiet coil can still reach the entry window as active volume returns. Keeps the ESTR faded-24h-mover guardrail (a genuine low-volume drift — no explosive bar AND active mean still below floor — is still blocked) + the deep-reclaim dead-cat guard. True (default) makes the dry-coil class tradeable; False = byte-identical to the coil-inclusive gate.",
    )
    chili_momentum_sustained_coil_range_atr_frac: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SUSTAINED_COIL_RANGE_ATR_FRAC"),
        description="CAPTURE-G2 (path B): a bar whose range (high-low) is below this fraction of its ATR is an identified low-range COIL bar, excluded from the coil-excluded active-bar sustain mean. 0.5 = half the ATR (a genuinely tight consolidation candle). Only consulted when chili_momentum_sustained_volume_coil_exempt_enabled is ON; a missing/zero ATR keeps the bar (fails toward the stricter coil-inclusive mean).",
    )
    # Ross candle / VWAP / MACD entry confirmations — the tape-reading the structural
    # pullback gate alone misses. Default ON; each fail-OPEN on thin/missing data.
    chili_momentum_entry_require_break_candle: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_REQUIRE_BREAK_CANDLE"),
        description="Require a conviction bull break candle (reject a doji/topping-tail break that wicks out).",
    )
    chili_momentum_entry_break_candle_min_close_pos: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_BREAK_CANDLE_MIN_CLOSE_POS"),
        description="Break bar must close at least this fraction up its range (0.5 = upper half).",
    )
    # ADAPTIVE break-candle close-position floor (gate-audit fix: the FIXED 0.50 close-pos
    # over-rejected explosive FIRST-pushes — 53%% of weak_break_candle blocks ran +3%%, and
    # SKYQ/FRTT/WEN/HELP/BOLD ran but never filled). Ross buys the FIRST strong push even when
    # the 1m candle isn't textbook-strong. When ON, an EXPLOSIVE break (trigger-bar RVOL >= the
    # existing E3 explosive floor, chili_momentum_explosive_floor_rvol) RELAXES the close-pos
    # requirement, floating it DOWN from 0.50 toward the relaxed floor below as RVOL exceeds the
    # E3 floor. Ordinary tape (RVOL < the explosive floor, or unknown) keeps the textbook 0.50;
    # a genuinely weak/doji break STILL blocks below the relaxed floor even at high RVOL.
    # Relaxation is RVOL-DERIVED (vs the SAME E3 floor — no new magic threshold); the relaxed
    # floor is the ONE documented base. DEFAULT OFF — this is a skip-tick gate whose evidence is
    # confounded by the full stack, so it must be A/B'd on the recorded-fills replay before going
    # live. KILL-SWITCH: False -> the fixed 0.50 gate, byte-identical. (entry_gates.py weak_break_candle)
    chili_momentum_break_candle_adaptive_close_pos_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAK_CANDLE_ADAPTIVE_CLOSE_POS_ENABLED"),
        description="Adaptive break-candle close-pos: when ON, an explosive break (RVOL >= chili_momentum_explosive_floor_rvol) relaxes the close-pos requirement DOWN from 0.50 toward the relaxed floor; ordinary tape keeps 0.50. DEFAULT OFF (A/B on replay first). KILL-SWITCH: False -> fixed 0.50, byte-identical.",
    )
    chili_momentum_break_candle_adaptive_close_pos_floor: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAK_CANDLE_ADAPTIVE_CLOSE_POS_FLOOR"),
        description="Adaptive break-candle close-pos: the relaxed floor an EXPLOSIVE break can decay the close-pos requirement toward (from 0.50). The ONE documented relaxed-floor base; below it a break still blocks (doji rejection preserved). Reached at RVOL = 2x the E3 explosive floor.",
    )
    # SD-1 (verticality cold-frame fail-OPEN): the entry verticality veto caps the
    # allowed extension above EMA9 at atr_pct x the verticality mult. On a COLD/short
    # frame the ATR isn't warm, so atr_pct is None and the cap collapses to its 0.5%%
    # punitive floor — which over-rejects valid low-float Ross names that normally
    # breathe >0.5%% per bar. When ON (default True) the verticality veto is SKIPPED
    # entirely on a cold frame (fail-OPEN — without ATR we cannot tell a chase from a
    # normal breath). The veto stays FULLY active whenever atr_pct IS known. KILL-SWITCH:
    # False -> the current 0.5%%-floor cold-frame behaviour, byte-identical. (entry_gates.py)
    chili_momentum_verticality_skip_on_cold_atr: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VERTICALITY_SKIP_ON_COLD_ATR"),
        description="Verticality veto SKIPS on a cold frame (atr_pct None) instead of applying the punitive 0.5%% floor that over-rejects low-float Ross names. Veto stays fully active when atr_pct is known. KILL-SWITCH: False -> 0.5%%-floor behaviour, byte-identical.",
    )
    # NaN-bar drop: a single transient NaN/zero/inverted OHLC bar made the whole-frame
    # integrity verdict reject the entire 5-day frame and blank an otherwise healthy
    # name. When ON (default True) clean_ohlcv drops the INDIVIDUAL bad bars (NaN on any
    # of O/H/L/C, Close<=0, or High<Low) BEFORE the integrity verdict runs. A mostly-bad
    # frame (>50%% bad bars) is real corruption and STILL falls through to the whole-frame
    # reject. KILL-SWITCH: False -> the prior whole-frame reject, byte-identical. (data_quality.py / market_data.py)
    chili_momentum_clean_drop_bad_bars: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CLEAN_DROP_BAD_BARS"),
        description="Drop INDIVIDUAL malformed OHLC bars before the integrity verdict so one transient NaN/zero bar does not reject a whole healthy frame. Mostly-bad frames (>50%%) still get the whole-frame reject. KILL-SWITCH: False -> whole-frame reject, byte-identical.",
    )
    # WT-1 (thin-frame stop fallback — SAFETY): on a frame too thin to compute a live
    # expected-move, the stop-ATR sizing falls back to the regime DEFAULT (~1.5%%) — a
    # noise-tight stop on a low-float that gaps 5-9%% THROUGH it (the -$697 tail of
    # MTEN/SDOT/CCTG/CAST). This is a 3-STATE knob on whether to require a LIVE
    # expected-move before taking the entry:
    #   "off"     -> no-op, byte-identical to today (sizes off the regime fallback).
    #   "observe" -> DEFAULT. Byte-identical behaviour (still trades), but EMIT a
    #                structured counter [momentum_wt1] would_abstain=... so the operator
    #                can read the intraday firing frequency before enforcing.
    #   "enforce" -> ABSTAIN: a thin frame (no live expected_move) -> no-trade, mirroring
    #                the _enforce_ross_price_band no-data-no-trade fail-safe. (Capital
    #                protection: the stop would otherwise be a blind regime-default guess.)
    # Default "observe" => no behaviour change at the open, just measurable. The wider-stop
    # variant is deferred; enforce is the safe terminal state. (live_runner.py / paper_execution.py)
    chili_momentum_require_live_atr_for_entry: str = Field(
        default="observe",
        validation_alias=AliasChoices("CHILI_MOMENTUM_REQUIRE_LIVE_ATR_FOR_ENTRY"),
        description="3-state thin-frame stop guard: 'off' (no-op, byte-identical, sizes off regime ATR fallback) | 'observe' (DEFAULT: byte-identical + emit [momentum_wt1] would_abstain counter) | 'enforce' (ABSTAIN when no live expected_move — no-data-no-trade fail-safe vs a noise-tight regime-default stop on a gappy low-float). Default observe => no behaviour change, measurable.",
    )
    chili_momentum_entry_require_vwap_hold: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_REQUIRE_VWAP_HOLD"),
        description="Require price to hold above session VWAP at entry (Ross stays long above VWAP).",
    )
    chili_momentum_entry_vwap_hold_buffer: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_VWAP_HOLD_BUFFER"),
        description="Tolerance below VWAP still treated as a hold (0 = strict).",
    )
    chili_momentum_entry_require_macd_bullish: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_REQUIRE_MACD_BULLISH"),
        description="Require MACD momentum confirmation (histogram >= 0 OR macd line >= signal); lenient, lagging-safe.",
    )
    # Ross topping-tail runner exit: lock the runner (post first-target scale-out) on
    # an exhaustion / upper-wick rejection candle instead of waiting for the trail stop.
    chili_momentum_exit_topping_tail_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_TOPPING_TAIL_ENABLED"),
        description="Exit the TRAILING runner on a topping-tail/shooting-star rejection candle.",
    )
    # Adaptive order-flow EXHAUSTION LOCK (crypto runner). The cushion trail band
    # is loose by design on an extended runner (~800bps at +1.9R into a 3R plan);
    # MEGA-USD peaked +1.9R, never reached the 3R partial (partial_taken stayed
    # False, runner floored at the loss-side stop), and bled the peak back inside
    # the band that never triggered. This lock fires an adaptive, FLOW-CONFIRMED
    # tighten BEFORE the fixed target when live OFI + micro-price say the thrust
    # is exhausting (the sign-mirror of the entry OFI tilt). Crypto-only; equity
    # exit byte-identical (the live caller hard-gates on `-USD`). Ratchet-only
    # over the structural stop (never loosens). The A/B counterfactual (fixed-R:R
    # candidate stop, lock OFF) is logged on EVERY armed tick so the realized-PnL
    # delta vs the baseline is measured LIVE before the partial moves size.
    chili_momentum_exit_ofi_lock_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_OFI_LOCK_ENABLED"),
        description="Master gate (kill-switch #1) for the crypto order-flow exhaustion lock. false = exact legacy cushion trail + fixed target; the A/B counterfactual still logs.",
    )
    # HARD MAX-LOSS-PER-TRADE CIRCUIT (#1 profitability lever, 2026-06-17). An 87-fill
    # audit found the lane net -$157.68 BUT the entire deficit is a -$697.76 tail of 4
    # RH low-float names (MTEN -896bps, SDOT -729bps, CCTG -893bps, CAST -497bps) that
    # GAPPED 5-9% THROUGH their tight structural stops and got a deep market-exit fill.
    # The circuit caps each trade's loss at K x the position's REALIZED structural risk
    # (stop_distance x qty — NOT the frozen risk_usd budget, which overstates ~12x) and
    # flattens at an ABSOLUTE loss-anchored limit (avg - K*stop_distance, place_limit_
    # order_gtc, no repeg). Because the floor is anchored to entry+structural-risk (not a
    # falling bid), a 9%-deep fill is mechanically impossible. RH-equity-first (where the
    # tail lives); crypto may still fire but keeps the bid-relative ladder (dust, 24/7,
    # no LULD). false = byte-identical legacy exits.
    chili_momentum_max_loss_circuit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_LOSS_CIRCUIT_ENABLED"),
        description="Kill-switch for the hard max-loss-per-trade circuit. false = exact legacy ladder/stop bailouts (byte-identical).",
    )
    chili_momentum_max_loss_risk_multiple: float = Field(
        default=2.0,
        ge=1.0,
        le=6.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_LOSS_RISK_MULTIPLE"),
        description="K — the circuit fires when unrealized loss <= -(K x structural_risk) and flattens at avg - K*stop_distance. ge=1.0 so the floor can never sit looser than the structural stop.",
    )
    # ── C1 PER-TRADE MAX-LOSS FRESH-QUOTE GUARD (2026-06-30, PULLBACK-SCALP-ENABLE) ──
    # The C1 1x per-trade max-loss check (unrealized_pnl <= -max_loss_per_trade_usd) had NO
    # fresh-quote guard (unlike the C1b #769 circuit just below it). A torn/stale/zero bid
    # (bid = float(tick.bid or mid) — falls back to a stale mid) trips a SPURIOUS full
    # liquidation on a phantom unrealized loss while the real NBBO is fine (CELZ session 9920
    # force-exited on a phantom unrealized=-$148 while the real bid was >= $4.22 / +18%). This
    # flag adds the SAME fresh-quote predicate C1b uses (finite bid > 0, halt_stale_streak==0,
    # no suspected_halt_since_utc): when ON and the quote is NOT fresh, SKIP C1 this pulse (the
    # structural stop + the fresh-guarded C1b still protect; the next fresh tick re-checks). A
    # genuine -max_loss on a FRESH bid still fires C1 immediately. Flag OFF = byte-identical
    # (C1 fires regardless of freshness).
    chili_momentum_max_loss_fresh_quote_guard_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_LOSS_FRESH_QUOTE_GUARD_ENABLED"),
        description="Kill-switch for the fresh-quote guard on the C1 per-trade max-loss check. true (default) = SKIP the C1 force-exit on a provably stale/torn/zero bid (reuses the C1b fresh-quote predicate: finite bid>0, halt_stale_streak==0, no suspected_halt) AND on an IQFeed tick-level NBBO cross-check (in-process bid materially below a fresh momentum_nbbo_spread_tape bid => phantom loss, skip); a genuine -max_loss on a FRESH bid still fires immediately. false = byte-identical legacy (C1 fires regardless of quote freshness). The #769 max-loss circuit + structural stop are untouched.",
    )
    # C1 IQFeed phantom-loss cross-check (2026-06-30): on the C1-trigger path ONLY, the
    # in-process bid is compared to the freshest IQFeed NBBO mirrored into
    # momentum_nbbo_spread_tape. The divergence tolerance is ADAPTIVE — a multiple of the
    # name's OWN recent median spread (no fixed bps clock); the multiple is the only knob.
    chili_momentum_max_loss_phantom_divergence_spread_mult: float = Field(
        default=3.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_LOSS_PHANTOM_DIVERGENCE_SPREAD_MULT"),
        description="C1 IQFeed cross-check: the in-process bid must sit below the fresh IQFeed/tape bid by MORE than (this multiple x the name's recent median spread_bps) to be flagged a PHANTOM loss (=> skip C1). Adaptive (name-relative); ge=1.0 so the tolerance is never tighter than one typical spread.",
    )
    # The ONE documented fallback divergence tolerance, used ONLY when the recent tape
    # spread is unavailable (thin/absent L1) so there is no name-relative scale to adapt to.
    chili_momentum_max_loss_phantom_divergence_fallback_bps: float = Field(
        default=100.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_LOSS_PHANTOM_DIVERGENCE_FALLBACK_BPS"),
        description="C1 IQFeed cross-check fallback: the divergence tolerance (bps below the fresh tape bid) used ONLY when the recent tape median spread is unavailable. The single documented base — adaptive name-relative scale is preferred when present.",
    )
    # ── EARLY TRAIL-ARM (2026-06-30, PULLBACK-SCALP-ENABLE) ──
    # The 4 add/reload paths (pyramid_add, micropullback_reentry, pullback_add, flag_breakout_add)
    # are ALL gated on st == STATE_LIVE_TRAILING. A fresh fill lands in STATE_LIVE_ENTERED; the
    # ENTERED-only no-confirmation bailouts (instant_bid_above_fill_unconfirmed, bail_on_no_
    # confirmation) run FIRST each tick and return early, so a normal entry goes ENTERED ->
    # BAILOUT -> recycle and NEVER reaches TRAILING => 0 adds (the Ross-style ride+add / micro-
    # reentry path is structurally unreachable). When ON, a CONFIRMED thrust (bid >= avg *
    # trail_activate_return, i.e. already in profit above the adaptive activation band) arms
    # TRAILING BEFORE those bailouts can cut — opening the add path. ANTI-REGRESSION: arms ONLY
    # when bid >= avg*trail_activate_return (already in profit); a position at/below entry is
    # untouched and STILL gets the no-confirmation cut. Uses the existing adaptive
    # params["trail_activate_return_bps"] (no magic numbers). Flag OFF = byte-identical (trail
    # arms only at its current post-bailout site).
    chili_momentum_early_trail_arm_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EARLY_TRAIL_ARM_ENABLED"),
        description="Kill-switch for arming STATE_LIVE_TRAILING on the adaptive price-rise threshold (bid >= avg*trail_activate_return) BEFORE the ENTERED no-confirmation bailouts. true (default) = a confirmed front-side runner reaches TRAILING and opens the ride+add / micro-reentry path (arms only when already in profit; a loser at/below entry still gets the no-confirmation cut). false = byte-identical (trail arms only at its post-bailout site). Reuses the adaptive trail_activate_return_bps; the structural stop + #769 circuit are untouched.",
    )
    # ── ADAPTIVE SPREAD-COST VETO/DERATE (2026-06-27, DEFAULT OFF = byte-identical) ──
    # Judges the live entry spread RELATIVE to (a) the name's OWN recent typical spread
    # (rolling p50/p75/p90 over its momentum_nbbo_spread_tape history) and (b) the trade's
    # expected reward (round-trip spread cost as a fraction of the structural risk R =
    # stop_distance). NEVER a flat bps bar — Ross low-float movers inherently trade wide
    # spreads (PAVS 317bps is the real market, not a bug; project_momentum_zero_fills_root_
    # cause). A flat spread veto re-creates the documented 0-fills over-restriction. So this
    # DERATES (sizes down) for moderate anomaly/cost and HARD-VETOES only at the extreme
    # (an EXTREME outlier vs the name's OWN p90 AND the cost eats > max fraction of R). A
    # wide-but-TYPICAL low-float spread with a good R PASSES unaffected. Flag OFF = the
    # sizing path is byte-identical (the derate function returns (True, 1.0) pass-through).
    chili_momentum_adaptive_spread_cost_veto_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_SPREAD_COST_VETO_ENABLED"),
        description="Kill-switch for the adaptive spread-cost veto/derate at entry sizing. DEFAULT FALSE = byte-identical (no new gate/derate). When ON: judge the live spread vs the name's OWN rolling spread distribution + vs expected-R; graceful size-down for moderate cases, hard veto only at the extreme. Adaptive (name-relative + R-relative), no flat bps.",
    )
    chili_momentum_spread_cost_max_fraction_of_r: float = Field(
        default=0.25,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_COST_MAX_FRACTION_OF_R"),
        description="ONE documented base: the max fraction of the structural risk R (= stop_distance) the round-trip spread cost may consume. Above this the spread is eating the edge; combined with an extreme name-relative anomaly it hard-vetoes, otherwise it size-derates toward the floor. Adaptive (R-relative), not a flat $ or bps.",
    )
    chili_momentum_spread_cost_reclaim_max_fraction_of_r: float = Field(
        default=0.35,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_COST_RECLAIM_MAX_FRACTION_OF_R"),
        description="RECLAIM CARVE-OUT — the ONE documented MORE-PERMISSIVE max fraction of the structural risk R the round-trip spread may consume when the entry-trigger is a RECLAIM/dip family (dip_buy / vwap_reclaim / flush_dip / deep_reclaim / wick_reclaim / bounce / curl). A reclaim fires at the widest-spread / thinnest-book moment by construction (the other entry gates already carve it out), so it gets a looser R base (default 0.35 vs the non-reclaim 0.25) AND is DERATE-ONLY (never hard-veto). Non-reclaim entries use chili_momentum_spread_cost_max_fraction_of_r unchanged. Adaptive (R-relative), not a flat $ or bps.",
    )
    chili_momentum_spread_anomaly_p50_mult: float = Field(
        default=2.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_ANOMALY_P50_MULT"),
        description="ONE documented base: a live spread >= this multiple of the name's OWN rolling median (p50) is 'anomalously wide FOR IT' and triggers the graceful size-down. Name-relative so a chronically-wide low-float name (judged vs its own norm) is NOT penalised for its baseline width.",
    )
    chili_momentum_spread_anomaly_extreme_p90_mult: float = Field(
        default=1.5,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_ANOMALY_EXTREME_P90_MULT"),
        description="Extra multiple beyond the name's OWN p90 that defines an EXTREME outlier (e.g. 1.5x the p90). A HARD VETO requires BOTH this extreme name-relative anomaly AND cost > max_fraction_of_r. Name-relative; thin-history names (no distribution) can never reach this -> never hard-vetoed on thin data.",
    )
    chili_momentum_spread_cost_derate_floor: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_COST_DERATE_FLOOR"),
        description="Floor for the spread-cost size-down multiplier — the derate never zeroes the size (preserves the explosive tail; size, not rejection, is the lever). Composes multiplicatively with the other size-down levers under the same 3x clamp.",
    )
    chili_momentum_spread_cost_derate_engage_frac: float = Field(
        default=0.5,
        ge=0.0,
        lt=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_COST_DERATE_ENGAGE_FRAC"),
        description="Dead-band: the cost-of-R size-down only ENGAGES once the round-trip spread cost reaches this fraction of the max-fraction-of-R cap (e.g. 0.5 = the upper half). Below it, a cheap-vs-R spread passes at mult=1.0 — this is what keeps a tight/typical low-float spread from over-restricting (the no-0-fills guarantee). Then linear to the floor at the cap.",
    )
    chili_momentum_spread_norm_lookback_days: float = Field(
        default=20.0,
        gt=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_NORM_LOOKBACK_DAYS"),
        description="Rolling window (trading days) of the name's OWN momentum_nbbo_spread_tape history used to compute its typical-spread distribution (p50/p75/p90). Self-relative per symbol; reuses the deployed tape (no new table).",
    )
    # ── FULL-CLOCK WINDOW (2026-06-25): two staged tiers ─────────────────────────
    # TIER 1 — EARLY PREMARKET (low risk, DEFAULT-ON). Adaptive pre-entry-window unlock:
    # the entry window opens at the FIRST-MOVER tape time once >=N spread-clean names move
    # >=5% in a fresh M-min window, instead of waiting for the fixed premarket_start clock.
    # Same extended session, same names, same size, same broker routing — only removes the
    # selection lag (FCUV ignited 04:23 ET but CHILI watched it at 07:00). Companion sampler-
    # pull guarantees the tape reaches the 04:00 exchange-open floor so it is warm to drive
    # the unlock. Flag OFF = byte-identical (fixed premarket_start clock + lead-derived data open).
    chili_momentum_early_premarket_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EARLY_PREMARKET_ENABLED"),
        description="Tier 1: adaptive pre-premarket-start entry unlock (tape-derived first-mover time) + pull the NBBO sampler open to the 04:00 ET exchange-extended-open floor. Low-risk (same session/names/size/routing). false = fixed-clock premarket_start + lead-derived data open (byte-identical).",
    )
    chili_momentum_early_premarket_min_movers: int = Field(
        default=3,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EARLY_PREMARKET_MIN_MOVERS"),
        description="Distinct spread-clean tape movers (>= the 5% _MIN_ABS_CHANGE_PCT floor, freshness <30s) within the lookback window required to UNLOCK the early-premarket entry window. Mitigates a false unlock on stale/garbage tape.",
    )
    chili_momentum_early_premarket_window_min: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EARLY_PREMARKET_WINDOW_MIN"),
        description="Lookback minutes for the early-premarket unlock test (move% reuses the existing 5% NBBO-tape floor; row freshness reuses 30s).",
    )
    # TIER 2 — OVERNIGHT / 24H (higher risk, GATED, operator flips after review). NOTE
    # (conflict with the no-dark-flags norm): this is the ONE justified default-OFF flag —
    # it changes broker routing (RH all_day_hours), carries irreducible overnight gap risk
    # (no broker-side stop), and has no quant cohort yet. Operator flips after reviewing the
    # overnight-safety design. Flag OFF = extended_hours routing only, no overnight (today).
    chili_momentum_overnight_trading_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERNIGHT_TRADING_ENABLED"),
        description="Tier 2 MASTER gate: allow overnight/24h arming+entry for 24h-ELIGIBLE, 24h-LIQUID names (RH all_day_hours routing). DEFAULT FALSE (higher risk: no broker stop overnight, thin books). false = no overnight, extended_hours routing only.",
    )
    chili_momentum_overnight_tape_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERNIGHT_TAPE_ENABLED"),
        description="Tier 2: sample the NBBO tape overnight for the 24h-eligible whitelist only (bounds DB growth). DEFAULT FALSE. false = overnight arming falls back to the last extended-hours tape row + a live quote probe.",
    )
    chili_momentum_overnight_size_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERNIGHT_SIZE_FRACTION"),
        description="Overnight risk-size multiplier on the equity-relative notional (composes with the other size-down levers under the 3x clamp). Adaptive, no fixed $. 1.0 = no reduction.",
    )
    chili_momentum_overnight_max_loss_pct_bp: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERNIGHT_MAX_LOSS_PCT_BP"),
        description="Overnight per-trade max-loss cap basis = max($50 irreducible base, this % of overnight buying power) — equity-relative, feeds caps.max_loss_per_trade_usd overnight. 0 = use the irreducible $50 base only.",
    )
    chili_momentum_overnight_min_dollar_volume: float = Field(
        default=5_000_000.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERNIGHT_MIN_DOLLAR_VOLUME"),
        description="Overnight 24h-LIQUID floor (dollar-volume) = max($5M, 2x the RTH $1M floor). Thin overnight books are the gap risk, so only deep names arm overnight.",
    )
    chili_momentum_overnight_max_stale_sec: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERNIGHT_MAX_STALE_SEC"),
        description="Price-bus-dark trigger (seconds): if a position is held overnight and the quote is stale longer than this, emit critical + attempt a flatten at the next fresh tick + arm nothing new. 0 = derive from the halt-stale threshold (chili_momentum_halt_stale_ticks x tick cadence).",
    )
    # OVERNIGHT DARK-BUS-SAFE FLATTEN (2026-06-25, FIX A — the GATE for overnight ON).
    # Adversarial finding: overnight_flatten_on_fresh was SET on a dark overnight book
    # but NEVER READ, and the per-trade loss circuit / stop fire ONLY on a fresh quote
    # (halt_stale_streak==0). So a fully DARK overnight price-bus (no ticks) left an
    # overnight position NAKED — no software stop fired and RH has no overnight stop;
    # a gap-down on resume filled THROUGH any intended stop. This flag turns on the
    # dark-bus-safe flatten: (1) honor overnight_flatten_on_fresh on the next fresh
    # tick, AND (2) PROACTIVELY flatten at the FIRST onset of stale/dark on an
    # overnight-held position (flatten at the last good tick while we still can — a
    # dark bus delivers NO fresh tick, so on-fresh alone is insufficient). Both route
    # through the existing operator-flatten chokepoint (cancel/clamp/place/confirm/
    # reconcile) — no oversell, no orphan. CONSERVATIVE PRINCIPLE: an overnight
    # position that cannot be protected by a working software-stop is FLATTENED, not
    # held naked. DEFAULT FALSE = current behavior (flag set, never read; no flatten).
    chili_momentum_overnight_dark_flatten_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERNIGHT_DARK_FLATTEN_ENABLED"),
        description="FIX A: flatten an OVERNIGHT-held position on price-bus-dark — proactively at the FIRST stale onset (last good tick) AND on the next fresh tick (honor overnight_flatten_on_fresh) — via the operator-flatten chokepoint. The dark-bus-safe gate for enabling overnight trading. false = legacy (flag set, never read; naked overnight risk).",
    )
    chili_momentum_overnight_dark_flatten_onset_ticks: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OVERNIGHT_DARK_FLATTEN_ONSET_TICKS"),
        description="FIX A proactive cutoff: stale-tick streak at which an overnight-held position is flattened at the last good tick (1 = first stale onset = most conservative; raise to tolerate brief overnight quote gaps before flattening).",
    )
    chili_momentum_tradability_cache_sec: int = Field(
        default=3600,
        ge=0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TRADABILITY_CACHE_SEC"),
        description="TTL of the get_equity_tradability 24h-eligibility cache (eligibility is an instrument property that changes slowly — re-checked hourly + next day).",
    )
    # MIDDAY-LULL ENTRY DE-WEIGHT (2026-06-17, project_profitability_levers): raise the
    # EFFECTIVE entry viability bar for NEW equity entries during the EXISTING schedule_window_now
    # "midday" window (10:30-14:30 ET) — live data: midday win-rate 1/17 = 6% vs morning 7/24 =
    # 29%, binomial P~0.02; Ross Cameron explicitly sits out midday. ORTHOGONAL to the existing
    # midday 0.5x SIZE multiplier (live_runner day-cushion ladder): that halves SIZE on midday
    # entries that happen anyway; THIS raises the ADMISSION bar so fewer marginal ones enter at
    # all (a 6%-win cohort is better skipped than half-sized). SOFT additive bump (not a ban) so a
    # 0.70+ exceptional mover still arms — it filters the 0.52-0.62 marginal setups the 78-min-hold
    # chop-bleed losers came from. ENTRY/ARM-side ONLY — NEVER touches exits. Crypto exempt
    # (in_midday_lull -> False for -USD). DST-correct (reuses schedule_window_now's America/New_York
    # clock — ONE canonical window, no new magic bound). Separate from the #769 max-loss circuit:
    # that bounds loss MAGNITUDE after entry (the morning gap-through tail); this prevents the
    # midday ENTRY. enabled=False OR bump<=0 => byte-identical.
    chili_momentum_midday_deweight_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MIDDAY_DEWEIGHT_ENABLED"),
        description="Kill-switch for the 1030-1400 ET midday entry de-weight (equity-only, entry-side). false = byte-identical (no bar raise, no emit).",
    )
    chili_momentum_midday_viability_bump: float = Field(
        default=0.05,
        ge=0.0,
        le=0.30,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MIDDAY_VIABILITY_BUMP"),
        description="Additive raise to entry_viability_min during the midday lull. ~0.05 filters marginal 0.52-0.62 setups while a 0.70+ exceptional mover still arms. <=0 == off. Keep <=0.10 (>=0.15 approaches a de-facto midday ban).",
    )
    # MACRO RUN-R BREAKER (L2.1, project_profitability_levers): the 2026-06-22 loss
    # decomposition (wf w6c11y2s9) found the loss is 65-83% MESO — the lane buys the top
    # of a leg / late extension with no follow-through. This breaker is the cheapest MACRO
    # guard: when the lane's recent realized-R turns negative AND worse than its OWN baseline
    # (a no-follow-through regime), SOFT-raise the entry bar so fewer marginal setups arm.
    # RELATIVE + graduated => releases the moment the recent stretch recovers (never a hard
    # freeze, unlike an absolute floor). Entry-side ONLY; never blocks exits. OFF = byte-identical.
    chili_momentum_run_r_breaker_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUN_R_BREAKER_ENABLED"),
        description="Kill-switch for the MACRO run-R breaker. false = byte-identical (no bump, no query, no emit).",
    )
    chili_momentum_run_r_breaker_viability_bump: float = Field(
        default=0.05,
        ge=0.0,
        le=0.30,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUN_R_BREAKER_VIABILITY_BUMP"),
        description="Additive raise to entry_viability_min when the run-R breaker triggers (mirrors the midday bump; clamped to the 0.95 ceiling so an exceptional mover still arms). <=0 == off.",
    )
    chili_momentum_run_r_breaker_lookback: int = Field(
        default=40,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUN_R_BREAKER_LOOKBACK"),
        description="How many recent closed live momentum fills (per execution family) form the run-R baseline window.",
    )
    chili_momentum_run_r_breaker_short_window: int = Field(
        default=10,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUN_R_BREAKER_SHORT_WINDOW"),
        description="The recent sub-window whose mean realized-R is compared against the full-lookback baseline; the breaker triggers only when this recent stretch is BOTH negative and below baseline, so it releases when performance recovers (never a permanent freeze).",
    )
    chili_momentum_run_r_breaker_min_history: int = Field(
        default=8,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUN_R_BREAKER_MIN_HISTORY"),
        description="Minimum closed-fill history before the breaker can trigger; below this it fails OPEN (bump 0).",
    )
    # WAVE-1 FIX-5 (B4) STOPS-ONLY-TIGHTEN INVARIANT-A. A defensive stop-tighten
    # (the C4 viability-degradation tighten) writes pos["stop_price"] but the
    # once-per-tick cached `stop_px` local was NOT refreshed, so the later trailing
    # chandelier composed its candidate against the STALE (looser) base and could
    # LOWER a just-tightened stop within the same tick (IREZ live-reproduced: tighten
    # 10.45745 -> loosened to 10.43334 +36ms). The fix refreshes the local base after
    # every stop write AND composes each candidate against the LIVE pos["stop_price"].
    # This is a pure INVARIANT-A repair (ratchet-only is already the documented
    # contract) — the flag exists ONLY for instant rollback, not as a dark gate.
    chili_momentum_stop_ratchet_strict_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STOP_RATCHET_STRICT_ENABLED"),
        description="WAVE-1 FIX-5: strict INVARIANT-A — a long position's stop may NEVER decrease within a tick; refresh the cached local base after each write and compose every candidate against the LIVE pos['stop_price']. false = legacy (stale-base) behavior for rollback only.",
    )
    # WAVE-1 FIX-7 SCORE-FLOOR RAISE-ONLY INTEGRITY. The entry viability floor is
    # composed as min(0.95, flat_min + midday_bump + run_r_bump) — every risk factor
    # RAISES the bar, none lowers it. This flag hardens that as an INVARIANT: after all
    # bumps, the effective floor is clamped to be NEVER below (flat_min + the applied
    # raises), so no future override / min() inserted between the bump and the gate can
    # silently lower the run-R-raised bar (the codex ross_audio_starter class of bug,
    # which is NOT present on main). On main this is a no-op today (composition already
    # raise-only) — the flag guards against regression on merge. false = no guard.
    chili_momentum_floor_raise_only_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLOOR_RAISE_ONLY_ENABLED"),
        description="WAVE-1 FIX-7: enforce the entry viability floor is raise-only — the effective floor may never fall below flat_min plus the applied midday + run-R raises. false = no raise-only clamp (rollback only).",
    )
    # ── BATCH E ─ management/discipline gaps (each kill-switched; OFF = byte-identical) ──
    #
    # E(1) MULTI-LEVEL SCALE-OUT GRID. Extends the single first-scale into a LADDER: sell
    # successive tranche fractions at successive R-multiple/round-number targets, trail the
    # remainder above breakeven. Routes through the EXISTING single scale-out chokepoint
    # (_apply_confirmed_live_partial_exit + the scale_limit_order_id interlock) — NO new
    # decrement path; each tranche clamps to the remaining held qty (scale_out_quantity); the
    # SUM of fractions is < 1.0 so a runner always remains. INVARIANT-A (ratchet stop) is
    # preserved (breakeven move unchanged). OFF => the lane takes ONE scale-out then trails
    # (today's behavior, byte-identical). docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_scale_grid_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SCALE_GRID_ENABLED"),
        description="Kill-switch for the multi-level scale-out grid (E1). false = single first-scale then trail (byte-identical).",
    )
    chili_momentum_scale_grid_fractions: str = Field(
        default="0.5,0.25",
        validation_alias=AliasChoices("CHILI_MOMENTUM_SCALE_GRID_FRACTIONS"),
        description="ONE documented base: comma-separated tranche fractions of the ORIGINAL position sold at successive ladder targets (1R, 2R, ...). The runner = 1 - sum; the SUM is clamped < 1.0 so a runner always remains (no oversell, never strand 0 shares). e.g. '0.5,0.25' -> half at 1R, quarter at 2R, quarter runs.",
    )
    chili_momentum_scale_grid_r_multiples: str = Field(
        default="1.0,2.0",
        validation_alias=AliasChoices("CHILI_MOMENTUM_SCALE_GRID_R_MULTIPLES"),
        description="Comma-separated R-multiples for the ladder targets (reward = R x stop-distance). Paired positionally with the fractions; a round number above entry that sits below the next R level pulls that tranche IN (Ross sells into the level where sellers stack). Adaptive: levels are R-multiples / the existing round-number grid, no fixed $.",
    )
    # ── MEASURED-MOVE SCALE TARGET + DOUBLE-TOP EXHAUSTION (winner-management).
    # WINNER-SAFE / RATCHET-ONLY. (1) Measure the name's OWN first impulse leg
    # (impulse_leg_high − entry, frozen at first-target scale-out) and project it
    # ABOVE the impulse high to a measured-move target; at the target SCALE OUT a
    # fraction (reuse the partial machinery) + ratchet the runner stop up — a
    # PARTIAL, never a full cut (a strong runner that blows through keeps running on
    # the cushion/chandelier trail). (2) Double-top: a lower-high RETEST of the
    # impulse high inside an ATR-relative band that's REJECTED ⇒ tighten the stop /
    # arm a partial; a clean higher-high ⇒ no exhaustion exit. Adaptive (name's own
    # leg height + ATR-relative band); ONE documented base each (the scale-out
    # fraction + the double-top retest ATR-mult). OFF (default) ⇒ both helpers are
    # inert pass-throughs and the runner trails EXACTLY as before (byte-identical).
    # docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_measured_move_exit_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MEASURED_MOVE_EXIT_ENABLED"),
        description="Kill-switch for the measured-move scale target + double-top exhaustion tighten (winner-management). false = inert no-op; the runner trails byte-identical.",
    )
    chili_momentum_measured_move_exit_scale_fraction: float = Field(
        default=0.33,
        gt=0.0,
        lt=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MEASURED_MOVE_EXIT_SCALE_FRACTION"),
        description="ONE documented base: fraction of the ORIGINAL position SCALED OUT into the measured-move target (the second equal leg). A partial — the remainder runs on the trail. Bounded (0,1) so it can never sell 0% (no-op) or 100% (no runner).",
    )
    chili_momentum_measured_move_exit_double_top_atr_mult: float = Field(
        default=0.75,
        gt=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MEASURED_MOVE_EXIT_DOUBLE_TOP_ATR_MULT"),
        description="ONE documented base: the double-top retest tolerance as a multiple of the position's OWN ATR risk unit (adaptive, not a fixed %). A lower-high retest within this ATR-relative band of the impulse high (and rejected) = double-top exhaustion.",
    )
    # E(2) WIN-CYCLE FATIGUE (entries-only). Tracks today's CLEAN WINS (live, per execution
    # family). After a YELLOW count of wins, NEW entries size DOWN by a fraction (mirrors the
    # streak/cushion size-down multipliers — composes under the same 3x clamp); after a RED
    # count, NEW entries are HALTED for the session (mirrors the profit-goal cap early-out).
    # ENTRIES ONLY — never blocks/delays an exit, stop, trail, bailout, scale-out, or dark-
    # flatten on an OPEN position. Adaptive: counts are derived from realized wins; the YELLOW
    # down-size never zeroes (preserves the explosive tail). OFF => no count, no down-size,
    # no halt (byte-identical). docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_win_cycle_fatigue_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WIN_CYCLE_FATIGUE_ENABLED"),
        description="Kill-switch for win-cycle fatigue (E2, entries-only). false = no win count, no YELLOW down-size, no RED halt (byte-identical).",
    )
    chili_momentum_win_cycle_yellow_wins: int = Field(
        default=4,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WIN_CYCLE_YELLOW_WINS"),
        description="ONE documented knob: clean wins today (per family) at/above which NEW entries size DOWN (YELLOW). Below it, no effect.",
    )
    chili_momentum_win_cycle_red_wins: int = Field(
        default=7,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WIN_CYCLE_RED_WINS"),
        description="Clean wins today (per family) at/above which NEW entries HALT for the session (RED) — lock in the green day. Exits/management unaffected. >= yellow_wins (clamped up if misconfigured).",
    )
    chili_momentum_win_cycle_yellow_size_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WIN_CYCLE_YELLOW_SIZE_FRACTION"),
        description="YELLOW down-size multiplier on the per-trade risk budget (composes with the streak/cushion/liquidity levers under the 3x clamp). Never zeroes (>0) so an exceptional setup still takes a (smaller) position.",
    )
    # ── P2 PER-SYMBOL ATTEMPT FATIGUE (entries-only). A per-session per-symbol live entry-
    # attempt counter: DERATE the borderline last allowed attempt (YELLOW down-size) then VETO
    # the Nth+ attempt on the SAME ticker today (Ross: stop trading a symbol after ~N tries).
    # ENTRIES ONLY — held positions NEVER consult it; every exit/stop/scale-out/bailout stays
    # allowed (the veto is a pre-position arm-gate skip; the down-size is at entry-fill sizing).
    # OFF => no count, no down-size, no veto (byte-identical). docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_per_symbol_fatigue_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PER_SYMBOL_FATIGUE_ENABLED"),
        description="Kill-switch for per-symbol attempt fatigue (P2, entries-only). false = no per-symbol attempt count, no YELLOW down-size, no RED veto (byte-identical).",
    )
    chili_momentum_per_symbol_max_attempts: int = Field(
        default=3,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PER_SYMBOL_MAX_ATTEMPTS"),
        description="ONE documented knob (default 3 per Ross): live entry attempts on the SAME ticker today at/above which a NEW entry is VETOED (RED). The attempt just below it is a YELLOW down-size. Clamped >= 2 (one allowed attempt before a down-size + a veto).",
    )
    chili_momentum_per_symbol_yellow_size_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PER_SYMBOL_YELLOW_SIZE_FRACTION"),
        description="YELLOW per-symbol down-size multiplier on the per-trade risk budget for the borderline last allowed attempt (composes with the streak/cushion/liquidity/win-cycle levers under the 3x clamp). Never zeroes (>0).",
    )
    # ── P3 HOT/COLD-TAPE SIZE SCALING (entries-only sizing). A bounded size multiplier composed
    # MULTIPLICATIVELY with the streak/cushion/liquidity levers under the same 3x clamp: size UP
    # on a hot/explosive tape, DOWN on a cold one. Scales the per-trade RISK BUDGET only — the
    # liquidity cap + equity-relative notional ceiling stay HARD caps (qty is capped at
    # max_notional downstream), so this can never push notional past any cap. The hot/cold read
    # reuses the SAME explosive ATR/RVOL floors entry_gates._is_hot_tape uses (no new magic).
    # OFF / fail-neutral => 1.0 (byte-identical). docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_hot_cold_size_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_COLD_SIZE_ENABLED"),
        description="Hot/cold-tape size scaling (P3, entry sizing only). DEFAULT-ON (no dark flags); never a gate — cold tape sizes DOWN (safe direction), hot tape sizes UP bounded [cold_floor, hot_ceil] and never past the liquidity/equity caps or the 3x clamp. Kill-switch via env=0 => multiplier always 1.0 (byte-identical).",
    )
    chili_momentum_hot_cold_cold_floor: float = Field(
        default=0.6,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_COLD_COLD_FLOOR"),
        description="One documented bound: the size multiplier on a COLD (non-explosive) tape — size DOWN to this fraction of the risk budget. In (0,1].",
    )
    chili_momentum_hot_cold_hot_ceil: float = Field(
        default=1.5,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HOT_COLD_HOT_CEIL"),
        description="One documented bound: the size multiplier on a HOT (explosive) tape — size UP to this fraction of the risk budget. >= 1.0; kept well under the 3x combined clamp so the other levers retain room.",
    )
    # E(3) HARD NO-TRADE REGIMES (entries-only). A hard no-NEW-ENTRY standdown window around
    # scheduled high-impact events (FOMC/CPI), and an OPTIONAL hard midday no-entry window
    # (default OFF — the existing SOFT midday de-weight stays in charge unless this hard flag
    # is on). ENTRIES ONLY — never blocks/delays an exit, stop, trail, bailout, scale-out, or
    # dark-flatten on an OPEN position. OFF => no hard standdown (byte-identical; the soft
    # midday de-weight is untouched). docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_hard_no_trade_regime_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HARD_NO_TRADE_REGIME_ENABLED"),
        description="Kill-switch for hard no-trade regimes (E3, entries-only). false = no event standdown, no hard midday window (byte-identical).",
    )
    chili_momentum_hard_no_trade_event_times_utc: str = Field(
        default="",
        validation_alias=AliasChoices("CHILI_MOMENTUM_HARD_NO_TRADE_EVENT_TIMES_UTC"),
        description="Comma-separated ISO-8601 UTC datetimes of scheduled high-impact events (FOMC/CPI), e.g. '2026-06-18T18:00:00Z,2026-07-15T12:30:00Z'. A small documented list; a calendar hook can populate it. Empty = no event standdown.",
    )
    chili_momentum_hard_no_trade_event_window_min: float = Field(
        default=30.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HARD_NO_TRADE_EVENT_WINDOW_MIN"),
        description="ONE documented knob: minutes BEFORE and AFTER each scheduled event during which NEW entries are halted (+/- window). Exits/management unaffected.",
    )
    chili_momentum_hard_no_trade_midday_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HARD_NO_TRADE_MIDDAY_ENABLED"),
        description="Optional HARD midday no-NEW-ENTRY window (reuses the SAME 10:30-14:30 ET in_midday_lull band as the soft de-weight). Default OFF: the soft de-weight stays in charge. Equity-only; exits/management unaffected.",
    )
    # L2.2 LIQUIDITY-SCALED RISK CAP (project_profitability_levers): shrink per-trade RISK
    # as the live spread eats the name's adaptive tolerance — wide-spread/illiquid names
    # (the −$697 low-float tail; QXL −$229 @119bps) get SIZED DOWN, never rejected (cuts the
    # loser tail without killing trades/winners — the surgical fix the L3 entry filter wasn't).
    chili_momentum_liquidity_risk_cap_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIQUIDITY_RISK_CAP_ENABLED"),
        description="Kill-switch for the liquidity-scaled per-trade risk cap. false = mult 1.0 = byte-identical sizing.",
    )
    chili_momentum_liquidity_risk_floor: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIQUIDITY_RISK_FLOOR"),
        description="Max risk shrink for the widest-spread admitted name (mult floor; 0.5 = at most half-size). The one documented base; the spread tolerance itself is adaptive (no magic).",
    )
    # REPLAY→LIVE sizing parity ("walang mintis"): pin the replay's equity sizing basis (USD).
    # 0 = read the live agentic account equity (the SAME source the live lane sizes off);
    # >0 pins it (deterministic A/B + a local run without the broker token). Falls back to
    # the replay's fixed BASIS_USD only if both are unavailable.
    chili_replay_equity_basis_usd: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_REPLAY_EQUITY_BASIS_USD"),
        description="Replay equity sizing basis in USD. 0 = use the live account equity (parity with live); >0 pins it.",
    )
    # RISK-NEUTRAL CONFIRMATION-PYRAMID (the one genuine scale-IN gap vs Ross). A single
    # ADD into an ALREADY-winning position (>1R banked) on confirmation (new HOD + OFI +
    # ratcheted trail), sized via the SAME risk-first machinery against a FRACTION of the
    # original R0, with the stop ratcheted to blended-breakeven and the #769 circuit
    # CLAMPED to R0 (max_loss_circuit_decision risk_anchor_usd) so the enlarged position's
    # worst-case loss stays <= the starter's original risk. RISK-ADDING => DEFAULT OFF;
    # must prove out on replay A/B + paper before any live flip (unlike #770). Entry-side
    # only — never blocks/delays an exit; the stop can only tighten (INVARIANT-A).
    chili_momentum_pyramid_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PYRAMID_ENABLED"),
        description="2026-07-04 DEFAULT-ON: the risk-neutral confirmation-pyramid single-add — PRESS the winner (Ross laddering) instead of only trimming it. THE diagnosed gap: the accurate-FSM JEM histogram showed CHILI is TRIM-BIASED (sell_into_strength ×556, ofi_exhaustion_lock ×556) but NEVER adds (pyramid/pullback_add ×0) — so it shrinks a runner Ross would grow (JEM +$121 vs Ross +$46k). Risk-NEUTRAL by design: adds ONLY when cushion >= min_cushion_r*R0 (default 1.0R banked) AND new-HOD AND OFI thrust AND stop>=breakeven, and pyramid_blend re-bases the #769 circuit to the starter R0 so the ENLARGED worst-case stays <= R0 (INVARIANT-A; stop only tightens). FSM-validated net-positive (with live-representative OFI): JEM +$121.56->+$160.33 (+$38.77), CELZ +$100.48->+$127.73 (+$27.25). Kill-switch false = byte-identical (no add, no pos mutation, #769 anchor None == legacy).",
    )
    chili_momentum_pyramid_min_cushion_r: float = Field(
        default=1.0,
        ge=1.0,
        le=4.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PYRAMID_MIN_CUSHION_R"),
        description="Banked-cushion floor (in original-R0 units) required before an add; >=1.0 funds the add's risk from realized cushion so the enlarged worst-case stays <= R0. Adaptive off the frozen R0, no magic $.",
    )
    chili_momentum_pyramid_add_risk_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PYRAMID_ADD_RISK_FRACTION"),
        description="rho: the add's structural risk as a fraction of the original R0 (slippage/fee headroom; <1 keeps the add inside the banked cushion). Sized via compute_risk_first_quantity, never a hardcoded share block.",
    )
    chili_momentum_pyramid_max_adds: int = Field(
        default=1,
        ge=1,
        le=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PYRAMID_MAX_ADDS"),
        description="Documented small N of confirmation-adds per position (default single add).",
    )
    # ── MICRO-PULLBACK RE-ENTRY (Ross "scale out into the pop, re-load on the next
    # micro-pullback dip"). A SEPARATE sub-branch inside the pyramid block with its OWN
    # predicate, OWN counter (micropullback_reentry_count), OWN kill-switch. ADDITIVE:
    # when _enabled is False the block is a no-op — the #772 continuation-add is byte-
    # identical. EQUITY-FIRST (crypto deferred, _is_equity_pyr). Re-loads route through
    # pyramid_blend_on_fill + pyramid_risk_anchor_usd VERBATIM so the max-loss circuit
    # keeps re-basing to the STARTER R0 (worst-case add risk = max_reentries * fraction
    # * R0 ≈ 3 * 0.30 * R0 = 0.9*R0 on top of the starter). docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_micropullback_reentry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULLBACK_REENTRY_ENABLED"),
        description="Kill-switch for the Ross micro-pullback re-load (a bounded ADD on a held runner's dip-and-curl). false = byte-identical (the #772 pyramid add is unchanged; no re-load, no pos mutation, no emit).",
    )
    chili_momentum_micropullback_reentry_max: int = Field(
        default=3,
        ge=1,
        le=8,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULLBACK_REENTRY_MAX"),
        description="Per-name/per-session cap on micro-pullback re-loads (bounds total re-load risk = max * fraction * R0). Separate counter from pyramid_add_count for clean attribution.",
    )
    chili_momentum_micropullback_reentry_cooldown_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULLBACK_REENTRY_COOLDOWN_SECONDS"),
        description="Cooldown between re-loads. PINNED to the bar cadence: enforced >= 2 * micropull_bar_seconds (min 30s @ 15s bars) so one wiggle cannot fire two re-loads before the ratcheting shelf re-ratchets.",
    )
    chili_momentum_micropullback_reentry_risk_fraction: float = Field(
        default=0.30,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULLBACK_REENTRY_RISK_FRACTION"),
        description="rho_reload: each re-load's structural risk as a fraction of the STARTER R0. Sized via compute_risk_first_quantity (never a hardcoded block); 3 re-loads * 0.30 = 0.9*R0 worst-case on top of the starter.",
    )
    chili_momentum_micropullback_reentry_ofi_thr: float = Field(
        default=0.30,
        ge=-1.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULLBACK_REENTRY_OFI_THR"),
        description="Positive-confirm OFI floor for a re-load (book turning up). FAILS-CLOSED on None (an extra discretionary BUY needs proof). Required simultaneously with the trade_flow floor.",
    )
    chili_momentum_micropullback_reentry_trade_flow_thr: float = Field(
        default=0.20,
        ge=-1.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULLBACK_REENTRY_TRADE_FLOW_THR"),
        description="Positive-confirm trade_flow floor for a re-load (executed tape turning up). FAILS-CLOSED on None. NOTE: a guessed constant — calibrate in replay before any live reliance (sweep on PLSM/RUN 2026-06-24).",
    )
    chili_momentum_micropullback_reentry_max_dip_pct: float = Field(
        default=0.04,
        gt=0.0,
        le=0.30,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MICROPULLBACK_REENTRY_MAX_DIP_PCT"),
        description="Shallow-dip cap: the micro-pullback dip from the local bounce-high must be <= this fraction (a deep rollover is NOT a micro-pullback). Adaptive convention; keep small.",
    )
    # ── ROSS BUY-THE-DIP / PULLBACK ADD (the operator ask). The #772 pyramid + the
    # micro-pullback re-load both add on CONTINUATION (UP/new-HOD, dip-and-curl). Ross
    # ALSO buys the controlled PULLBACK to support (a higher-low / breakout shelf / VWAP)
    # in an INTACT uptrend — sized conservatively, the add's stop just below the higher-low.
    # FALLING-KNIFE-GUARDED by the just-shipped front-side strength (front_side_strength_score
    # >= an adaptive floor) + OFI-not-collapsing + above-VWAP-or-reclaiming + higher-low. A
    # SEPARATE sub-branch in the held-position tick path with its OWN predicate
    # (pullback_add_decision), OWN counter (pullback_add_count), OWN in-flight marker
    # (pullback_add_order_id), OWN cooldown. ADDITIVE: composes with — never double-fires
    # with — the UP-pyramid / micro-pullback (it refuses when EITHER has an add in flight).
    # Re-loads route through pyramid_blend_on_fill + pyramid_risk_anchor_usd VERBATIM so the
    # #769 max-loss circuit keeps re-basing to the STARTER R0. EQUITY-FIRST (crypto deferred).
    # Default ON (no dark flags); flag OFF ⇒ byte-identical (no pullback-add). This is an ADD
    # lever (more position on a healthy dip), NEVER a veto. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_pullback_add_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_ADD_ENABLED"),
        description="Kill-switch for the Ross BUY-THE-DIP pullback ADD (add on a controlled pullback to support in an intact uptrend). false = byte-identical (no pullback-add, no pos mutation, no emit, no broker call). Default ON.",
    )
    chili_momentum_pullback_add_max: int = Field(
        default=2,
        ge=1,
        le=4,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_ADD_MAX"),
        description="Per-name/per-session cap on pullback-adds (bounds total pullback-add risk = max * risk_fraction * R0). Separate counter from the UP-pyramid for clean attribution. Documented small N.",
    )
    chili_momentum_pullback_add_cooldown_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_ADD_COOLDOWN_SECONDS"),
        description="Cooldown between pullback-adds. PINNED to >= 2 * micropull_bar_seconds (min 30s @ 15s bars) so one wiggle cannot fire two adds before the structure re-forms.",
    )
    chili_momentum_pullback_add_risk_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_ADD_RISK_FRACTION"),
        description="rho: each pullback-add's structural risk as a fraction of the STARTER R0 (Ross sizes the pullback-add conservatively; <=1 keeps the add inside the banked cushion). Sized via compute_risk_first_quantity, never a hardcoded block. The add size is structurally <= the initial entry (R-funded off R0).",
    )
    chili_momentum_pullback_add_depth_lo_frac: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_ADD_DEPTH_LO_FRAC"),
        description="Lower edge of the CONTROLLED pullback-depth band: the dip from the HWM must be >= this fraction of the move's range (a 1-tick wiggle is NOT a pullback-buy). ONE documented FLOOR; range-relative (no fixed-price magic). Default 0.20.",
    )
    chili_momentum_pullback_add_depth_hi_frac: float = Field(
        default=0.62,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_ADD_DEPTH_HI_FRAC"),
        description="Upper edge of the CONTROLLED pullback-depth band: the dip from the HWM must be <= this fraction of the move's range (a deeper retrace is a rollover, not a healthy dip). A Fibonacci-0.618 retrace ceiling; range-relative. Default 0.62.",
    )
    chili_momentum_pullback_add_strength_floor: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_ADD_STRENGTH_FLOOR"),
        description="⭐ FALLING-KNIFE GUARD base: the minimum front_side_strength_score for the uptrend to count as INTACT before a pullback-add. ONE documented base (the neutral 0.50 midpoint = a FLOOR); when the regime-adaptive p25 (the entry-side s_lo) is warm and HIGHER, the caller uses that instead. front_side_strength None ⇒ fail-CLOSED (no add). Default 0.50.",
    )
    # ROSS ADD GAP — ADD-ON-FLAG-BREAKOUT on the HELD position. The FOURTH held-position add:
    # while holding a winner that consolidated into a tight BULL FLAG (a base after the
    # impulse), Ross buys the BREAK of the flag's swing high — a CONTINUATION add at the
    # breakout. DISTINCT from (1) the UP-pyramid (new-HOD + OFI thrust), (2) the micro-pullback
    # re-load, and (3) the BUY-THE-DIP pullback-add (bounce off support). The flag geometry +
    # the confirmed break reuse bull_flag_confirmation on the held position's recent bars; this
    # block has its OWN counter (flag_breakout_add_count), kill-switch, in-flight marker
    # (flag_breakout_add_order_id), and cooldown. ADDITIVE: composes with — never double-fires
    # with — the other 3 adds (it refuses when ANY of them has an add in flight). Re-loads route
    # through pyramid_blend_on_fill + pyramid_risk_anchor_usd VERBATIM so the #769 max-loss
    # circuit keeps re-basing to the STARTER R0. EQUITY-FIRST (crypto deferred). Default ON (no
    # dark flags); flag OFF ⇒ byte-identical. ADD lever (more position on a confirmed flag-break),
    # NEVER a veto. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_flag_breakout_add_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLAG_BREAKOUT_ADD_ENABLED"),
        description="Kill-switch for the Ross ADD-ON-FLAG-BREAKOUT (pyramid into a held-position bull-flag BREAK — a continuation add at the flag-top take-out). false = byte-identical (no flag-break add, no pos mutation, no emit, no broker call). Default ON.",
    )
    chili_momentum_flag_breakout_add_max: int = Field(
        default=2,
        ge=1,
        le=4,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLAG_BREAKOUT_ADD_MAX"),
        description="Per-name/per-session cap on flag-breakout adds (bounds total flag-add risk = max * risk_fraction * R0). Separate counter from the UP-pyramid / dip-add for clean attribution. Documented small N.",
    )
    chili_momentum_flag_breakout_add_cooldown_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLAG_BREAKOUT_ADD_COOLDOWN_SECONDS"),
        description="Cooldown between flag-breakout adds. PINNED to >= 2 * micropull_bar_seconds (min 30s @ 15s bars) so one break wiggle cannot fire two adds before a new flag re-forms.",
    )
    chili_momentum_flag_breakout_add_risk_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLAG_BREAKOUT_ADD_RISK_FRACTION"),
        description="rho: each flag-breakout add's structural risk as a fraction of the STARTER R0 (Ross sizes a continuation add conservatively; <=1 keeps the add inside the banked cushion and structurally <= the initial entry). Sized via compute_risk_first_quantity, never a hardcoded block.",
    )
    chili_momentum_flag_breakout_add_margin_frac: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLAG_BREAKOUT_ADD_MARGIN_FRAC"),
        description="The breakout-confirmation margin: the live bid must clear the flag high by >= this fraction of the flag RANGE (flag_high - flag_low) for the break to count as a GENUINE take-out (not a 1-tick wick). ONE documented base; range-relative (no fixed-price magic). Default 0.10 (one-tenth of the flag's own range).",
    )
    chili_momentum_flag_breakout_add_strength_floor: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FLAG_BREAKOUT_ADD_STRENGTH_FLOOR"),
        description="⭐ FALLING-KNIFE GUARD base: the minimum front_side_strength_score for the uptrend to count as INTACT before a flag-breakout add. ONE documented base (the neutral 0.50 midpoint = a FLOOR); when the regime-adaptive p25 (the entry-side s_lo) is warm and HIGHER, the caller uses that instead. front_side_strength None ⇒ fail-CLOSED (no add). Default 0.50.",
    )
    # ROSS EXIT GAP 1 — "lost VWAP → flatten" on the HELD position. Ross's intraday
    # line-in-the-sand: after entry, if price LOSES session VWAP in a CONFIRMED way he
    # is OUT. The CONFIRMED-LOSS definition (anti-whipsaw, the ONE documented base): the
    # last CLOSED bar closed below session VWAP AND the live bid is still below VWAP by
    # an ADAPTIVE margin (the name's OWN close-vs-VWAP dispersion sigma, NOT a fixed
    # magnitude) AND order-flow is NOT positive (tape/OFI confirms the break, fail-OPEN
    # to "confirmed" only when both the closed-bar and the margin already agree). A
    # momentary 1-tick undercut canNOT fire it (a CLOSED bar is required); a dip that
    # HOLDS/RECLAIMS VWAP is a DIP-BUY (the pullback-add), not a flatten. COMPOSES with
    # the pullback-add by PRE-EMPTING it: this check runs BEFORE the dip-add block and
    # returns on a confirmed loss, so the same tick can never both add and flatten.
    chili_momentum_lost_vwap_flatten_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LOST_VWAP_FLATTEN_ENABLED"),
        description="Kill-switch for the Ross lost-VWAP → flatten exit on a held LONG (confirmed loss: last closed bar below session VWAP AND live bid below VWAP by the name's own dispersion-sigma margin AND order-flow not positive). false = byte-identical (no flatten, no transition, no emit). EXIT-only — respects INVARIANT-A (can flatten, never loosens the ratchet floor). Default ON.",
    )
    chili_momentum_lost_vwap_margin_sigma: float = Field(
        default=0.25,
        ge=0.0,
        le=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LOST_VWAP_MARGIN_SIGMA"),
        description="Anti-whipsaw margin for the lost-VWAP flatten: the live bid must sit below session VWAP by this many of the name's OWN close-vs-VWAP sigma before the loss counts as CONFIRMED (a fraction of the name's own dispersion, NOT a fixed-price magnitude). ONE documented base; raise to demand a deeper confirmed break. Default 0.25.",
    )
    # ROSS EXIT GAP 2 — live close-below-structure (BOS). Ross exits on a confirmed bar
    # CLOSE below structure (the last confirmed swing low), not an intrabar wick. The
    # live lane only had ATR/chandelier INTRABAR trailing; this ports the backtest/paper
    # bos_exit_triggered_long onto a CLOSED-bar read so it fires on a confirmed close
    # below the swing low, distinct from the intrabar trail (whichever fires first wins).
    chili_momentum_bos_exit_live_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BOS_EXIT_LIVE_ENABLED"),
        description="Kill-switch for the live close-below-structure (BOS) exit on a held LONG: a CONFIRMED last-closed-bar close below the last confirmed swing low (minus a small buffer) flattens. Distinct from the intrabar chandelier trail; whichever fires first wins. false = byte-identical (no BOS exit, no transition, no emit). EXIT-only — respects INVARIANT-A. Default ON.",
    )
    chili_momentum_bos_exit_buffer_pct: float = Field(
        default=0.003,
        ge=0.0,
        le=0.05,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BOS_EXIT_BUFFER_PCT"),
        description="Buffer below the last confirmed swing low for the live BOS exit: the closed-bar close must be below swing_low * (1 - buffer) to flatten (a small structural cushion so a tick AT the swing low does not flatten). ONE documented base; matches the backtest/paper bos_exit_triggered_long default. Default 0.003 (30 bps).",
    )
    # EVENT-DRIVEN TICK EXIT (Lever B-2, 2026-06-16): a held crypto trailing position
    # whose order flow rolls over (OFI < thr) wakes the exit runner on the WS tick —
    # up to 15s sooner than the poll (Ross "eject the moment the ask thickens"). A
    # DISPATCH HINT only — tick_live_session re-checks the full INVARIANT-A-safe
    # confluence and is the sole decider of any sell. Ships OBSERVE-FIRST: _enabled
    # OFF logs the would-dispatch counterfactual so the operator validates before the
    # flip (it changes WHEN winners are sold). Flip _enabled=True to act.
    chili_momentum_exit_event_driven_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_EVENT_DRIVEN_ENABLED"),
        description="False => observe-only (log the would-dispatch hint, do not act). True => the WS-tick OFI rollover dispatches the exit runner.",
    )
    chili_momentum_exit_event_driven_observe: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_EVENT_DRIVEN_OBSERVE"),
        description="When _enabled is False, still log the would-dispatch counterfactual for validation. Set both False to fully no-op the per-tick OFI read.",
    )
    chili_momentum_exit_event_ofi_rollover_thr: float = Field(
        default=-0.25,
        ge=-1.0,
        le=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_EVENT_OFI_ROLLOVER_THR"),
        description="OFI (normalized [-1,1]) below this = sell-side exhaustion rollover -> dispatch hint. Loose by design: the runner's real exit gate decides.",
    )
    chili_momentum_exit_ofi_lock_partial_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_OFI_LOCK_PARTIAL_ENABLED"),
        description="Action B: arm the early PARTIAL (scale-out → breakeven) on strong exhaustion. Default OFF = log-would-fire-first; the ratchet-tighten (Action A) still applies. Promote after the counterfactual proves net-positive.",
    )
    chili_momentum_exit_ofi_arm_frac: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_OFI_ARM_FRAC"),
        description="Profit-arm point as a FRACTION of the plan's own reward:risk (arm_r = arm_frac·rr, floored 0.5R). Derived from rr — not a fixed-R magic number. Below the arm the lock is inert (the trail/stop owns healthy pullbacks).",
    )
    chili_momentum_exit_ofi_base_lock_bps: float = Field(
        default=120.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_OFI_BASE_LOCK_BPS"),
        description="The ONE irreducible knob: base lock tightness (bps below the high-water mark). Scaled tighter by move strength (peak_r/rr) and flow magnitude, clamped no looser than the cushion band already is and no tighter than 0.25× the base.",
    )
    chili_momentum_exit_ofi_hidden_seller_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_OFI_HIDDEN_SELLER_ENABLED"),
        description="Accelerant: hidden-seller absorption at the highs arms the lock on profit-arm + micro-rollover alone (distribution is the one LEADING signal). OFF at ship — promote only after OFI+micro proves net-positive (log-only-first).",
    )
    # ── Tape-acceleration reversal exit (sell-into-strength climax lock) ──────────
    # Sibling of the OFI exhaustion lock for the names the lock MISSES. The OFI lock
    # is L2-data-starved on equity (only ~88/684 names carry iqfeed_depth_snapshots);
    # this rides signed_tape_accel from the executed TRADE tape (iqfeed_trade_ticks,
    # broad equity coverage). It locks the runner at the spike's climax — the moment
    # the aggressive-buy push ENDS / turns while price is still NEAR the high — so the
    # next tick exits near the top BEFORE the giveback. RATCHET-ONLY (Invariant A):
    # it can only ever exit a WINNER near its top, never cut a loser early, never
    # loosen a stop. Reuses the OFI lock's arm_frac + base_lock_bps (NO new magic
    # numbers) — the ONE new documented knob is the near-high giveback band fraction.
    # Crypto (signed_tape_accel_features ⇒ None) no-ops ⇒ byte-identical.
    chili_momentum_exit_tape_accel_reversal_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_TAPE_ACCEL_REVERSAL_ENABLED"),
        description="Kill-switch for the tape-acceleration reversal exit (sell-into-strength climax lock on the equity TRADE tape). Default ON — it is a fail-safe, ratchet-only WINNER exit (Invariant A: can only raise the stop, never cut a loser, never loosen). OFF ⇒ no signed_tape_accel fetch, the held tick is byte-identical. Crypto always no-ops (no equity tick tape).",
    )
    chili_momentum_exit_accel_reversal_giveback_frac: float = Field(
        default=0.35,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_ACCEL_REVERSAL_GIVEBACK_FRAC"),
        description="The ONE new knob: 'near-high' band for the sell-into-strength fire, as a fraction of the position's OWN risk unit (giveback (hwm−bid) must be ≤ giveback_frac · risk_dist). Adaptive to the name's ATR — NOT a fixed %. If price has already given back more than this, the trail owns the exit (this never fires after a real drop). The arm point (arm_frac·rr) and the climax cushion (base_lock_bps) are REUSED from the OFI exhaustion lock — no duplicate magic numbers.",
    )
    # 1m CANDLE EXHAUSTION CONFIRMER (2026-06-16): the live entry trigger runs on 1m,
    # but the exhaustion lock's only candle read (the standalone topping-tail exit) uses
    # the 15m _entry_df — too coarse to corroborate a fast 1m momentum rollover. Fetch a
    # 1m df (cached once/min/session like the 5m-EMA anchor) and read a topping-tail (+
    # optional MACD-hist rollover) as ONE MORE AND-gated corroborant fed into the lock's
    # FLOW confluence (micro-rollover ∧ OFI-flip ∧ giveback). AND-gated ⇒ it can only ever
    # SUPPRESS a flow fire whose 1m candle shows no exhaustion (a noisy-OFI early-sell);
    # it never causes a new fire (so it can't sell a winner the lock wouldn't already).
    # Fail-OPEN (no 1m df ⇒ candle_ok=True ⇒ existing captures untouched). Class-agnostic
    # (crypto + equity identical — same fetch_ohlcv_df). The absorption OR-bypass is NOT
    # candle-gated (it is the one LEADING signal). Validated on LNAI 5170/5192/5204
    # (06-16): the confirmer AGREED with 2/2 live lock fires (MACD caught 5204 where the
    # wick did not) ⇒ zero capture regression; ships OBSERVE-FIRST to measure the
    # would-suppress ticks before gating live. docs/DESIGN/ADAPTIVE_OFI_EXIT.md
    chili_momentum_exit_candle_confirm_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_CANDLE_CONFIRM_ENABLED"),
        description="Kill-switch for the 1m candle exhaustion confirmer. ON = fetch the cached 1m df, compute topping-tail (+ MACD rollover), feed it to the exhaustion lock and emit the candle_would_suppress A/B on every armed tick. OFF = no 1m fetch, lock byte-identical (candle_ok fails open).",
    )
    chili_momentum_exit_candle_confirm_live: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_CANDLE_CONFIRM_LIVE"),
        description="Gate the lock's FLOW confluence on the 1m candle. Default OFF = observe-first: the LIVE lock decision is byte-identical and only the would-suppress counterfactual logs. Flip ON once the A/B shows the would-suppress fires were early-sells (price recovered), not real tops. AND-gated ⇒ can only tighten the fire criterion, never loosen it (INVARIANT A preserved).",
    )
    chili_momentum_exit_candle_confirm_use_macd: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_CANDLE_CONFIRM_USE_MACD"),
        description="Include the 1m MACD-hist rollover in the candle confirmer (OR'd with the topping-tail). Default ON — LNAI 5204's real top was caught ONLY by MACD (no dominant wick); topping-tail-only would have wrongly suppressed that capture. OFF = wick-only confirmer.",
    )
    # ── Sell-into-strength ladder (v2 proactive exit) ────────────────────────────
    # Ross-style: post a SMALL resting limit at/above the bid into genuine strength
    # (unfilled = free option, fills only on a real up-trade). Safety = the mechanism
    # (resting-limit + continuation-veto + INVARIANT A), not a forecast.
    chili_momentum_exit_ladder_rung_bps: float = Field(
        default=60.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_LADDER_RUNG_BPS"),
        description="The ONE base knob for v2: distance (bps) below the high-water mark for the first sell-into-strength rung. Widened on a stronger run (let winners run); the limit is clamped to never post below the live bid. Everything else (arm_r, deep-run gap, exit/micro thresholds, increment size) derives from the plan rr / the position's ATR risk unit / window percentiles.",
    )
    chili_momentum_exit_ladder_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_LADDER_ENABLED"),
        description="Master gate / kill-switch for the v2 sell-into-strength layer. ON = the ladder read + decision run and emit live_sell_into_strength with the pure-hold counterfactual on every armed tick, AND the INVARIANT-A stop-ratchet applies (can only help). The size-MOVING resting limit is separately gated by chili_momentum_exit_ladder_live.",
    )
    chili_momentum_exit_ladder_live: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_LADDER_LIVE"),
        description="The size-moving gate: when ON, a fired distribution read posts the small resting sell-into-strength limit live. Default OFF — a DELIBERATE staged step: the armed-tick counterfactual + INVARIANT-A stop-ratchet (exit_ladder_ENABLED) are already live, but the size-moving sell is not yet A/B-proven net-positive (2026-07-06: a premature default-ON was reverted — the BJDX/LUCY give-backs were the dup-Reference 409 orphan bug, fixed independently, NOT a missing harvest). Flip ON (or set CHILI_MOMENTUM_EXIT_LADDER_LIVE=true) to promote once the counterfactual proves out.",
    )
    # Class gate: extend the adaptive exit (v1 exhaustion lock + v2 sell-into-strength)
    # to the EQUITY lane too (using equity L2 from iqfeed_depth_snapshots). The helpers
    # are class-agnostic; this un-gates the live_runner hooks from crypto-only. Default
    # ON (no dark flags) — equity gets Step-1 (emit counterfactual + INVARIANT-A stop
    # ratchet, can only help); the size-moving sell is still gated by exit_ladder_live.
    # OFF ⇒ equity is byte-identical to pre-extension (parity kill-switch / rollback).
    chili_momentum_exit_adaptive_equity_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_ADAPTIVE_EQUITY_ENABLED"),
        description="Run the adaptive exit (v1 exhaustion lock + v2 sell-into-strength) for EQUITY positions, not just crypto. Equity L2 from iqfeed. Default ON; the size-moving sell stays gated by exit_ladder_live. OFF = equity byte-identical (parity / rollback).",
    )
    # ── Cadence-aware exit (v2 modulation) ───────────────────────────────────────
    # Make the v2 sell-into-strength ladder CADENCE-AWARE: when the runner has gone
    # quiet (a SLOW chopper — low velocity, 5m-EMA not rising, RVOL not accelerating)
    # LOOSEN the distribution gate so the existing ladder banks the small first
    # increment EARLIER at the stall, instead of waiting for the full distribution
    # confluence that a dead chopper may never print. A FAST runner is NEVER touched
    # (only SLOW choppers loosen; the [0.35,0.65] uncertainty band + the cold-start
    # guard both default to FAST/normal). The continuation veto is NEVER loosened, the
    # ratchet-only stop is preserved, and floors clamp the loosening. OFF ⇒ the ladder
    # is byte-identical to its pre-cadence behaviour (parity kill-switch / rollback).
    chili_momentum_cadence_aware_exit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CADENCE_AWARE_EXIT_ENABLED"),
        description="Master gate / kill-switch for the cadence-aware sell-into-strength modulation. ON = a SLOW_CHOPPER cadence class loosens the v2 ladder's distribution gate (fires earlier at the stall) while a FAST/UNCERTAIN class leaves the gate byte-identical to today. The continuation veto, the INVARIANT-A ratchet, and the single-chokepoint partial path are unchanged regardless. OFF = the ladder is byte-identical to its pre-cadence behaviour (no modulation ever), the only safe rollback.",
    )
    chili_momentum_cadence_atr_pct_slow_threshold: float = Field(
        default=0.20,
        ge=0.01,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CADENCE_ATR_PCT_SLOW_THRESHOLD"),
        description="The ONE irreducible cadence knob: a runner is velocity-SLOW when its realized velocity (price move since entry per minute held, as a fraction of price) falls below this fraction of the trade's own entry ATR%/minute. Everything else in the cadence classifier (trend via 5m-EMA rising, RVOL acceleration, the [0.35,0.65] uncertainty band, the cold-start gate) derives from signals already present at the tick — no other magic numbers. Lower = harder to call SLOW (more runners stay FAST/normal).",
    )
    # ── L2-aware anti-shake-out (LOSS side) ──────────────────────────────────
    # OPG-USD was shaken out at a dip VALLEY (trail_stop -41bps) then recovered +
    # re-armed 6 min later. The existing >=1s flicker guard catches a single bad
    # PRINT but not a multi-second CHOP dip. This gate classifies the breach via
    # L2/OFI at the moment of breach: a REAL BREAKDOWN sells immediately (latency
    # <= today — breakdown is vetoed FIRST), a CHOP dip with bids absorbing is
    # held for a HARD-BOUNDED beat (<= max_ticks, <= max_age_s) so a transient
    # shake-out can recover. INVARIANT A is untouched: this delays the SELL
    # EXECUTION only, it never moves/loosens the stop. Default ON (ship live, no
    # dark flags): the bounded CHOP hold is live AND the stop_breach_l2_classify
    # A/B counterfactual emits on every confirmed breach so realized-vs-baseline
    # is measured continuously. Worst case bounded (<=2 ticks/<=2.5s) + reversible
    # via the kill-switch (=0). Equity auto-guards: stale iqfeed off-RTH => BREAKDOWN.
    chili_momentum_stop_l2_confirm_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STOP_L2_CONFIRM_ENABLED"),
        description="L2-aware anti-shake-out: hold a CHOP-classified stop breach for a hard-bounded beat (BREAKDOWN sells immediately). Default ON (ship live, no dark flags) — the bounded CHOP hold is live AND the stop_breach_l2_classify A/B counterfactual emits on every confirmed breach (realized-vs-baseline). INVARIANT-A-clean (delays the SELL execution <=2 ticks/<=2.5s, never moves the stop); worst case bounded + reversible. Set =0 to revert (pure kill-switch).",
    )
    chili_momentum_stop_l2_confirm_max_ticks: int = Field(
        default=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STOP_L2_CONFIRM_MAX_TICKS"),
        description="Hard cap on how many consecutive ticks a CHOP-classified breach may be held before forcing the sell. Bounds the worst-case hold-through of a breakdown the classifier mislabels. Structural (not a tuning knob).",
    )
    chili_momentum_stop_l2_confirm_max_age_s: float = Field(
        default=2.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STOP_L2_CONFIRM_MAX_AGE_S"),
        description="L2 staleness floor AND the wall-clock cap on a CHOP hold: a snapshot older than this (or a breach held this long total) is treated as BREAKDOWN and sells. Never hold on stale data.",
    )
    chili_momentum_stop_l2_confirm_min_snaps: int = Field(
        default=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STOP_L2_CONFIRM_MIN_SNAPS"),
        description="Minimum L2 snapshots required to trust a CHOP classification; below this the breach is treated as BREAKDOWN (sell). Never hold on too-few snapshots.",
    )
    # Runaway-break allowance: take a high-conviction break that ran away WITHOUT a
    # retest (else a vertical runner that never comes back is missed). Strict — only
    # the retest WAIT is waived; raised volume + candle/VWAP/MACD confirmations stand.
    chili_momentum_entry_allow_runaway_break: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_ALLOW_RUNAWAY_BREAK"),
        description="Enter a high-conviction breakout that ran away without offering a retest (don't miss vertical runners).",
    )
    chili_momentum_entry_runaway_min_volume_spike: float = Field(
        default=2.5,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_RUNAWAY_MIN_VOLUME_SPIKE"),
        description="Raised volume-spike floor for a runaway (no-retest) entry — more conviction than a normal break.",
    )
    # ADAPTIVE-RETEST explosive RAW-BREAK escape (Ross's asymmetric rule: raw break on the
    # STRONGEST setups, retest on the rest). When require_retest is True and a break RAN with no
    # pullback (waiting_for_retest), this converts the wait into a raw-break FIRE *only* when the
    # break is, BY THE TAPE, clearly EXPLOSIVE (RVOL >= explosive_floor x mult AND signed-tape
    # thrust confirms the ask is being eaten). TAPE REQUIRED + FAIL-CLOSED; runs ALL 4 chase-guards
    # on the raw-break path. DEFAULT OFF: this loosens the retest discipline on a live ENTRY path,
    # so it ships dark for the operator to flip after live observation (per the analysis, the
    # latent over-gate is already backstopped by allow_runaway_break, so this is incremental, not
    # required — default-OFF is the safe default and flag-OFF is byte-identical).
    chili_momentum_pullback_raw_break_when_explosive: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_RAW_BREAK_WHEN_EXPLOSIVE"),
        description="ADAPTIVE-RETEST: when require_retest is True, allow a GUARDED raw-break entry (instead of waiting_for_retest) on a clearly EXPLOSIVE break (RVOL>=explosive_floor x mult AND tape-confirmed thrust). Default OFF (touches live retest discipline); flag-OFF is byte-identical.",
    )
    chili_momentum_pullback_raw_break_rvol_mult: float = Field(
        default=1.0,
        ge=1.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_RAW_BREAK_RVOL_MULT"),
        description="ADAPTIVE-RETEST: the explosive raw-break RVOL floor = chili_momentum_explosive_floor_rvol x this multiplier (>=1.0 = AT the explosive floor; raise to demand a stronger surge). The ONE documented base for the explosive raw-break escape.",
    )
    chili_momentum_pullback_raw_break_thrust_frac: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLBACK_RAW_BREAK_THRUST_FRAC"),
        description="ADAPTIVE-RETEST: strong-thrust fraction for the explosive raw-break escape — the back-half aggressor-buy acceleration must clear this fraction of back-half buy volume (when exposed) else a strictly-rising tape is required. Higher = stricter.",
    )
    # FIX C(1) — EXPLOSIVE RAW FIRST-PUSH (Ross's asymmetric rule, extended to the bar BEFORE a
    # retest can even form). The existing chili_momentum_pullback_raw_break_when_explosive escape
    # only rescues a break that ALREADY printed and then ran without offering a retest
    # (``waiting_for_retest``). But the decisive w0av0u3qy study showed CHILI was still stranded at
    # ``waiting_for_break`` on the EXPLOSIVE tier — the retest ladder forces a wait even before the
    # first completed-bar break, so a vertical low-float runner (NVCT/FISN/SKYQ class) never arms.
    # When this flag is ON (default True) and require_retest is forcing a ``waiting_for_break`` wait
    # on a CLEARLY EXPLOSIVE name (RVOL >= the SAME adaptive explosive floor AND tape-confirmed
    # aggressive buying via the SAME _explosive_raw_break_escape gate — TAPE REQUIRED + FAIL-CLOSED),
    # re-evaluate the trigger as a RAW first break (require_retest=False semantics for THIS tier
    # only) so it fires the instant a completed bar crosses the pullback high. It is NOT a chase: the
    # SAME 4 chase-guards every other fire runs (tape-required-fail-closed, extension/verticality,
    # backside EMA/MACD + front_side_state + VWAP-hold, structural stop) + the RAISED runaway volume
    # floor + fail-CLOSED sustained-volume all run downstream UNCHANGED. Default-ON per operator
    # style (backstops intact); KILL-SWITCH False ⇒ this whole block is skipped ⇒ BYTE-IDENTICAL to
    # the require_retest ladder. Heavily instrumented (debug["explosive_raw_first_push"] + the
    # raw_break_* tape patch) for the live A/B. docs/DESIGN/MOMENTUM_LANE.md
    chili_momentum_explosive_raw_break_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_RAW_BREAK_ENABLED"),
        description="FIX C(1): when require_retest is True and an EXPLOSIVE name is stranded at waiting_for_break, re-evaluate as a tape-confirmed RAW first break (require_retest=False for the explosive tier only). All 4 chase-guards still run. Default ON; flag-OFF is byte-identical.",
    )
    # FIX C(2) — RVOL-RELATIVE break-volume floor (kills the fixed 1.5x/2.0x magic on the
    # break_low_volume gate). The fixed volume_spike_multiple / runaway_min_volume_spike held a
    # +400%-RVOL explosive name to the SAME trigger-bar relative-volume bar as a +20% name — so the
    # w0av0u3qy study saw break_low_volume reject 529 explosive breaks. When this flag is ON (default
    # True) the per-bar break-volume floor SCALES DOWN as the name's own day-RVOL rises: a name whose
    # rel-vol is R x the explosive floor needs only floor / (R-relative ratio) on the trigger bar.
    # The base ratio chili_momentum_break_volume_rvol_ratio is the ONE documented knob; the floor
    # NEVER drops below a documented absolute minimum (a hyper-explosive name still needs a real
    # green volume bar, not a one-lot poke). The RAISED runaway/deep-reclaim/late-pullback floor is
    # the relative ceiling for the weaker-prior set (those still demand MORE). Self-relative, no magic
    # share count. KILL-SWITCH False ⇒ the EXACT fixed multiple below (byte-identical).
    chili_momentum_break_volume_rvol_relative: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAK_VOLUME_RVOL_RELATIVE"),
        description="FIX C(2): RVOL-relative break-volume floor — a higher-RVOL name clears a LOWER trigger-bar relative-volume bar (kills the fixed 1.5x/2.0x). Default ON; flag-OFF is the fixed multiple (byte-identical).",
    )
    chili_momentum_break_volume_rvol_ratio: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAK_VOLUME_RVOL_RATIO"),
        description="FIX C(2): the ONE documented base. The RVOL-relative break-volume floor = base_floor x clamp(explosive_floor_rvol / day_rvol, ratio, 1.0). At ratio=0.25 a name running >=4x the explosive RVOL floor needs only 25% of the base trigger-bar volume bar; a name AT the floor still needs the full base.",
    )
    chili_momentum_break_volume_rvol_min_floor: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAK_VOLUME_RVOL_MIN_FLOOR"),
        description="FIX C(2): absolute minimum trigger-bar relative-volume the RVOL-relative floor can scale down to (a hyper-explosive name still needs a real green volume bar, not a one-lot poke). Default 1.0 = at-or-above the trailing average.",
    )
    # #2 Breakout-or-bailout fast exit (Ross flat-top): if the broken level fails to
    # hold shortly after entry, cut at market — well inside the structural stop.
    chili_momentum_breakout_bailout_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAKOUT_BAILOUT_ENABLED"),
        description="Enable the early breakout-failed fast exit for pullback_break entries.",
    )
    chili_momentum_breakout_bailout_max_bars: float = Field(
        default=2.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAKOUT_BAILOUT_MAX_BARS"),
        description="Early window for the fast bail, in entry-interval bars (window = bars x interval seconds).",
    )
    chili_momentum_breakout_bailout_buffer_pct: float = Field(
        default=0.001,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAKOUT_BAILOUT_BUFFER_PCT"),
        description="Small wick buffer below the breakout level before fast-bailing (0.001 = 10 bps).",
    )
    # ── GAP-A — SMART POST-ENTRY HOLD (vol-adaptive fast-bail band) ─────────────
    # The deployed fast-bail (breakout_failed_to_hold) cuts the instant the bid dips a
    # FIXED 0.001 (10bps) below the broken level — far inside the lane's explosive,
    # wide-spread low-float names' OWN microstructure noise, so it bails working winners
    # at b/e (FCUV +21% after a 4.5s bail). GAP-A replaces that fixed wick buffer with a
    # VOL-ADAPTIVE band on the name's live realized vol scaled to the holding horizon, and
    # gates the actual CUT on order-flow + a volume-confirmed breach + an adaptive
    # time-floor. It governs ONLY the first post-entry window and can only TIGHTEN
    # (INVARIANT-A): the structural stop + the #769 max-loss circuit are evaluated AHEAD of
    # and independently from this gate (every tick), so a genuinely collapsing position
    # still exits regardless. OFF (default) ⇒ the fixed-0.001-buffer path is BYTE-IDENTICAL.
    # Dimensional note (the exit reviewer's correction, applied identically): rv_live is the
    # PER-GRID-STEP stdev, so band_frac = k*rv_live*sqrt(N), N = expected_hold_s/grid_secs
    # (GRID STEPS), NOT tick_rate*hold (a tick count).
    chili_momentum_smart_hold_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SMART_HOLD_ENABLED"),
        description="GAP-A: replace the fixed 0.001 fast-bail wick buffer with a vol-adaptive hold band + flow/volume/time-floor CUT gating, governing only the first post-entry window (INVARIANT-A: only tightens; structural stop + #769 circuit run ahead, always). OFF (default) ⇒ fixed-0.001-buffer path byte-identical.",
    )
    # GAP-A — the band tightness multiplier (in ATR/stdev-equivalents). ONE documented base
    # (1.2); applied as k = k_atr * sqrt(pi/2) (=1.2533, the mean-abs→stdev half-width
    # conversion matching the vol-norm exit's band geometry). band_frac = k*rv_live*sqrt(N),
    # clamped between 0 and the existing 0.15 ATR-pct ceiling. A/B-tunable seed; band [0.5, 3.0].
    chili_momentum_smart_hold_k_atr: float = Field(
        default=1.2,
        ge=0.5,
        le=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SMART_HOLD_K_ATR"),
        description="GAP-A vol-adaptive hold-band tightness (band_frac = k_atr*sqrt(pi/2)*rv_live*sqrt(N), N in GRID STEPS). Default 1.2, band [0.5,3.0]. A/B-tunable seed.",
    )
    # GAP-A — T_flow / S_flow: the negative-flow CUT thresholds, ADAPTIVE as the |lower-tail|
    # of the name's OWN recent OFI level / slope distribution (calibration recipe: T_flow =
    # |q(level, flow_tail_q)|, S_flow = |q(slope, flow_tail_q)|). ONE documented base each used
    # as the FLOOR when the live distribution is too thin to estimate (never a hard ceiling):
    # the absolute OFI threshold the lane already uses (0.25) for level, and 0 for the slope
    # (any sustained roll-over). flow_tail_q is the single documented tail quantile (0.15).
    chili_momentum_smart_hold_flow_tail_q: float = Field(
        default=0.15,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SMART_HOLD_FLOW_TAIL_Q"),
        description="GAP-A lower-tail quantile of the name's own recent OFI level/slope used to derive T_flow/S_flow (the decisive negative-flow CUT thresholds). Default 0.15.",
    )
    chili_momentum_smart_hold_t_flow_floor: float = Field(
        default=0.25,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SMART_HOLD_T_FLOW_FLOOR"),
        description="GAP-A T_flow FLOOR (min |negative OFI level| to count as decisive distribution) when the live OFI distribution is too thin to estimate. Default 0.25 (the lane's existing ofi_threshold).",
    )
    chili_momentum_smart_hold_s_flow_floor: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SMART_HOLD_S_FLOW_FLOOR"),
        description="GAP-A S_flow FLOOR (min |negative OFI slope| to count as rolling-over) when the live distribution is too thin to estimate. Default 0.0 (any sustained roll-over).",
    )
    # GAP-A — rho: the persistence fraction for the tick_rate pace test (hold while live
    # tick_rate > rho*tick_rate_ref). ONE documented base (0.6), shared geometry with the
    # 2B velocity persist_frac. Band [0.1, 1.0]. tick_rate_ref = the entry tick_rate.
    chili_momentum_smart_hold_rho: float = Field(
        default=0.6,
        ge=0.1,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SMART_HOLD_RHO"),
        description="GAP-A persistence fraction rho: hold while live tick_rate > rho*entry_tick_rate. Default 0.6, band [0.1,1.0].",
    )
    # GAP-A — the adaptive TIME-FLOOR quantile: suppress the fast-bail while held_seconds is
    # below the q(time_floor_q) of the name's own recent break-resolution times. ONE documented
    # quantile base (0.25 = q25). Fallback when <N samples = 2*bar_seconds (a structural
    # retest of the broken level typically resolves within ~2 bars). Band [0.0, 0.5].
    chili_momentum_smart_hold_time_floor_q: float = Field(
        default=0.25,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SMART_HOLD_TIME_FLOOR_Q"),
        description="GAP-A time-floor quantile (q25): suppress the fast-bail while held_seconds < q(this) of recent break-resolution times. Default 0.25; fallback 2*bar_seconds when <min_samples.",
    )
    chili_momentum_smart_hold_time_floor_min_samples: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SMART_HOLD_TIME_FLOOR_MIN_SAMPLES"),
        description="GAP-A minimum recent break-resolution samples before the adaptive time-floor is trusted; below this the fallback (2*bar_seconds) is used. Default 5.",
    )
    # ── GAP-B — TIGHT-MOMENTUM FALSE-BREAK-REVERSAL / VWAP-RECLAIM ENTRY ─────────
    # A NEW entry trigger family (false_break_reclaim_confirmation): on a COMPRESSED
    # (coiled) tape with REQUIRED order-flow + a self-relative volume surge, fire on a
    # false-breakout REVERSAL (pierce L -> fail/flush below L -> rip back & reclaim L) OR
    # a VWAP-reclaim from below. Carries the SAME four chase-guards every breakout trigger
    # carries (tape REQUIRED+fail-closed, _hod_extension_ok, backside + front_side_state,
    # _l2_entry_veto). Every threshold is an ADAPTIVE percentile/ratio of the name's OWN
    # recent distribution with ONE documented base each (NO magic). The structural stop +
    # #769 max-loss circuit stay ahead + always-live; GAP-A governs only the first post-
    # entry window. OFF (default) ⇒ the detector returns disabled before any compute ⇒
    # BYTE-IDENTICAL. Mutually exclusive per-tick with raw_break/break_retest at dispatch.
    chili_momentum_entry_tight_false_break_reclaim_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_TIGHT_FALSE_BREAK_RECLAIM_ENABLED"),
        description="GAP-B: enable the TIGHT-MOMENTUM false-breakout-reversal / VWAP-reclaim entry trigger family. OFF (default) ⇒ detector disabled before any compute, byte-identical.",
    )
    # GAP-B compression: TIGHT when compression = atr_pct_now/median(atr_pct, Lc) sits below
    # theta_c = the p(pctile) of the name's OWN recent compression distribution. ONE base each.
    chili_momentum_tight_compression_lookback: int = Field(
        default=20,
        ge=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_COMPRESSION_LOOKBACK"),
        description="GAP-B compression lookback Lc (bars) for the per-bar atr_pct baseline median. Default 20.",
    )
    chili_momentum_tight_compression_pctile: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_COMPRESSION_PCTILE"),
        description="GAP-B theta_c quantile: TIGHT when compression < p(this) of the name's own recent compression distribution. Default 0.30 (p30).",
    )
    chili_momentum_tight_compression_coil_bars: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_COMPRESSION_COIL_BARS"),
        description="GAP-B coil width (bars) — the recent base leading INTO the firing bar whose MEDIAN atr_pct is compared to the Lc baseline median (the firing bar excluded; a median over >=5 bars is robust to the 1-2 wider pierce/flush bars of the false-break event). Default 5.",
    )
    # GAP-B volume: vol_ok when bar volume > vmult*median(vol, Lv); vmult = clamp(q(pctile)
    # of the recent RVOL distribution, floor, ceil) — the SAME adaptive-volume kill-2.5x shape.
    chili_momentum_tight_volume_lookback: int = Field(
        default=20,
        ge=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_VOLUME_LOOKBACK"),
        description="GAP-B volume lookback Lv (bars) for the recent RVOL distribution + the volume median. Default 20.",
    )
    chili_momentum_tight_volume_pctile: float = Field(
        default=0.60,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_VOLUME_PCTILE"),
        description="GAP-B volume multiple quantile: vmult = clamp(q(this) of recent RVOL dist, floor, ceil). Default 0.60 (q60).",
    )
    chili_momentum_tight_volume_mult_floor: float = Field(
        default=1.5,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_VOLUME_MULT_FLOOR"),
        description="GAP-B vmult FLOOR (and thin-sample fallback) — the documented base volume multiple. Default 1.5.",
    )
    chili_momentum_tight_volume_mult_ceil: float = Field(
        default=3.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_VOLUME_MULT_CEIL"),
        description="GAP-B vmult CEIL (caps how high the adaptive volume multiple can climb). Default 3.0.",
    )
    # GAP-B flow: flow_ok REQUIRED + FAIL-CLOSED (ofi_level > +T_flow_entry AND ofi_slope > 0).
    # T_flow_entry = |lower-tail (flow_tail_q)| of the name's own OFI level dist, floored at the
    # lane's existing chili_momentum_ofi_threshold (ONE documented base).
    chili_momentum_tight_flow_tail_q: float = Field(
        default=0.15,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_FLOW_TAIL_Q"),
        description="GAP-B flow tail quantile for T_flow_entry (the entry OFI-level pressure floor). Default 0.15; floored at chili_momentum_ofi_threshold.",
    )
    # GAP-B geometry window: how many recent COMPLETED bars define the pierce/flush/reclaim
    # (B.2) and the VWAP-reclaim (B.3) shapes. Default 6.
    chili_momentum_tight_geometry_lookback: int = Field(
        default=6,
        ge=3,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIGHT_GEOMETRY_LOOKBACK"),
        description="GAP-B geometry lookback K (bars) for the false-break-reversal pierce/flush level + the VWAP-reclaim swing low. Default 6.",
    )
    # ── EXPLOSIVE-MOVER RECALIBRATION (master) ─────────────────────────────────
    # Ross small-caps step the bid DOWN / widen the spread mid-squeeze and dip-test
    # the broken level within seconds — exactly when the breakout is WORKING, not
    # failing. The conservative gates read that backwards and cut winners at b/e
    # (FCUV +21% after a 4.5s fast-bail; ILLR/WEN +bid_prop blocks then ran). This
    # MASTER kill-switch gates ONLY the explosive-aware carve-outs below; with it
    # OFF (default) every sub-feature is a no-op and the lane is BYTE-IDENTICAL.
    # The protections it relaxes (buy-into-selling veto, failed-breakout bail) STAY
    # binding for genuinely failing / falling-momentum names — these only widen the
    # thresholds for the high-RVOL / extreme-ATR regime the lane explicitly targets.
    # Risk-first sizing + the #769 max-loss circuit + the cushion-funded pyramid are
    # untouched (they bound the downside regardless).
    chili_momentum_explosive_recalibration_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_RECALIBRATION_ENABLED"),
        description="MASTER kill-switch for the explosive-mover recalibration (bid-prop exempt, fast-bail lock-in, extension/flow RVOL carve-outs, pyramid eligibility-flicker skip). OFF (default) => every carve-out is a no-op, lane byte-identical.",
    )
    chili_momentum_explosive_atr_pct_floor: float = Field(
        default=0.045,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_ATR_PCT_FLOOR"),
        description="ATR%% at/above which a name is treated as EXPLOSIVE for the recalibration carve-outs (top of the normal regime; regime_atr_pct basis). Sub-master flag gates whether it is read at all.",
    )
    chili_momentum_explosive_rvol_floor: float = Field(
        default=3.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_RVOL_FLOOR"),
        description="Relative-volume (vol_ratio) at/above which a name is treated as EXPLOSIVE for the recalibration carve-outs when an RVOL reading is available (OR-ed with the ATR floor).",
    )
    # COILING-SQUEEZE exemption (2026-06-26): an EXTREME-RVOL name (>= mult x rvol_floor)
    # clears the Ross CHANGE floor even at a modest %-change — accumulation/coil before the
    # pop (SDOT: 65x RVOL, 744K float, +4.4%, wrongly benched by the 10% change floor though
    # Ross was trading it). Selection-only; rvol_floor still applies; entry vetoes still guard.
    chili_momentum_coiling_squeeze_exempt_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_COILING_SQUEEZE_EXEMPT_ENABLED"),
        description="Allow an extreme-RVOL (coiling) name to clear the Ross change floor even at a modest %-change.",
    )
    chili_momentum_coiling_exempt_rvol_mult: float = Field(
        default=3.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_COILING_EXEMPT_RVOL_MULT"),
        description="RVOL multiple of the rvol_floor at/above which the coiling-squeeze change-floor exemption applies (default 3x5x = 15x).",
    )
    # GATE 1 — bid-prop confirmer explosive exemption.
    chili_momentum_bid_prop_explosive_exempt: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BID_PROP_EXPLOSIVE_EXEMPT"),
        description="When ON (and master ON), BYPASS the bid-prop deterioration confirmer for explosive names (ATR%% >= floor OR RVOL >= floor): a squeeze legitimately steps the bid down / widens the spread; the structural pullback-break trigger already read volume+structure. OFF => no-op.",
    )
    # GATE 2 — fast-bail lock-in window (give the breakout structural room).
    chili_momentum_breakout_bailout_lock_in_seconds: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAKOUT_BAILOUT_LOCK_IN_SECONDS"),
        description="Seconds after entry fill during which the fast-bail CANNOT fire (give the breakout time to stabilize through a normal retest). 0 (default) => no lock-in, byte-identical. Master flag must also be ON.",
    )
    chili_momentum_breakout_bailout_lock_in_explosive_seconds: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BREAKOUT_BAILOUT_LOCK_IN_EXPLOSIVE_SECONDS"),
        description="Lock-in seconds for EXPLOSIVE names (ATR%% >= floor OR RVOL >= floor) — wider than the base lock-in (a violent squeeze dip-tests the level later). 0 (default) => falls back to the base lock-in. Master flag must also be ON.",
    )
    # GATE 3 — entry-extension RVOL boost (more chase room for a true squeeze).
    chili_momentum_entry_extension_rvol_boost_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_EXTENSION_RVOL_BOOST_ENABLED"),
        description="When ON (and master ON), BOOST the entry-extension cap for high-RVOL outlier squeezes by min(boost_max, boost_per * max(0, rvol - rvol_floor)). OFF => no boost, byte-identical extension veto.",
    )
    chili_momentum_entry_extension_rvol_boost_per: float = Field(
        default=0.05,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_EXTENSION_RVOL_BOOST_PER"),
        description="Extension-cap boost added per 1.0 of RVOL above the explosive RVOL floor (e.g. 0.05 => +5pp per extra 1x rvol).",
    )
    chili_momentum_entry_extension_rvol_boost_max: float = Field(
        default=0.15,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_EXTENSION_RVOL_BOOST_MAX"),
        description="Hard ceiling on the RVOL extension-cap boost (caps the chase room even on extreme RVOL so a +33%% blow-off chase still vetoes).",
    )
    # GATE 4 — entry flow-veto strong-leg relaxation for explosive names.
    chili_momentum_entry_flow_veto_explosive_exempt: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FLOW_VETO_EXPLOSIVE_EXEMPT"),
        description="When ON (and master ON), RAISE the strong-tape OR-leg threshold for explosive names (ATR%% >= floor OR RVOL >= floor) from trade_flow_strong to trade_flow_strong_explosive so a thin-tape one-seller dip does not veto — MAXIMUM selling (<= explosive thr) still vetoes, and the both-bearish AND-leg is unchanged. OFF => no-op.",
    )
    chili_momentum_entry_flow_veto_trade_flow_strong_explosive: float = Field(
        default=-0.85,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FLOW_VETO_TRADE_FLOW_STRONG_EXPLOSIVE"),
        description="Strong-tape OR-leg threshold for EXPLOSIVE names (only used when the flow-veto explosive exemption is ON): a thin-tape low-float vetoes only on near-maximum selling, not merely strong selling. The both-bearish AND-leg is unchanged so a mixed-flow break still vetoes.",
    )
    # GATE 5 (pyramid) — skip the neural-viability re-check on an ALREADY-HELD winner.
    chili_momentum_pyramid_skip_viability_recheck: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PYRAMID_SKIP_VIABILITY_RECHECK"),
        description="When ON (and master ON), an add to an ALREADY-HELD winner (STATE_LIVE_TRAILING) is NOT refused for an eligibility/freshness flicker (live_eligible / viability_freshness) — the entry already passed admission and the cushion gate + max-loss circuit still bound the add. Kill-switch / drawdown / daily-loss / position-cap blocks are NEVER skipped. OFF => legacy re-check, byte-identical.",
    )
    chili_momentum_pyramid_add_submit_retry_max: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PYRAMID_ADD_SUBMIT_RETRY_MAX"),
        description="Max times a transient pyramid add-order submit failure (broker isError) is re-attempted on a later tick before giving up. 0 (default) => no retry, byte-identical (a single failed submit emits pyramid_add_blocked as today). Master flag must also be ON.",
    )
    chili_momentum_order_notional_guard_bps: float = Field(
        default=25.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORDER_NOTIONAL_GUARD_BPS"),
        description="Extra bps cushion applied to live market-entry ask when sizing against max notional; 0 disables.",
    )
    # ── EXIT GAPS (Warrior re-audit 2026-06-26): PROTECTIVE exits only — they cut a
    # FAILED/non-confirming entry FASTER (never weaken an existing stop) or HOLD a hot
    # runner LONGER. Each kill-switch defaults OFF ⇒ the lane is BYTE-IDENTICAL. They
    # reuse the deployed bailout / cushion-trail machinery; they NEVER fire on a winner
    # that pops-then-consolidates (genuine non-confirmation is required) and NEVER add risk.
    #
    # GAP 1 — bail on ABSENCE-of-strength (affirmative breakout-or-bailout). The deployed
    # bailout is REACTIVE (price-retest-FAIL + tape-weakness). This adds the affirmative
    # side: within a short window after the fill, if the breakout shows NO confirming
    # strength — tape NOT accelerating up AND no new high since entry AND price at/below a
    # small buffer over the entry — the thesis did not confirm, so bail before the stop.
    # A winner that pops (prints a new high above the confirm buffer) is IMMUNE.
    chili_momentum_bail_on_no_confirmation_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BAIL_ON_NO_CONFIRMATION_ENABLED"),
        description="GAP1: within the no-confirmation window after entry, bail if the breakout shows NO confirming strength (no new high since entry AND price at/below the confirm buffer over entry). A new high above the buffer makes the position immune (a popping winner is never cut). OFF (default) ⇒ byte-identical.",
    )
    chili_momentum_no_confirmation_window_seconds: float = Field(
        default=20.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NO_CONFIRMATION_WINDOW_SECONDS"),
        description="GAP1: seconds after entry fill during which the no-confirmation bail can fire. The check only applies inside this window; afterwards the structural stop/trail governs as today.",
    )
    chili_momentum_no_confirmation_min_hold_seconds: float = Field(
        default=8.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NO_CONFIRMATION_MIN_HOLD_SECONDS"),
        description="GAP1: minimum seconds the position must be held before the no-confirmation bail can fire (give the very first ticks a chance to print follow-through). Must be < the window.",
    )
    chili_momentum_no_confirmation_buffer_bps: float = Field(
        default=10.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NO_CONFIRMATION_BUFFER_BPS"),
        description="GAP1: confirmation buffer in bps above the entry fill. A new high at/above entry*(1+buffer) is CONFIRMATION (immune). The bail requires the high-water mark to be BELOW this buffer (no follow-through) AND the live bid at/below entry. Larger ⇒ stricter immunity (harder to count as confirmed).",
    )
    # GAP 2 — INSTANT bid-below-fill cut. Right after the fill, if the BID drops BELOW the
    # fill price by more than spread noise (the move failed at the entry tick), cut FAST —
    # don't wait for the structural stop. Distinguishes a real bid-collapse from normal
    # spread chatter via a bps margin below the fill. Tight first-seconds window.
    chili_momentum_instant_bid_below_fill_cut_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_INSTANT_BID_BELOW_FILL_CUT_ENABLED"),
        description="GAP2: within the instant-cut window after entry, cut fast if the live bid has dropped below the fill price by more than the noise margin (entry failed at the tick). OFF (default) ⇒ byte-identical.",
    )
    chili_momentum_instant_bid_cut_window_seconds: float = Field(
        default=6.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_INSTANT_BID_CUT_WINDOW_SECONDS"),
        description="GAP2: seconds after entry fill during which the instant bid-below-fill cut can fire. Tight (first few seconds); afterwards the structural stop governs.",
    )
    chili_momentum_instant_bid_cut_margin_bps: float = Field(
        default=25.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_INSTANT_BID_CUT_MARGIN_BPS"),
        description="GAP2: how far BELOW the fill (bps) the bid must drop before the instant cut fires — the spread-noise discriminator. The bid must be < entry*(1 - margin) so a bid merely sitting at/just under the fill (normal spread) does NOT trigger.",
    )
    # GAP 3 — REGIME-CONDITIONED HOLD-TIME. The deployed regime conditions SIZE only. This
    # scales the runner trail give-back by the entry regime: an EXPLOSIVE (hot) name gets a
    # WIDER trail (hold the runner through red LONGER); a non-explosive (cold/choppy) name
    # gets a TIGHTER trail (cut quicker). Reuses _session_is_explosive (the deployed regime
    # classifier) + the cushion-trail band. LENGTHENS hot holds; only tightens cold ones.
    chili_momentum_regime_holdtime_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REGIME_HOLDTIME_ENABLED"),
        description="GAP3: scale the runner cushion-trail give-back by the entry regime — HOT/explosive ⇒ wider trail (hold longer), COLD ⇒ tighter trail (cut quicker). The structural stop is NEVER widened past its current value (ratchet-only preserved — a hot mult can only tighten a live stop, never loosen it). DEFAULT-ON (no dark flags); exit-side only, adds zero entry over-gating risk. Kill-switch via env=0 ⇒ byte-identical.",
    )
    chili_momentum_regime_holdtime_hot_mult: float = Field(
        default=1.25,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REGIME_HOLDTIME_HOT_MULT"),
        description="GAP3: trail-band give-back multiple for a HOT (explosive) regime — >= 1.0 (widens the give-back so a runner is held through red longer). Applied to the cushion-trail band bps.",
    )
    chili_momentum_regime_holdtime_cold_mult: float = Field(
        default=0.85,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REGIME_HOLDTIME_COLD_MULT"),
        description="GAP3: trail-band give-back multiple for a COLD (non-explosive) regime — <= 1.0 (tightens the give-back so a chop is cut quicker). Applied to the cushion-trail band bps.",
    )
    # ── Entry fill-rate (the equity 0-fill blocker: a marketable limit was cancelled
    # the instant the bid pipped one tick past it, while it was at the front of the book
    # and about to fill → orphaned). All three default to TODAY's exact behavior (parity
    # kill-switches); the non-zero values are set by the paper A/B, not by guess.
    chili_momentum_entry_chase_ceiling_bps: float = Field(
        default=0.0, ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_CHASE_CEILING_BPS"),
        description="Bid may drift this many bps above the buy limit before the resting order is abandoned as left-behind (vol-widened by chase_move_ratio, hard-capped at the live spread cap). The resting limit is TOLERATED, never re-pegged up. 0 = today's cancel-on-first-tick (parity).",
    )
    chili_momentum_entry_chase_move_ratio: float = Field(
        default=0.25, ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_CHASE_MOVE_RATIO"),
        description="Fraction of the name's expected per-bar move added to the chase ceiling (adaptive widening; inert while chase_ceiling_bps=0).",
    )
    chili_momentum_entry_guard_move_ratio: float = Field(
        default=0.0, ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_GUARD_MOVE_RATIO"),
        description="Fraction of expected move added to the marketable-limit guard premium over the ask (born-marketable on volatile names, capped at the spread cap). 0 = the fixed 25bps notional guard (parity).",
    )
    # MARKETABLE RE-PEG ENTRY CHASE (2026-06-22): on a left-behind runaway, cancel+replace
    # the resting buy UP to the live ask so a fast vertical actually FILLS instead of being
    # abandoned. SAFETY (red-teamed): equity-only (crypto maker-only never chased); fails
    # CLOSED on a stale/blocked quote; the chase price is bounded by a CUMULATIVE ceiling
    # off the ORIGINAL limit (the adaptive spread budget the risk model already accepts), so
    # total entry drift — and thus 2:1 R:R against the fixed structural stop — can never
    # erode past one spread budget no matter how many re-pegs; capped at max_repegs.
    chili_momentum_entry_chase_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_CHASE_ENABLED"),
        description="Master switch for the marketable re-peg entry chase. False = byte-identical cancel-on-first-tick (parity). Equity-only.",
    )
    chili_momentum_entry_max_repegs: int = Field(
        default=3, ge=0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_MAX_REPEGS"),
        description="Max cancel-and-replace re-pegs per entry before falling back to cancel+re-watch. Bounds the chase so a runaway can't loop forever. 0 = no re-peg even with chase_enabled (parity).",
    )
    # ── FIX B — AGGRESSIVE REPEG/CROSS ON FAST PUSHES (2026-06-29) ───────────────
    # The decisive test (w0av0u3qy, db=chili): SKYQ 56 submitted / 0 filled. Root
    # cause = the existing repeg only fires on `entry_limit_left_behind` (the BID
    # ABOVE our limit). On a fast vertical push the ASK climbs THROUGH our resting
    # limit first (we stop being marketable, the offer ran away) while the bid lags
    # below the chase ceiling — so the order sits unfilled until the rest-backstop
    # (~2 bars / 120s) cancels it, after the move is gone. FIX B adds an EARLIER,
    # EVENT-DRIVEN escalation: when the order is submitted-but-unfilled AND the live
    # ASK has advanced past our resting limit by more than a small adaptive band
    # (the move is pushing up THROUGH us, the most Ross-like names), cancel+replace
    # UP to a fresh marketable cross IMMEDIATELY — re-using the SAME repeg machinery
    # (cumulative-spread ceiling, risk-first resize, cancel-before-replace phantom-2x
    # guard, max_repegs counter, equity gate, fresh-quote gate). It NEVER chases past
    # the cumulative chase ceiling (_entry_repeg_price caps at the original limit's
    # adaptive spread budget), so 2:1 R:R against the fixed structural stop is intact.
    # false ⇒ byte-identical to FIX-A behavior (only bid-left-behind triggers a repeg).
    chili_momentum_runaway_cross_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUNAWAY_CROSS_ENABLED"),
        description="FIX B master switch. True (default) = also escalate to a marketable cross when the live ASK advances past our resting entry limit on a fast push (not only when the BID crosses). False = parity (bid-left-behind is the only repeg trigger).",
    )
    # The ask must advance past our resting limit by MORE than this adaptive band
    # before FIX B escalates — so a single-tick jitter at the offer doesn't churn a
    # cancel+replace. ONE documented base (bps), widened by a fraction of the name's
    # expected per-bar move (reuses chase_move_ratio so explosive names get more
    # rope), and HARD-CAPPED at the same adaptive live spread cap the chase ceiling
    # uses (the escalation can never tolerate more drift than the risk budget). The
    # actual cross price is STILL bounded by _entry_repeg_price's cumulative ceiling.
    chili_momentum_runaway_cross_ask_band_bps: float = Field(
        default=8.0, ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RUNAWAY_CROSS_ASK_BAND_BPS"),
        description="FIX B: the live ask must exceed our resting limit by more than this many bps (vol-widened by chase_move_ratio, hard-capped at the live spread cap) before escalating to a marketable cross — debounces single-tick offer jitter. Inert while runaway_cross_enabled=False.",
    )
    # ── VERTICAL HALT-RESUME FILL AGGRESSION (2026-06-29) ─────────────────────
    # On a fast vertical halt-resume print (INHD/UPC-style) the ASK gaps 5-15% on
    # the resume tick, blowing past the 3% cumulative repeg ceiling
    # (_entry_repeg_price) in ONE tick -> the cross is abandoned ('past_cumulative_
    # ceiling') -> the move is missed. This flag RAISES the cumulative chase ceiling
    # ONLY for a CONFIRMED-THRUST halt-resume vertical (recent halt-resume + tape
    # thrust + squeeze fuel + RVOL confluence), from the base abs-cap up toward a
    # HARD per-name max-chase, scaled by the confluence strength. Recoverable: the
    # repeg re-sizes risk-first at the chased price, so dollar-risk stays pinned at
    # _eff_max_loss and the #769 max-loss circuit still caps the trade. flag OFF ⇒
    # byte-identical (ceiling = the 300bps abs_cap as today).
    chili_momentum_vertical_chase_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VERTICAL_CHASE_ENABLED"),
        description="Master switch for the confirmed-thrust halt-resume vertical chase-ceiling raise. False = the cumulative repeg ceiling stays at the abs_cap (byte-identical parity). Equity-only, halt-resume-gated.",
    )
    chili_momentum_vertical_chase_max_bps: float = Field(
        default=800.0, ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VERTICAL_CHASE_MAX_BPS"),
        description="HARD ceiling (bps over the original limit) the confirmed-thrust vertical chase may reach. The ONE documented cap on vertical aggression (default 800bps=8%). The repeg re-sizes risk-first so dollar-risk is unchanged; this caps adverse-selection on a wrong chase. Inert while vertical_chase_enabled=False.",
    )
    chili_momentum_vertical_chase_min_confluence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VERTICAL_CHASE_MIN_CONFLUENCE"),
        description="Minimum confirmed-thrust confluence in [0,1] (tape-thrust AND squeeze-fuel AND RVOL, halt-resume-gated) before any raise above the abs_cap is granted. Below this the ceiling stays at the abs_cap. Adaptive: the raise scales linearly from abs_cap@this to max_bps@1.0.",
    )
    chili_momentum_vertical_chase_nohalt_thrust_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VERTICAL_CHASE_NOHALT_THRUST_ENABLED"),
        description="Unlock the deep vertical chase-ceiling raise on a CONFIRMED no-halt UP-thrust vertical (a genuine 1m new-high push that never halted), not ONLY inside a halt-resume. The no-halt unlock is FAIL-CLOSED + knife-guarded: it requires ALL of live OFI>0 (buyers lifting) AND price making a NEW HIGH above the breakout level (ask being eaten up) AND above-VWAP-or-reclaiming AND RVOL above the explosive floor — a fade / below-VWAP / OFI<=0 / non-new-high move stays at the abs_cap. The chased price is still risk-first re-sized (dollar-risk unchanged) + bounded by the #769 max-loss circuit. False ⇒ the deep budget stays halt-resume-gated (byte-identical to the prior behavior). Inert while vertical_chase_enabled=False.",
    )
    chili_momentum_vertical_chase_nohalt_min_confluence: float = Field(
        default=0.6, ge=0.0, le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_VERTICAL_CHASE_NOHALT_MIN_CONFLUENCE"),
        description="Floor share of the thrust confluence granted to a CONFIRMED no-halt UP-thrust vertical (vs the 0.5 halt-resume floor). Set above the halt floor (default 0.6) because a no-halt vertical must clear a genuinely strong, UP, confirmed-thrust bar (OFI>0 + new-high + above-VWAP + RVOL) to earn the deep budget; squeeze-fuel + RVOL still add bounded share on top, capped at 1.0. Inert while vertical_chase_nohalt_thrust_enabled=False.",
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
    # Adaptive spread tolerance: the live spread cap above is a FLOOR; the runner
    # also allows up to this fraction of the instrument's expected per-bar move
    # (realized 15m volatility) so explosive momentum names (wide absolute spread,
    # tiny vs. their move) are tradable without a magic fixed bps cap. Only ever
    # loosens above the floor; quiet/illiquid names keep the conservative floor.
    chili_momentum_risk_spread_to_expected_move_ratio: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_SPREAD_TO_EXPECTED_MOVE_RATIO"),
    )
    # Absolute spread CAP (Ross's "if the spread is too wide, skip the trade" rule):
    # the adaptive tolerance above never exceeds this, no matter how explosive the
    # name. Uncapped, a huge-expected-move runner would tolerate an ~8% spread =
    # start down 8% + can't exit on the reversal (bid vanishes). Ross steps back
    # at ~2% (WHLR 30c/$14). Default 300bps (3%) — generous: blocks the catastrophic
    # cost-traps, still lets a name in once its spread compresses at peak volume.
    chili_momentum_risk_max_spread_bps_abs_cap: float = Field(
        default=300.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_MAX_SPREAD_BPS_ABS_CAP"),
    )
    # STEP-E #15: the FIXED 300bps abs cap clamped a legitimately-wide low-float's adaptive
    # EM-based ceiling (DSY adaptive 721bps -> clamped to 300 -> 1,358 wide_bbo blocks). When
    # ON, the effective cap SCALES with the name's OWN adaptive EM ceiling:
    #   effective_cap = max(configured_abs_cap, k * (ratio * expected_move_bps))
    # so a name whose expected move JUSTIFIES a 721bps ceiling isn't clamped to 300, while
    # junk (small EM => small ratio*em) stays capped at 300 and still blocks. k is the ONE
    # documented base (>=1.0 = never clamp below the EM-justified ceiling). Acceptance of the
    # wider spread is COUPLED to a proportional size-down (the spread is priced as entry cost)
    # via the existing spread-cost derate. Missing EM => the fixed cap (fail-closed). OFF =>
    # the fixed 300 cap (byte-identical legacy).
    chili_momentum_risk_spread_abs_cap_em_scale_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_SPREAD_ABS_CAP_EM_SCALE_ENABLED"),
        description="Scale the absolute spread cap with the name's adaptive EM-based ceiling (max(abs_cap, k*ratio*EM)) so a legitimately-wide low-float isn't clamped to 300bps; junk (small EM) still blocks. Coupled to a proportional size-down.",
    )
    chili_momentum_risk_spread_abs_cap_em_scale_k: float = Field(
        default=1.0,
        ge=1.0,
        le=5.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RISK_SPREAD_ABS_CAP_EM_SCALE_K"),
        description="Multiplier on the adaptive EM ceiling for the scaled abs cap. The ONE documented base; k=1.0 = the cap never clamps below the EM-justified ceiling. A FLOOR (>=1.0), not a ceiling.",
    )
    # SKIP-FOR-LIMITS (operator 2026-06-23): the momentum entry is a marketable LIMIT
    # (place_limit_order_gtc at/above the guarded ask) — the LIMIT PRICE itself bounds the
    # fill cost, so the adaptive wide-spread gate is redundant for it (it protects against
    # MARKET-order slippage we don't do; it was rejecting the inherently-wide volatile
    # low-float movers the strategy targets — 2111 wide + the abs cap clipped NXTS 413bps).
    # When True, the ENTRY quote gate skips the tighter adaptive spread + stability checks and
    # uses only the abs_cap as a BROKEN-QUOTE ceiling (a halted/broken book is still rejected);
    # stale_bbo + invalid_bbo reliability checks ALWAYS apply. Spread is then handled as a sized
    # COST (the L2.2 liquidity risk multiplier) + the bounded limit, not a binary gate. 0 =
    # the full adaptive spread gate applies (legacy). EXITS / market paths unaffected.
    chili_momentum_skip_spread_gate_for_limit_entry: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SKIP_SPREAD_GATE_FOR_LIMIT_ENTRY"),
    )
    # FIX A — adaptive spread-cap robustness (the stale_bbo/wide_bbo #1-killer fix).
    # The live spread cap scales with the name's expected move (win-win): explosive
    # names tolerate proportionally more spread, quiet names keep the tight floor.
    # DEFECT: when the 15m frame is too cold/thin to compute an ATR-based expected
    # move (exactly the low-float names Ross trades pre-momentum), expected_move_bps
    # is None and the cap COLLAPSES to the 12bps mega-cap floor — blocking the very
    # movers we want. When True, derive a CONSERVATIVE adaptive fallback expected-move
    # from the name's OWN data (relaxed realized 15m range on as few as 2 bars, shrunk
    # by the factor below; else a price-tier floor) so the cap scales appropriately
    # instead of collapsing. WIN-WIN INVARIANT PRESERVED: the fallback is deliberately
    # an UNDER-estimate of the move (never over-allows), the abs_cap still hard-caps,
    # and a toxic wide spread on a genuinely small-move name STILL blocks. 0 / False =
    # the current collapse-to-floor behavior, byte-identical.
    chili_momentum_spread_cap_em_fallback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_CAP_EM_FALLBACK_ENABLED"),
    )
    # Conservative shrink applied to the fallback expected-move so the spread cap is
    # never loosened on the basis of a thin, noisy estimate (under-allow, never over).
    # One documented knob; 0.5 = trust half the relaxed realized-range as the move.
    chili_momentum_spread_cap_em_fallback_shrink: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_CAP_EM_FALLBACK_SHRINK"),
    )
    # Price-tier floor for the fallback when even a relaxed realized-range is
    # unavailable (truly no candles). Lower-priced low-floats structurally carry
    # wider bps spreads at the same dollar tick; this is the conservative expected
    # per-bar move (bps) we ascribe to a sub-$5 name so its cap doesn't collapse to
    # 12bps. Capped by the abs_cap regardless. One documented base; reference is a
    # FLOOR not a ceiling.
    chili_momentum_spread_cap_em_fallback_price_tier_bps: float = Field(
        default=150.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SPREAD_CAP_EM_FALLBACK_PRICE_TIER_BPS"),
    )
    # LOG-ONLY diagnostics for the #1 quote-quality killer: when True, _quote_quality_block
    # classifies a stale_bbo as a REAL age-breach (a quote row exists but is old) vs a
    # MISSING row (no IQFeed L1 coverage at all) and logs the distinction, plus emits a
    # structured line on every wide_bbo_spread block. NO behavior change — purely so the
    # operator can tell whether the killer is freshness, coverage, or spread.
    chili_momentum_quote_block_diagnostics: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_QUOTE_BLOCK_DIAGNOSTICS"),
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
    # 2026-07-04 — REMOVE THE FIXED STOP-OUT TIMER (default OFF). The wall-clock re-entry timer
    # above was a band-aid over an OLD substring-classifier bug (a losing trail_stop was mis-tagged
    # profit -> a 0.25x SHORT cooldown -> IPW re-armed in 3s -> -$78.62; fixed sign-authoritative in
    # risk_policy.adaptive_reentry_cooldown_seconds) AND it BLOCKS the fast Ross re-buy of a strong
    # leader on a shallow pullback. Accurate-FSM proof (CELZ 06-30 12:35-14:30): the 300s timer
    # suppressed the profitable 1.54/2.98 re-entries -> -$108; a 5s timer -> +$229. Ross watches no
    # clock — he re-enters when the SETUP RE-FORMS. With the timer OFF, re-entry quality is gated
    # DOWNSTREAM by the existing reentry_escalation_decision (structural trigger + HWM reclaim + tape
    # buyers, at escalation level>=1) PLUS the stopout-cycle cap and day-leader exemption — a real
    # setup condition, not a clock. A failing name won't show structure+buyers => won't re-enter
    # (the IPW protection, preserved); a strong leader on a shallow pullback WILL => Ross re-buy.
    # TRUE restores the legacy fixed timer (kill-switch).
    chili_momentum_stopout_cooldown_timer_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STOPOUT_COOLDOWN_TIMER_ENABLED"),
        description="2026-07-04: when FALSE (default) the fixed wall-clock stop-out re-entry timer is REMOVED — the session recycles to WATCHING immediately and re-entry quality is gated by reentry_escalation_decision (structure+reclaim+buyers) + the stopout-cycle cap. TRUE restores the legacy fixed timer.",
    )
    # TASK#8 — ADAPTIVE AFTER-EXIT COOLDOWN. The fixed 300s above is the documented BASE/floor;
    # scale it by the exit REASON (a clean profit/target exit => a SHORT re-scalp window so a
    # winner can be re-entered on the next micro-pullback — the TNMG case; a stop-out => full
    # base, sit out the chop) AND by the name's realized vol (entry_stop_atr_pct). The loss-side
    # reason_mult is pinned 1.0 so an adaptive cooldown is NEVER shorter than the base on a loss.
    # OFF ⇒ byte-identical (uses the fixed base verbatim).
    chili_momentum_adaptive_reentry_cooldown_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_REENTRY_COOLDOWN_ENABLED"),
        description="TASK#8: scale the fixed after-exit cooldown by exit-reason (profit => short re-scalp; loss => full base) and realized vol. OFF ⇒ byte-identical fixed base.",
    )
    chili_momentum_reentry_profit_cooldown_factor: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REENTRY_PROFIT_COOLDOWN_FACTOR"),
        description="TASK#8: multiplier on the base cooldown after a CLEAN profit/target exit so a winner can be re-scalped quickly (the TNMG re-enter-after-the-pop case). 0.25 => ~75s on a 300s base.",
    )
    chili_momentum_reentry_cooldown_vol_ref_atr_pct: float = Field(
        default=0.03,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REENTRY_COOLDOWN_VOL_REF_ATR_PCT"),
        description="TASK#8: reference ATR% for the vol-scaling of the re-entry cooldown (the ONE documented base; vol_mult = clamp(entry_stop_atr_pct / ref, 1/span, span)). A 3% ATR name sits at vol_mult 1.0.",
    )
    chili_momentum_reentry_cooldown_vol_span: float = Field(
        default=1.5,
        ge=1.0,
        le=5.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REENTRY_COOLDOWN_VOL_SPAN"),
        description="TASK#8: symmetric clamp span for the cooldown vol multiplier: vol_mult is bounded to [1/span, span] so a very high- or low-ATR name never explodes or zeroes the cooldown.",
    )
    # TASK#8 — BOUNDED RE-ENTRY AFTER STOP-OUT. After this many LOSS recycles for a name the
    # session terminalizes (FINISHED) instead of re-arming to WATCHING — a chopper cannot bleed
    # via unlimited re-arms. Profit recycles are free (never counted). OFF ⇒ unlimited (legacy).
    chili_momentum_reentry_after_stop_bound_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REENTRY_AFTER_STOP_BOUND_ENABLED"),
        description="TASK#8: kill-switch for the bounded re-entry-after-stop-out cap. True => after max_stopout_reentries LOSS recycles the session terminalizes. OFF ⇒ byte-identical unlimited recycle.",
    )
    chili_momentum_max_stopout_reentries: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MAX_STOPOUT_REENTRIES"),
        description="TASK#8: per-name/per-session cap on re-entries permitted after a STOP-OUT/loss. Only loss recycles count; profit recycles are unbounded. ge 1, le 10.",
    )
    chili_momentum_reentry_chase_cap_r: float = Field(
        default=1.5,
        ge=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REENTRY_CHASE_CAP_R"),
        description="ANTI-CHASE-THE-TOP re-entry guard (live_runner standalone gate). After a LOSING exit on a name, do NOT re-buy far ABOVE where the last attempt failed. Block a re-entry whose price is more than this many ATR ABOVE the prior losing tranche's HIGH-WATER-MARK. The unit is the name's ATR (its honest 'how far it moves'), NOT the prior stop_distance — a pathologically wide prior stop (SVRE 06-30: stop_distance=0.737 ≈ 10% of $7.54) would otherwise inflate the ceiling past every chase. SVRE 06-30: stopped 7.54->7.51 (hwm 7.70, ATR≈0.385), then re-entered wick-reclaims at 8.34/8.25/8.70 — all >1.5 ATR above 7.70, into the 8.91 top -> faded (-$7); the cap blocks the FIRST chase, which cascades (that trade never opens, so the 7.70 anchor holds) -> SVRE takes only the -$0.33 initial stop. JEM-style profit-recycle re-entries are NEVER touched (was_loss=False). The ONE documented base (default 1.5 ATR). 0 disables (byte-identical, unbounded chase).",
    )
    chili_momentum_reentry_chase_cap_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REENTRY_CHASE_CAP_ENABLED"),
        description="Master kill-switch for the anti-chase re-entry gate (live_runner). ON ⇒ after any loss exit (was_loss) at any escalation level, block a re-entry whose price is more than chili_momentum_reentry_chase_cap_r ATR-multiples above the prior losing tranche's high-water-mark (see that field for the full rationale + the SVRE worked example). OFF ⇒ the gate is a no-op (byte-identical, unbounded chase).",
    )
    # ── G4 (losers-eat-the-winner fix, 2026-07-03): grind-aware exits + same-symbol
    # re-entry escalation. Two kill-switches, everything else derived (leader = the
    # existing within-day p90 live_eligible rank; structure = the 5m EMA-9 / confirmed
    # higher-low the trail already anchors on; escalation margins in the trade's OWN
    # frozen risk-distance units). Single-flag rollback each.
    chili_momentum_g4_grind_exit_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_G4_GRIND_EXIT_ENABLED"),
        description="G4 P1: GRIND/TREND exit mode on the held runner. When the position's symbol is the day leader (top-ranked/p90/wildcard-dominant), cadence is FAST, >=1R peak and a confirmed HIGHER-LOW above entry has formed, the exit machinery switches to STRUCTURE-trailing: climax-lock ratchets are clamped to the 5m-EMA9/higher-low structure floor (candidates only — the placed stop NEVER loosens, INVARIANT-A), the topping-tail full-flatten defers to the structure trail, and the pyramid re-add cap becomes cushion-adaptive. Fail-CLOSED: any missing/uncertain input ⇒ scalp behavior byte-identical. OFF ⇒ byte-identical.",
    )
    chili_momentum_g4_reentry_escalation_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_G4_REENTRY_ESCALATION_ENABLED"),
        description="G4 P2: SAME-SYMBOL re-entry escalation. After each stop-out the NEXT entry on the name needs higher-quality confirmation (a STRUCTURAL trigger class + price reclaim of the failed attempt's high-water mark, margin scaling with consecutive stops in the trade's own risk-distance units, + positive tape when readable). Never a lockout — a WAIT that clears when the market proves the level. Resets on a green banked round (green_banked_reentry_free parity). The day-leader additionally bypasses the TASK#8 terminal cap (escalation still applies). OFF ⇒ byte-identical.",
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
    # WAVE-4 ITEM-6(b): ARM-TIME MINIMUM-REMAINING-BUDGET viability refresh. At confirm, a
    # viability row whose age is already past HALF the max-age (600s) has < 0.5x the budget
    # left, so the entry could go stale mid-tick (DXST confirmed at 537s/600s and died). When
    # ON, if the age at confirm > 0.5 x chili_momentum_risk_viability_max_age_seconds, inline
    # re-score the ONE symbol via the existing pipeline seam (run_momentum_neural_tick) and
    # confirm ONLY on the fresh score — NEVER blind-touch freshness_ts (that would fake
    # freshness without re-validating live-eligibility). Reuses the ONE documented 600s base.
    # KILL-SWITCH: False -> confirm uses the row as-is (byte-identical to pre-ITEM-6).
    chili_momentum_arm_time_viability_refresh_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ARM_TIME_VIABILITY_REFRESH_ENABLED"),
        description="WAVE-4 ITEM-6(b): at confirm_live_arm, if the viability row age > 0.5 x chili_momentum_risk_viability_max_age_seconds, inline re-score the symbol via run_momentum_neural_tick and confirm only on the FRESH score (never blind-touch freshness_ts). Ensures a minimum-REMAINING freshness budget at arm time (DXST died at 537s/600s). KILL-SWITCH: False -> confirm uses the row as-is (byte-identical).",
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
    # Live-eligibility TOCTOU recency grace (2026-06-29 UPC +500% miss). When ON, a
    # live_eligible=False FLICKER at the entry instant is downgraded to a warn iff the
    # session was live-eligible at arm/confirm within the grace window AND there is live
    # forward momentum — so a transient re-scoring flicker can't terminally veto a
    # just-confirmed active mover. OFF => byte-identical (block on any flicker). Only
    # relaxes on positive evidence; never touches the drawdown/kill-switch/max-loss blocks.
    chili_momentum_live_eligible_recency_grace_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_ELIGIBLE_RECENCY_GRACE_ENABLED"),
    )
    chili_momentum_live_eligible_recency_grace_seconds: float = Field(
        default=90.0,
        ge=1.0,
        le=900.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_ELIGIBLE_RECENCY_GRACE_SECONDS"),
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
    # ADAPTIVE daily-loss cap (operator 2026-06-11): pct-of-equity governs; the
    # fixed-USD leg is an explicit OVERRIDE only (0 = disabled). If equity cannot
    # be resolved, the failsafe floor below applies (fail closed, never uncapped).
    chili_global_max_daily_loss_usd: float = Field(
        default=0.0,
        ge=0.0,
        le=1_000_000.0,
        validation_alias=AliasChoices("CHILI_GLOBAL_MAX_DAILY_LOSS_USD"),
    )
    chili_global_max_daily_loss_pct_of_equity: float = Field(
        # 5% of the live trading account = the operator's equity-relative daily-loss budget
        # (~$515 off $10.3k BP / ~$687 off $13.7k equity). Raised 2026-06-22 from a spurious
        # 1.5% that, combined with the wrong None->Coinbase equity basis (~$3.7k), produced a
        # $55 cap that froze the $13.7k agentic lane on an -$84 day. 5% matches the momentum
        # per-family daily-loss fraction so the global + lane caps are now ONE coherent number.
        # [[feedback_adaptive_no_magic]] [[project_per_broker_daily_loss]]
        default=0.05,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_GLOBAL_MAX_DAILY_LOSS_PCT_OF_EQUITY"),
    )
    chili_global_daily_loss_failsafe_usd: float = Field(
        default=300.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_GLOBAL_DAILY_LOSS_FAILSAFE_USD"),
        description="Daily-loss floor used ONLY when pct-of-equity is configured but equity cannot be resolved (fail closed).",
    )
    # INTRADAY-RECOVERY SELF-HEAL (operator 2026-06-16: a transient morning −$300
    # blip that recovered to +$265 stayed frozen ALL DAY because the only auto-clear
    # was the ET-day-roll → the whole profitable day was locked out). When the daily
    # kill switch is active for a global_daily_loss_breach AND today's realized PnL
    # has climbed back to ABOVE -(cap * fraction), the breach no longer describes
    # reality → self-clear. The fraction is a HYSTERESIS band (recovery must clear it
    # by a margin, so realized hovering at the cap cannot trip/clear/trip). Relative
    # to the cap (adaptive, not a fixed $). Set <= 0 to disable (date-roll-only).
    chili_daily_loss_recovery_clear_fraction: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_DAILY_LOSS_RECOVERY_CLEAR_FRACTION"),
        description="Auto-clear a daily-loss kill switch when realized recovers to >= -(cap*fraction). 0 disables (manual/date-roll only).",
    )
    chili_daily_loss_recovery_check_interval_s: float = Field(
        default=30.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_DAILY_LOSS_RECOVERY_CHECK_INTERVAL_S"),
        description="Throttle for the intraday-recovery auto-clear PnL check (is_kill_switch_active is on the hot order path).",
    )
    # PER-BROKER daily-loss caps (operator 2026-06-15: "dapat ang kill switch is
    # by broker"). Each broker is capped off ITS OWN real equity; a breach blocks
    # ONLY that broker (NOT the single global kill switch), so a Coinbase loss can
    # never freeze Robinhood and exits/true-global halts are unaffected. Reuses the
    # existing pct/usd knobs above (no new magic number for the cap itself).
    chili_per_broker_daily_loss_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_PER_BROKER_DAILY_LOSS_ENABLED"),
        description="When True, daily-loss caps are evaluated PER BROKER (off each broker's real equity). =0 reverts to the single global check (with the None->Coinbase equity-basis bug still fixed at the activator callsites).",
    )
    chili_per_broker_count_manual_as_rh: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_PER_BROKER_COUNT_MANUAL_AS_RH"),
        description="When True, manual (broker_source='manual') Trade rows count against the Robinhood per-broker budget. Default False (operator-originated, not lane-generated). reconcile_import is always excluded.",
    )
    chili_per_broker_aggregate_backstop_mult: float = Field(
        default=1.0,
        ge=1.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_PER_BROKER_AGGREGATE_BACKSTOP_MULT"),
        description="Global catastrophic backstop: if AGGREGATE realized loss across brokers exceeds (sum of per-broker caps) * this multiple, the true global kill switch trips. 1.0 = trip at the combined budget.",
    )

    # Lane-health FROZEN alert. A tripped safety breaker (global kill switch or a
    # per-broker daily-loss block) silently empties the momentum lane; on 2026-06-15
    # the lane sat frozen ~8h before the operator noticed. This emits a LOUD signal
    # (logger.critical + a cockpit banner + an audit row) so a frozen lane is never
    # silent again. Reversible: =0 fully reverts to the prior silent behaviour.
    chili_lane_health_alert_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_LANE_HEALTH_ALERT_ENABLED"),
        description="When True, the scheduler watches the momentum lane and raises a loud FROZEN alert (critical log + cockpit banner + audit row) when a safety breaker has held the lane idle past the grace window. =0 disables the alert entirely (the breakers themselves are unaffected).",
    )
    chili_lane_health_freeze_alert_seconds: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_LANE_HEALTH_FREEZE_ALERT_SECONDS"),
        description="Grace before a held safety state counts as FROZEN. 0 = ADAPTIVE (derive from the lane's own watch cadence: auto_arm max_watch + watch_extend) so there is no separate magic number; a positive value overrides it. The same value is reused as the re-remind cooldown so a long freeze keeps nagging without spamming.",
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
    chili_autotrader_live_require_feature_parity: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_LIVE_REQUIRE_FEATURE_PARITY"),
    )
    chili_feature_parity_fail_closed_on_error: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_FEATURE_PARITY_FAIL_CLOSED_ON_ERROR"),
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
    # Ross-style cadence: a momentum entry/exit window is fleeting (seconds-
    # minutes), so the 2min floor above missed fast breaks. When > 0 this
    # SECONDS cadence wins over the minutes knob. 30s = 4x faster, safely above
    # the ~12s batch run time (max_instances=1 + coalesce prevent overlap).
    chili_momentum_live_runner_scheduler_interval_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_RUNNER_SCHEDULER_INTERVAL_SECONDS"),
    )
    # The live-runner batch ticks each open live session on a small bounded pool
    # so the batch wall-time is ~the slowest single session, not the SERIAL SUM
    # of every session's network I/O (Coinbase quote/product + OHLCV trigger
    # fetch). Serial fan-out over ~5 sessions was overrunning the 30s cadence.
    # 0 (default) DERIVES the cap from chili_momentum_risk_max_concurrent_live_sessions
    # — no second magic number; set > 0 only to throttle parallelism independently
    # (e.g. to be gentler on Coinbase rate limits). Each worker owns its own DB
    # Session + adapter; entry/exit/risk semantics are unchanged. [[project_momentum_lane]]
    chili_momentum_live_runner_batch_workers: int = Field(
        default=0,
        ge=0,
        le=20,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LIVE_RUNNER_BATCH_WORKERS"),
    )
    # Auto-arm-live: autonomously arm ONE live session for the fresh, live-
    # eligible candidate whose momentum trigger is firing now (Ross "the one
    # moving right now"). Guarded by kill-switch + drawdown + concurrency=1 +
    # broker can_trade + equity-relative caps via the operator arm flow.
    chili_momentum_auto_arm_live_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_LIVE_ENABLED"),
    )
    chili_momentum_auto_arm_live_scheduler_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_ENABLED"),
    )
    chili_momentum_auto_arm_live_scheduler_interval_seconds: int = Field(
        default=30,
        ge=10,
        le=3600,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_LIVE_SCHEDULER_INTERVAL_SECONDS"),
    )
    # Max DISTINCT live-eligible candidates probed for an entry trigger per auto-arm tick.
    # Was 10, which on a hot day (many 24h movers) truncated the board by viability_score
    # and STARVED a fresh-firing mid-viability name (NPT Jun-8 was live-eligible at 17:11
    # but ranked #11+ behind 8 names up >100%, so it was never probed until the board
    # thinned ~18:51 — after its setup). The M4 freshness picker only re-ranks WITHIN this
    # slice, so the slice width was the leak. Widened so the freshness picker sees the whole
    # fresh board; the probe wave itself is bounded by the time budget below (derived
    # ~= trigger_workers x budget / per-probe-latency), so a larger ceiling never blows the
    # ~30s scheduler cadence. docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md
    chili_momentum_auto_arm_scan_limit: int = Field(
        default=40,
        ge=1,
        le=100,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_SCAN_LIMIT"),
    )
    # Wall-clock budget for the whole concurrent entry-trigger probe wave. Auto-arm arms
    # from whatever has COMPLETED within this budget (the freshest-firing of those wins);
    # any candidate not probed in time defers to the next tick. This is the ADAPTIVE control
    # on probe breadth (breadth = as many as finish in the budget — no magic candidate count)
    # AND the safety belt that keeps a wide net inside the scheduler cadence. Kept < 30s.
    chili_momentum_auto_arm_probe_time_budget_seconds: float = Field(
        default=18.0,
        ge=1.0,
        le=29.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_PROBE_TIME_BUDGET_SECONDS"),
    )
    # Selection->entry alignment (M4 keystone): the viability board ranks 24h movers,
    # but many FADE into a deep intraday retrace before the pullback gate sees them
    # (faded names = 0.00% break fire-rate over recent bars). When True (default) auto-arm
    # drops faded names and watches the FRESHEST name positively in an intraday up-impulse
    # near its recent high — Ross's "the one moving right now" — instead of pinning the
    # single live slot on the stale 24h leader. The freshness "near-high" bar reuses the
    # entry gate's own retracement_threshold (no separate magic cutoff). Set False to
    # restore arm-only-on-an-active-break. docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md
    chili_momentum_auto_arm_require_fresh_impulse: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_REQUIRE_FRESH_IMPULSE"),
    )
    # DUAL-PATH PARITY (2026-06-15): the auto-arm selection probe must evaluate the
    # SAME settings-resolved Ross trigger the live + paper runners use
    # (``momentum_pullback_trigger``: require_retest/sustained-vol/candle/VWAP/MACD/
    # runaway/verticality + deep_reclaim + dip-buy, symbol-aware), NOT a raw
    # ``pullback_break_confirmation`` with library defaults (require_retest=False).
    # The defaults call dispatched ``_evaluate_raw_break``, which can NEVER reach the
    # deep_reclaim path (only the require_retest=True ``_evaluate_break_retest`` does),
    # so deep-retrace reclaim / dip-buy setups the live runner WOULD enter (MTEN,
    # KAIO-USD, EDHL) were INVISIBLE to selection and never armed, while raw breaks the
    # live runner then DECLINED were armed (wasted churn). ON = parity (the correct
    # behaviour); set False to revert to the legacy library-defaults probe.
    # docs/DESIGN/MOMENTUM_LANE.md [[project_equity_alist_a0_state]]
    chili_momentum_auto_arm_trigger_parity_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_TRIGGER_PARITY_ENABLED"),
    )
    # Auto-arm checks each candidate's entry trigger via an OHLCV fetch; run them
    # concurrently so a pass is ~the slowest single fetch (not the serial sum).
    chili_momentum_auto_arm_trigger_workers: int = Field(
        default=8,
        ge=1,
        le=32,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_TRIGGER_WORKERS"),
    )
    # Reap a pre-entry live session that has watched this long without entering,
    # freeing the slot for a fresher mover (Ross moves on; default 30min).
    # Watch reap (2026-06-12 throughput study): triggers that EVER fire do so in
    # 29s median / 56s p75 — dead watches squatted slots for the full 1800s
    # (84-97% of armed sessions died at the reap; ~32 slot-hours dead in one
    # day). 300s base; tick-armed setups (watch_break_level set = a reclaim is
    # actually forming) get chili_momentum_auto_arm_watch_extend_seconds.
    chili_momentum_auto_arm_max_watch_seconds: int = Field(
        default=300,
        ge=60,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_MAX_WATCH_SECONDS"),
    )
    chili_momentum_auto_arm_watch_extend_seconds: int = Field(
        default=600,
        ge=60,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_WATCH_EXTEND_SECONDS"),
    )
    # ADAPTIVE MAX-WATCH (2026-06-25, operator "gawin mong adaptive — no magic wall clock"):
    # the effective watch deadline adapts to whether the watcher is BUILDING a setup or
    # DEAD, derived from signals ALREADY on the session snapshot (no new data source):
    # a tick-armed session (watch_break_level set) whose last_mid is APPROACHING the break
    # level (within proximity_pct, conservatively close) earns the EXTEND window (it is
    # about to fire — keep the slot); a flat/oscillating watch with no level or price far
    # from the level reaps at the BASE window (free the slot fast for a fresher mover).
    # Refines the existing binary (watch_break_level + within extend_cutoff) with a
    # proximity classifier. CONSERVATIVE: any missing signal -> treat as BUILDING (keep the
    # slot — never cut a genuinely-building setup short). FLAG OFF => byte-identical to the
    # current fixed base/extend binary. ONE documented base knob (proximity_pct) + the
    # existing base/extend windows as the floor/ceiling clamp.
    chili_momentum_adaptive_watch_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_WATCH_ENABLED"),
        description="Kill-switch for the adaptive max-watch deadline. OFF => the exact current fixed base/extend binary (byte-identical). ON => a tick-armed watcher whose price is approaching its break level earns the extend window; one far from the level reaps at the base window.",
    )
    chili_momentum_adaptive_watch_proximity_pct: float = Field(
        default=1.5,
        ge=0.0,
        le=50.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_WATCH_PROXIMITY_PCT"),
        description="The ONE base knob: a tick-armed watcher counts as BUILDING (earns the extend window) when last_mid is within this percent of its watch_break_level. Wider => more watchers earn the extend (more conservative, keeps more slots). Tuned so a typical near-break name reproduces ~the current extend behavior.",
    )
    # EVENT / STRUCTURE-BASED ABANDONMENT (2026-06-26, operator: "Ross stays on a strong
    # stock all day — kill the fixed wall-clock"). ROOT (IVF +66%, 14 clean 2-red-pullback
    # setups across the day; CHILI armed it only EARLY, got reaped at the base window, and
    # never watched it during its 14 setups). The FIX: before reaping a stale pre-entry
    # watcher, ask whether the NAME is still worth watching, not whether a clock expired:
    #   * KEEP (do NOT reap) if it is STILL HIGH-CONVICTION (ross_score>=floor OR rvol>=the
    #     coiling-exempt extreme floor OR daily_breaking_major — the SAME conviction the
    #     arm-queue/continuation gate read) AND STILL FRONT-SIDE (not faded/backside/below
    #     VWAP per the cached snapshot). Such a name is still setting up; keep its slot so the
    #     lane is watching when its next pullback fires.
    #   * REAP (exactly as today) the instant it FADES / goes backside / cools out of high
    #     conviction — so a cooled name never leaks its slot.
    # HARD FALLBACK CEILING: even a kept session reaps past an absolute max
    # (chili_momentum_event_based_max_extend_seconds) so a truly-stuck watcher cannot watch
    # forever. Conviction is read from a SINGLE bulk viability query built BEFORE the reap
    # loop (no per-session fetch in the loop); front-side is read from the session's OWN
    # cached snapshot (fail-open to front-side when absent — never veto a keep candidate
    # short on missing data). FLAG OFF => the reap loop is byte-identical to the fixed
    # base/extend clock (no conviction/front-side check runs at all).
    chili_momentum_event_based_abandonment_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EVENT_BASED_ABANDONMENT_ENABLED"),
        description="Kill-switch for event/structure-based abandonment of pre-entry watchers. OFF (default) => the exact current fixed base/extend reap clock (byte-identical). ON => a still-high-conviction, still-front-side mover keeps its watch slot past the clock (Ross stays on a strong stock all day) until it fades/cools or hits the hard ceiling; a faded/cooled name reaps exactly as today.",
    )
    # The hard fallback ceiling for a KEPT (high-conviction, front-side) watcher, derived
    # ADAPTIVELY from the extend window (ONE documented multiple — no scattered magic clock):
    # ceiling = chili_momentum_event_based_max_extend_mult * chili_momentum_auto_arm_watch_extend_seconds.
    # A watcher that has watched longer than this absolute max reaps EVEN IF still high-
    # conviction + front-side, so a name that never triggers all day cannot squat a slot
    # forever. Default mult 3.0 over the 600s extend => 1800s (30min) ceiling; raise to let a
    # strong leader ride longer, lower to recycle slots sooner.
    chili_momentum_event_based_max_extend_mult: float = Field(
        default=3.0,
        ge=1.0,
        le=24.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EVENT_BASED_MAX_EXTEND_MULT"),
        description="Adaptive hard-ceiling multiple over the extend window: a kept high-conviction watcher reaps once it has watched longer than this * chili_momentum_auto_arm_watch_extend_seconds, even if still high-conviction + front-side. Caps a truly-stuck session. ONE documented knob (no fixed second-count); derived from the extend window so it scales with it.",
    )
    # Backside detection for the front-side keep gate: a kept watcher is demoted to REAP when
    # the cached snapshot shows it has retraced MORE than this fraction of its day's up-move
    # from the high-of-day (Ross's "it faded — move on"). Mirrors front_side_state's
    # retrace_veto so the reaper's faded test matches the entry gate's. Only applied when the
    # snapshot AFFIRMATIVELY carries the retrace/HOD evidence; absent => fail-open front-side.
    chili_momentum_event_based_retrace_veto: float = Field(
        default=0.66,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EVENT_BASED_RETRACE_VETO"),
        description="Faded-backside threshold for the keep gate: a kept watcher reaps when its cached snapshot shows retrace_from_hod > this fraction of the day's up-move. Mirrors front_side_state.retrace_veto. Only applied when the snapshot carries the evidence (else fail-open front-side).",
    )
    # ── MOVE-EXHAUSTION ABANDON (pre-arm VETO; ORTHOGONAL to the reaper above) ─────────
    # A RISK-REDUCING pre-arm veto: when a fresh entry trigger fires but the move is
    # GENUINELY EXHAUSTED by MULTIPLE AGREEING signals, REFUSE to arm a new watcher (sit
    # flat on a done move) rather than chase the last leg. AGREEMENT RULE (conservative,
    # never over-restrictive): abandon ONLY when FADED-FROM-HOD **AND** (COLD-TAPE **OR**
    # VIABILITY-REGRESSED). A still-front-side strong mover (near HOD, hot tape, viability
    # high) NEVER trips this — it still arms. This gates NEW arming only; it does NOT touch
    # the event-based reaper (which KEEPS strong watchers) — the two are orthogonal.
    # FLAG OFF (default) => the exhaustion check never runs => arm-time is BYTE-IDENTICAL.
    chili_momentum_move_exhaustion_abandon_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MOVE_EXHAUSTION_ABANDON_ENABLED"),
        description="Kill-switch for the pre-arm move-exhaustion abandon veto. OFF (default) => no exhaustion check runs => arm-time is byte-identical. ON => after a trigger fires, REFUSE the arm only when the move is exhausted by AGREEING signals: faded-from-HOD AND (cold-tape OR viability-regressed). A still-front-side strong mover (near HOD, hot tape, viability high) still arms.",
    )
    # FADED-FROM-HOD threshold for the exhaustion veto. Reuses front_side_state's own
    # retrace_from_hod (fraction of the day's up-move retraced off the HOD) — ADAPTIVE by
    # construction (name-relative ratio, no fixed %). Defaults to the SAME base as the
    # event-based reaper's retrace_veto so the lane has ONE documented "it has faded"
    # boundary. A move that has retraced MORE than this fraction off its HOD is "faded".
    chili_momentum_move_exhaustion_retrace_floor: float = Field(
        default=0.66,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MOVE_EXHAUSTION_RETRACE_FLOOR"),
        description="Faded-from-HOD threshold for the exhaustion veto: the move is 'faded' when front_side_state.retrace_from_hod exceeds this fraction of the day's up-move. Adaptive (name-relative ratio). Defaults to the same base as chili_momentum_event_based_retrace_veto so there is one documented 'faded' boundary.",
    )
    # VIABILITY-REGRESSED threshold for the exhaustion veto: the move's conviction has
    # regressed when its CURRENT best signal (ross_score, normalized) has dropped by at
    # least this FRACTION below its recent session PEAK (tracked in-process per symbol). A
    # name still at/near its peak is NOT regressed. Adaptive: measured as a fraction of the
    # name's OWN peak (no fixed score magnitude). 0 disables the viability-regressed axis.
    chili_momentum_move_exhaustion_regress_frac: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MOVE_EXHAUSTION_REGRESS_FRAC"),
        description="Viability-regressed threshold for the exhaustion veto: the conviction has regressed when the current ross_score has fallen by at least this fraction below its recent in-process session peak. Adaptive (fraction of the name's own peak). 0 disables this axis.",
    )
    # ── NO-A-SETUP SESSION SIT-CASH GATE (NEW-INITIATION ONLY) ─────────────────────────
    # A CONSERVATIVE, margin-gated session-level veto: SUPPRESS a fresh entry initiation only
    # when the day's BEST available setup quality (top ross_score among the fresh live-eligible
    # board) is CLEARLY below an A+ bar (by a documented margin) AND the regime is poor (cold
    # tape-breadth AND no fresh news catalyst on any candidate). Ross sits in cash when nothing
    # A+ is up — but a genuine A+ (explosive top ross_score + catalyst) MUST still initiate, and
    # a BORDERLINE-good setup still trades (the margin prevents over-restriction). NEW-INITIATION
    # ONLY: this gate is evaluated ONCE per auto-arm pass, BEFORE the candidate scan/arm loop —
    # it NEVER blocks, delays, or downsizes any EXIT / stop / trail / scale-out / flatten / open-
    # position management (those run exclusively in the live runner, which does not consult this
    # gate). FLAG OFF (default) => the gate never runs and run_auto_arm_pass is BYTE-IDENTICAL
    # (no new query, no new logic). docs/DESIGN/MOMENTUM_LANE.md [[feedback_adaptive_no_magic]]
    chili_momentum_no_asetup_sit_cash_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NO_ASETUP_SIT_CASH_ENABLED"),
        description="Kill-switch for the no-A-setup session sit-cash gate (NEW INITIATION ONLY). OFF (default) => the gate never runs => run_auto_arm_pass is byte-identical. ON => suppress a FRESH arm only when the board's best ross_score is CLEARLY below an adaptive A+ bar (by a margin) AND the regime is poor (cold tape AND no fresh catalyst). A genuine A+ (top ross_score + catalyst) still initiates; a borderline-good setup still trades; it NEVER blocks or downsizes an exit/position-management action.",
    )
    # The ONE documented margin knob for the sit-cash gate's adaptive A+ bar. The A+ bar is
    # max(A-setup conviction floor, median_ross - margin_multiple * std_dev_ross) over the fresh
    # board's ross_score distribution — so the bar adapts to the tape (a hot board raises it, a
    # cold board lowers it toward the conviction floor) with NO fixed numeric cutoff. A LARGER
    # margin lowers the bar (more permissive — suppress only when the best is far below median);
    # a SMALLER margin raises it (stricter). Default 1.0 (one std-dev below median).
    chili_momentum_no_asetup_sit_cash_margin_multiple: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NO_ASETUP_SIT_CASH_MARGIN_MULTIPLE"),
        description="The ONE documented margin for the sit-cash gate's adaptive A+ bar: bar = max(A-setup conviction floor, median_ross - this * std_dev_ross). Larger => more permissive (lower bar); smaller => stricter. Adaptive to the board's ross_score distribution (no fixed cutoff).",
    )
    # ── TIME-OF-DAY SCHEDULE (prime-window size lever + fade-driven late-day cutoff) ────────
    # NEW-INITIATION ONLY. Kill-switch; OFF (default) => byte-identical (the gate never runs,
    # no prime-window mult, no late-day suppression). ON => (1) a BOUNDED-UPWARD size multiplier
    # (>=1.0, <= prime_window_size_mult_max) during the documented prime window that composes
    # into the SAME live_runner _eff_max_loss product under the 3x clamp (so it can NEVER push
    # notional past base*3.0 and is NEVER a veto); (2) a FADE-DRIVEN late-day NEW-ENTRY cutoff —
    # suppress a fresh arm only when the day's momentum/breadth has FADED (reusing the SAME
    # tape-cold-breadth + catalyst regime signal the no-asetup-sit-cash gate uses), with the
    # fallback clock as a documented hard-ceiling (a strong-momentum afternoon still trades).
    # It NEVER blocks/delays/downsizes an EXIT or open-position management (live runner owns
    # those, ungated). docs/DESIGN/MOMENTUM_LANE.md [[feedback_adaptive_no_magic]]
    chili_momentum_timeofday_schedule_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIMEOFDAY_SCHEDULE_ENABLED"),
        description="Kill-switch for the time-of-day schedule (prime-window size lever + fade-driven late-day NEW-entry cutoff), NEW-INITIATION ONLY. OFF (default) => byte-identical (no prime-window mult, no late-day suppression). ON => bounded-upward size boost in the prime window (composes under the 3x ceiling, never a veto) + suppress a fresh arm when day momentum/breadth has FADED past the fallback clock. NEVER touches an exit/open-position management.",
    )
    # Prime window bounds (ONE base each). Default 04:00-10:30 ET = the documented premarket+open
    # drive band (mirrors schedule_window_now's "hot" window — ONE canonical window, no new magic
    # bound). Parsed via market_profile._parse_hhmm; malformed => the documented fallback minutes.
    chili_momentum_timeofday_prime_window_start_et: str = Field(
        default="04:00",
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIMEOFDAY_PRIME_WINDOW_START_ET"),
        description="Prime-window OPEN, 'HH:MM' ET (default 04:00 = premarket drive start). Inside [start,end) the bounded-upward size lever applies. Documented base; parsed via _parse_hhmm.",
    )
    chili_momentum_timeofday_prime_window_end_et: str = Field(
        default="10:30",
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIMEOFDAY_PRIME_WINDOW_END_ET"),
        description="Prime-window CLOSE, 'HH:MM' ET (default 10:30 = end of the open drive). Documented base; parsed via _parse_hhmm.",
    )
    # The ONE bound on the prime-window size lever. Clamped to [1.0, this]; the lever is NEVER
    # < 1.0 (never a shrink) and NEVER a veto. It composes into the runner's _eff_max_loss product
    # under the SAME min(..., base*3.0) clamp + hard notional ceiling, so it can never escape 3x.
    chili_momentum_timeofday_prime_window_size_mult_max: float = Field(
        default=1.5,
        ge=1.0,
        le=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIMEOFDAY_PRIME_WINDOW_SIZE_MULT_MAX"),
        description="Max bounded-upward size multiplier for the prime window (default 1.5x). Clamped [1.0, this]; never < 1.0, never a veto. Composes under the live_runner 3x combined-multiplier ceiling, so it can NEVER push notional past base*3.0.",
    )
    # The DOCUMENTED FALLBACK CLOCK (a ceiling, NOT the primary driver). The cutoff is FADE-DRIVEN
    # (momentum/breadth faded per the regime signal); this clock only gates WHEN that fade is
    # allowed to suppress a fresh entry. A strong-momentum afternoon (no fade) still trades past it.
    chili_momentum_timeofday_fallback_clock_et: str = Field(
        default="14:30",
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIMEOFDAY_FALLBACK_CLOCK_ET"),
        description="Documented hard-ceiling fallback clock, 'HH:MM' ET (default 14:30 = end of midday lull). NOT the primary driver: a NEW entry is suppressed only when the day momentum/breadth has FADED *and* the clock is at/past this. A strong-momentum (non-faded) afternoon still initiates. Parsed via _parse_hhmm.",
    )
    chili_momentum_timeofday_fade_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TIMEOFDAY_FADE_ENABLED"),
        description="When ON (default), the late-day cutoff is FADE-DRIVEN: past the fallback clock, suppress a NEW entry only if the day momentum/breadth has FADED (cold tape-breadth AND no fresh catalyst, reusing the no-asetup-sit-cash regime signal). When OFF, the cutoff is clock-only (past the fallback clock => suppress). Either way it NEVER touches an exit.",
    )
    # ADAPTIVE REAP-COOLDOWN (2026-06-25): scale the post-reap sit-out by the per-symbol
    # OSCILLATION COUNT (how many arm->reap loops the name has churned recently). A first
    # reap = the short base; a serial oscillator (RENDER looped 88x) = a long cooldown,
    # clamped. cooldown = base * (1 + osc_count * step), capped at max_mult * base. Reuses
    # the in-process _REAP_COOLDOWN dict pattern (a parallel _REAP_OSCILLATION counter with
    # the same bounded-prune + TTL decay). FLAG OFF => the exact fixed base (byte-identical).
    chili_momentum_adaptive_reap_cooldown_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_REAP_COOLDOWN_ENABLED"),
        description="Kill-switch for the oscillation-scaled reap cooldown. OFF => the fixed chili_momentum_reap_cooldown_sec for every reap (byte-identical). ON => a serial arm->reap oscillator sits out progressively longer (clamped).",
    )
    chili_momentum_adaptive_reap_cooldown_step: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_REAP_COOLDOWN_STEP"),
        description="Per-oscillation multiplier increment: cooldown = base * (1 + osc_count * step). osc_count is the number of recent arm->reap loops for the symbol (0 on the first reap => exactly the base). Higher => serial oscillators are damped harder.",
    )
    chili_momentum_adaptive_reap_cooldown_max_mult: float = Field(
        default=6.0,
        ge=1.0,
        le=50.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADAPTIVE_REAP_COOLDOWN_MAX_MULT"),
        description="Hard cap on the oscillation multiplier so a runaway loop can never freeze a name indefinitely: cooldown <= max_mult * base.",
    )
    # Post-reap cooldown (seconds): after a name is reaped pre-entry (watched the
    # full window without firing), sit it out this long before it can re-arm, so
    # the same non-firing name (RENDER/WLD looped 88x/56x/24h) stops hogging the
    # single live slot — a different fresh mover gets watched instead. 0 disables
    # (instant kill-switch). One watch-window default; env-tunable.
    chili_momentum_reap_cooldown_sec: float = Field(
        default=300.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REAP_COOLDOWN_SEC"),
    )
    # ENTRY-REJECT cooldown (seconds): when the broker REFUSES a live entry
    # (place_equity_order isError — e.g. a leveraged/inverse ETF tripping
    # EQUITY_SUITABILITY like RKLZ/CORD, or a name untradable in the current session),
    # sit that name out this long before it can re-arm, so the lane stops looping
    # arm->break->reject->reap on a name the rail won't fill and a FILLABLE mover gets
    # the slot. ADAPTIVE: the lane learns the unfillable names from the real rejections
    # (no hardcoded leveraged-ETF list, no per-tick broker call); SELF-HEALING via this
    # TTL (a transient halt re-arms after it clears). 3x the reap window (suitability
    # blocks are more persistent than a no-break reap). 0 disables (instant kill-switch).
    # Diagnosed 2026-06-22 (RKLZ 5x/CORD 4x isError loop). env-tunable.
    chili_momentum_entry_reject_cooldown_sec: float = Field(
        default=900.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_REJECT_COOLDOWN_SEC"),
    )
    # AGENTIC-TRADABILITY PRE-FILTER (learn-from-401, 2026-06-29): the Robinhood AGENTIC
    # MCP rail rejects SOME instruments at entry-place with a 401 Unauthorized ("instrument
    # not available for agentic trading on the isolated CASH account" — CTNT 2026-06-29).
    # When a place returns that signature, RECORD the symbol as non-agentic-tradeable in a
    # bounded, TTL'd in-memory set, then SKIP it at ARM so it never becomes a watcher/entry
    # again — the lane stops wasting the single slot looping arm->break->401 on a name the
    # rail will never fill, and a FILLABLE mover gets the slot. ADAPTIVE (learns the
    # untradeable names from the real rejections, no hardcoded list, no per-candidate broker
    # pre-check), SELF-HEALING (TTL ~1 trading day — tradability can change). Scoped to the
    # robinhood_agentic_mcp family ONLY (crypto/alpaca have different tradable universes).
    # FAIL-OPEN: any cache/lookup error lets the name try (the 401 re-catches) — the
    # pre-filter never starves the lane. Flag-OFF => byte-identical (no recording, no skip).
    chili_momentum_agentic_tradability_prefilter_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AGENTIC_TRADABILITY_PREFILTER_ENABLED"),
    )
    # TTL for the agentic non-tradeable negative cache, seconds. Base 1 trading day
    # (~6.5h RTH ≈ 23400s; defaulted to a calendar day so an overnight re-arm still sees
    # the block) — tradability is an instrument property that changes slowly, but CAN
    # change, so the entry is re-admitted after the TTL. 0 disables the pre-filter
    # (instant kill-switch, equivalent to the flag off). env-tunable.
    chili_momentum_agentic_non_tradeable_ttl_sec: float = Field(
        default=86400.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AGENTIC_NON_TRADEABLE_TTL_SEC"),
    )
    # CONSERVATIVE ASSET-TYPE ARM-SKIP (warrant / non-common-stock, 2026-06-29): 67/72 of the
    # momentum lane's recurring live_error were PRE-entry no_bbo on thin non-common-stock names
    # — WARRANTS especially (the "W" 5th-letter class suffix, e.g. RVMDW, appeared 5×). A warrant
    # has its OWN illiquid / often-untradeable book, so it arms, hits no_bbo/place-error, burns
    # the single slot, and paints a live_error every pass. With no real asset_type field anywhere
    # in the candidate/viability data, the TICKER STRUCTURE is the only signal: SKIP at ARM the
    # names whose ticker unambiguously denotes a warrant/right/unit (explicit .WS/-WT/.U/.R class
    # suffix, the ``=`` warrant marker, or a 5-letter all-alpha root ending in W). CONSERVATIVE +
    # crypto-exempt: it NEVER matches a thin-but-quoted COMMON premarket mover (the UPC-class
    # +500% low-float name — normal 1–4 letter root), which must still arm. A wide spread / low
    # float is NOT a skip reason. FAIL-OPEN: any uncertainty/error => arm. Flag OFF => byte-
    # identical (no skip). [[feedback_no_dark_flags]] [[project_momentum_lane]]
    chili_momentum_asset_type_arm_skip_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ASSET_TYPE_ARM_SKIP_ENABLED"),
    )
    # CLEAN PRE-ENTRY DECLINE TERMINAL (2026-06-29): a DETERMINISTIC policy decline at the entry
    # instant (a known risk-eval BLOCK — no_bbo / not-live-eligible / spread-too-wide / product-
    # not-tradable — on a name that never held a position) terminalizes as the CLEAN live_cancelled
    # state, not the alarm-coloured live_error. live_error is then RESERVED for genuine unexpected
    # failures (zero-fill, place_equity_order isError, missing frozen snapshot), so the REAL errors
    # stop being buried under decline noise and the reaper churn drops. This NEVER weakens a risk
    # block — the session still does NOT enter; only the terminal STATE/label changes. live_cancelled
    # is already terminal across every consumer (focus-set, reaper, feedback learner, busy-set).
    # Flag OFF => byte-identical legacy (decline => live_error). [[feedback_no_dark_flags]]
    chili_momentum_clean_decline_terminal_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CLEAN_DECLINE_TERMINAL_ENABLED"),
    )
    # RANK-DISPLACEMENT (2026-06-17): when arm slots are FULL, evict the worst-ranked
    # truly-inert pre-entry watcher (armed_pending_runner/queued_live ONLY) so a
    # top-ranked NEWCOMER can arm — instead of first-come slots starving the best
    # movers (UTSI #7 @0.7275 sat un-armed all session while 7/9 slots held 0.55-0.69
    # names; Ross made +$52k on UTSI). Guarded: row-locked reap (no orphan), per-symbol
    # in-flight veto, min-dwell + reap-cooldown anti-thrash, 1 displacement/pass.
    # Kill-switch: set =0 to revert to byte-identical skip-on-full (no redeploy).
    chili_momentum_rank_displacement_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RANK_DISPLACEMENT_ENABLED"),
    )
    # SYMBOL-OF-THE-DAY FOCUS (Batch F): Ross trades the ONE best mover INTENSELY rather than
    # spreading thin. ON identifies the highest-conviction explosive LEADER each refresh
    # (reuses the 3-layer explosive scorer — ross_momentum.identify_leader: top (tier, score,
    # %-move×RVOL) among names clearing the hard floors) and gives it ONE guaranteed priority
    # slot: hoisted to first-in-arm-queue, never the rank-displacement victim, and granted the
    # EXTENDED watch window so a transient dip does not rotate the stock of the day out. NOT an
    # exclusive lock — the REMAINING slots still arm the #2/#3 movers by normal rank (no over-
    # concentration). OFF => the batch ranking / displacement / reap are byte-identical to today.
    # docs/DESIGN/MOMENTUM_LANE.md [[project_momentum_lane]] [[feedback_adaptive_no_magic]]
    chili_momentum_symbol_of_day_focus_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SYMBOL_OF_DAY_FOCUS_ENABLED"),
    )
    # A3 (Ross CLRO-lesson 2026-07-02): SCANNER-BREADTH WILDCARD REGIME. When the scanner is
    # DEAD (bottom-decile breadth vs the trailing-20-session same-time-of-day p20) but ONE mover
    # dominates (dominance >= its own trailing percentile) — the wildcard effect ("one stock
    # squeezes for lack of anything else") — CONCENTRATE the lane on that leader: rank-boost +
    # hoist + eviction-protect the dominant watch slot, and size-tilt DOWN B-grade admissions
    # (tilt, never veto). A pre-holiday day feeds the low-breadth PRIOR as a size/trail deweight.
    # FAIL-CLOSED for the up-weights: unreadable breadth => neutral, zero effects. ONE documented
    # base = the breadth percentile floor (p20). Default-ON; kill-switch =0.
    chili_momentum_wildcard_breadth_regime_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WILDCARD_BREADTH_REGIME_ENABLED"),
        description="A3: detect the wildcard breadth regime (dead scanner + one dominant mover) and concentrate slots/size on the leader — rank-boost/hoist/eviction-protect the dominant name + B-grade size-tilt DOWN + pre-holiday breadth-prior deweight. Tilt, never veto. Fail-closed to neutral on unreadable breadth. false => byte-identical.",
    )
    # The ONE adaptive base knob: minimum CURRENT-viability-score gap a newcomer must
    # STRICTLY exceed over the worst inert victim to displace it (hysteresis). Derived
    # as a score-gap margin within the live batch — not a fixed per-class number. Raise
    # to damp churn; 0.0 = any-better displaces.
    chili_momentum_rank_displacement_margin: float = Field(
        default=0.02,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RANK_DISPLACEMENT_MARGIN"),
    )
    # Anti-thrash: a victim must have sat in its inert state (updated_at) at least this
    # long before it is displaceable, so a freshly-armed watcher is not instantly bumped.
    chili_momentum_rank_displacement_min_dwell_sec: float = Field(
        default=45.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RANK_DISPLACEMENT_MIN_DWELL_SEC"),
    )
    # ADOPT-ON-CANCEL-FILL (2026-06-17 root fix for the CRVO/FTHM orphan): when
    # cancel_automation_session's order-sweep finds the entry order already FILLED, ADOPT
    # the position into a managed momentum session (re-point -> PENDING_ENTRY -> fill-
    # handler -> soft stop + #704 adaptive exit) instead of orphaning it. Coordinated with
    # the legacy bracket-reconciler via a single-writer management_scope='momentum_neural'
    # BATON so exactly ONE subsystem ever manages the shares (no double-sell). This ONE
    # flag moves BOTH the adopt branch AND the reconciler's momentum-owned skip together.
    # Kill-switch: =0 -> byte-identical to today (orphan + slow legacy backstop).
    chili_momentum_adopt_on_cancel_fill_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADOPT_ON_CANCEL_FILL_ENABLED"),
    )
    # A6 (open-burst bandwidth): arm up to N distinct fresh candidates per
    # auto-arm pass while slots remain (was 1/pass; 74 fresh candidates in the
    # 13:30-13:50Z burst vs 6 armed).
    chili_momentum_auto_arm_max_arms_per_pass: int = Field(
        default=3,
        ge=1,
        le=10,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_MAX_ARMS_PER_PASS"),
    )
    # A4 (2026-06-12 winner/loser DNA): live crypto momentum is 0/17 winners
    # ever (-$171/wk) AND its losses spend the daily-loss budget that gates the
    # profitable equity window. Live crypto arming OFF until the weekend crypto
    # program proves a profitable config; paper/alpaca crypto unaffected.
    chili_momentum_crypto_live_arm_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CRYPTO_LIVE_ARM_ENABLED"),
    )
    # Verticality entry skip: max extension above the 1m EMA9 at trigger, as a
    # multiple of the instrument's ATR%% (3/3 fills with >3%% extension went a
    # full R underwater on 2026-06-12). 0 disables.
    chili_momentum_entry_verticality_atr_mult: float = Field(
        default=1.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_VERTICALITY_ATR_MULT"),
    )
    # ── Ross dip-buy QUALITY gates (5 knobs, ALL default-off / byte-identical) ────
    # Flag-gated discriminators that make MARGINAL/round-trip dip entries PASS while
    # clean ones still FIRE. EVERY default is the value that is byte-identical to the
    # current behavior (parity is load-bearing); the operator replay-validates ON vs
    # OFF and flips safest-first. ADAPTIVE / self-relative (no fixed magic numbers):
    # MACD self-relative, volume push-mean-relative, L2 ladder percentile.
    # Gate 1 — MACD-open STRICT: require the MACD LINE above SIGNAL (not the lenient
    # "hist>=0 OR line>=signal"). Only tightens the EXISTING macd-bullish veto (which
    # is already ON live); fail-open on warmup (m/s None) is preserved. OFF ⇒ exact
    # current expression.
    chili_momentum_entry_macd_open_strict: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_MACD_OPEN_STRICT"),
        description="Gate 1 (dip-buy quality): inside the existing require_macd_bullish veto, require the MACD LINE strictly above SIGNAL instead of the lenient hist>=0 OR line>=signal. Warmup (line/signal None) still fails OPEN. false = byte-identical current expression.",
    )
    # Gate 2a — high-volume SELLING-candle veto: a RED pullback candle (close<open)
    # printing >= mult × the impulse's mean per-bar volume is distribution (a big
    # seller stepping in) → PASS. Self-relative to the push volume (no fixed share).
    # 0 disables (the per-candle loop is skipped) ⇒ byte-identical. Reference floor 2.5.
    chili_momentum_dipbuy_distribution_vol_mult: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DIPBUY_DISTRIBUTION_VOL_MULT"),
        description="Gate 2a (dip-buy quality): a RED pullback candle with per-bar volume >= mult × the impulse mean volume = distribution → veto the dip entry. 0 = disabled (loop skipped, byte-identical). Reference floor 2.5.",
    )
    # Gate 2b — impulse-ACCUMULATION confirm: the up-impulse's per-bar volume should be
    # non-decreasing (real buyers piling in, not fading). Least-squares slope of the
    # push volumes normalized by the push mean must be >= this floor. Sentinel -1 =
    # DISABLED (byte-identical default); 0.0 = require non-decreasing volume.
    chili_momentum_dipbuy_impulse_accum_min_slope: float = Field(
        default=-1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DIPBUY_IMPULSE_ACCUM_MIN_SLOPE"),
        description="Gate 2b (dip-buy quality): require the impulse's per-bar volume to be accumulating — normalized least-squares slope of push volumes >= this. Sentinel -1 = DISABLED (byte-identical). 0.0 = require non-decreasing.",
    )
    # Gate 3 — L2 hidden-seller / big-seller veto (reuses #699 OFI + #704 ladder
    # readers). Before a dip/first-pullback FIRE/ARM, read the L2 ladder distribution
    # at the entry level: veto on a large resting ASK wall the price can't lift
    # (ladder ask-heavy, big-seller percentile BELOW the floor) OR absorption /
    # micro-price rollover despite buy-side OFI. FAIL-OPEN: db None / empty / stale /
    # _NULL read ⇒ NEVER veto. Class-aware (equity iqfeed / crypto fast_orderbook).
    # OFF ⇒ branch skipped ⇒ byte-identical. The absorption side reuses
    # chili_momentum_ofi_threshold (no new OFI knob).
    chili_momentum_entry_l2_veto_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_L2_VETO_ENABLED"),
        description="Gate 3 (dip-buy quality): enable the L2 hidden-seller / big-seller entry veto (reuses read_ladder_distribution + OFI/micro). FAIL-OPEN on any missing/stale L2. false = branch skipped, byte-identical.",
    )
    chili_momentum_entry_l2_bigseller_pctile_floor: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_L2_BIGSELLER_PCTILE_FLOOR"),
        description="Gate 3 (dip-buy quality): depth-imbalance percentile at/below which the NEWEST book is treated as a big resting ASK wall (distribution trend) → veto. Self-relative to the symbol's own recent window. Only consulted when chili_momentum_entry_l2_veto_enabled is on.",
    )
    # ── ENTRY-TIME FLOW VETO (separate from selection): never BUY this exact tick into
    # max selling. Keys on LIVE FLOW (OFI + trade_flow), NOT the static book_imbalance
    # the existing L2 veto reads — the PLSM flush had book_imbalance=+0.21 (stale) but
    # OFI=-1.0 / trade_flow=-0.51 (tape actively selling). Applies to ALL names incl
    # extreme movers (ross>=0.8): the never-penalize-the-tail rule is a SELECTION rule
    # (keep on watchlist); ENTRY-TIMING must respect live flow. Defers the buy (stays
    # WATCHING, can re-enter when flow flips). ADDITIVE: OFF or OFI/trade_flow absent
    # (None) ⇒ no veto ⇒ byte-identical. Both thresholds are NEGATIVE (signed [-1,1]).
    chili_momentum_entry_flow_veto_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FLOW_VETO_ENABLED"),
        description="Entry-time flow veto: defer the buy this tick when OFI AND trade_flow are both sufficiently negative (tape actively selling). Applies to extreme movers too (selection vs entry-timing). false / either flow None = no veto, byte-identical.",
    )
    chili_momentum_entry_flow_veto_ofi: float = Field(
        default=-0.6,
        ge=-1.0,
        le=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FLOW_VETO_OFI"),
        description="Entry-time flow veto: OFI (signed [-1,1], <0 = net selling) at/below this triggers the veto leg. Must be negative. Only consulted with chili_momentum_entry_flow_veto_enabled on (AND-ed with the trade_flow leg).",
    )
    chili_momentum_entry_flow_veto_trade_flow: float = Field(
        default=-0.25,
        ge=-1.0,
        le=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FLOW_VETO_TRADE_FLOW"),
        description="Entry-time flow veto: executed-tape aggressor imbalance (signed [-1,1], <0 = sellers hitting the bid) at/below this triggers the veto leg. Must be negative. AND-ed with the OFI leg.",
    )
    chili_momentum_entry_flow_veto_trade_flow_strong: float = Field(
        default=-0.5,
        ge=-1.0,
        le=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_FLOW_VETO_TRADE_FLOW_STRONG"),
        description="Entry-time flow veto STRONG-tape OR-leg: executed-tape trade_flow (signed [-1,1]) at/below this STRONG-negative bar vetoes the buy ALONE, regardless of OFI (06-24 RUN: ofi=+0.5 mild buy but trade_flow=-0.63 strong executed selling — the strict AND-leg missed it). -0.5 is a strong bar (most healthy entries have trade_flow > -0.5). Only consulted with chili_momentum_entry_flow_veto_enabled on; trade_flow None = no veto, byte-identical.",
    )
    # FIX-19(c) STICKY FLOW VETO. The per-tick flow veto forgets INSTANTLY: a real-selling
    # veto clears the moment ONE tick reads non-negative flow, so a single spoofy imbalance
    # print can flip a genuine-selling veto and fire the buy 53s later. When True (default),
    # once the flow veto LATCHES it PERSISTS until flow has cleared the veto for a short
    # rolling window (chili_momentum_sticky_flow_veto_window_sec, ONE documented base) —
    # measured by consecutive non-veto flow reads spanning the window. A single positive
    # print no longer releases a real-selling veto. Sizing/timing only (defers the buy; the
    # exit path is never consulted). OFF => byte-identical (the veto forgets per tick).
    chili_momentum_sticky_flow_veto_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STICKY_FLOW_VETO_ENABLED"),
        description="FIX-19(c): True (default) => a latched entry flow-veto persists until flow is non-negative across a rolling window (one spoofy print can't release a real-selling veto). OFF => the veto forgets per tick (legacy).",
    )
    # Rolling window (seconds) the flow must stay non-veto before a latched sticky flow-veto
    # releases. ONE documented base (~ a few tick cadences); a longer window = stickier.
    chili_momentum_sticky_flow_veto_window_sec: float = Field(
        default=20.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STICKY_FLOW_VETO_WINDOW_SEC"),
    )
    # ── Entry-EXTENSION (chase) veto ─────────────────────────────────────────────
    # Defer the buy when the entry sits too far ABOVE the breakout level (bought near a
    # local top after the move already ran; 06-24 RUN @15.51 vs break 12.94 = +19.9%,
    # PLSM @10.21 vs break 7.63 = +33.8%). Ross enters AT the break / on the pullback to
    # it, never chases the extension. The allowed extension above the level is ADAPTIVE
    # to volatility = max(floor, K·atr_pct) (no flat magic %). Defers to WATCHING (can
    # re-enter on a pullback toward the level). ADDITIVE: OFF or breakout_level/atr_pct
    # absent ⇒ no veto ⇒ byte-identical.
    chili_momentum_entry_extension_veto_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_EXTENSION_VETO_ENABLED"),
        description="Entry-extension (chase) veto: defer the buy this tick when entry_price >= breakout_level * (1 + max(floor, K*atr_pct)) — i.e. bought too far above the break (near a local top). Defers to WATCHING (re-enter on a pullback). false / breakout_level or atr_pct absent = no veto, byte-identical.",
    )
    chili_momentum_entry_extension_atr_mult: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_EXTENSION_ATR_MULT"),
        description="Entry-extension veto: volatility multiplier K. Allowed extension above the breakout level = max(floor_pct, K*atr_pct), so a volatile small-cap gets proportional room. Recalibrated 06-24 8.0->1.0: the veto is now fed the CLEAN regime_atr_pct (intraday-range vol, clamped 0.004-0.12) instead of the stop-focused _eff_atr_pct that the structural override inflated (a deeper chase used to LOOSEN the cap). K must be low enough that the cap stays BELOW the RUN(+19.9%)/PLSM(+33.8%) chase distance even at the top of the regime-ATR range (K*0.15=0.15 < 0.199) so those chases are vetoed across the FULL realistic vol range — a +10% follow-through is then allowed only when the regime is genuinely explosive (atr_pct>=~0.10), which is exactly when +10% above the break is proportionate. (The task's K~3-4 hint was validated only at the cherry-picked atr_pct~0.015 and FAILS the full-range RUN/PLSM veto at atr_pct>=0.06; the safety MUST wins.) Only consulted with chili_momentum_entry_extension_veto_enabled on.",
    )
    chili_momentum_entry_extension_floor_pct: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_EXTENSION_FLOOR_PCT"),
        description="Entry-extension veto: minimum allowed extension above the breakout level (fraction, e.g. 0.10 = 10%) regardless of ATR — a calm name still gets at least this room. Recalibrated 06-24 0.05->0.10: the high-ATR binding constraint that vetoes RUN(+19.9%)/PLSM(+33.8%) is K*0.12=0.12 (K=1.0, regime_atr clamp ceiling 0.12), governed by K NOT the floor, so raising the floor to 0.10 keeps RUN/PLSM vetoed across the full vol range while ALLOWING a calm +9.9% break-and-go (cap = max(0.10, 1.0*atr)). The cap is max(this, K*atr_pct).",
    )
    # ── CANDLE-QUALITY + MULTI-TF (HTF-against) ENTRY VETO ───────────────────────
    # Two ADDITIVE entry-quality gates that slot AFTER the trigger fires and BEFORE the
    # downstream VWAP/MACD/volume confirmations: (1) DOJI veto — reject when the trigger
    # candle has a weak body relative to its range (indecision), a strong full-body
    # commitment candle passes; (2) MULTI-TF ALIGNMENT — reject ONLY when the higher TF
    # (5m, derived from the 1m df, no new feed) is CLEARLY AGAINST the long (5m EMA-9
    # rolling DOWN or MACD histogram clearly peaked/rolled). A NEUTRAL/LAGGING HTF (not
    # yet up but not down) MUST still pass — requiring full multi-TF alignment would break
    # Ross's 1m-FAST geometry (the 1m leads, the HTF lags). Default OFF -> byte-identical
    # (both gates skipped). Fail-OPEN on thin/unreadable data (never block a valid break).
    chili_momentum_candle_quality_multitf_veto_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CANDLE_QUALITY_MULTITF_VETO_ENABLED"),
        description="Master enable for BOTH the doji veto and the HTF-against (multi-TF alignment) veto in pullback_break_confirmation. false = byte-identical (both gates skipped, no change to entry logic). true = a doji trigger candle vetoes AND a 5m HTF that is clearly bearish (EMA-9 rolling down / MACD peaked) vetoes; a neutral/lagging HTF still passes (1m-fast preserved).",
    )
    chili_momentum_doji_body_frac: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DOJI_BODY_FRAC"),
        description="Doji veto: the ADAPTIVE base body/range fraction below which the trigger candle is a DOJI (indecision) and the entry is vetoed. ONE documented base = 0.25 (25% of the range is the calm-name indecision floor). The effective threshold WIDENS with volatility: doji_threshold = base + atr_pct (an explosive ~5% ATR name gets a looser doji band so its normal full-body bars are not over-restricted). Range-relative, no fixed cents. A green full-body commitment candle (close in upper half, upper wick not dominant) always passes regardless. Only consulted with chili_momentum_candle_quality_multitf_veto_enabled on.",
    )
    chili_momentum_htf_against_macd_threshold: float = Field(
        default=0.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HTF_AGAINST_MACD_THRESHOLD"),
        description="HTF-against veto: the 5m MACD histogram is treated as PEAKED/rolled-over (HTF clearly bearish) when hist[-1] < hist[-2] >= hist[-3] AND hist[-2] > this threshold. Default 0.0 = any positive-then-declining rollover counts (strict). Raise to require MORE 5m up-momentum before the rollover registers as HTF-against (e.g. 0.01). Self-relative MACD on the resampled HTF bars, no new feed. Only consulted with chili_momentum_candle_quality_multitf_veto_enabled on.",
    )
    chili_momentum_htf_against_ema9_rolldown_bars: int = Field(
        default=3,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HTF_AGAINST_EMA9_ROLLDOWN_BARS"),
        description="HTF-against veto: the 5m EMA-9 counts as CLEARLY rolling DOWN (HTF bearish) only on a SUSTAINED decline — the EMA-9 must be strictly lower across each of the last N HTF samples (a multi-bar negative slope), NOT a single lagging down-tick. ONE documented base = 3 samples (the EMA-9 has fallen for 2 consecutive 5m steps). A single down-tick (a lagging EMA dipping for one sample off a flush while the 1m has already turned up) must NOT register as clearly-against — that is exactly the dip-rip/VWAP-reclaim the lane wants to catch. Raise for a longer required roll-down; lower (min 2) for a quicker read. Only consulted with chili_momentum_candle_quality_multitf_veto_enabled on.",
    )
    # ── L2 ENTRY CONFIRMER (Phase 1, DEFER-only) ─────────────────────────────────
    # docs/DESIGN/L2_PRIMARY_SIGNAL.md — graduate L2/T&S from veto→CONFIRMER. AFTER the
    # chart trigger fires AND AFTER both existing vetoes (_l2_entry_veto + _entry_flow_veto)
    # pass, require the TAPE to actively confirm thrust before the buy submits. TAPE-PRIMARY:
    # confirm needs signed_tape_accel>0 (back-half aggressor-signed buy volume > front-half,
    # same Lee-Ready as _aggressor_imbalance) AND tick_rate>=its self-relative floor; OFI
    # (>=threshold OR micro_edge>0) + a RISING depth-imbalance percentile are SECONDARY
    # agreement confirmers. CONSERVATIVE-ACTIVE: DEFER only on CLEAR no-confirmation
    # (signed_tape_accel<=0 AND OFI<0); otherwise confirm. On defer → stay WATCHING_LIVE +
    # re-enter next tick (MIRRORS the flow-veto defer; the adaptive watch/reap bounds the
    # slot — no new hold) + emit live_l2_confirm_defer as the COUNTERFACTUAL. ENTRY-ONLY
    # (never blocks an exit/stop/flatten — held states never call it). FAIL-OPEN: any helper
    # None / n_snaps<3 / empty-tape / stale snapshot ⇒ CONFIRM (never defer on bad data).
    # OFF (default) ⇒ return confirm BEFORE any I/O ⇒ byte-identical (will be ENABLED in env).
    chili_momentum_l2_confirm_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_L2_CONFIRM_ENABLED"),
        description="Phase-1 L2 entry CONFIRMER (DEFER-only): after the chart trigger + both existing vetoes pass, require the executed tape to confirm thrust (signed_tape_accel>0 AND tick_rate>=self-relative floor; OFI/micro + rising depth-pctile secondary) before submitting the buy. DEFER only on CLEAR no-tape (accel<=0 AND OFI<0); fail-open (confirm) on any missing/stale/thin data; entry-only (never blocks exits). false = return confirm before any I/O, byte-identical.",
    )
    chili_momentum_l2_multilevel_ofi_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_L2_MULTILEVEL_OFI_ENABLED"),
        description="P3 — multi-level OFI (make equity L2 real). ON: when the per-price ladder (iqfeed_depth_snapshots.bids_json/asks_json, written by the depth bridge) is present, _compute_ofi_micro sums the Cont-Kukanov-Stoikov per-level OFI events across the top-N levels per side, harmonic depth-decay weighted (w_m=1/(m+1)) and normalized by gross flow (dimensionless, [-1,1], no magic depth constant). OFF, OR ladder absent/NULL ⇒ the multi-level block is skipped before any ladder iteration and the legacy level-1 OFI runs — byte-identical to pre-P3.",
    )
    # Self-relative tick-rate floor PERCENTILE within the symbol's own recent tape window:
    # the back-half ticks/sec must sit at/above this percentile of the per-half tick rates
    # for the tape to count as ACTIVELY accelerating (not a dead, thinning book). Adaptive /
    # no magic absolute rate — it is a percentile of the name's OWN recent activity. 0.0
    # (permissive) ⇒ any nonzero back-half rate clears the floor (the conservative-active
    # start: tune UP only if the live counterfactual shows it catches losers). The ONE
    # documented base knob; everything else is self-relative.
    chili_momentum_l2_confirm_tick_rate_floor_pctile: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_L2_CONFIRM_TICK_RATE_FLOOR_PCTILE"),
        description="L2 confirmer: self-relative tick-rate floor percentile within the symbol's own recent tape window (back-half ticks/sec must sit at/above this percentile of the per-half rates). Adaptive (no absolute magic rate). 0.0 = permissive (any nonzero back-half rate clears). Only consulted when chili_momentum_l2_confirm_enabled is on.",
    )
    # Recent tape window (seconds) the confirmer splits in half to compute signed_tape_accel
    # + tick_rate. Defaults to the same 15s short-horizon window the OFI/flow readers use so
    # the tape and book signals are time-aligned. Lookahead-free (trailing now()/as_of).
    chili_momentum_l2_confirm_window_s: float = Field(
        default=15.0,
        gt=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_L2_CONFIRM_WINDOW_S"),
        description="L2 confirmer: recent tape window (seconds), split in half for signed_tape_accel + tick_rate. Aligned to the OFI/flow short-horizon window. Only consulted when chili_momentum_l2_confirm_enabled is on.",
    )
    # Staleness ceiling (seconds): if the newest L2 ladder snapshot is older than this the
    # book is treated as stale ⇒ FAIL-OPEN (confirm), never defer on a frozen feed. The
    # equity depth bridge writes at ~2s cadence, so 10s tolerates a few missed pulses while
    # still catching a dead feed.
    chili_momentum_l2_confirm_max_snapshot_age_s: float = Field(
        default=10.0,
        gt=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_L2_CONFIRM_MAX_SNAPSHOT_AGE_S"),
        description="L2 confirmer: staleness ceiling (seconds) on the newest L2 ladder snapshot; older ⇒ fail-open (confirm), never defer on a frozen feed. Only consulted when chili_momentum_l2_confirm_enabled is on.",
    )
    # ── FIX C: TAPE-CONFIRMED-HOLD EARLY ENTRY ──────────────────────────────────────
    # Graduate the L2 confirmer from a defer-gate to a TRIGGER. Armed pullback sessions wait
    # for a price-BREAK (waiting_for_reclaim_high) that choppy explosive names never give
    # inside the 300s watch window before they are reaped — so the lane sits FLAT through the
    # move. Ross enters EARLIER: he buys the pullback-HOLD bounce the moment the TAPE confirms
    # buyers, BEFORE the confirmed break. When ON, the live runner fires the marketable-LIMIT
    # entry on a VALID, tape-confirmed, non-backside pullback HOLD (close holding the 9-EMA +
    # a higher low vs the pullback low) WITHOUT requiring close > pullback_high. The tape
    # confirm is REQUIRED + FAIL-CLOSED (no/thin/stale tape ⇒ no early fire, keep the break
    # path); ALL existing entry vetoes + the quote gate still run (the early fire only promotes
    # WATCHING -> LIVE_ENTRY_CANDIDATE, which routes through the full LIVE_PENDING_ENTRY veto
    # chain), so it CANNOT fire on an extended/faded/rolled-over name. OFF (default) = the
    # confirmer is never even probed in this path ⇒ byte-identical break-only behaviour.
    chili_momentum_tape_hold_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TAPE_HOLD_ENTRY_ENABLED"),
        description="FIX C: enter the pullback-HOLD bounce when the TAPE confirms buyers (signed_tape_accel>0 AND tick_rate>=self-relative floor) on a VALID, non-backside pullback that is holding the 9-EMA with a higher low — BEFORE the confirmed break (Ross enters earlier than the break). Tape-confirm is REQUIRED + fail-closed (no/thin/stale tape ⇒ no early fire, fall back to the existing break trigger). All existing entry vetoes + the quote gate still run downstream; does NOT touch evaluate_sticky_backside_bench. CAPTURE-G1(b) 2026-07-03: flipped False->True — this now governs ONLY the FIX-C EARLY-FIRE path (the inline pattern-trigger tape gate was decoupled onto chili_momentum_pattern_tape_gate_enabled). The early fire is fail-closed on missing/thin tape and every downstream LIVE_PENDING_ENTRY veto still runs; OFF restores break-only for the early-fire path.",
    )
    # CAPTURE-G1(b) DECOUPLE (2026-07-03): the INLINE tape-confirmation gate that the 12
    # tape-required pattern triggers (bull_flag/wedge/absorption/false_break/ask_thins/
    # sub_vwap_trap/pulling_away/premarket_pivot/inverse_h&s/cup_and_handle/bottom_reversal)
    # + the momentum-continuation entry call via tape_confirms_hold. Previously that gate was
    # fused to chili_momentum_tape_hold_entry_enabled, so with that flag OFF (the deployed
    # default) all 12 triggers were DARK live — including cup_and_handle, which WAVE-4 R4 had
    # flipped ON as a proven filler (dead code in prod). Decoupled: tape_confirms_hold now keys
    # on TAPE AVAILABILITY (dense recent iqfeed ticks ⇒ evaluate; genuinely missing/thin/stale/
    # crypto ⇒ the existing fail-CLOSED refusal STANDS — never weakened). Default True makes the
    # 12 triggers reachable; OFF is the instant rollback to the legacy hard-False (dark) behavior.
    chili_momentum_pattern_tape_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PATTERN_TAPE_GATE_ENABLED"),
        description="CAPTURE-G1(b): govern the inline tape_confirms_hold gate that the 12 tape-required pattern triggers + momentum-continuation entry use as their fail-closed LAST gate. True (default) = evaluate the executed tape when dense+healthy and fail-CLOSED on missing/thin/stale/crypto tape (the 12 triggers become REACHABLE, still tape-gated). False = legacy hard-False short-circuit (every dependent trigger's tape gate refuses ⇒ those setups go dark) — the one-flag instant rollback. Independent of chili_momentum_tape_hold_entry_enabled, which now governs ONLY the FIX-C early-fire path.",
    )
    # 2026-07-04 — UNIFIED BUYERS-CONFIRMATION for the HOT-TAPE-ONLY touch triggers. The two
    # touch triggers that fire on price/level GEOMETRY alone with NO buyers check — wick_reclaim
    # and micro_pullback_primary — are the whack-a-mole gap: gating only one relocates the
    # too-early fire to the other (adversarial review 07-04). `buyers_confirmed` is the ONE
    # crypto-safe gate applied to BOTH: equity => the validated trade-tape confirmer
    # (signed_tape_accel>0 AND tick_rate>=floor, FAIL-CLOSED on missing tape — a hot-tape trigger
    # has dense ticks); crypto (-USD, no iqfeed trade tape) => L2 book OFI (ofi_level>0),
    # FAIL-OPEN when the per-process book ring is empty (must NOT silently disable crypto — the
    # review's crypto-exclusion finding). OFF ⇒ byte-identical (touch triggers fire on geometry
    # alone, as before this change).
    chili_momentum_buyers_confirm_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BUYERS_CONFIRM_ENABLED"),
        description="2026-07-04: require 'buyers actually lifting' before the HOT-TAPE-only touch triggers (wick_reclaim, micro_pullback_primary) fire — equity: signed_tape_accel>0 (fail-closed on missing tape); crypto: OFI ofi_level>0 (fail-open on empty book ring). The unified, crypto-safe, non-whack-a-mole buyers gate. OFF ⇒ triggers fire on price geometry alone (legacy).",
    )
    # ── FIX 1: MOMENTUM-CONTINUATION ENTRY (catch the straight-up runners) ───────────
    chili_momentum_momentum_continuation_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_MOMENTUM_CONTINUATION_ENTRY_ENABLED"),
        description="FIX 1: enter the continuation (a fresh NEW HIGH, NO prior pullback/base required) on a HIGH-CONVICTION mover that trends STRAIGHT UP and so never triggers the pullback/base entries (WSHP +47% 40x RVOL, SDOT +25% 132x RVOL — caught + watched but reaped at 300s, never entered). Fires ONLY when ALL hold: (1) high-conviction (ross_score >= chili_momentum_continuation_ross_floor OR RVOL >= explosive_rvol_floor x coiling_exempt_rvol_mult OR daily_breaking_major); (2) momentum_continuation_trigger new-high break; (3) tape_confirms_hold REQUIRED + fail-closed (no/thin/stale/selling/crypto tape ⇒ no fire); (4) NOT parabolic (_hod_extension_ok / _entry_extension_veto vs 9-EMA AND VWAP); (5) NOT backside / NOT below-VWAP (_detect_back_side + front_side_state) + a structural stop + ALL downstream LIVE_PENDING_ENTRY vetoes. Skipped for benched names. CAPTURE-G1(c) 2026-07-03: flipped False->True (paired with chili_momentum_continuation_arm_skip_tape True to resolve the arm-time tape chicken-and-egg, and chili_momentum_conviction_rvol_fallback_enabled True for scanner-only names) — the purpose-built early path for the JEM/SDOT straight-up class. Guard suite proven (test_ross_mistakes_guarded parabolic/backside avoided, test_momentum_mock_fire_pullback); every guard fail-closed; entry-time tape gate REQUIRED (now decoupled + reachable). False = byte-identical (the trigger returns disabled before any compute).",
    )
    chili_momentum_continuation_ross_floor: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CONTINUATION_ROSS_FLOOR"),
        description="FIX 1: the ross_score (Ross momentum quality, [0,1]) at/above which a name is high-conviction enough for the momentum-continuation new-high entry (one of three OR-ed conviction gates with the coiling-exempt RVOL multiple and daily_breaking_major). Only consulted when chili_momentum_momentum_continuation_entry_enabled is ON.",
    )
    chili_momentum_conviction_rvol_fallback_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CONVICTION_RVOL_FALLBACK_ENABLED"),
        description="FIX 1b: when a momentum-continuation candidate's OWN persisted scanner signal is EMPTY (no ross_score, no RVOL, not daily_breaking_major — a SCANNER-only name the ignition enricher never scored, e.g. PED +25% AT HOD with a true 13.72x intraday RVOL), COMPUTE intraday relative volume from the ALREADY-FETCHED 5m/5d OHLCV frame (today's cumulative session volume / the trailing average of prior complete sessions) and admit as high-conviction iff it is >= chili_momentum_explosive_rvol_floor x chili_momentum_coiling_exempt_rvol_mult (~9x). ZERO new fetch (reuses the frame the continuation trigger already holds). Shared helper => arm-time (auto_arm) and entry-time (live_runner) stay identical. FAIL-CLOSED: if RVOL cannot be computed reliably (no Volume column, < 2 sessions, NaN/zero average) the name stays low-conviction (never admit a genuinely low-RVOL name — this is the chase-safety). Row signal precedence is preserved: the fallback ONLY fills the empty case; ross_score>=floor and daily_breaking_major paths are unchanged. CAPTURE-G1(c) 2026-07-03: flipped False->True (paired with the continuation entry flip) so scanner-only movers with a genuine RVOL (PED class) are admitted; FAIL-CLOSED keeps genuinely low-RVOL names out. False = empty-signal names remain low_conviction (the continuation lane misses scanner-only movers).",
    )
    chili_momentum_continuation_arm_skip_tape: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CONTINUATION_ARM_SKIP_TAPE"),
        description="ARM-TIME TAPE GATE KILL-SWITCH: in auto_arm._continuation_active_trigger the continuation ARM places NO order — it only starts WATCHING, and arming is what subscribes the trade/depth bridges so tape (iqfeed_trade_ticks) THEN begins flowing for the symbol. A fresh scanner mover (e.g. PED RVOL 13.72x, +25% AT HOD) has ZERO tape before it is armed (tape_hold_no_data), so the unconditional arm-time tape_confirms_hold gate is UNSATISFIABLE → arm never fires → chicken-and-egg (the only thing that bootstraps tape is the arm). CAPTURE-G1(c) 2026-07-03: flipped False->True (paired with the continuation entry flip) — arm on conviction(+rvol-fallback) + STRUCTURE ONLY (momentum_continuation_trigger still enforces new-HOD + NOT-extended + NOT-backside) and SKIP the arm-time tape call; the strict tape gate STAYS at the live_runner ENTRY (which places the order) — by entry-time the now-watching symbol is subscribed and tape flows, so NO order is EVER placed without tape confirmation. Without this the continuation entry flip is inert (nothing ever arms). Does NOT weaken structure/extension/backside/conviction; ONLY the arm-time tape call becomes optional. False = tape REQUIRED at arm-time (the continuation lane cannot bootstrap).",
    )
    # ── FIX 2: EMPTY-SIGNAL DE-RANK (push no-momentum-signal names below real movers) ──
    chili_momentum_no_signal_derank_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NO_SIGNAL_DERANK_ENABLED"),
        description="FIX 2: when the batch scored SOME names but THIS symbol has NO ross_score (empty/missing momentum signal — GALT/PYXS/ANGI sit eligible at base ~0.6 via fail-open and GALT was ENTERED over the real movers), apply a viability DE-RANK penalty so any scored real mover (base + tilt ~0.7+) outranks it for the slots. DE-RANK, not hard-exclude (it still trades if nothing better is up). A scored real mover (symbol IN ross_scores) is NEVER touched. false (default) = byte-identical.",
    )
    chili_momentum_no_signal_derank_fraction: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NO_SIGNAL_DERANK_FRACTION"),
        description="FIX 2: the de-rank penalty is ROSS_QUALITY_VIABILITY_TILT x 0.5 x this fraction (the SAME tilt magnitude the scored names use — adaptive, not a scattered magic number; default 1.0 -> a 0.10 viability penalty that pushes an empty-signal name clearly below a scored mover). Only consulted when chili_momentum_no_signal_derank_enabled is ON.",
    )
    # ── WARRIOR RE-AUDIT (2026-06-26): 6 ENTRY-trigger gaps, all default OFF = byte-identical.
    # Each new trigger carries the SAME chase-guards (tape REQUIRED+fail-closed, extension
    # veto, NOT-backside / NOT-below-VWAP, a structural stop) and routes through the IDENTICAL
    # LIVE_ENTRY_CANDIDATE -> LIVE_PENDING_ENTRY veto chain. No existing veto is weakened. ──
    chili_momentum_wedge_break_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_WEDGE_BREAK_ENTRY_ENABLED"),
        description="GAP 1: enter a CONVERGING-WEDGE break — 3+ taps on a DESCENDING upper trendline AND an ASCENDING lower trendline coiling to an apex, fire on the body/wick breaking OUT of the wedge at the apex with tape; stop = back INTO the wedge (the apex-low). A falling/descending wedge (downward upper line) is the stronger bull setup; a rising/ascending wedge (both lines rising) is lower-odds and is SKIPPED (never fires). Carries the SAME chase-guards as momentum_continuation_trigger: tape REQUIRED via the live_runner tape_confirms_hold call, _hod_extension_ok (NOT parabolic) + _detect_back_side + front_side_state (NOT backside / NOT below-VWAP) + _l2_entry_veto, joins the SAME setup-selector candidate set + the downstream LIVE_PENDING_ENTRY vetoes. false (default) = the trigger returns disabled before any compute = byte-identical.",
    )
    chili_momentum_round_number_entry_timing_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ROUND_NUMBER_ENTRY_TIMING_ENABLED"),
        description="GAP 2: a CONTEXT modifier (NOT a veto on its own — it only DEFERS, identical to the extension veto, and re-enters on a hold over the level) on the existing breakout entries: prefer a break-and-HOLD OVER a whole/half-dollar round number; avoid firing right INTO a round number from BELOW (overhead supply). When the marketable entry sits just BELOW a round number AND the breakout level has NOT yet cleared+held it, defer (stay WATCHING). ADDITIVE: OFF / no level / no round number nearby ⇒ no effect, byte-identical.",
    )
    chili_momentum_absorption_snap_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ABSORPTION_SNAP_ENTRY_ENABLED"),
        description="GAP 3: an L2/tape LONG trigger — a large resting SELLER on the ask being ABSORBED (eaten, the ask-wall refilled repeatedly but price HOLDS just under it on buy-side OFI) then the SNAP when the wall CLEARS (price ticks through the absorption level on accelerating buy flow). Reuses read_ladder_distribution (OFI / micro_edge / ask_build) + the bar structure; stop = back below the absorption level. Carries the SAME chase-guards (tape REQUIRED via the live_runner tape_confirms_hold call, _hod_extension_ok + _detect_back_side + front_side_state + the downstream vetoes), joins the SAME setup-selector candidate set. false (default) = returns disabled before any compute = byte-identical.",
    )
    # ── LOCATE 10 scalp/dip triggers + modifiers (each default OFF = byte-identical) ──
    # All ten are wired into the EXISTING entry ladder / veto chain. New ENTRY triggers
    # (2,4,5,6) carry the SAME chase-guards as wedge/absorption (tape REQUIRED+fail-closed
    # via tape_confirms_hold, _hod_extension_ok, _detect_back_side + front_side_state,
    # _l2_entry_veto) and join the SAME setup-selector candidate set + the downstream
    # LIVE_PENDING_ENTRY vetoes. The modifiers/guards (1,3,7,8,9,10) only REFINE/REDUCE.
    chili_momentum_sub5min_scalp_bailout_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SUB5MIN_SCALP_BAILOUT_ENABLED"),
        description="LOCATE #1 SUB-5MIN SCALP BAILOUT: a scalp-family fast time-stop. When the deployed cadence classifier (_classify_cadence) reports a SLOW_CHOPPER (a scalp that is NOT extending) AND the position has been held >= chili_momentum_sub5min_scalp_bailout_minutes AND is NOT green (bid <= entry), bail via the EXISTING bailout machinery (distinct from the runner trail; a runner — FAST / rising-EMA — is NEVER time-stopped). PROTECTIVE-ONLY: it can only EXIT a stalled scalp sooner; it never widens a stop or admits a worse entry. false (default) = the time-stop is never evaluated = byte-identical.",
    )
    chili_momentum_sub5min_scalp_bailout_minutes: float = Field(
        default=5.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SUB5MIN_SCALP_BAILOUT_MINUTES"),
        description="LOCATE #1: the scalp max-hold in MINUTES — a SLOW_CHOPPER not green by this age is time-stopped. One documented base (the irreducible scalp clock). Only consulted when chili_momentum_sub5min_scalp_bailout_enabled is ON.",
    )
    chili_momentum_ask_thins_dip_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ASK_THINS_DIP_ENTRY_ENABLED"),
        description="LOCATE #2 ASK-THINS-TO-ZERO DIP: an L2 ask-DEPLETION dip-bottom long. After a real dip (an ATR-scaled retrace that holds a structural higher-low), the resting ASK supply has been EXHAUSTED — read_ladder_distribution.ask_build <= -chili_momentum_ask_thins_min_depletion_frac (Σask5 collapsed across the window) WITH buy-side OFI (>= chili_momentum_ofi_threshold) — then price ticks back up off the dip low. Entry = the bounce/recent high (pullback_high); stop = the dip low (pullback_low). Carries ALL chase-guards (tape REQUIRED+fail-closed via tape_confirms_hold, _hod_extension_ok, _detect_back_side + front_side_state, _l2_entry_veto), joins the setup-selector set + LIVE_PENDING_ENTRY vetoes. false (default) = returns disabled before any compute = byte-identical.",
    )
    chili_momentum_ask_thins_min_depletion_frac: float = Field(
        default=0.25,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ASK_THINS_MIN_DEPLETION_FRAC"),
        description="LOCATE #2: the minimum ASK depletion (a fraction in [0,1]) the offer must lose across the L2 window to read as 'sellers exhausted' — fire only when ask_build <= -this (e.g. 0.25 = Σask5 shrank >= 25%). One documented base. Only consulted when chili_momentum_ask_thins_dip_entry_enabled is ON.",
    )
    chili_momentum_dip_velocity_conviction_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DIP_VELOCITY_CONVICTION_ENABLED"),
        description="LOCATE #3 DIP-VELOCITY CONVICTION: a CONVICTION modifier on a dip-family fire (flush_dip / ask-thins / vwap_reclaim / wick_reclaim) — scale entry SIZE by the dip ROC (a STEEPER, faster flush = a more violent algo-stop-run that snaps back harder, per Ross's flush read). The size multiplier is in [1.0, 1+chili_momentum_dip_velocity_conviction_max_boost], computed from the dip's measured ROC (steepness) above an ATR-noise floor; it NEVER shrinks size below 1.0 and is bounded by the same 3x clamp + max_notional the other size levers obey, so it can never increase per-trade RISK beyond the existing caps. ADDITIVE: OFF / non-dip / no ROC ⇒ mult 1.0 = byte-identical.",
    )
    chili_momentum_dip_velocity_conviction_max_boost: float = Field(
        default=0.25,
        ge=0.0,
        le=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DIP_VELOCITY_CONVICTION_MAX_BOOST"),
        description="LOCATE #3: the maximum fractional size BOOST for the steepest qualifying dip (0.25 = up to +25% size on the fastest flush). The multiplier interpolates 1.0..1+this by the dip ROC over an ATR-noise floor; clamped here so it can never run away. Only consulted when chili_momentum_dip_velocity_conviction_enabled is ON.",
    )
    chili_momentum_sub_vwap_trap_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SUB_VWAP_TRAP_ENTRY_ENABLED"),
        description="LOCATE #4 SUB-VWAP TRAP: a breakdown BELOW VWAP that FAILS to follow through (no new low for K bars, a bottoming-tail flush that got bought) then RECLAIMS back above VWAP = a bear-trap / short-cover long. DISTINCT from vwap_reclaim (which needs K closes below): the trap is a SHARP undercut-and-reclaim (the stop-run below VWAP), not a sustained loss. Entry = the reclaim bar high (pullback_high); stop = the trap low (pullback_low). Carries ALL chase-guards (tape REQUIRED+fail-closed, _hod_extension_ok, _detect_back_side + front_side_state, _l2_entry_veto) + the LIVE_PENDING_ENTRY vetoes. false (default) = returns disabled before any compute = byte-identical.",
    )
    chili_momentum_pulling_away_roc_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLING_AWAY_ROC_ENTRY_ENABLED"),
        description="LOCATE #5 PULLING-AWAY ROC: a ROC-INFLECTION breakout — price tapped a multi-tap resistance >= chili_momentum_pulling_away_min_taps times (a tested ceiling) then PULLS AWAY on a ROC spike (the current-bar rate-of-change accelerates above its recent baseline by an ATR-scaled margin = the break is finally going). Entry = the resistance/break level (pullback_high); stop = the last swing low under the base (pullback_low). Carries ALL chase-guards (tape REQUIRED+fail-closed, _hod_extension_ok, _detect_back_side + front_side_state, _l2_entry_veto) + the setup-selector + LIVE_PENDING_ENTRY vetoes. false (default) = returns disabled before any compute = byte-identical.",
    )
    chili_momentum_pulling_away_min_taps: int = Field(
        default=2,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PULLING_AWAY_MIN_TAPS"),
        description="LOCATE #5: the minimum number of swing-high TAPS at the resistance band (ATR-derived) required before a pulling-away ROC break is a tested break, not a first touch. Only consulted when chili_momentum_pulling_away_roc_entry_enabled is ON.",
    )
    chili_momentum_premarket_pivot_macd_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_PIVOT_MACD_ENTRY_ENABLED"),
        description="LOCATE #6 PREMARKET PIVOT + MACD: a premarket gap-and-go pivot break — price breaks a premarket pivot (the premarket swing high) WITH a fresh MACD re-cross (line crosses back ABOVE signal within the lookback) AND a COLD-MARKET avoid (skip when RVOL is below the cold floor = no premarket interest). Entry = the pivot level (pullback_high); stop = the premarket pivot low (pullback_low). Carries ALL chase-guards (tape REQUIRED+fail-closed, _hod_extension_ok, _detect_back_side + front_side_state, _l2_entry_veto) + the setup-selector + LIVE_PENDING_ENTRY vetoes. EQUITY-ONLY (crypto is 24/7, no premarket). false (default) = returns disabled before any compute = byte-identical.",
    )
    chili_momentum_premarket_pivot_cold_rvol_floor: float = Field(
        default=1.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PREMARKET_PIVOT_COLD_RVOL_FLOOR"),
        description="LOCATE #6: the COLD-MARKET avoid floor — skip the premarket-pivot trigger when the current relative-volume is below this (a cold premarket with no interest is a fake-out). Only consulted when chili_momentum_premarket_pivot_macd_entry_enabled is ON.",
    )
    chili_momentum_instant_bid_above_fill_confirm_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_INSTANT_BID_ABOVE_FILL_CONFIRM_ENABLED"),
        description="LOCATE #7 INSTANT BID-ABOVE-FILL CONFIRM: the positive MIRROR of instant_bid_below_fill_cut. In the first chili_momentum_instant_bid_confirm_window_seconds after the fill, the live BID must hold AT/ABOVE the fill (within margin noise); if instead it has NOT held above the fill by the end of that window (the entry showed no immediate positive confirmation), FEED the existing instant_bid_below_fill / no-confirmation bail path (it does NOT add a new exit — it only flips on the SAME bailout the operator already gates). PROTECTIVE-ONLY: it can only cut a non-confirming entry sooner; never widens a stop. false (default) = no-op = byte-identical.",
    )
    chili_momentum_instant_bid_confirm_window_seconds: float = Field(
        default=6.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_INSTANT_BID_CONFIRM_WINDOW_SECONDS"),
        description="LOCATE #7: the post-fill window (seconds) in which the bid must confirm at/above the fill. Only consulted when chili_momentum_instant_bid_above_fill_confirm_enabled is ON.",
    )
    chili_momentum_second_leg_preference_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SECOND_LEG_PREFERENCE_ENABLED"),
        description="LOCATE #8 SECOND-LEG PREFERENCE: a SELECTION/conviction tilt in the setup-selector — prefer a later, BASED leg (a breakout whose base sits ABOVE a prior consolidation+support, i.e. the second leg of a two-leg move) over a 1st-leg break that is already extended off the open. When two breakout candidates fire, the one with a confirmed prior base/support between legs gets an R:R tilt of +chili_momentum_second_leg_rr_tilt. It is a PREFERENCE among already-passing fires — it never admits a NEW entry and never loosens a guard. ADDITIVE: OFF / single candidate ⇒ no tilt = byte-identical.",
    )
    chili_momentum_second_leg_rr_tilt: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SECOND_LEG_RR_TILT"),
        description="LOCATE #8: the fractional R:R tilt added to a based second-leg candidate when arbitrating the setup-selector (0.15 = +15% effective R:R weight). A preference only — bounded so it cannot dominate a vastly-worse R:R. Only consulted when chili_momentum_second_leg_preference_enabled is ON.",
    )
    chili_momentum_order_burst_candle_guard_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORDER_BURST_CANDLE_GUARD_ENABLED"),
        description="LOCATE #9 8AM BURST GUARD: a narrow time-windowed DISTRUST of the top-of-hour burst candle (esp. 08:00 ET) — within chili_momentum_order_burst_guard_window_minutes of a top-of-hour boundary, DEFER a fresh entry trigger (stay WATCHING, re-enter after the window) because the burst candle is order-imbalance noise, not a tradeable break. EQUITY-ONLY; mirrors the opening-bell suppression. RISK-REDUCING ONLY: it can only DEFER a fresh fire (never enables/loosens). false (default) = no-op = byte-identical.",
    )
    chili_momentum_order_burst_guard_window_minutes: float = Field(
        default=3.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ORDER_BURST_GUARD_WINDOW_MINUTES"),
        description="LOCATE #9: the minutes after a top-of-hour boundary (esp. 08:00 ET) during which a fresh burst-candle trigger is deferred. Only consulted when chili_momentum_order_burst_candle_guard_enabled is ON.",
    )
    chili_momentum_red_candle_entry_block_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RED_CANDLE_ENTRY_BLOCK_ENABLED"),
        description="LOCATE #10 RED-CANDLE ENTRY BLOCK: do NOT fire a fresh entry while the CURRENT 1m (entry-interval) bar is RED (close < open) — Ross never buys into a red candle; wait for the green confirmation. DEFER (stay WATCHING) when the latest bar on the entry frame is red. RISK-REDUCING ONLY: it can only DEFER a fresh fire (never enables/loosens). false (default) = no-op = byte-identical.",
    )
    chili_momentum_dip_buy_rth_only_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DIP_BUY_RTH_ONLY_ENABLED"),
        description="GAP 4 (bug fix): the flush-dip / deep-reclaim DIP-BUY only works 09:30-16:00 ET because stops fire then (there are NO stops premarket, so a premarket dip-buy that breaks down cannot be exited at the stop). flush_dip_buy_confirmation has an unused now param + no clock check, and _evaluate_deep_reclaim's morning-only gate is SKIPPED when bar_ts is None (it leaks premarket). When ON, the flush-dip gate requires the current bar (now param, else df.index[-1]) to be inside the RTH window [chili_momentum_dip_buy_rth_start_hour, chili_momentum_dip_buy_rth_end_hour) ET; outside ⇒ no fire. EQUITY-ONLY (crypto is 24/7 and exempt). ADDITIVE: OFF / crypto / no usable clock ⇒ no gate, byte-identical.",
    )
    chili_momentum_dip_buy_rth_start_hour: float = Field(
        default=9.5,
        ge=0.0,
        le=24.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DIP_BUY_RTH_START_HOUR"),
        description="GAP 4: RTH window START as ET hours-of-day (9.5 = 09:30). Only consulted when chili_momentum_dip_buy_rth_only_enabled is ON.",
    )
    chili_momentum_dip_buy_rth_end_hour: float = Field(
        default=16.0,
        ge=0.0,
        le=24.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_DIP_BUY_RTH_END_HOUR"),
        description="GAP 4: RTH window END as ET hours-of-day (16.0 = 16:00). The window is half-open [start, end). Only consulted when chili_momentum_dip_buy_rth_only_enabled is ON.",
    )
    chili_momentum_big_buyer_bid_starter_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BIG_BUYER_BID_STARTER_ENABLED"),
        description="GAP 5: the BID-side MIRROR of _l2_entry_veto (which vetoes on a big SELLER / hidden seller) — a large stacked BUYER on the bid (depth-imbalance percentile at/ABOVE chili_momentum_big_buyer_bid_pctile_ceiling, a TREND of accumulation in its own window) near a whole/half dollar PERMITS / confirms a dip-buy starter. Keeps the existing SPREAD caveat (a wide bid-ask spread still blocks — never arm a starter on an illiquid book). It is an ENABLER (a positive confirmation overlay), never a veto: it cannot block any existing entry. Reuses read_ladder_distribution; FAIL-CLOSED (returns None / no permit on missing/stale/wide-spread L2). false (default) = never consulted = byte-identical.",
    )
    chili_momentum_big_buyer_bid_pctile_ceiling: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BIG_BUYER_BID_PCTILE_CEILING"),
        description="GAP 5: depth-imbalance percentile at/ABOVE which the NEWEST book is treated as a big resting BID wall (accumulation trend) → permit/confirm the dip-buy starter. Self-relative to the symbol's own recent window (mirror of chili_momentum_entry_l2_bigseller_pctile_floor). Only consulted when chili_momentum_big_buyer_bid_starter_enabled is on.",
    )
    chili_momentum_big_buyer_bid_max_spread_bps: float = Field(
        default=80.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BIG_BUYER_BID_MAX_SPREAD_BPS"),
        description="GAP 5: the SPREAD caveat — a bid-ask spread (bps) at/above this BLOCKS the big-buyer-on-bid permit (a wide spread = illiquid book, never arm a starter there). Only consulted when chili_momentum_big_buyer_bid_starter_enabled is on.",
    )
    chili_momentum_add_into_halt_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADD_INTO_HALT_ENABLED"),
        description="GAP 6 (RISKIEST — KEEP OFF UNTIL SOAKED): permit a SMALL pyramid ADD while the name is HALTED LIMIT-UP, gated by EVERY extra condition (fail-CLOSED on any miss): (1) ALREADY IN PROFIT on the name (bid > avg_entry by chili_momentum_add_into_halt_min_profit_r of the entry risk), (2) the halt is LIMIT-UP / bullish (resume side is up — read from the halt state), (3) the add is SMALL (the existing pyramid sizing + chili_momentum_pyramid_max_adds cap already bound it; this gate adds NO new size), (4) the ORIGINAL STRUCTURAL STOP is intact (unchanged since entry), (5) RTH-only. It NEVER adds if underwater. ROUTES through the SAME pyramid_add_decision + risk_evaluator admission as a normal add (kill-switch, daily-loss registry, governance). false (default) = the halt-add path is never entered = byte-identical to the existing pyramid behavior. Deploy recipe: KEEP OFF until soaked.",
    )
    chili_momentum_add_into_halt_min_profit_r: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADD_INTO_HALT_MIN_PROFIT_R"),
        description="GAP 6: the minimum open profit (in units of the entry's structural risk R = avg_entry - original_stop) required before an add-into-halt is permitted. 1.0 = at least +1R in the green. Only consulted when chili_momentum_add_into_halt_enabled is ON.",
    )
    chili_momentum_add_into_halt_swing_lookback: int = Field(
        default=6,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ADD_INTO_HALT_SWING_LOOKBACK"),
        description="GAP 6 hardening: the number of recent COMPLETED bars used to derive the breakout level (recent swing high) the add price is measured against for the extension / not-parabolic chase guard. Mirrors the momentum-continuation swing_lookback default (6). Only consulted when chili_momentum_add_into_halt_enabled is ON.",
    )
    # ── Warrior RISK re-audit gaps (4 RISK controls; each default OFF = byte-identical) ──
    # GAP 1 — RULE-BREAK -> NO-TRADE-NEXT-DAY LOCKOUT (PSY101 Mod 10 operant conditioning):
    # when a hard discipline rule is broken TODAY (a global daily-loss breach, the daily-
    # trade-count budget exceeded, or a max-loss-circuit fire), arm a lockout that BLOCKS live
    # arming for the NEXT ET trading session and AUTO-CLEARS once that session's ET day rolls
    # past (never permanent). Persisted in trading_risk_state (regime='rulebreak_nextday_lockout')
    # reusing the kill-switch DB infrastructure. RISK-REDUCING ONLY: it can ONLY block arming,
    # never permit a trade that was otherwise blocked, never change sizing. OFF (default) => the
    # lockout is never armed AND never consulted => byte-identical.
    chili_momentum_rulebreak_nextday_lockout_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_RULEBREAK_NEXTDAY_LOCKOUT_ENABLED"),
        description="GAP 1 (RISK): when a discipline rule is broken today (daily-loss breach / trade-count budget exceeded / max-loss circuit fire), block LIVE ARMING for the next ET trading session, auto-clearing after that session's ET day rolls. Persisted in trading_risk_state (regime='rulebreak_nextday_lockout'). RISK-REDUCING ONLY (it can only block arming, never permit or up-size). false (default) = never armed, never consulted = byte-identical.",
    )
    # GAP 2 — TIME/DECISION-FATIGUE DERATE (PSY101 decision-fatigue; Ross trades best EARLY):
    # size DOWN as the session lengthens (minutes since the 09:30 ET RTH open) and/or today's
    # real entered-trade count grows. A multiplier in [floor, 1.0] applied to the per-trade RISK
    # budget BEFORE compute_risk_first_quantity. RISK-REDUCING ONLY by construction: it is bounded
    # to (0, 1.0] (never > 1.0), composes multiplicatively under the existing 3x clamp, and the
    # equity-relative notional ceiling + liquidity cap still bound qty — so it can ONLY shrink size.
    # OFF (default) => multiplier forced to 1.0 => byte-identical.
    chili_momentum_fatigue_derate_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FATIGUE_DERATE_ENABLED"),
        description="GAP 2 (RISK): derate the per-trade risk budget DOWN as the session lengthens (minutes since 09:30 ET open) and/or today's entered-trade count grows (Ross trades best early). Multiplier in [floor, 1.0]; composes under the existing 3x clamp. RISK-REDUCING ONLY (<= 1.0, only shrinks size). false (default) = 1.0 = byte-identical.",
    )
    chili_momentum_fatigue_derate_floor: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FATIGUE_DERATE_FLOOR"),
        description="GAP 2: the FLOOR of the time/trade-count fatigue multiplier (the most the budget can be reduced). 0.5 = at most half size at peak fatigue. The ONE documented base; the derate is otherwise derived from elapsed RTH minutes + trade count. Only consulted when chili_momentum_fatigue_derate_enabled is ON.",
    )
    chili_momentum_fatigue_full_session_minutes: float = Field(
        default=240.0,
        gt=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_FATIGUE_FULL_SESSION_MINUTES"),
        description="GAP 2: minutes since the 09:30 ET RTH open at which the TIME leg of the fatigue derate reaches full weight (240 = 4h ~ Ross's prime 09:30-13:30 window). Only consulted when chili_momentum_fatigue_derate_enabled is ON.",
    )
    # GAP 3 — PYRAMID-ADD REQUIRES A FRESH DISCRETE SUB-PATTERN (HVM101): the deployed pyramid
    # add fires on CONTINUOUS cushion + new-HOD + OFI. This ADDS an extra AND guard requiring a
    # FRESH DISCRETE entry trigger (a new higher-low bounce off the rising EMA/VWAP after a dip)
    # so the lane adds on a re-set setup, not merely continuous green. RISK-REDUCING ONLY: it can
    # ONLY turn a would-fire add into a no-fire (it never relaxes the existing cushion/HOD/OFI/
    # iceberg guards, never fires an add they blocked). OFF (default) => the discrete trigger is
    # passed as None => the guard is inert => byte-identical to the existing pyramid behavior.
    chili_momentum_pyramid_discrete_add_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_PYRAMID_DISCRETE_ADD_ENABLED"),
        description="GAP 3 (RISK): require a FRESH DISCRETE entry sub-pattern (a new higher-low bounce off the rising EMA/VWAP) for a pyramid ADD, on top of the existing CONTINUOUS cushion + new-HOD + OFI guards. RISK-REDUCING ONLY (it only tightens the add — turns a would-fire into a no-fire — never loosens a veto). false (default) = discrete trigger passed as None = guard inert = byte-identical.",
    )
    # GAP 4 — CONSECUTIVE-HALT-DOWN LIQUIDATE (SS101-062 ZJYL/HKD halt-ladder liquidation trap):
    # if a HELD name prints CONSECUTIVE halt-DOWNs (each halt resumes LOWER = a cascading
    # limit-down death-spiral) at/above the threshold, LIQUIDATE via the SAME bailout exit
    # machinery rather than holding into the cascade. RISK-REDUCING ONLY: it can ONLY force an
    # EXIT of an existing position (it never opens, sizes, or holds anything). OFF (default) =>
    # the consecutive-down-halt counter is never consulted for a liquidation => byte-identical.
    chili_momentum_halt_down_cascade_liquidate_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_DOWN_CASCADE_LIQUIDATE_ENABLED"),
        description="GAP 4 (RISK): when a held name prints CONSECUTIVE down-halts (each halt resumes lower = a cascading limit-down) at/above chili_momentum_halt_down_cascade_threshold, LIQUIDATE via the existing bailout exit. RISK-REDUCING ONLY (it can only force an EXIT, never open/size/hold). false (default) = never consulted = byte-identical.",
    )
    chili_momentum_halt_down_cascade_threshold: int = Field(
        default=2,
        ge=2,
        validation_alias=AliasChoices("CHILI_MOMENTUM_HALT_DOWN_CASCADE_THRESHOLD"),
        description="GAP 4: the number of CONSECUTIVE down-halts (resume-lower events) at/above which the held position is liquidated. 2 = the second consecutive limit-down resume triggers the stand-aside (SS101-062). Only consulted when chili_momentum_halt_down_cascade_liquidate_enabled is ON.",
    )
    # ── FIX D: cache the Robinhood Agentic MCP adapter (perf bug-fix) ────────────────
    chili_momentum_cache_rh_agentic_adapter: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CACHE_RH_AGENTIC_ADAPTER"),
        description="FIX D: reuse a process-wide RobinhoodAgenticMcpAdapter singleton in auto_arm's 24h-tradability probe instead of constructing a fresh one per tick (which re-ran the 42-tool MCP discovery ~20/min and slowed the tick). Thread-safe + self-healing (rebuilt only when the cached instance reports unhealthy via is_enabled()). Same tradability probe — pure perf. false = legacy per-call construction (byte-identical).",
    )
    # ── L2 microstructure (crypto full-book persistence + OFI/micro-price tilt) ──
    # Cadence to drain the warmed Coinbase WS full-book ring into fast_orderbook
    # (crypto only; persists L2 so the live OFI tilt is measurable). 5s start.
    chili_crypto_l2_drain_seconds: float = Field(
        default=5.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_CRYPTO_L2_DRAIN_SECONDS"),
    )
    # OFI lookback window (seconds) for the viability tilt. Must be <= the ring's
    # _DEFAULT_BOOK_HISTORY (30s) so recent() spans it.
    chili_crypto_l2_ofi_window_s: float = Field(
        default=15.0,
        gt=0.0,
        validation_alias=AliasChoices("CHILI_CRYPTO_L2_OFI_WINDOW_S"),
    )
    # Live OFI/micro-price viability tilt (research: OFI = strongest L2 short-horizon
    # predictor, Cont/Kukanov/Stoikov; micro-price confirmer). Applied as a SMALL
    # agreement-guarded adjustment to the viability score (long-bias selection
    # tilt, NOT bps-scalping) — validated by live A/B + instant rollback. The
    # weight is the lever: tune live, set 0 to disable the tilt without redeploy.
    chili_momentum_ofi_tilt_weight: float = Field(
        default=0.015,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OFI_TILT_WEIGHT"),
    )
    # Normalized-OFI magnitude (in [0,1]) required before the tilt fires (with
    # micro-price agreement). Guards against thin-book / flicker noise.
    chili_momentum_ofi_threshold: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_OFI_THRESHOLD"),
    )
    # Iceberg / hidden-seller per-add probe (Ross SS101 #038, add-path only). The probe
    # score is refill_size / (price_advance_bps + 1) over the short L2 window: HIGH means
    # the displayed ask keeps REFILLING at the same price (absorbing seller) instead of
    # lifting as price advances. The add is blocked when score >= this threshold. This is
    # the ONE irreducible base; the score is otherwise self-normalizing (refill shares per
    # bp of advance). Fail-OPEN on absent/stale L2; reversible via
    # chili_momentum_iceberg_add_probe_enabled (flag) without redeploy.
    chili_momentum_iceberg_add_refill_ratio: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ICEBERG_ADD_REFILL_RATIO"),
    )
    # trade_flow (executed-tape aggressor imbalance) CONFIRMATION premium: scales the OFI+micro tilt
    # magnitude by (1+gain) when the EXECUTED tape AGREES in direction (and clears the threshold).
    # le=1.0 is the STRUCTURAL guarantee that trade_flow can never out-tilt OFI nor reach the 2x
    # double-count (3-way tilt = w*(1+gain) < 2w for gain<1). gain=0 -> trade_flow inert (kill-switch);
    # trade_flow absent (no tape) -> mult 1.0 -> byte-identical to the bare OFI tilt. No redeploy.
    chili_momentum_trade_flow_agreement_gain: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TRADE_FLOW_AGREEMENT_GAIN"),
    )
    chili_momentum_trade_flow_threshold: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_TRADE_FLOW_THRESHOLD"),
    )
    # ── microstructure_log (#698, parallel agent): background signal-LOG layer
    # (trading_microstructure_log). MEASUREMENT only — does not gate the live OFI
    # tilt above; persists OFI/micro/ask-eaten/etc + (later) forward returns so the
    # live A/B can quantify which signal helps. Settings kept so #698 honors env.
    chili_micro_log_drain_seconds: float = Field(
        default=5.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_MICRO_LOG_DRAIN_SECONDS"),
    )
    chili_micro_log_equity_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MICRO_LOG_EQUITY_ENABLED"),
    )
    chili_micro_log_control_sample_size: int = Field(
        default=8,
        ge=0,
        validation_alias=AliasChoices("CHILI_MICRO_LOG_CONTROL_SAMPLE_SIZE"),
    )
    chili_micro_log_retain_days: int = Field(
        default=21,
        ge=1,
        validation_alias=AliasChoices("CHILI_MICRO_LOG_RETAIN_DAYS"),
    )
    # ── LOG-ONLY 10-second candle pattern layer (Ross's 10s chart; measurement) ──
    # Aggregates the per-tick mid into 10s candles + runs ABCD / flat-top SHAPE
    # detectors, persisting detections + forward-returns so a FRESH calibration can
    # learn whether they predict BEFORE any wiring (the −1.58pp sub-bar lesson; the
    # old baseline is STALE; pattern-SHAPE ≠ speed). ZERO decision path. Crypto-first
    # (equity 60s tape is too sparse → the min_ticks guard fails closed; no fiction).
    chili_tenbeat_candle_enabled: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_TENBEAT_CANDLE_ENABLED"),
        description="Master gate for the LOG-ONLY 10s-candle pattern layer (crypto). Pure measurement — no decision path. ON = a visible instrument like the micro_log.",
    )
    chili_tenbeat_candle_equity_enabled: bool = Field(
        default=False, validation_alias=AliasChoices("CHILI_TENBEAT_CANDLE_EQUITY_ENABLED"),
        description="Equity 10s candles — DATA-gated OFF (the 60s equity NBBO tape cannot form a 10s candle; the min_ticks guard fails closed). Un-gate only when a 2-3s equity mid source lands.",
    )
    chili_tenbeat_candle_drain_seconds: int = Field(
        default=10, ge=5, validation_alias=AliasChoices("CHILI_TENBEAT_CANDLE_DRAIN_SECONDS"),
    )
    chili_tenbeat_bucket_seconds: int = Field(
        default=10, ge=5, validation_alias=AliasChoices("CHILI_TENBEAT_BUCKET_SECONDS"),
    )
    chili_tenbeat_min_ticks_per_bar: int = Field(
        default=2, ge=1, validation_alias=AliasChoices("CHILI_TENBEAT_MIN_TICKS_PER_BAR"),
        description="A 10s bucket with fewer ticks is a GAP (skipped, never synthesized). The single guard that keeps a 60s-sparse equity source dark.",
    )
    chili_tenbeat_window_bars: int = Field(
        default=12, ge=4, validation_alias=AliasChoices("CHILI_TENBEAT_WINDOW_BARS"),
    )
    chili_tenbeat_abcd_retrace_base: float = Field(
        default=0.50, gt=0.0, validation_alias=AliasChoices("CHILI_TENBEAT_ABCD_RETRACE_BASE"),
        description="The ONE ABCD base knob: the shallow-retrace cap (ATR-widened). Ross floor 0.50.",
    )
    chili_tenbeat_flatop_touches_min: int = Field(
        default=3, ge=2, validation_alias=AliasChoices("CHILI_TENBEAT_FLATOP_TOUCHES_MIN"),
        description="The ONE flat-top base knob: minimum highs clustered at the flat resistance.",
    )
    chili_tenbeat_flatop_lookback_bars: int = Field(
        default=6, ge=2, validation_alias=AliasChoices("CHILI_TENBEAT_FLATOP_LOOKBACK_BARS"),
    )
    chili_tenbeat_backfill_maturity_minutes: int = Field(
        default=6, ge=5, validation_alias=AliasChoices("CHILI_TENBEAT_BACKFILL_MATURITY_MINUTES"),
        description="Forward-return maturity floor (the +5m tail must be fully past + persisted before labeling — no lookahead).",
    )
    chili_tenbeat_candle_log_retain_days: int = Field(
        default=14, ge=1, validation_alias=AliasChoices("CHILI_TENBEAT_CANDLE_LOG_RETAIN_DAYS"),
    )
    chili_tenbeat_entry_tilt_weight: float = Field(
        default=0.03, ge=0.0,
        validation_alias=AliasChoices("CHILI_TENBEAT_ENTRY_TILT_WEIGHT"),
        description="LIVE use of the 10s chart (Ross-style): a fresh 10s ABCD/flat-top BREAKOUT nudges a crypto name's viability up by this × the pattern score (bounded; a fired breakout is bullish ⇒ agreement-guarded for the long-only lane). 0 = log-only (kill-switch). The detections keep accruing forward-returns so the live A/B keeps measuring — revert if it turns negative.",
    )
    chili_micro_log_roundtrip_cost_bps_crypto: float = Field(
        default=100.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MICRO_LOG_ROUNDTRIP_COST_BPS_CRYPTO"),
    )
    chili_micro_log_roundtrip_cost_bps_equity: float = Field(
        default=2.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MICRO_LOG_ROUNDTRIP_COST_BPS_EQUITY"),
    )
    # Ask-heavy book size-down: risk fraction applied when the decision-tick
    # L2 imbalance5 < -0.4 (the measured chronic-late threshold).
    chili_momentum_entry_ask_heavy_size_fraction: float = Field(
        default=0.5,
        gt=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ENTRY_ASK_HEAVY_SIZE_FRACTION"),
    )
    # Booking truth: live sessions idle in exited/cooldown beyond this window
    # are walked to live_finished so their realized PnL books an outcome row
    # (2026-06-12: $195 of exits never booked; the day looked -$70 vs -$265
    # broker truth). 0 disables.
    chili_momentum_exited_finalize_idle_min: float = Field(
        default=20.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXITED_FINALIZE_IDLE_MIN"),
    )
    # Exit-limit repeg window: an unfilled marketable-limit exit older than
    # this re-submits one rung down the ladder (wider guard, then market).
    chili_momentum_exit_limit_repeg_seconds: float = Field(
        default=20.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_LIMIT_REPEG_SECONDS"),
    )
    # EOD flatten (2026-06-12 QH: a 3:19 PM entry was still held 2 min before
    # the Friday close — no momentum scalp holds the bell, ever). Equity
    # positions flatten through the operator-flatten chokepoint this many
    # minutes before the 16:00 ET close.
    chili_momentum_eod_flatten_lead_min: float = Field(
        default=5.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EOD_FLATTEN_LEAD_MIN"),
    )
    # The momentum live lane executes via coinbase_spot (crypto). The viability
    # board ALSO carries equities (ARKK, CLSK...) that go live-eligible at US
    # market open — auto-arming one via Coinbase would fail mid-session. When True
    # (default) auto-arm only considers Coinbase-tradeable crypto pairs.
    chili_momentum_auto_arm_crypto_only: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_CRYPTO_ONLY"),
    )
    # Inverse focus: EQUITY-ONLY (Ross small-cap lane). When True, the lane excludes
    # crypto ("-USD") pairs entirely and trades stocks only — the Ross thesis is equity
    # day-trading, and crypto pre-entry watchers were consuming concurrency slots + adding
    # cancelled-pre-entry noise. Operator-controlled (set via env now; revisit crypto
    # later). Mutually exclusive with crypto_only (crypto_only takes precedence if both).
    chili_momentum_auto_arm_equity_only: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_EQUITY_ONLY"),
    )
    # ── SESSION-HYGIENE / CRYPTO-SEGREGATION (2026-06-25) ──────────────────────
    # All four default-ON; each flag-OFF restores byte-identical legacy behavior.
    #
    # (A) BROKER-ZERO CLOSE: when the exit broker-qty clamp SUCCESSFULLY reads the
    # held quantity at 0 (broker_zero=True is set ONLY on a successful read in
    # _submit_live_market_exit — never on None/exception), trust that confirmed-zero
    # and reconcile the session to LIVE_EXITED WITHOUT a second independent broker
    # read. The second read (_broker_position_confirms_zero) does not handle the
    # robinhood_agentic_mcp family (returns False), so a broker-FLAT agentic bailout
    # (FCUV sess 8791) looped live_bailout forever, pinning the slot. broker_zero is
    # already a confirmed-zero from a successful read; re-confirming only ADDS a
    # failure dependency. false = require the second read (legacy, byte-identical).
    chili_momentum_broker_zero_trust_clamp_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BROKER_ZERO_TRUST_CLAMP_ENABLED"),
        description="Trust a successful broker-qty-clamp zero (broker_zero=True) to reconcile to LIVE_EXITED without a second broker read. false = legacy double-read.",
    )
    # (A2) BROKER-ZERO CONFIRM-READS (2026-06-25, FIX B — the FCUV bailout phantom).
    # FCUV sess 8791 sat in live_bailout emitting live_exit_submit_failed +
    # live_exit_qty_clamped_to_broker(broker_qty=0) 98x in 20min and NEVER reconciled
    # (the bailout exit loop did not satisfy the confirmed-flat reconcile because the
    # robinhood_agentic_mcp family fell through the trust-clamp + the second-read
    # _broker_position_confirms_zero returns False for it). HARD REQUIREMENT: the
    # broker-zero reconcile must fire ONLY on a CONFIRMED-flat read — broker_zero=True
    # seen on N CONSECUTIVE exit pulses, NOT a single spurious 0 — so a one-off API
    # blip can never abandon a real position. The clamp read is per-pulse; this counts
    # consecutive confirmations (le["broker_zero_confirm_streak"]) and only reconciles
    # at N. 1 = single confirmed read (legacy trust-clamp behavior); 2 = belt-and-
    # suspenders (default). The streak resets on any non-zero / failed / None read.
    chili_momentum_broker_zero_confirm_reads: int = Field(
        default=2,
        ge=1,
        validation_alias=AliasChoices("CHILI_MOMENTUM_BROKER_ZERO_CONFIRM_READS"),
        description="FIX B: number of CONSECUTIVE successful broker_zero=True clamp reads required before the bailout/exit path reconciles to LIVE_EXITED (guards against a single spurious 0 abandoning a real position). 1 = single confirmed read; 2 = default belt-and-suspenders.",
    )
    # (B) CANCEL-ON-CONFIRM-BLOCK: when confirm_live_arm is BLOCKED after
    # begin_live_arm already created the session in live_arm_pending (a TOCTOU:
    # no_longer_eligible / risk_blocked / broker_not_ready / allocator_blocked), the
    # begin-created session is stranded in live_arm_pending — pinning a concurrency
    # slot (IQST sess 8804, 70+min). Release it via cancel_automation_session: a
    # pre-entry arm_pending session has NO broker order (no momentum_live_execution),
    # so the cancel is a pure CHILI-state transition to LIVE_CANCELLED. Covers BOTH
    # the primary RH arm and the alpaca paper-twin. false = legacy leak (TTL-reaped).
    chili_momentum_cancel_on_confirm_block_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CANCEL_ON_CONFIRM_BLOCK_ENABLED"),
        description="Cancel the begin-created live_arm_pending session when confirm_live_arm is blocked. false = leave it for the TTL reaper (legacy).",
    )
    # FIX-18 (B1) — ZOMBIE-WALL TTL on the begin_live_arm DEDUPE. A transient confirm
    # failure strands a live_arm_pending session; the begin_live_arm dedupe then returns it
    # as "already active" forever, blocking re-arm of the SAME symbol for hours (80 zombies/
    # 7d, median 6.6h; JEM x3 on 06-30). When True (default) the dedupe treats a
    # live_arm_pending session OLDER than the TTL below as DEAD: it terminalizes it
    # (live_arm_expired) and allows the new arm to proceed. ONLY live_arm_pending is
    # affected — a genuinely-active session (watching_live/entered/etc.) is NEVER expired,
    # and a FRESH pending (younger than the TTL) still dedupes (no double-arm). OFF =>
    # byte-identical legacy (any non-terminal session dedupes forever).
    chili_momentum_arm_pending_ttl_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ARM_PENDING_TTL_ENABLED"),
        description="FIX-18 (B1): True (default) => the begin_live_arm dedupe terminalizes an EXPIRED live_arm_pending (older than the TTL) as live_arm_expired and allows re-arm. OFF => legacy (dedupe forever).",
    )
    # Adaptive TTL (seconds) for a live_arm_pending session in the dedupe. Base ~ a small
    # multiple of the typical confirm latency (confirm normally lands same auto-arm tick, so
    # 120s = ~2x a generous latency budget). ONE documented base (feedback_adaptive_no_magic);
    # a stranded pending older than this is a zombie and is recycled.
    chili_momentum_arm_pending_ttl_seconds: float = Field(
        default=120.0,
        ge=10.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_ARM_PENDING_TTL_SECONDS"),
    )
    # (C) STALE-SESSION REAPER: a SAFE BOUNDED sweep (runs inside the existing
    # auto-arm pass, NOT a parallel loop) that terminalizes dead-but-lingering
    # sessions: (1) live_error past the TTL, (2) live_bailout past the TTL whose
    # broker position is CONFIRMED 0. NEVER closes a session with a real broker
    # position or in-flight order — every close requires a SUCCESSFUL broker-flat
    # read (fail-safe: any unknown/failed read leaves the session ALONE). arm_pending
    # is already handled by expire_stale_live_arm_sessions. false = no reaper (legacy).
    chili_momentum_stale_session_reaper_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STALE_SESSION_REAPER_ENABLED"),
        description="Bounded broker-truth-gated reaper for stale live_error / broker-flat live_bailout sessions. false = no reaper (legacy).",
    )
    # TTL (seconds) before a stale live_error / live_bailout session is eligible for
    # the reaper. Adaptive floor on the max-watch setting (no new magic number) so a
    # genuinely-active exit/error window is never reaped mid-flight. Default 2h.
    chili_momentum_stale_session_reaper_ttl_seconds: float = Field(
        default=7200.0,
        ge=300.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_STALE_SESSION_REAPER_TTL_SECONDS"),
    )
    # (D) CRYPTO VIABILITY GATE: when the lane is NOT trading crypto (crypto_only is
    # False AND there is no positive crypto live-arm intent), STOP scoring "-USD"
    # symbols into momentum_symbol_viability — they pollute the equity scoring pool
    # while never being armable (the auto_arm candidate query already excludes them).
    # Gate sits at the single persistence chokepoint (persist_neural_momentum_tick),
    # downstream of all scoring, so equity scoring is byte-identical. false = score
    # all symbols (legacy). When crypto IS re-enabled (crypto_only or crypto_live_arm),
    # the gate self-disables and crypto scoring resumes untouched.
    chili_momentum_crypto_viability_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CRYPTO_VIABILITY_GATE_ENABLED"),
        description="Skip persisting -USD viability rows when crypto is not traded. false = persist all symbols (legacy).",
    )
    # Liquidity-bias selection (ON): among the price-band-passed Ross small-caps,
    # prefer high-dollar-volume (tighter-spread, FILLABLE) names so triggers convert
    # to fills — the live spread gate blocks wide-spread entries, so a trigger on an
    # illiquid name never fills. Adaptive rank-blend (viability + dollar-volume),
    # no fixed threshold. Spread sweep proved the payoff (liquid +$12,818 vs wide +$634).
    chili_momentum_auto_arm_liquidity_bias: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_AUTO_ARM_LIQUIDITY_BIAS"),
    )
    chili_momentum_leveraged_etf_rank_weight: float = Field(
        # DOWN-WEIGHT leveraged/inverse ETFs (DRN/KMRK/SOXL/...) in the arm queue: they top the
        # raw RVOL/gap ranking but are geared index products, not the low-float company squeezes
        # the lane trades (and KMRK already cost -$58 on 2026-06-22). Their ross+viability rank
        # score is scaled by this factor (0.5 = halved -> a real mover outranks them; they still
        # arm if nothing better is up = down-weight, not ban). 1.0 = kill-switch (no down-weight).
        # Equity-only (crypto is never an equity ETF). [operator 2026-06-22 choice A]
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_LEVERAGED_ETF_RANK_WEIGHT"),
    )
    # FIX A (2026-06-23): HARD-VETO leveraged/inverse ETFs from the Ross lane at the
    # viability eligibility gate (live AND paper). The rank down-weight above only
    # demotes them and LEAKED — SOXS (3x-inverse semis) armed + traded, and 11 of 18
    # eligible names that morning were the Tradr/Defiance/T-REX "2X Short XXX" wave.
    # The lane is for low-float COMMON stock; these geared trackers do not belong.
    # Default-ON; kill-switch CHILI_MOMENTUM_EXCLUDE_LEVERAGED_ETFS=0 (reverts to the
    # soft down-weight-only behavior above). [operator 2026-06-23 choice A]
    chili_momentum_exclude_leveraged_etfs: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXCLUDE_LEVERAGED_ETFS"),
    )
    # A8 (Ross CLRO-lesson, 2026-07-02): REIT / closed-end-fund NAME token in the
    # structural selection filter. Ross passed "Wheeler Real Estate Investment Trust" at
    # a glance ("Not interested"); CHILI armed WHLR at 5:50 ET (a wasted watch slot). A
    # REIT / closed-end fund is not the low-float company squeeze the lane trades. Unlike
    # the leveraged-ETF HARD veto above, this is a soft DOWN-WEIGHT (score derate) at the
    # same viability site — a real mover still outranks it, but junk fund structures no
    # longer waste a slot. Fail-OPEN (False) on a name miss so a real mover is never
    # wrongly demoted. Default-ON; kill-switch CHILI_MOMENTUM_EXCLUDE_FUND_STRUCTURES=0.
    chili_momentum_exclude_fund_structures_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXCLUDE_FUND_STRUCTURES_ENABLED"),
        description="A8: DOWN-WEIGHT (not ban) REIT / closed-end-fund named structures in the momentum selection score. Fail-open on a name miss. OFF => byte-identical (no derate).",
    )
    # A-SETUP QUALITY FLOOR (2026-06-26): the 'puro talo' root — the lane had NO
    # quality floor, so it armed/traded ANYTHING that fired a trigger -> B/C junk and
    # small losses all day (9 round-trips, all ~-$65 losers). Live proof: AREC (float
    # 107M, rvol 5.9, +12.9% — LARGE float, MODEST mover) armed+lost then re-armed;
    # CODI (float=None, rvol 0, +0%) queued on a bare pullback shape. The real A-setups
    # are LOW-FLOAT EXPLOSIVE names (UPC 648K/+227%, SDOT 744K/+84%, WSHP ~11M/+47%) —
    # Ross trades those ONLY and sits out the junk. This is a LIVE-eligibility quality
    # floor that can ONLY RESTRICT (set live_eligible False; never newly True). A name
    # is LIVE-tradeable ONLY if ALL hold: (1) LOW FLOAT (float_shares <= the ceiling —
    # THE primary discriminator: AREC 107M FAILS, UPC/SDOT/WSHP PASS), (2) real RVOL >=
    # the explosive-rvol floor, (3) meaningful change >= the change floor, and (4)
    # FLOAT-CONFIRMED: FAIL-CLOSED when float is missing/None/0 (CODI) — cannot confirm
    # low-float => not an A-setup => reject (also cleanly rejects empty-signal scanner
    # names). PAPER eligibility is UNCHANGED. Default-OFF -> byte-identical loose
    # eligibility. Kill-switch CHILI_MOMENTUM_A_SETUP_QUALITY_FLOOR_ENABLED.
    chili_momentum_a_setup_quality_floor_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_A_SETUP_QUALITY_FLOOR_ENABLED"),
        description="LIVE-eligibility A-setup quality floor for the Ross momentum lane: a name is live-tradeable only if low-float (<= ceiling) AND real-RVOL AND meaningful-change AND float-confirmed (fail-closed on missing float). RESTRICT-only; paper unchanged. OFF = byte-identical.",
    )
    # ONE documented float ceiling (the primary A-setup discriminator). 20M rejects the
    # AREC-class large floats (107M) while passing every real low-float squeeze the lane
    # exists to trade (UPC 648K / SDOT 744K / WSHP ~11M). A FLOOR/reference, not magic:
    # raise it via env if the universe shifts. The B-zone ceiling below is a stricter
    # OPTIONAL second tier (names above the primary ceiling but below the B-zone are NOT
    # admitted by this gate — it only restricts — they remain rejected; the B-zone is
    # reserved for a future graded-quality consumer and is documented here as the one
    # adaptive knob, not scattered).
    chili_momentum_a_setup_quality_floor_float_ceiling_shares: float = Field(
        default=20_000_000.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_A_SETUP_QUALITY_FLOOR_FLOAT_CEILING_SHARES"),
        description="Max float_shares for an A-setup LIVE name (~20M): rejects AREC-class 107M, passes UPC/SDOT/WSHP. ONE documented ceiling.",
    )
    chili_momentum_a_setup_quality_floor_change_pct_min: float = Field(
        default=10.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_A_SETUP_QUALITY_FLOOR_CHANGE_PCT_MIN"),
        description="Min absolute day-change %% for an A-setup LIVE name; aligned with the Ross change floor (~10%%). CODI (0%%) rejected.",
    )
    # EXPLOSIVE-PREQUAL SCORE FLOOR (LIVE only; the UPC blocker fix). A +500% low-float
    # mover scored viability 0.55 — BELOW the impulse_breakout entry bar (0.56,
    # strategy_params.py:35) — so it never armed while a generic 0.56 bar cleared. This
    # RAISE-ONLY floor lifts the viability of a GENUINE Ross A-setup (low-float + SIGNED
    # up-change >= the change floor + RVOL ok) just OVER the default bar so the score
    # arithmetic stops vetoing the exact name the lane exists to trade. It NEVER lowers a
    # score and is gated by the SAME hardened explosive conjunction the extreme-vol relax
    # uses (tradable + spread-ok + affirm-explosive + not-below-floor + still live-eligible)
    # plus a SIGNED-up A-setup conjunction (fail-CLOSED on missing change so a low-float
    # CRASHER cannot be lifted). A floored name is coupled to RISK-BOUNDED sizing (sized
    # DOWN). Default-ON (no dark flags); OFF => byte-identical.
    chili_momentum_explosive_prequal_floor_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_PREQUAL_FLOOR_ENABLED"),
        description="Raise-only viability floor that lifts a genuine low-float + signed-up + rvol Ross A-setup just over the impulse_breakout entry bar (the UPC 0.55<0.56 fix). Anti-junk fail-closed, size-down-coupled, RESTRICT-only (never lowers). OFF = byte-identical.",
    )
    # ONE documented base margin: how far ABOVE the reference bar to lift a qualifying
    # name. 0.02 over the 0.56 default bar => 0.58, which clears the impulse_breakout
    # entry_viability_min in the DEFAULT (no-bump) regime. Reference, not magic.
    chili_momentum_explosive_prequal_margin: float = Field(
        default=0.02,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_PREQUAL_MARGIN"),
        description="Margin lifted ABOVE the reference bar for a qualifying explosive A-setup (0.02 => 0.58 over the 0.56 default bar). ONE documented base.",
    )
    # The reference entry bar this floor clears. 0.56 = impulse_breakout
    # entry_viability_min (strategy_params.py:35), the DEFAULT family bar. NOTE (RISK #1):
    # the LIVE binding can be RAISED above this (midday-lull +0.05, run-R breaker, families
    # that bind higher e.g. 0.57/0.60), so floor+margin clears the bar in the DEFAULT /
    # no-bump impulse_breakout regime, NOT unconditionally — which is acceptable since Ross
    # sits out the midday lull anyway. Raise via env to track a higher resolved bar.
    chili_momentum_explosive_prequal_bar_ref: float = Field(
        default=0.56,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXPLOSIVE_PREQUAL_BAR_REF"),
        description="Reference entry bar the prequal floor clears = impulse_breakout entry_viability_min (0.56, strategy_params.py:35), the DEFAULT family bar. Documents the no-bump regime the floor unblocks.",
    )
    # FIX B (2026-06-23): QUALITY SLOT-PRIORITY TIER in the arm queue. The
    # multiplicative ETF down-weight above leaks (a fresh-ross ETF at ×0.5 still
    # outranks a real company whose ross score went stale -> 0.0; 13 such inversions
    # live on 2026-06-23). Make instrument CLASS a LEADING tier key so genuine
    # low-float companies are floored STRICTLY above any leveraged/inverse ETF, with
    # the existing (ross, viability) order preserved WITHIN each tier. Backstops Fix
    # A's fail-open (an ETF with missing fundamentals stays eligible -> still floored
    # here). Default-ON; =0 restores byte-identical (ross, viability) ordering.
    chili_momentum_quality_slot_priority_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_QUALITY_SLOT_PRIORITY_ENABLED"),
    )
    # AGENTIC EXIT — cancel covering SELLs before a full-position exit (2026-06-23 strand
    # fix). A resting partial-target SELL (tracked OR untracked) locks shares, so the
    # agentic stop/trail/bailout is rejected "Not enough shares to sell" -> 8 retries ->
    # live_error -> stranded (PALI/LILA/RDGT/AIIO). Cancel ANY working agentic sell for
    # the symbol first (mirrors crypto _cancel_coinbase_open_sell_orders); re-runs each
    # attempt to clear a cancel-propagation race. Agentic-only; spot/crypto byte-identical.
    # Default-ON; kill-switch CHILI_MOMENTUM_EXIT_CANCEL_COVERING_SELLS=0.
    chili_momentum_exit_cancel_covering_sells: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EXIT_CANCEL_COVERING_SELLS"),
    )
    # NBBO spread tape (ON): each RTH cycle, persist the CLEAN consolidated bid/ask
    # (Massive snapshot lastQuote) for the Ross universe so the spread-sensitive
    # replay uses REAL spreads, not a proxy (the dollar-volume proxy read PAVS at
    # 53bps vs the 317bps the live lane actually saw). Source = what the lane already
    # receives; no fragile raw-quote NBBO reconstruction. (nbbo_tape.py)
    chili_momentum_nbbo_tape_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NBBO_TAPE_ENABLED"),
    )
    chili_momentum_nbbo_tape_sample_seconds: int = Field(
        default=60, ge=15, le=900,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NBBO_TAPE_SAMPLE_SECONDS"),
    )
    chili_momentum_nbbo_tape_retention_days: int = Field(
        default=30, ge=1, le=365,
        validation_alias=AliasChoices("CHILI_MOMENTUM_NBBO_TAPE_RETENTION_DAYS"),
    )
    # UNIVERSE TICK DENSIFICATION (2026-06-15, the JRSH/CUPR "missed name" gap):
    # the tape recorder only persists ticks for ARMED names, so a name the lane
    # never armed has only the 1-min sampler rows — too coarse to replay its
    # micro-pullback faithfully. This densifies EVERY uncapped-universe member's
    # WS quote into the tape (source='massive_ws_universe') via an INDEPENDENT
    # listener on the ignition loop, so tomorrow's replay HAS sub-minute ticks for
    # the names we missed today. FORWARD-only (no historical ticks). Write-only
    # side path: no trading logic reads it; the densified rows carry a SHORTER
    # retention than the 30d snapshot tape (bounded growth, the exit_parity bloat
    # lesson). KILL-SWITCH: False ⇒ the ignition loop registers no extra listener
    # ⇒ byte-identical to current.
    chili_momentum_universe_tick_record_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_MOMENTUM_UNIVERSE_TICK_RECORD_ENABLED"),
        description="Densify the whole momentum universe's WS quotes into the NBBO tape (source='massive_ws_universe') so missed names are replayable. KILL-SWITCH: False ⇒ no extra listener (byte-identical).",
    )
    chili_momentum_universe_tick_retention_days: int = Field(
        default=5, ge=1, le=30,
        validation_alias=AliasChoices("CHILI_MOMENTUM_UNIVERSE_TICK_RETENTION_DAYS"),
        description="Retention (days) for densified universe ticks — shorter than the snapshot tape's 30d; the prune drops source='massive_ws_universe' rows older than this.",
    )
    # TICK-FAITHFUL REPLAY (2026-06-15): replay the densified per-tick tape inside
    # the 1-min entry grid so a micro-pullback break that happened INSIDE a minute
    # fires at the true sub-minute instant where WS ticks exist (SUPERSET: where
    # only the 1-min sampler exists, it degrades to exactly today's 1-sample
    # behavior — byte-identical). Default OFF until replay-proven.
    chili_momentum_replay_tick_entry_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_TICK_ENTRY_ENABLED"),
        description="Replay every densified tick in the entry window (true sub-minute resolution where WS ticks exist; byte-identical where only 1-min snapshots exist). Default OFF.",
    )
    # FULL-PIPELINE REPLAY (2026-06-15): re-run the REAL selection pipeline as-of
    # each replay step from raw tape — Stage1 build_equity_universe re-screen,
    # Stage2 re-score, Stage3/4 re-arm/re-enter — so the replay can test whether a
    # NEW selection/scoring change would arm names the recorded day missed. Default
    # OFF; armed_source='live'/'asof' stay byte-identical when off.
    chili_momentum_replay_full_pipeline_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_REPLAY_FULL_PIPELINE_ENABLED"),
        description="Replay armed_source='full_pipeline': re-run the real as-of selection pipeline (build_equity_universe re-screen → re-score → re-arm) from raw tape. Default OFF.",
    )
    # Alpaca execution lane (DMA-style limit-posting over RH). Off until keys are set;
    # PAPER by default — the free sandbox proves fills before any real money. Activation
    # = enabled flag + keys present (a real dependency, not a dark gate). (ALPACA_LANE.md)
    chili_alpaca_enabled: bool = Field(
        default=False, validation_alias=AliasChoices("CHILI_ALPACA_ENABLED"),
    )
    chili_alpaca_paper: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_ALPACA_PAPER"),
    )
    # Ortex short-mechanics API key (squeeze-fuel tilt). Trader plan: 1,000 credits/mo,
    # 1 req/s, single-stock only — the fetch is gated to top-N explosive low-float
    # candidates + cached 12h (see short_mechanics.py). Empty ⇒ no fetch ⇒ no tilt
    # (fail-open / byte-identical). Use the literal "TEST" key for free random-data tests.
    chili_ortex_api_key: str = Field(
        default="", validation_alias=AliasChoices("CHILI_ORTEX_API_KEY"),
        description="Ortex short-interest / cost-to-borrow API key for the squeeze-fuel selection tilt. Empty ⇒ no Ortex fetch ⇒ no tilt (fail-open).",
    )
    chili_alpaca_api_key: str = Field(
        default="", validation_alias=AliasChoices("CHILI_ALPACA_API_KEY"),
    )
    chili_alpaca_api_secret: str = Field(
        default="", validation_alias=AliasChoices("CHILI_ALPACA_API_SECRET"),
    )
    chili_alpaca_data_feed: str = Field(
        default="iex", validation_alias=AliasChoices("CHILI_ALPACA_DATA_FEED"),
    )
    chili_alpaca_quote_max_age_seconds: float = Field(
        default=60.0, ge=1.0, le=600.0,
        validation_alias=AliasChoices("CHILI_ALPACA_QUOTE_MAX_AGE_SECONDS"),
    )
    # DATA/EXECUTION DECOUPLING (2026-07-07): source Alpaca-lane ENTRY quotes from IQFeed L1
    # (momentum_nbbo_spread_tape) instead of the thin Alpaca-IEX feed that dormantized the lane
    # 06-18 (stale_bbo/no_bbo on Ross names). Alpaca stays EXECUTION-only. Default ON (no dark
    # flags); OFF => Alpaca-IEX (legacy). (ALPACA_PAPER_ENABLE_PLAN.md)
    chili_alpaca_quotes_via_iqfeed: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_ALPACA_QUOTES_VIA_IQFEED"),
    )
    # PRIMARY EQUITY -> ALPACA PAPER routing (2026-07-07): when ON, the momentum lane routes the
    # PRIMARY equity arm to alpaca_spot (Alpaca PAPER, fake money) instead of the RH live rail —
    # for running the lane on Alpaca paper during the RH->Alpaca cash transfer (no real-money RH
    # entries). Requires chili_alpaca_enabled + paper + key. alpaca_spot is excluded from real
    # risk caps (paper-by-construction). Default OFF => byte-identical (RH live).
    chili_momentum_equity_execution_via_alpaca_paper: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_EQUITY_EXECUTION_VIA_ALPACA_PAPER"),
    )
    # ── Short-side lane (SHORT_SIDE_LANE.md) ──────────────────────────────────
    # Master gate for the SHORT side on the Alpaca rail. DEFAULT-OFF on purpose:
    # shorting is asymmetric/dangerous (unbounded squeeze upside) and not yet
    # wired into the momentum lane (no short triggers until P1+) or soaked, so
    # paper-first + OFF until proven. OFF ⇒ byte-identical long-only lane. This
    # is the ONE deliberate dark flag (an un-soaked dangerous capability with no
    # triggers yet) — unlike the profitable LONG levers, which ship ON.
    chili_momentum_short_lane_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_MOMENTUM_SHORT_LANE_ENABLED"),
        description="Master gate for the Alpaca SHORT lane. Default OFF (paper-first, un-soaked, no triggers wired yet). OFF ⇒ byte-identical long-only lane.",
    )
    # Shake-out learning: how long after an exit to watch the price path to judge
    # whether the thesis would have worked (was the stop too tight?). 30min.
    chili_momentum_post_exit_horizon_seconds: int = Field(
        default=1800,
        ge=300,
        le=86400,
        validation_alias=AliasChoices("CHILI_MOMENTUM_POST_EXIT_HORIZON_SECONDS"),
    )

    # Shake-out learning: outer age bound for the durable-cursor labeler. A pending
    # marker older than this (measured from the marker's own exit_time, NOT the
    # session's frozen updated_at) is retired as 'expired' rather than labeled — the
    # post-exit bars would be gappy and the signal stale. Generous (48h) so a
    # scheduler restart / backlog can never orphan a marker the way the old
    # updated_at>=now-3h window did. (post_exit_excursion.run_post_exit_excursion_pass)
    chili_momentum_post_exit_max_age_seconds: int = Field(
        default=172800,
        ge=3600,
        le=1209600,
        validation_alias=AliasChoices("CHILI_MOMENTUM_POST_EXIT_MAX_AGE_SECONDS"),
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
    chili_autotrader_alert_confidence_probability_weight: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_ALERT_CONFIDENCE_PROBABILITY_WEIGHT"
        ),
        description=(
            "Weight applied when converting uncalibrated alert confidence into "
            "edge probability; 0 keeps probability at 50%, 1 trusts confidence fully."
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
        description=(
            "AutoTrader alerts fetched per tick. When "
            "chili_autotrader_candidate_batch_adaptive is True (default) this is "
            "only the cold-start seed; the live batch is sized adaptively from "
            "the tick budget and observed per-candidate latency. When adaptive is "
            "False this is the fixed batch."
        ),
    )
    chili_autotrader_candidate_batch_adaptive: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_CANDIDATE_BATCH_ADAPTIVE"),
        description=(
            "When True (default), size the per-tick candidate fetch batch "
            "adaptively: soft tick budget / EWMA per-candidate latency, clamped "
            "to [default, max]. Fast skips => larger batch, slow LLM "
            "revalidations => smaller; the tick budget defers any overflow. Set "
            "False to pin the batch to chili_autotrader_candidate_batch_size."
        ),
    )
    chili_autotrader_candidate_select_statement_timeout_ms: int = Field(
        default=0,
        ge=0,
        le=10000,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_CANDIDATE_SELECT_STATEMENT_TIMEOUT_MS"
        ),
        description=(
            "Optional hard Postgres statement timeout for each AutoTrader "
            "candidate selection query. Zero derives the timeout from the "
            "tick budget fraction."
        ),
    )
    chili_autotrader_candidate_select_timeout_fraction: float = Field(
        default=AUTOTRADER_CANDIDATE_SELECT_TIMEOUT_DEFAULT_FRACTION,
        ge=0.01,
        le=0.5,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_CANDIDATE_SELECT_TIMEOUT_FRACTION"
        ),
        description=(
            "Fraction of the AutoTrader tick budget reserved for each "
            "candidate selection query when no explicit statement timeout is set."
        ),
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
    chili_autotrader_candidate_price_prefetch_batch_timeout_seconds: float = Field(
        default=AUTOTRADER_CANDIDATE_PRICE_PREFETCH_BATCH_TIMEOUT_DEFAULT_SECONDS,
        ge=AUTOTRADER_CANDIDATE_PRICE_PREFETCH_BATCH_TIMEOUT_MIN_SECONDS,
        le=AUTOTRADER_CANDIDATE_PRICE_PREFETCH_BATCH_TIMEOUT_MAX_SECONDS,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_CANDIDATE_PRICE_PREFETCH_BATCH_TIMEOUT_SECONDS"
        ),
        description=(
            "Wall-clock cap for the opportunistic AutoTrader candidate quote "
            "batch prefetch. When exceeded, the tick proceeds without the "
            "prefetched quote rather than spending the trading budget on "
            "market-data latency."
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
    chili_autotrader_stock_momentum_context_exempt_eligible: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_STOCK_MOMENTUM_CONTEXT_EXEMPT_ELIGIBLE"
        ),
        description=(
            "When true, patterns already certified + promoted to a trade-eligible "
            "lifecycle (promoted/pilot/live) skip the stock momentum-context gate. "
            "Its gap/relative-volume requirement is a momentum-surge proxy that "
            "systematically drops the mean-reversion setups (oversold bounce, IBS, "
            "BB reversion) that are a large share of the equity book's proven edge; "
            "those patterns cleared a far higher bar and still face the "
            "expected-edge gate downstream. Set false to restore the legacy "
            "behavior where the proxy gates every stock candidate when the "
            "candidate queue is saturated."
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
    chili_autotrader_positive_reprice_entry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_POSITIVE_REPRICE_ENTRY_ENABLED"
        ),
        description=(
            "Allow adverse slipped entries to proceed only after rechecking "
            "that expected net edge remains positive at the current price."
        ),
    )
    chili_autotrader_positive_reprice_entry_asset_types: str = Field(
        default="stock,crypto",
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_POSITIVE_REPRICE_ENTRY_ASSET_TYPES"
        ),
        description="Comma-separated asset types eligible for positive-edge slippage reprice acceptance.",
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
    chili_execution_event_lag_min_samples: int = Field(
        default=5,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices("CHILI_EXECUTION_EVENT_LAG_MIN_SAMPLES"),
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
    chili_edge_evolution_time_decay_tighten_fraction: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_EDGE_EVOLUTION_TIME_DECAY_TIGHTEN_FRACTION"
        ),
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
    chili_coinbase_absent_no_fill_reconcile_streak_min: int = Field(
        default=12,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_ABSENT_NO_FILL_RECONCILE_STREAK_MIN"
        ),
    )
    chili_coinbase_absent_no_fill_reconcile_min_age_seconds: int = Field(
        default=1800,
        ge=0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_ABSENT_NO_FILL_RECONCILE_MIN_AGE_SECONDS"
        ),
    )
    # Minimum fraction of the position that recoverable broker SELL fills must
    # cover for a stale-Coinbase close to be priced at the observed-fill VWAP
    # (data-first 2026-06-05). Above this floor, the whole close is priced at the
    # real VWAP (best estimate) instead of pnl=NULL / no_exit_price; below it the
    # fills are too thin to represent the close so the exit price stays unknown
    # (never fabricated). 0.5 = at least half the position must be observed.
    chili_coinbase_stale_close_min_fill_coverage: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_STALE_CLOSE_MIN_FILL_COVERAGE"
        ),
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
    # Round-trip MAKER fee for Coinbase. Used by the cost-aware gate instead of
    # the taker fee when chili_coinbase_maker_only_enabled is set, because
    # entries route strictly post-only (maker) and never pay taker. Default
    # 80bps = 40bps/side (Tier-1 Advanced Trade maker), ~2/3 of the 120bps taker
    # round-trip; lower at higher volume tiers.
    chili_coinbase_maker_fee_bps_round_trip: int = Field(
        default=80,
        validation_alias=AliasChoices("CHILI_COINBASE_MAKER_FEE_BPS_ROUND_TRIP"),
    )
    # Cushion above the raw fee floor — covers spread + slippage
    # plus a small margin for execution drift. 30bps is conservative
    # for Tier 1 retail; tighter at higher tiers.
    chili_min_edge_safety_buffer_bps: int = Field(
        default=30,
        validation_alias=AliasChoices("CHILI_MIN_EDGE_SAFETY_BUFFER_BPS"),
    )
    # ── Crypto liquidity floor (2026-06-13 crypto-live plan, A1) ──────────────
    # The Ross scorer ranks crypto on burst signals that are blind to whether
    # the name can be traded at size; the lane was arming $24k/24h names. A
    # crypto pair is tradeable iff its 24h quote ($) volume clears this floor.
    # $1.44M/24h = ~$1k/min, the plan's median-1m-$vol floor. Adaptive by
    # design: ONE documented number, no hardcoded ticker whitelist.
    chili_crypto_min_quote_volume_24h_usd: float = Field(
        default=1_440_000.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CRYPTO_MIN_QUOTE_VOLUME_24H_USD"),
    )
    # Max acceptable live spread (bps) for a crypto entry — a wide book is a
    # hidden round-trip cost the $-volume floor won't catch. Probed via the
    # venue adapter when chili_crypto_liquidity_spread_probe_enabled.
    chili_crypto_max_spread_bps: float = Field(
        default=50.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CRYPTO_MAX_SPREAD_BPS"),
    )
    chili_crypto_liquidity_spread_probe_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_CRYPTO_LIQUIDITY_SPREAD_PROBE_ENABLED"),
    )
    # Per-name notional cap = this fraction of one minute's $-volume. Never
    # post more than half a minute of turnover (the liquidity ceiling).
    chili_crypto_notional_vol_fraction: float = Field(
        default=0.5,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_CRYPTO_NOTIONAL_VOL_FRACTION"),
    )
    # Crypto entry-window clock (2026-06-13 crypto-live plan, A5). The clock
    # analysis found 0/21 earned in the 21:00–05:00 UTC dead band; bursts +
    # follow-through concentrate in 05:00–10:00 and 12:00–21:00 UTC. When on,
    # the lane arms NO new crypto entries outside those windows (exits
    # unaffected) — so the weekend soak measures productive-window behavior,
    # not dead-hours noise that would pollute the validation gate.
    chili_crypto_schedule_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_CRYPTO_SCHEDULE_ENABLED"),
    )
    # Per-asset-class geometry (2026-06-13 crypto-live plan, A4). The global
    # reward:risk + scale-out knobs are tuned for the equity lane (2:1, 0.33);
    # crypto's fatter-tail moves want a wider target and a heavier first
    # de-risk. These crypto OVERRIDES apply only to -USD symbols; left at None
    # they fall back to the global equity knobs (so equity is never affected).
    chili_momentum_crypto_reward_risk_ratio: Optional[float] = Field(
        default=3.0,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CRYPTO_REWARD_RISK_RATIO"),
    )
    chili_momentum_crypto_scale_out_fraction: Optional[float] = Field(
        default=0.5,
        validation_alias=AliasChoices("CHILI_MOMENTUM_CRYPTO_SCALE_OUT_FRACTION"),
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
    chili_coinbase_cost_gate_window_days: int = Field(
        default=30,
        ge=1,
        validation_alias=AliasChoices(
            "CHILI_COINBASE_COST_GATE_WINDOW_DAYS"
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
    chili_shadow_vetting_min_pilot_roster: int = Field(
        default=1,
        ge=0,
        validation_alias=AliasChoices("CHILI_SHADOW_VETTING_MIN_PILOT_ROSTER"),
    )
    chili_shadow_vetting_min_pilot_score: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_SHADOW_VETTING_MIN_PILOT_SCORE"),
    )
    chili_shadow_vetting_min_pilot_score_threshold_ratio: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_MIN_PILOT_SCORE_THRESHOLD_RATIO"
        ),
    )
    chili_shadow_vetting_min_pilot_effective_n: float = Field(
        default=10.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_SHADOW_VETTING_MIN_PILOT_EFFECTIVE_N"),
    )
    chili_shadow_vetting_min_pilot_weighted_wr: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_SHADOW_VETTING_MIN_PILOT_WEIGHTED_WR"),
    )
    chili_shadow_vetting_min_pilot_recent_wr: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_SHADOW_VETTING_MIN_PILOT_RECENT_WR"),
    )
    chili_shadow_vetting_min_pilot_freshness: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_SHADOW_VETTING_MIN_PILOT_FRESHNESS"),
    )
    chili_shadow_vetting_max_pilot_directional_decay: float = Field(
        default=0.40,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_MAX_PILOT_DIRECTIONAL_DECAY"
        ),
    )
    chili_shadow_vetting_refresh_blocked_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_REFRESH_BLOCKED_GATE_ENABLED"
        ),
    )
    chili_shadow_vetting_refresh_blocked_gate_limit: int = Field(
        default=4,
        ge=0,
        validation_alias=AliasChoices("CHILI_SHADOW_VETTING_REFRESH_BLOCKED_GATE_LIMIT"),
    )
    chili_shadow_vetting_refresh_blocked_gate_min_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_REFRESH_BLOCKED_GATE_MIN_SCORE"
        ),
    )
    chili_shadow_vetting_refresh_blocked_gate_threshold_ratio: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_REFRESH_BLOCKED_GATE_THRESHOLD_RATIO"
        ),
    )
    chili_shadow_vetting_hold_failed_realized_gate_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_HOLD_FAILED_REALIZED_GATE_ENABLED"
        ),
    )
    chili_shadow_vetting_failed_gate_max_median_sharpe: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_FAILED_GATE_MAX_MEDIAN_SHARPE"
        ),
    )
    # Realized-edge pilot lane: graduate a shadow pattern to the reversible
    # pilot lane when the lower-confidence bound of its realized per-trade
    # expectancy is provably positive. Complements the CPCV/quality-weighted
    # pilot score, which under-credits high-consistency low-variance grinders.
    # Default ON (the bar is a provably positive realized edge; losers can
    # never clear it).
    chili_shadow_vetting_realized_edge_pilot_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_REALIZED_EDGE_PILOT_ENABLED"
        ),
    )
    chili_shadow_vetting_realized_edge_ci_level: float = Field(
        default=0.90,
        validation_alias=AliasChoices(
            "CHILI_SHADOW_VETTING_REALIZED_EDGE_CI_LEVEL"
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
    # Deterministic native revalidation replaces the per-candidate LLM viability
    # call with the same hard-invalidation checks computed directly (instant, no
    # model call, cannot fail-closed on LLM unavailability). Default ON; set False
    # to fall back to the LLM path. The _should_run_llm_revalidation gate (incl.
    # the enabled flag above and shadow/options skips) still decides whether to
    # revalidate at all.
    chili_autotrader_deterministic_revalidation_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "CHILI_AUTOTRADER_DETERMINISTIC_REVALIDATION_ENABLED"
        ),
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
    # Fix 5B (2026-06-05): the pattern monitor's ``exit_now`` advisory is
    # beneficial only ~21% of the time (measured via was_beneficial); a fresh but
    # UNCORROBORATED exit_now is rerouted to a stop-TIGHTEN instead of a hard cut.
    # An exit_now is honored as a hard market exit only once price has traversed
    # at least this fraction of the entry->stop distance (price corroborates the
    # exit); below it, the protective stop is tightened to the corroboration level
    # so a genuine adverse move still triggers a normal stop while a recovery keeps
    # its upside. Only ever tightens (never loosens) the stop; the hard stop +
    # drawdown breaker are untouched. Policy knob, live + on.
    chili_monitor_exit_corroboration_floor: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_MONITOR_EXIT_CORROBORATION_FLOOR"),
    )
    # Comma-separated ``decision_source`` values whose ``exit_now`` is dropped
    # entirely (not even rerouted). Seeded with ``heuristic`` -- measured 0/296
    # beneficial exit_now decisions over 90d. Data-derived denylist, tunable.
    chili_monitor_exit_denylisted_sources: str = Field(
        default="heuristic",
        validation_alias=AliasChoices("CHILI_MONITOR_EXIT_DENYLISTED_SOURCES"),
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
    # all-closed PnL distribution. Lives next to the pattern
    # tier; the two are independent (D5 in the brief) and each gates only
    # what its trip signal can act on. The pattern tier gates
    # CHILI-attributed entries from the autotrader; the portfolio tier
    # gates every BUY entry from the venue adapters regardless of source.
    # ARMED 2026-06-07 (Hard Rule 2): the 2026-06-07 momentum-lane audit
    # found this Hard-Rule-2 guard was wired everywhere but globally
    # disabled — no entry path enforced portfolio drawdown. History is
    # ready (≥30 all-closed close-days) so the gate is live, not dormant.
    chili_portfolio_dd_breaker_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_PORTFOLIO_DD_BREAKER_ENABLED"),
    )
    # FIX-DD — DRAWDOWN-CALC DENOMINATOR. The 5/30-day + MTM drawdown-% trips divide the
    # rolling realized PnL by a `capital` basis passed by the caller. A contaminated caller
    # basis (~$19.5 instead of the real ~$13k account equity) inflated a -$76 loss to
    # "-389.7%" and re-tripped the breaker TWICE (trading_risk_state ids 151-154 class).
    # When True (default) the drawdown-% denominator is resolved from the SAME real account-
    # equity source the sizing uses (equity_relative_* machinery, prefer_equity/cash-value),
    # falling back to the passed `capital` only when it clears the sane floor below; if
    # NEITHER is available the % trips are SKIPPED (fail-closed on the %, never divide by a
    # garbage base) while the absolute-dollar breakers stay intact. OFF => byte-identical
    # legacy (trust the passed `capital`). (feedback_report_binding_not_defaults, Hard Rule 2)
    chili_drawdown_breaker_real_equity_denominator_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_DRAWDOWN_BREAKER_REAL_EQUITY_DENOMINATOR_ENABLED"),
        description="FIX-DD: True (default) => the drawdown-% denominator is the REAL account-equity basis (equity_relative_* source), fail-closed skip of the % trip when equity is unavailable and the passed capital is below the sane floor. OFF => legacy (trust the passed capital).",
    )
    # Sane-equity floor (USD) below which a passed `capital` basis is treated as contaminated
    # and NOT used as a drawdown-% denominator. ONE documented base; the real account is
    # ~$13k, so a basis under this floor is garbage (the $19.5 false-trip). Reference point =
    # a FLOOR, not a ceiling (feedback_adaptive_no_magic).
    chili_drawdown_breaker_min_equity_basis_usd: float = Field(
        default=1_000.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_DRAWDOWN_BREAKER_MIN_EQUITY_BASIS_USD"),
    )
    # Live-blocking gate. When enabled=True AND live=True, a tripped
    # breaker BLOCKS the entry at the venue-adapter boundary and in the
    # momentum arm path. When enabled=True AND live=False it runs in
    # shadow mode (computes "would have tripped", persists a shadow row to
    # trading_risk_state regime='portfolio_breaker_shadow', logs a
    # structured INFO line) but DOES NOT block. ARMED LIVE 2026-06-07 per
    # operator decision (Hard Rule 2): the portfolio tier samples ALL
    # closed trades, so it hard-blocks only when the whole account is in
    # real drawdown. Live mode is fail-CLOSED — DB/threshold errors block
    # the entry with an auditable portfolio_dd_breaker_unavailable reason.
    chili_portfolio_dd_breaker_live: bool = Field(
        default=True,
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

    # Per learning-cycle resource caps (miners). Zero cap = unlimited for that
    # dimension; None => ADAPTIVE (BrainResourceBudget.from_settings derives it from
    # the mining universe brain_mine_patterns_max_tickers). The OHLCV/row caps used
    # to be fixed at slow-serial-fetch sizes (280 fetches x ~8s = ~38 min/cycle),
    # which throttled mining to <30% of the universe. With provider-aware
    # concurrency the full universe fetches fast AND rate-safe, so they now scale
    # to cover it. An explicit int still pins them (operator override).
    brain_budget_ohlcv_per_cycle: int | None = None
    brain_budget_miner_rows_per_cycle: int | None = None
    brain_budget_pattern_injects_per_cycle: int = 32
    brain_budget_miner_error_trip: int = 5

    # Work-ledger stall watchdog: flag a work type whose processor has gone silent
    # (overdue pending + zero processing in the window) — catches a dead/absent
    # dedicated worker in minutes, not hours. Conservative defaults to avoid
    # false-positives on slow-cadence work types.
    chili_work_ledger_stall_threshold_minutes: int = 120
    chili_work_ledger_stall_min_pending: int = 5

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
    brain_hypothesis_bootstrap_iterations: int = Field(
        default=500,
        ge=1,
        le=100_000,
        validation_alias=AliasChoices("BRAIN_HYPOTHESIS_BOOTSTRAP_ITERATIONS"),
    )

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
    # Per-sweep ceiling for the exit-parity prune drain loop. The sweep now
    # loops the batch delete (committing each batch) until the eligible set is
    # drained, so steady-state ingestion is fully cleared daily; this cap only
    # bounds one-time backlog catch-up so a single sweep cannot spike WAL/dead
    # tuples for hours. Steady-state volume is far below it.
    brain_retention_exit_parity_max_rows_per_sweep: int = 5_000_000
    brain_retention_bracket_reconciliation_days: int = 30
    brain_retention_execution_event_days: int = 180
    # Replay v3 R1: TTL for the append-only momentum_viability_history (mig311). The
    # eligibility series is only needed for recent-day replay/incident reconstruction;
    # 30d keeps the table small (one row per viable name per tick). Pruned by the same
    # batched _prune_operational_time_log drain the other operational logs use.
    brain_retention_viability_history_days: int = 30
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
    # Deterministic revalidation stale-price veto: an entry must trade on a
    # price no older than this ceiling (seconds). Adaptive per setup — capped at
    # the pattern's own bar window — so it tightens for short timeframes. Guards
    # against acting on a price the market has moved past when a tick runs slow
    # (e.g. a Coinbase rate-limit backoff widens the price→decision gap).
    chili_autotrader_revalidation_max_price_age_seconds: int = 60
    pattern_imminent_alert_enabled: bool = True
    # Timeframe-tiered scan: a FAST imminent pass for short-timeframe patterns
    # (1m/5m) runs every ~60s, alongside the standard 15-min sweep. A 15-min
    # scan structurally cannot catch a 1m/5m setup — many bars elapse between
    # looks — so intraday/scalping patterns need detection at their own cadence.
    # ON by default. Crypto runs 24/7; stock is gated to US hours by the
    # existing session check inside the scan. Interval matches the fastest tier
    # timeframe (1m -> 60s); widen the timeframe set only after measuring the
    # alert / LLM-revalidation load it generates.
    pattern_imminent_fast_enabled: bool = True
    pattern_imminent_fast_interval_seconds: int = 60
    pattern_imminent_fast_timeframes: str = "1m,5m"
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
