"""Centralized configuration for CHILI. Loads from .env with type safety."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    openai_api_key: str = ""
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

    # Polygon.io market data (secondary fallback â€” replaces yfinance for speed)
    polygon_api_key: str = ""
    polygon_base_url: str = "https://api.polygon.io"
    use_polygon: bool = False  # feature flag: set USE_POLYGON=true in .env to enable
    polygon_max_rps: int = 5  # soft cap; governor will smooth bursts around this

    # After Massive, allow Polygon + yfinance for OHLCV/quotes (scanner batch, prescreener, etc.).
    # Set MARKET_DATA_ALLOW_PROVIDER_FALLBACK=false to use Massive only and avoid Yahoo noise in logs.
    market_data_allow_provider_fallback: bool = True

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
        default=False,
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
    brain_smart_bt_max_workers_in_process: int = 8  # cap ticker-thread pool inside each process worker
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
    # Emit ``market_snapshots_batch`` outcome when scheduler snapshot job finishes.
    brain_work_snapshots_outcome_enabled: bool = True
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
    brain_net_edge_ranker_mode: str = "off"
    brain_net_edge_ops_log_enabled: bool = True
    brain_net_edge_min_samples: int = 50
    brain_net_edge_cache_ttl_s: int = 300
    brain_net_edge_shadow_sample_pct: float = 1.0

    # ExitEngine unification (Phase B) — canonical ExitEvaluator shadow rollout.
    # Rollout ladder mirrors the prediction-mirror + NetEdgeRanker contract:
    # off -> shadow -> compare -> authoritative. In any mode != "authoritative"
    # the canonical evaluator MUST NOT decide exits; it only logs parity against
    # the legacy backtest/live paths. See docs/TRADING_BRAIN_EXIT_ENGINE_ROLLOUT.md.
    brain_exit_engine_mode: str = "off"
    brain_exit_engine_ops_log_enabled: bool = True
    brain_exit_engine_parity_sample_pct: float = 1.0

    # Economic-truth ledger (Phase A) — canonical append-only ledger of
    # entry/exit fills + fees + cash-delta + realized-PnL-delta. Shadow-only
    # until a later cutover phase. Rollout ladder mirrors Phase B/E:
    # off -> shadow -> compare -> authoritative. Legacy Trade.pnl and
    # PaperTrade.pnl remain authoritative until the cutover phase.
    # See docs/TRADING_BRAIN_ECONOMIC_LEDGER_ROLLOUT.md.
    brain_economic_ledger_mode: str = "off"
    brain_economic_ledger_ops_log_enabled: bool = True
    brain_economic_ledger_parity_tolerance_usd: float = 0.01

    # PIT hygiene audit (Phase C) — classifies `ScanPattern.rules_json`
    # condition indicators against an explicit allow/deny list and writes
    # results to `trading_pit_audit_log`. Shadow-only until cutover. See
    # docs/TRADING_BRAIN_PIT_HYGIENE_ROLLOUT.md.
    brain_pit_audit_mode: str = "off"
    brain_pit_audit_ops_log_enabled: bool = True

    # Triple-barrier labels (Phase D) — replaces fixed-horizon binary labels
    # with (TP, SL, timeout) outcomes for training and economic promotion.
    # Shadow-only until cutover. See docs/TRADING_BRAIN_TRIPLE_BARRIER_ROLLOUT.md.
    brain_triple_barrier_mode: str = "off"
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
    brain_execution_cost_mode: str = "off"
    brain_execution_cost_default_fee_bps: float = 1.0
    brain_execution_cost_impact_cap_bps: float = 50.0
    brain_execution_capacity_max_adv_frac: float = 0.05

    # Venue-truth telemetry (Phase F) — compares expected vs realized
    # costs per fill. Shadow writes to `trading_venue_truth_log` only.
    brain_venue_truth_mode: str = "off"
    brain_venue_truth_ops_log_enabled: bool = True

    # Live brackets + reconciliation (Phase G) — persists bracket intent
    # per live Trade and runs a read-only sweep comparing local bracket
    # state to broker-reported open orders. In shadow mode no broker
    # writes happen; only `trading_bracket_intents` + `trading_bracket_
    # reconciliation_log` are populated. Flipping to authoritative is
    # Phase G.2 and requires extending the venue adapter protocol first.
    # See docs/TRADING_BRAIN_LIVE_BRACKETS_ROLLOUT.md.
    brain_live_brackets_mode: str = "off"
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
    brain_position_sizer_mode: str = "off"
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
    brain_risk_dial_mode: str = "off"
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

    brain_capital_reweight_mode: str = "off"
    brain_capital_reweight_ops_log_enabled: bool = True
    brain_capital_reweight_cron_day_of_week: str = "sun"
    brain_capital_reweight_cron_hour: int = 18
    brain_capital_reweight_lookback_days: int = 14
    brain_capital_reweight_max_single_bucket_pct: float = 35.0

    # Phase J - Drift monitor + re-cert queue (shadow rollout).
    brain_drift_monitor_mode: str = "off"
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

    brain_recert_queue_mode: str = "off"
    brain_recert_queue_ops_log_enabled: bool = True
    brain_recert_queue_include_yellow: bool = False

    # Phase K - Divergence panel + ops health endpoint (shadow rollout).
    brain_divergence_scorer_mode: str = "off"
    brain_divergence_scorer_ops_log_enabled: bool = True
    brain_divergence_scorer_min_layers_sampled: int = 1
    brain_divergence_scorer_yellow_threshold: float = 0.9
    brain_divergence_scorer_red_threshold: float = 1.8
    brain_divergence_scorer_lookback_days: int = 7
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
    brain_macro_regime_mode: str = "off"
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
    brain_breadth_relstr_mode: str = "off"
    brain_breadth_relstr_ops_log_enabled: bool = True
    brain_breadth_relstr_cron_hour: int = 6
    brain_breadth_relstr_cron_minute: int = 45
    brain_breadth_relstr_min_coverage_score: float = 0.5
    brain_breadth_relstr_trend_up_threshold: float = 0.01
    brain_breadth_relstr_strong_trend_threshold: float = 0.03
    brain_breadth_relstr_tilt_threshold: float = 0.02
    brain_breadth_relstr_risk_on_ratio: float = 0.65
    brain_breadth_relstr_risk_off_ratio: float = 0.35
    # Diagnostics endpoint default lookback (clamped [1, 180] at the route).
    brain_breadth_relstr_lookback_days: int = 14

    # Phase L.19 - Cross-asset signals v1 (shadow).
    # One row per trading day is appended to
    # trading_cross_asset_snapshots by a daily scheduled sweep when mode
    # != "off". L.19.1 never flips to "authoritative"; the service layer
    # hard-refuses that mode until the L.19.2 plan is opened explicitly.
    brain_cross_asset_mode: str = "off"
    brain_cross_asset_ops_log_enabled: bool = True
    brain_cross_asset_cron_hour: int = 7
    brain_cross_asset_cron_minute: int = 0
    brain_cross_asset_min_coverage_score: float = 0.5
    brain_cross_asset_fast_lead_threshold: float = 0.01
    brain_cross_asset_slow_lead_threshold: float = 0.03
    brain_cross_asset_vix_percentile_shock: float = 0.80
    brain_cross_asset_beta_window_days: int = 60
    brain_cross_asset_composite_min_agreement: int = 2
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
    brain_ticker_regime_mode: str = "off"
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
    brain_vol_dispersion_mode: str = "off"
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
    brain_intraday_session_mode: str = "off"
    brain_intraday_session_ops_log_enabled: bool = True
    # 22:00 local scheduler slot (post US cash close and after L.17-L.21
    # jobs at 06:30-07:30).
    brain_intraday_session_cron_hour: int = 22
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
    brain_pattern_regime_perf_mode: str = "off"
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
    brain_pattern_regime_tilt_mode: str = "off"
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
    brain_pattern_regime_promotion_mode: str = "off"
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
    brain_pattern_regime_killswitch_mode: str = "off"
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
    brain_use_gpu_ml: bool = False       # GPU for pattern meta-learner (LightGBM) â€” ML train step only, not queue BT

    # Full learning cycle (run_learning_cycle): optional slim mode
    brain_secondary_miners_on_cycle: bool = True   # intraday/refine/exit/fakeout/sizing/inter-alert/timeframe/synergy steps
    # Learning cycle AI report: 0 = template-only (no LLM); N>0 = call LLM every Nth stored report for polish.
    learning_cycle_report_llm_every_n: int = 0

    # Pattern backtest queue: how soon a pattern is eligible again (was hardcoded 7).
    brain_retest_interval_days: int = 7
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
    # Require stability across chronological segments before save_insight from mine_patterns.
    brain_mining_purged_cpcv_enabled: bool = True
    # When True, CPCV + DSR + PBO gate blocks promotion after ensemble/DSR/holdout (HR1 path).
    # Default OFF: metrics computed at promotion-attempt time only; shadow / logging only.
    chili_cpcv_promotion_gate_enabled: bool = False
    # Q1.T2: 3-state Gaussian HMM regime tags on snapshots (default OFF = byte parity with pre-T2).
    chili_regime_classifier_enabled: bool = False
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
    # Pool: brain worker + parallel queue backtests can hold many connections; default 30 is too small.
    database_pool_size: int = 25
    database_max_overflow: int = 55

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

    # APScheduler process split: ``all`` (default, single process), ``web`` (no heavy market scans),
    # ``worker`` (heavy scans + heartbeat only), ``none`` (no scheduler — use with a separate worker).
    # Docker Compose: ``chili`` uses ``none``, ``scheduler-worker`` uses ``all`` + ``CHILI_SCHEDULER_EMIT_HEARTBEAT=1``.
    chili_scheduler_role: str = Field(
        default="all",
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
    # P1.2 venue-health (ack-to-fill P95) and dashboards. Default off so
    # shipping changes nothing until flipped. Re-read live.
    chili_order_state_machine_enabled: bool = Field(
        default=False,
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
    # Off by default during the P1.1 rollout — flip on per-environment once
    # shadow traffic has been observed for a week. Disabled mode is a hard
    # no-op: no rows are written, callers receive ``reason='disabled'``.
    chili_order_state_machine_enabled: bool = Field(
        default=False,
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
    chili_autotrader_user_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_USER_ID"),
    )
    chili_autotrader_per_trade_notional_usd: float = Field(
        default=300.0,
        ge=1.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_PER_TRADE_NOTIONAL_USD"),
    )
    chili_autotrader_synergy_scale_notional_usd: float = Field(
        default=150.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SYNERGY_SCALE_NOTIONAL_USD"),
    )
    chili_autotrader_synergy_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_SYNERGY_ENABLED"),
    )
    chili_autotrader_daily_loss_cap_usd: float = Field(
        default=150.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_DAILY_LOSS_CAP_USD"),
    )
    chili_autotrader_max_concurrent: int = Field(
        default=3,
        ge=1,
        le=50,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_CONCURRENT"),
    )
    chili_autotrader_confidence_floor: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_CONFIDENCE_FLOOR"),
    )
    chili_autotrader_min_projected_profit_pct: float = Field(
        default=12.0,
        ge=0.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MIN_PROJECTED_PROFIT_PCT"),
    )
    chili_autotrader_max_symbol_price_usd: float = Field(
        default=50.0,
        ge=0.01,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_SYMBOL_PRICE_USD"),
    )
    chili_autotrader_max_entry_slippage_pct: float = Field(
        default=1.0,
        ge=0.0,
        le=50.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MAX_ENTRY_SLIPPAGE_PCT"),
    )
    chili_autotrader_monitor_interval_seconds: int = Field(
        default=30,
        ge=5,
        le=600,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_MONITOR_INTERVAL_SECONDS"),
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
    chili_autotrader_rth_only: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_RTH_ONLY"),
    )
    chili_autotrader_allow_extended_hours: bool = Field(
        default=False,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_ALLOW_EXTENDED_HOURS"),
        description=(
            "When true and chili_autotrader_rth_only is also true, the monitor "
            "runs during Mon-Fri US/Eastern 04:00-20:00 (pre + RTH + post) "
            "instead of RTH only. Set rth_only=false to disable the session "
            "gate entirely (weekends and overnight included)."
        ),
    )
    chili_autotrader_llm_revalidation_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_LLM_REVALIDATION_ENABLED"),
    )
    chili_autotrader_assumed_capital_usd: float = Field(
        default=25_000.0,
        ge=100.0,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_ASSUMED_CAPITAL_USD"),
    )
    chili_autotrader_tick_interval_seconds: int = Field(
        default=10,
        ge=5,
        le=120,
        validation_alias=AliasChoices("CHILI_AUTOTRADER_TICK_INTERVAL_SECONDS"),
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
    # Future: spawn ScanPattern from miner stats; must still pass normal backtest/OOS (default off).
    brain_miner_scanpattern_bridge_enabled: bool = False

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

    # Portfolio: max simultaneous open longs per coarse sector (0 = disabled).
    brain_max_open_per_sector: int = 0
    brain_max_correlated_positions: int = 0
    brain_allocator_enabled: bool = True
    brain_allocator_shadow_mode: bool = False
    brain_allocator_live_soft_block_enabled: bool = False
    brain_allocator_live_hard_block_enabled: bool = True
    brain_allocator_incumbent_score_margin: float = 0.08

    # Decision ledger + net-expectancy allocator (momentum autopilot / brain). Live enforcement OFF by default.
    brain_enable_decision_ledger: bool = Field(default=True, validation_alias=AliasChoices("CHILI_BRAIN_ENABLE_DECISION_LEDGER"))
    brain_decision_packet_required_for_runners: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_DECISION_PACKET_REQUIRED_FOR_RUNNERS")
    )
    brain_expectancy_allocator_shadow_mode: bool = Field(
        default=False, validation_alias=AliasChoices("CHILI_BRAIN_EXPECTANCY_ALLOCATOR_SHADOW_MODE")
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
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_ENFORCE_NET_EXPECTANCY_LIVE")
    )
    brain_capacity_hard_block_paper: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_CAPACITY_HARD_BLOCK_PAPER")
    )
    brain_capacity_hard_block_live: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_CAPACITY_HARD_BLOCK_LIVE")
    )
    brain_paper_deployment_enforcement: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_PAPER_DEPLOYMENT_ENFORCEMENT")
    )
    brain_live_deployment_enforcement: bool = Field(
        default=True, validation_alias=AliasChoices("CHILI_BRAIN_LIVE_DEPLOYMENT_ENFORCEMENT")
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
