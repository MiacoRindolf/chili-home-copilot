"""Centralized configuration for CHILI. Loads from .env with type safety."""
from typing import Optional

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Ollama (local planner, wellness, RAG, vision)
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "phi4-mini"
    wellness_model: str = "phi4-mini"
    ollama_vision_model: str = "llama3.2-vision"

    # Primary LLM â€” defaults to Groq free tier (Llama 3.3 70B, ~800 tok/s).
    # Override with Ollama or other OpenAI-compatible provider.
    llm_api_key: str = ""
    openai_api_key: str = ""  # backward compat; used as primary if llm_api_key empty
    llm_model: str = "llama-3.3-70b-versatile"
    llm_base_url: str = "https://api.groq.com/openai/v1"

    # Fallback LLM â€” defaults to Google Gemini free tier (OpenAI-compatible endpoint).
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

    # Learning schedule (1h = faster research cycles if worker + OHLCV provider keep up)
    learning_interval_hours: int = 1
    # If a cycle crashes without clearing _learning_status["running"], the brain worker would skip
    # forever; clear the lock after this many seconds (default 3h).
    learning_cycle_stale_seconds: int = 10800

    # Trading-brain Phase 2: mirror cycle/stage rows + optional lease telemetry + status dual-read (legacy authoritative).
    brain_cycle_shadow_write_enabled: bool = False
    brain_status_dual_read_enabled: bool = False
    brain_lease_shadow_write_enabled: bool = False
    # Phase 3: single-flight via `brain_cycle_lease` using dedicated DB sessions (admission only; legacy status authoritative for UI).
    brain_cycle_lease_enforcement_enabled: bool = False
    # Phase 4: append-only mirror of legacy `get_current_predictions` (dedicated session; routers unchanged; not read-authoritative).
    brain_prediction_dual_write_enabled: bool = False
    # Phase 5: mirror read compare + optional candidate-authoritative (explicit API tickers only; no router/API shape change).
    brain_prediction_read_compare_enabled: bool = False
    brain_prediction_read_authoritative_enabled: bool = False
    brain_prediction_read_max_age_seconds: int = 900
    # Phase 6: one bounded INFO line per _get_current_predictions_impl (chili_prediction_ops); WARNING paths unchanged when off.
    brain_prediction_ops_log_enabled: bool = False

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
    brain_smart_bt_max_workers: int | None = 28  # max threads per insight ticker pool (None = max(8, cpu*2))

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
    brain_insight_backtest_on_cycle: bool = False  # legacy TradingInsight smart backtests (ScanPattern queue is canonical)
    brain_secondary_miners_on_cycle: bool = True   # intraday/refine/exit/fakeout/sizing/inter-alert/timeframe/synergy steps

    # Pattern backtest queue: how soon a pattern is eligible again (was hardcoded 7).
    brain_retest_interval_days: int = 7
    # When the retest queue is thin, add oldest-tested active patterns up to this many per cycle.
    brain_queue_exploration_enabled: bool = True
    brain_queue_exploration_max: int = 40

    # Research integrity: causality checks + provenance on pattern backtests (Freqtrade-style hygiene, CHILI-native).
    brain_research_integrity_enabled: bool = True
    brain_research_integrity_strict: bool = False  # True = block promotion to promoted when causality fails
    brain_research_integrity_max_check_bars: int = 48

    # Lightweight prediction refresh: promoted ScanPatterns only (no full learning cycle).
    brain_fast_eval_enabled: bool = True
    # When True, APScheduler also runs fast eval on an interval. Default False: full
    # ``run_learning_cycle`` (worker or Learn) already refreshes the promoted cache.
    brain_fast_eval_scheduler_enabled: bool = False
    brain_fast_eval_interval_minutes: int = 10
    brain_fast_eval_max_tickers: int = 400

    # Snapshots + mining: canonical bar key (ticker, interval, bar_start_utc). Intraday is crypto-focused.
    brain_intraday_snapshots_enabled: bool = False
    brain_intraday_intervals: str = "15m"
    brain_intraday_max_tickers: int = 40
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

    # Brain learning worker: UI starts the Docker Compose ``brain-worker`` service (not subprocess).
    brain_worker_compose_service: str = "brain-worker"
    # Optional: restrict to one compose project label (empty = match any project with that service name)
    brain_worker_compose_project: str = ""

    # Pattern backtests (backtesting.py): spread = constant bid/ask friction as fraction of price.
    # Combined proxy for half-spread + slippage (e.g. 0.0002 â‰ˆ 2 bps per side order of magnitude).
    backtest_spread: float = 0.0002
    backtest_commission: float = 0.001
    # Hold out the last fraction of bars for out-of-sample metrics when training/evaluating patterns.
    brain_oos_holdout_fraction: float = 0.25
    brain_oos_gate_enabled: bool = True
    brain_oos_min_win_rate_pct: float = 42.0
    brain_oos_max_is_oos_gap_pct: float = 38.0
    brain_oos_min_evaluated_tickers: int = 2
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
    brain_bench_walk_forward_gate_enabled: bool = False
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
    brain_oos_bootstrap_iterations: int = 0
    # Reject promotion when bootstrap CI lower bound for mean OOS WR is below this (None = skip).
    brain_oos_bootstrap_ci_min_wr: Optional[float] = None

    # Two-tier queue: cheap prescreen then full backtest (final OOS gate unchanged).
    brain_queue_prescreen_enabled: bool = True
    brain_queue_prescreen_tickers: int = 6
    brain_queue_prescreen_period: str = "3mo"
    brain_queue_prescreen_min_win_rate_pct: float = 45.0

    # Live vs research: downgrade patterns when realized win rate lags research OOS materially.
    brain_live_depromotion_enabled: bool = False
    brain_live_depromotion_min_closed_trades: int = 8
    brain_live_depromotion_max_gap_pct: float = 25.0

    # When a pattern is promoted, initialize paper_book_json for optional shadow tracking.
    brain_paper_book_on_promotion: bool = False

    # OHLCV quality: log warnings from assess_ohlcv_bar_quality; strict can skip miner rows (future hook).
    brain_bar_quality_strict: bool = False
    brain_bar_quality_max_gap_bars: int = 5

    # Portfolio: max simultaneous open longs per coarse sector (0 = disabled).
    brain_max_open_per_sector: int = 0

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
