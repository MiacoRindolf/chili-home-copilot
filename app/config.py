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
    brain_work_delegate_queue_from_cycle: bool = True
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
    brain_intraday_intervals: str = "15m"
    brain_intraday_max_tickers: int = 1000
    brain_snapshot_backfill_years: int = 10

    # Crypto universe for prescreen / ticker_universe: 0 = fetch all pages from provider (CoinGecko, capped by safety limit); N>0 = top N by market cap.
    brain_crypto_universe_max: int = 200
    # Drop cryptos below this 24h USD volume when building universe (0 = off). Reduces illiquid tail when universe is large.
    brain_crypto_universe_min_volume_usd: float = 0.0
    # When False, prescreen uses a smaller crypto list (150) for faster cycles; True = merge full configured crypto universe into prescreen.
    brain_scan_include_full_crypto_universe: bool = True

    # Pattern mining: max tickers to pull OHLCV for per cycle (0 = no cap; use full merged mining list).
    brain_mine_patterns_max_tickers: int = 1000
    # Require stability across chronological segments before save_insight from mine_patterns.
    brain_mining_purged_cpcv_enabled: bool = True
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
