"""Canonical Trading Brain learning-cycle architecture (single source of truth).

Used by ``get_trading_brain_network_graph`` for the Network tab and by
``run_learning_cycle`` for ``current_step`` / ``phase`` strings.

**Network tab metadata:** Each cluster and step carries ``description`` (short summary),
``remarks`` (what / where / why — the narrative shown in the node detail panel),
and concrete ``inputs`` / ``outputs`` lists. Those fields live **here** so the graph
stays one import away from the cycle; ``learning.py`` marks each step with a
``# graph-node: cluster_id/step_sid`` comment next to ``apply_learning_cycle_step_status``
for traceability (validated in tests).

When you add, remove, or reorder phases, edit **this module** and the matching
``apply_*`` calls in ``learning.py``, then bump ``graph_version`` in
``brain_network_graph`` when the public graph shape changes.

Do **not** import ``learning`` from here (avoids circular imports).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CycleStepDef:
    sid: str
    label: str
    code_ref: str
    runner_phase: str
    description: str
    remarks: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class CycleClusterDef:
    id: str
    label: str
    phase_summary: str
    description: str
    remarks: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    steps: tuple[CycleStepDef, ...]


@dataclass(frozen=True)
class TradingBrainRootMetadata:
    """Documentation for the graph root (orchestrator)."""

    description: str
    remarks: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


TRADING_BRAIN_ROOT_METADATA = TradingBrainRootMetadata(
    description=(
        "Top-level orchestrator for one full learning cycle: refreshes market memory, "
        "mines and validates patterns, evolves hypotheses, runs optional secondary miners, "
        "journals results, trains meta-models, generates proposals and reports, then "
        "finalizes state. Universe prescreen and full market scan run as separate cron jobs."
    ),
    remarks=(
        "What: ``run_learning_cycle`` is the single entry point that runs the trading-brain "
        "learning pipeline in order (after batch jobs have populated prescreen + scan tables), "
        "mutating PostgreSQL and producing the in-memory ``report`` dict.\n\n"
        "Where: ``app.services.trading.learning.run_learning_cycle`` (orchestrator), "
        "invoked by the brain worker and status APIs.\n\n"
        "Why: A coherent cycle boundary keeps snapshots, mining, backtests, and meta-learning "
        "consistent; prescreen (2:00) and market scan (2:30 America/Los_Angeles) are scheduled "
        "separately in ``trading_scheduler``."
    ),
    inputs=(
        "``db: Session`` — SQLAlchemy DB session for the worker",
        "``user_id: int | None`` — scope for watchlists and user-specific rows",
        "``full_universe: bool`` — retained for shadow/API compatibility",
        "``settings`` — CHILI config (providers, ``brain_secondary_miners_on_cycle``, flags)",
        "Prior rows in ``trading_prescreen_candidates`` and ``trading_scans`` from batch jobs",
        "External OHLCV/quote providers (Massive, Polygon, or yfinance) via market modules",
    ),
    outputs=(
        "``report: dict[str, Any]`` — prescreen/scan counts (DB read), snapshots, patterns, "
        "backtests, ML metrics, ``elapsed_s``, ``step_timings``, funnel snapshot",
        "Rows written/updated: ``market_snapshots``, ``scan_patterns``, insights, "
        "backtests, ``learning_events``, journal, proposals (per sub-phase)",
        "``_learning_status`` global — ``current_step``, ``phase``, ``steps_completed``, "
        "``last_cycle_funnel`` for the Brain desk",
    ),
)

TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS: tuple[CycleClusterDef, ...] = (
    CycleClusterDef(
        id="c_universe",
        label="Scheduled universe & scan",
        phase_summary="batch jobs (not in run_learning_cycle)",
        description=(
            "Daily prescreen and full market scan populate PostgreSQL before the learning "
            "cycle runs; this cluster documents those jobs for the Network graph."
        ),
        remarks=(
            "What: **Not** executed inside ``run_learning_cycle``. APScheduler runs "
            "``run_daily_prescreen_job`` at **2:00** and ``run_full_market_scan`` at **2:30** "
            "(America/Los_Angeles) so ``trading_prescreen_candidates`` and ``trading_scans`` "
            "are fresh for snapshot selection.\n\n"
            "Where: ``app.services.trading_scheduler`` (``_run_daily_prescreen_job``, "
            "``_run_daily_market_scan_job``), ``prescreen_job``, ``scanner.run_full_market_scan``.\n\n"
            "Why: Decouples heavy universe I/O from the interactive learning cycle and keeps "
            "one authoritative prescreen + scan pass per day."
        ),
        inputs=("Cron triggers", "Provider APIs for prescreen and OHLCV for scoring"),
        outputs=(
            "``trading_prescreen_snapshots`` / ``trading_prescreen_candidates`` rows",
            "``trading_scans`` (``ScanResult``) ranked scores per ticker",
            "``brain_batch_jobs`` audit rows per run",
        ),
        steps=(
            CycleStepDef(
                sid="batch_prescreen_scan",
                label="Daily prescreen + market scan (cron)",
                code_ref="trading_scheduler._run_daily_prescreen_job + _run_daily_market_scan_job",
                runner_phase="scheduled",
                description=(
                    "2:00 prescreen job, then 2:30 full scan over active prescreen tickers "
                    "(``brain_default_user_id`` for scan rows)."
                ),
                remarks=(
                    "See cluster remarks. Each run is logged to ``brain_batch_jobs`` with "
                    "start/end timestamps."
                ),
                inputs=("``SessionLocal``", "``settings.brain_prescreen_scheduler_enabled``", "scan scheduler flag"),
                outputs=("Prescreen + scan DB rows", "batch job audit ids"),
            ),
        ),
    ),
    CycleClusterDef(
        id="c_state",
        label="Market state & memory",
        phase_summary="snapshots → backfill → confidence decay",
        description=(
            "Captures point-in-time market state into the database, fills derived fields, "
            "and prunes stale analytical artifacts."
        ),
        remarks=(
            "What: Persists normalized market snapshots, fills forward-return and score "
            "columns, then decays stale trading insights.\n\n"
            "Where: ``learning.take_snapshots_parallel``, ``backfill_future_returns``, "
            "``backfill_predicted_scores``, ``decay_stale_insights`` in "
            "``app.services.trading.learning``.\n\n"
            "Why: Mining and backtests assume fresh, labeled snapshot rows; decay prevents "
            "old insights from skewing promotion and alerts."
        ),
        inputs=(
            "``load_top_scan_tickers_for_snapshots`` (``trading_scans`` + prescreen fallback) "
            "+ watchlist",
            "Historical bars and quotes from market_data stack",
        ),
        outputs=(
            "``market_snapshots`` rows (upserts)",
            "Updated forward-return and predicted-score fields on snapshots",
            "Insight decay/prune counters in ``report``",
        ),
        steps=(
            CycleStepDef(
                sid="snapshots",
                label="Taking market snapshots",
                code_ref="learning.take_snapshots_parallel",
                runner_phase="snapshots",
                description=(
                    "Fetches OHLCV and quotes in parallel and upserts normalized snapshot rows "
                    "for top names (and optional intraday intervals for crypto)."
                ),
                remarks=(
                    "What: Parallel fetch + ``upsert_market_snapshot`` for many tickers; "
                    "optional intraday intervals when ``brain_intraday_snapshots_enabled``.\n\n"
                    "Where: ``take_snapshots_parallel`` in ``learning.py``; progress updates "
                    "use ``apply_learning_cycle_step_status_progress`` for UI.\n\n"
                    "Why: Snapshots are the shared feature store for prediction, mining, and "
                    "RAG-style brain context for the cycle."
                ),
                inputs=(
                    "``db: Session``",
                    "``top_tickers`` from scan head + watchlist merge (~800 cap in cycle)",
                    "``bar_interval`` (default ``1d``); optional crypto intraday list from settings",
                ),
                outputs=(
                    "``snap_count`` — rows written this step",
                    "``report['snapshots_taken']``",
                    "``market_snapshots`` table updates per (ticker, interval)",
                ),
            ),
            CycleStepDef(
                sid="backfill",
                label="Backfilling future returns",
                code_ref="learning.backfill_future_returns (+ backfill_predicted_scores)",
                runner_phase="backfilling",
                description=(
                    "Computes forward returns and predicted-score fields where missing so "
                    "patterns and backtests have consistent labels."
                ),
                remarks=(
                    "What: Batch DB updates to attach realized forward returns and refill "
                    "predicted score columns on recent snapshots.\n\n"
                    "Where: ``backfill_future_returns`` and ``backfill_predicted_scores`` in "
                    "``learning.py``.\n\n"
                    "Why: Supervised mining and walk-forward checks need aligned labels; "
                    "missing backfill produces null targets and weak pattern stats."
                ),
                inputs=(
                    "Existing ``market_snapshots`` and price history",
                    "Prediction pipeline for score columns (limit 1000 scores in cycle)",
                ),
                outputs=(
                    "``report['returns_backfilled']``, ``report['scores_backfilled']``",
                    "Updated snapshot rows in-place",
                ),
            ),
            CycleStepDef(
                sid="decay",
                label="Decaying stale insights",
                code_ref="learning.decay_stale_insights",
                runner_phase="confidence_decay",
                description=(
                    "Reduces confidence or removes insights that are outdated relative to "
                    "fresh data so the brain does not overweight stale signals."
                ),
                remarks=(
                    "What: Policy-driven decay and pruning on ``TradingInsight`` (and related) "
                    "rows that are older than freshness thresholds.\n\n"
                    "Where: ``decay_stale_insights(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Prevents zombie insights from competing with newly mined patterns "
                    "and keeps promotion queues honest."
                ),
                inputs=(
                    "``db``, ``user_id``",
                    "Insight rows linked to stale snapshots or scores",
                ),
                outputs=(
                    "``report['insights_decayed']``, ``report['insights_pruned']``",
                    "Lower confidence or inactive flags on affected insights",
                ),
            ),
        ),
    ),
    CycleClusterDef(
        id="c_discovery",
        label="Pattern discovery",
        phase_summary="mining → active seeking",
        description=(
            "Discovers candidate patterns from data and selectively boosts under-sampled "
            "ideas to improve coverage."
        ),
        remarks=(
            "What: Primary pattern generation plus an active-learning style seek pass for "
            "thin evidence.\n\n"
            "Where: ``mine_patterns`` and ``seek_pattern_data`` in ``learning.py``.\n\n"
            "Why: Raw mining alone can starve interesting hypotheses; seek pass balances "
            "the pattern library before expensive backtests."
        ),
        inputs=("Fresh snapshots and OHLCV", "``ScanPattern`` / catalog state"),
        outputs=(
            "New ``ScanPattern`` rows or updates (``report['patterns_discovered']``)",
            "``report['patterns_boosted']`` from seek",
        ),
        steps=(
            CycleStepDef(
                sid="mine",
                label="Mining patterns",
                code_ref="learning.mine_patterns",
                runner_phase="mining",
                description=(
                    "Runs pattern mining over recent market structure to propose new "
                    "rules and hypotheses stored as patterns."
                ),
                remarks=(
                    "What: Scans indicator/snapshot space for candidate ``ScanPattern`` "
                    "structures and persists them.\n\n"
                    "Where: ``mine_patterns(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Core discovery loop for the trading brain — without it the queue "
                    "and evolution stages have nothing to validate."
                ),
                inputs=("``db``, ``user_id``", "Snapshot + bar windows"),
                outputs=(
                    "``discoveries`` list",
                    "``report['patterns_discovered']``",
                    "``_learning_status['patterns_found']``",
                ),
            ),
            CycleStepDef(
                sid="seek",
                label="Active pattern seeking",
                code_ref="learning.seek_pattern_data",
                runner_phase="active_seeking",
                description=(
                    "Targets patterns that need more evidence by pulling or weighting "
                    "additional data so they can graduate or fail faster."
                ),
                remarks=(
                    "What: Identifies under-sampled patterns and requests or prioritizes "
                    "additional data paths.\n\n"
                    "Where: ``seek_pattern_data(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Reduces false negatives from data starvation before queue "
                    "backtests burn cycles on immature ideas."
                ),
                inputs=("Active patterns below evidence thresholds", "Ticker coverage maps"),
                outputs=(
                    "``seek_result`` dict (e.g. ``sought``)",
                    "``report['patterns_boosted']``",
                ),
            ),
        ),
    ),
    CycleClusterDef(
        id="c_validation",
        label="Evidence & backtests",
        phase_summary="insight BT (optional) → ScanPattern queue",
        description=(
            "Validates ideas with backtests: legacy TradingInsight paths when enabled, "
            "plus the canonical ScanPattern priority queue."
        ),
        remarks=(
            "What: Optional legacy insight backtests, then the main queue drain for "
            "``ScanPattern`` backtests.\n\n"
            "Where: ``_auto_backtest_patterns``, ``_auto_backtest_from_queue`` in "
            "``learning.py`` (gated by ``brain_insight_backtest_on_cycle``).\n\n"
            "Why: Empirical evidence gates promotion; queue path is the system of record "
            "for pattern validation."
        ),
        inputs=(
            "Open ``TradingInsight`` rows (if flag on)",
            "``ScanPattern`` queue entries + OHLCV windows",
            "Spread/commission settings from config",
        ),
        outputs=(
            "``BacktestResult`` / related rows",
            "``report['backtests_run']``, queue pending/empty flags",
        ),
        steps=(
            CycleStepDef(
                sid="bt_insights",
                label="Backtesting insights",
                code_ref="learning._auto_backtest_patterns (brain_insight_backtest_on_cycle)",
                runner_phase="backtesting",
                description=(
                    "Optional legacy backtest pass over TradingInsight-linked ideas when "
                    "brain_insight_backtest_on_cycle is enabled."
                ),
                remarks=(
                    "What: When enabled, runs automated backtests for legacy insight-linked "
                    "strategies.\n\n"
                    "Where: ``_auto_backtest_patterns`` guarded by "
                    "``settings.brain_insight_backtest_on_cycle`` in ``learning.py``.\n\n"
                    "Why: Backward compatibility for insight-centric workflows while the "
                    "queue path becomes canonical."
                ),
                inputs=(
                    "``db``, ``user_id``",
                    "Eligible insights and backtest engine parameters",
                ),
                outputs=(
                    "``bt_count`` insight backtests run (or 0 if skipped)",
                    "``report['insight_backtests_skipped']`` boolean when disabled",
                ),
            ),
            CycleStepDef(
                sid="bt_queue",
                label="Backtesting patterns from queue",
                code_ref="learning._auto_backtest_from_queue",
                runner_phase="queue_backtesting",
                description=(
                    "Drains the ScanPattern backtest queue: runs backtests, records results, "
                    "and may enqueue exploration variants."
                ),
                remarks=(
                    "What: Dequeues patterns, runs backtests, persists results, may add "
                    "exploration children to the queue.\n\n"
                    "Where: ``_auto_backtest_from_queue(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Central throughput limiter for pattern evidence — feeds evolution "
                    "and promotion decisions."
                ),
                inputs=("Priority queue state in DB", "Per-pattern OHLCV slices"),
                outputs=(
                    "``queue_result`` — ``backtests_run``, ``patterns_processed``, ``pending``, etc.",
                    "``report['queue_backtests_run']``, ``report['queue_pending']``, …",
                ),
            ),
        ),
    ),
    CycleClusterDef(
        id="c_evolution",
        label="Evolution & hypotheses",
        phase_summary="variants → validate_and_evolve → breakouts",
        description=(
            "Evolves pattern families, tests statistical hypotheses, and learns from "
            "resolved breakout-style outcomes."
        ),
        remarks=(
            "What: Structural evolution of pattern variants, statistical hypothesis testing "
            "with weight updates, then breakout outcome learning.\n\n"
            "Where: ``evolve_pattern_strategies``, ``validate_and_evolve``, "
            "``learn_from_breakout_outcomes`` in ``learning.py``.\n\n"
            "Why: Static patterns decay in live markets; this block adapts families and "
            "strategy weights from fresh evidence."
        ),
        inputs=(
            "Parent ``ScanPattern`` rows and variant trees",
            "Hypothesis / weight state",
            "Resolved alerts for breakouts",
        ),
        outputs=(
            "``report['evolution']``, hypothesis and weight counters",
            "``report['breakout_patterns_learned']``",
        ),
        steps=(
            CycleStepDef(
                sid="variants",
                label="Evolving pattern variants",
                code_ref="learning.evolve_pattern_strategies",
                runner_phase="pattern_variant_evolution",
                description=(
                    "Forks and compares ScanPattern variants (entries, exits, combos) and "
                    "promotes or demotes based on comparative evidence."
                ),
                remarks=(
                    "What: Creates/compares variant children (exit, entry, combo, timeframe, …) "
                    "and updates promotion fields.\n\n"
                    "Where: ``evolve_pattern_strategies(db)`` in ``learning.py``.\n\n"
                    "Why: Captures local improvements without full re-mining from scratch."
                ),
                inputs=("Active patterns with comparable backtests", "Variant origin taxonomy"),
                outputs=("``evo_stats`` dict in ``report['evolution']``", "Child pattern rows"),
            ),
            CycleStepDef(
                sid="hypotheses",
                label="Testing hypotheses & evolving strategy",
                code_ref="learning.validate_and_evolve",
                runner_phase="evolving",
                description=(
                    "Runs dynamic hypothesis tests and strategy weight adjustments from "
                    "live and research performance signals."
                ),
                remarks=(
                    "What: Hypothesis CRUD + weight evolution driven by performance deltas.\n\n"
                    "Where: ``validate_and_evolve(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Connects macro performance feedback into per-strategy weights and "
                    "spawns new pattern hypotheses when justified."
                ),
                inputs=("Registered hypotheses", "Live vs research metrics"),
                outputs=(
                    "``evolve_result`` — tested/challenged counts, ``weights_evolved``, details",
                    "``report`` fields: ``hypotheses_tested``, ``real_trade_adjustments``, …",
                ),
            ),
            CycleStepDef(
                sid="breakout",
                label="Learning from breakout outcomes",
                code_ref="learning.learn_from_breakout_outcomes",
                runner_phase="breakout_learning",
                description=(
                    "Consumes resolved breakout alerts to update pattern parameters or "
                    "confidence based on real outcomes."
                ),
                remarks=(
                    "What: Closes the loop between fired breakout alerts and pattern params.\n\n"
                    "Where: ``learn_from_breakout_outcomes(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Realized path outcomes are higher signal than synthetic backtests "
                    "for certain regime tags."
                ),
                inputs=("Resolved breakout alerts in DB", "Linked ``ScanPattern`` ids"),
                outputs=(
                    "``bo_result['patterns_learned']``, ``total_resolved``",
                    "Pattern patch dicts applied inside the helper",
                ),
            ),
        ),
    ),
    CycleClusterDef(
        id="c_secondary",
        label="Secondary miners",
        phase_summary="brain_secondary_miners_on_cycle",
        description=(
            "Optional deep-dive miners (intraday, fakeouts, sizing, synergies, etc.) when "
            "brain_secondary_miners_on_cycle is on; skipped otherwise for faster cycles."
        ),
        remarks=(
            "What: Eight specialized miners and tuners gated by "
            "``settings.brain_secondary_miners_on_cycle``.\n\n"
            "Where: Sequential block inside ``run_learning_cycle`` in ``learning.py`` "
            "(intraday/HV, refine, exit, fakeout, sizing, inter-alert, timeframe, synergy).\n\n"
            "Why: Adds depth when cycle budget allows; skipping keeps hourly cycles feasible."
        ),
        inputs=(
            "``BrainResourceBudget`` for the cycle",
            "``db``, ``user_id``",
            "Patterns and bars required by each miner",
        ),
        outputs=(
            "Per-step counts in ``report`` (intraday_discoveries, refined, exit_adjustments, …)",
            "Updated ``ScanPattern`` / insight side effects",
        ),
        steps=(
            CycleStepDef(
                sid="intraday_hv",
                label="Mining intraday breakout patterns",
                code_ref="mine_intraday_patterns + mine_high_vol_regime_patterns",
                runner_phase="intraday_mining",
                description=(
                    "Mines shorter-interval compression/expansion and high-vol regime "
                    "setups as separate hypothesis families."
                ),
                remarks=(
                    "What: Two related miners — intraday compression/breakout and high-vol "
                    "regime patterns.\n\n"
                    "Where: ``mine_intraday_patterns``, ``mine_high_vol_regime_patterns`` "
                    "called from ``learning.py`` with ``cycle_budget``.\n\n"
                    "Why: Captures structure invisible on daily bars only."
                ),
                inputs=("15m (etc.) OHLCV slices", "Volatility regime labels"),
                outputs=(
                    "``intra_result``, ``hv_result`` discovery and rows_mined metrics",
                    "``report['intraday_discoveries']``, ``report['high_vol_discoveries']``",
                ),
            ),
            CycleStepDef(
                sid="refine",
                label="Refining patterns",
                code_ref="learning.refine_patterns",
                runner_phase="refining",
                description=(
                    "Parameter sweeps and refinements on candidate patterns to tighten "
                    "entries, filters, or timeframes."
                ),
                remarks=(
                    "What: Sweeps parameters on eligible patterns to improve fit without "
                    "full variant forks.\n\n"
                    "Where: ``refine_patterns(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Bridges raw mine output and promotion-quality rules."
                ),
                inputs=("Patterns marked refinable", "Search limits from config"),
                outputs=("``report['patterns_refined']``", "Updated pattern params in DB"),
            ),
            CycleStepDef(
                sid="exit",
                label="Learning exit optimization",
                code_ref="learning.learn_exit_optimization",
                runner_phase="exit_optimization",
                description=(
                    "Adjusts exit rules from historical trade paths to improve risk-adjusted "
                    "outcomes for active strategies."
                ),
                remarks=(
                    "What: Learns exit timing/rules from realized and simulated trade paths.\n\n"
                    "Where: ``learn_exit_optimization(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Exit edge often dominates entry for short-horizon crypto/stock plays."
                ),
                inputs=("Historical fills and alerts", "Per-pattern exit configs"),
                outputs=("``report['exit_adjustments']``", "Persisted exit metadata patches"),
            ),
            CycleStepDef(
                sid="fakeout",
                label="Mining fakeout patterns",
                code_ref="learning.mine_fakeout_patterns",
                runner_phase="fakeout_mining",
                description=(
                    "Finds recurring false-break structures to downgrade or hedge similar "
                    "setups in the future."
                ),
                remarks=(
                    "What: Mines failed-break DNA for anti-patterns.\n\n"
                    "Where: ``mine_fakeout_patterns(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Reduces repeated losses on similar liquidity grabs."
                ),
                inputs=("Price paths around failed breaks", "Pattern library for negatives"),
                outputs=("``report['fakeout_patterns']``", "New fakeout-tagged patterns"),
            ),
            CycleStepDef(
                sid="sizing",
                label="Tuning position sizing",
                code_ref="learning.tune_position_sizing",
                runner_phase="position_sizing",
                description=(
                    "Feeds back realized volatility and outcome data into sizing hints or "
                    "constraints for live vs research."
                ),
                remarks=(
                    "What: Adjusts sizing hints from realized vol and PnL.\n\n"
                    "Where: ``tune_position_sizing(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Keeps live risk in band as regimes shift."
                ),
                inputs=("Position and PnL history", "Risk caps from settings"),
                outputs=("``report['sizing_adjustments']``", "Sizing hint columns/JSON"),
            ),
            CycleStepDef(
                sid="inter_alert",
                label="Learning inter-alert patterns",
                code_ref="learning.learn_inter_alert_patterns",
                runner_phase="inter_alert",
                description=(
                    "Detects correlations and sequences across alerts to improve stacking "
                    "or deduplication of signals."
                ),
                remarks=(
                    "What: Sequence/correlation mining across alert stream.\n\n"
                    "Where: ``learn_inter_alert_patterns(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Stacked signals can be redundant or toxic; this learns which combos work."
                ),
                inputs=("Time-ordered alerts", "Pattern linkage"),
                outputs=("``report['inter_alert_insights']``", "Correlation feature records"),
            ),
            CycleStepDef(
                sid="timeframe",
                label="Learning timeframe performance",
                code_ref="learning.learn_timeframe_performance",
                runner_phase="timeframe_learning",
                description=(
                    "Attributes performance to holding horizon and bar interval so the "
                    "brain can prefer better timeframes per style."
                ),
                remarks=(
                    "What: Maps performance to hold horizon and bar interval.\n\n"
                    "Where: ``learn_timeframe_performance(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Wrong timeframe destroys edge even with good entries."
                ),
                inputs=("Trades/backtests tagged by interval", "Pattern style tags"),
                outputs=("``report['timeframe_insights']``", "Preference weights per style"),
            ),
            CycleStepDef(
                sid="synergy",
                label="Mining signal synergies",
                code_ref="learning.mine_signal_synergies",
                runner_phase="synergy_mining",
                description=(
                    "Finds pairs or groups of signals that outperform together versus alone."
                ),
                remarks=(
                    "What: Co-occurrence mining with outcome lift vs marginal signals.\n\n"
                    "Where: ``mine_signal_synergies(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Portfolio-aware signals beat isolated pattern fires."
                ),
                inputs=("Alert pairs and outcomes", "Co-occurrence windows"),
                outputs=("``report['synergies_found']``", "Synergy pattern or rule candidates"),
            ),
        ),
    ),
    CycleClusterDef(
        id="c_journal",
        label="Journal & signals",
        phase_summary="journaling → signal events",
        description=(
            "Writes a narrative market journal for the cycle and evaluates signal-related "
            "events for the user."
        ),
        remarks=(
            "What: Human-readable journal plus mechanical signal event sweep.\n\n"
            "Where: ``journal.daily_market_journal``, ``journal.check_signal_events`` "
            "from ``app.services.trading.journal``.\n\n"
            "Why: Narrative aids operators and RAG; events drive alerting UX."
        ),
        inputs=("Aggregated cycle context in ``report``", "Open alerts and positions"),
        outputs=("Journal row or null", "``events`` list and count"),
        steps=(
            CycleStepDef(
                sid="journal",
                label="Writing market journal",
                code_ref="journal.daily_market_journal",
                runner_phase="journaling",
                description=(
                    "Synthesizes the day's market narrative into a stored journal artifact "
                    "for humans and downstream RAG."
                ),
                remarks=(
                    "What: LLM or template-backed narrative persisted for the day/cycle.\n\n"
                    "Where: ``daily_market_journal(db, user_id)`` in ``journal`` module.\n\n"
                    "Why: Operators and chat RAG need prose summaries, not only metrics."
                ),
                inputs=("``db``, ``user_id``", "Market summary inputs assembled in cycle"),
                outputs=("``journal`` object or None", "``report['journal_written']`` bool"),
            ),
            CycleStepDef(
                sid="signals",
                label="Checking signal events",
                code_ref="journal.check_signal_events",
                runner_phase="signals",
                description=(
                    "Scans for actionable signal events (entries, exits, warnings) tied to "
                    "the user's book and watchlists."
                ),
                remarks=(
                    "What: Enumerates actionable events for notifications and UI chips.\n\n"
                    "Where: ``check_signal_events(db, user_id)`` in ``journal`` module.\n\n"
                    "Why: Closes the loop from patterns to user-visible alerts."
                ),
                inputs=("Signal definitions and thresholds", "Latest quotes"),
                outputs=(
                    "``events: list``",
                    "``report['signal_events']`` = len(events)",
                ),
            ),
        ),
    ),
    CycleClusterDef(
        id="c_meta",
        label="Meta-learning & cycle close",
        phase_summary="ML → proposals → pattern engine → report → finalize",
        description=(
            "Trains the pattern meta-learner, emits strategy proposals, runs the pattern "
            "engine pass, stores an AI cycle report, applies depromotion rules, and logs."
        ),
        remarks=(
            "What: Meta-model training, proposal generation, pattern-engine sub-cycle, "
            "markdown cycle report, live depromotion, finalize + ``learning_event``.\n\n"
            "Where: End of ``run_learning_cycle`` in ``learning.py`` plus "
            "``pattern_ml``, ``alerts``, ``learning_cycle_report`` modules.\n\n"
            "Why: Compresses the whole cycle into learnable weights, user proposals, "
            "audit narrative, and integrity gates before idle."
        ),
        inputs=(
            "``report`` dict accumulated through the cycle",
            "Active patterns and feature rows for ML",
            "LLM/config for cycle report",
        ),
        outputs=(
            "``report['ml_trained']``, ``proposals_generated``, engine stats",
            "``cycle_ai_report_id``",
            "``live_depromotion`` dict",
            "Final ``log_learning_event`` + idle status",
        ),
        steps=(
            CycleStepDef(
                sid="ml",
                label="Training pattern meta-learner",
                code_ref="pattern_ml.get_meta_learner + apply_ml_feedback",
                runner_phase="ml_training",
                description=(
                    "Fits the meta-model on pattern outcomes and applies feedback boosts or "
                    "penalties to live pattern scores."
                ),
                remarks=(
                    "What: Trains CV model on pattern features; writes feedback boosts.\n\n"
                    "Where: ``pattern_ml.get_meta_learner()``, ``train``, ``apply_ml_feedback`` "
                    "in ``learning.py`` block.\n\n"
                    "Why: Ranks which pattern families deserve capital vs research dustbin."
                ),
                inputs=("Pattern feature matrix from DB", "Meta-learner hyperparameters"),
                outputs=(
                    "``ml_result`` (ok, cv_accuracy, …)",
                    "``report['ml_trained']``, ``ml_feedback_boosted/penalised``",
                ),
            ),
            CycleStepDef(
                sid="proposals",
                label="Generating strategy proposals",
                code_ref="alerts.generate_strategy_proposals",
                runner_phase="proposals",
                description=(
                    "Turns high-confidence patterns and context into user-facing strategy "
                    "proposal objects for review or execution."
                ),
                remarks=(
                    "What: Builds actionable proposal records for the trading desk UI.\n\n"
                    "Where: ``alerts.generate_strategy_proposals(db, user_id)``.\n\n"
                    "Why: Bridges research patterns to human-approved trades."
                ),
                inputs=("Patterns passing confidence gates", "User risk profile fields"),
                outputs=("``proposals`` list", "``report['proposals_generated']``"),
            ),
            CycleStepDef(
                sid="pattern_engine",
                label="Pattern discovery & evolution",
                code_ref="learning._run_pattern_engine_cycle",
                runner_phase="pattern_engine",
                description=(
                    "Runs the dedicated pattern-engine sub-cycle: hypothesis generation, "
                    "testing, and evolution separate from the main mining pass."
                ),
                remarks=(
                    "What: Isolated engine pass for hypotheses/tests/evolution.\n\n"
                    "Where: ``_run_pattern_engine_cycle(db, user_id)`` in ``learning.py``.\n\n"
                    "Why: Allows heavier logic without blocking the earlier fast path."
                ),
                inputs=("Engine-internal config", "Pattern and insight ORM state"),
                outputs=(
                    "``pe_result`` — hypotheses_generated, patterns_tested, patterns_evolved",
                    "Merged into ``report``",
                ),
            ),
            CycleStepDef(
                sid="cycle_report",
                label="Generating cycle AI report",
                code_ref="learning_cycle_report.generate_and_store_cycle_report",
                runner_phase="cycle_ai_report",
                description=(
                    "Produces a markdown deep-dive of the cycle and persists it for the UI "
                    "and audit trail."
                ),
                remarks=(
                    "What: LLM synthesis of the cycle into stored markdown.\n\n"
                    "Where: ``generate_and_store_cycle_report(db, user_id, report)``.\n\n"
                    "Why: Auditability and operator read of what the machine did this cycle."
                ),
                inputs=("Full ``report`` dict", "Model/template settings"),
                outputs=("``report['cycle_ai_report_id']``", "Markdown persisted in DB"),
            ),
            CycleStepDef(
                sid="depromote",
                label="Live vs research depromotion",
                code_ref="learning.run_live_pattern_depromotion",
                runner_phase="live_depromotion",
                description=(
                    "Downgrades or deactivates patterns that fail live-vs-research integrity "
                    "or promotion gates."
                ),
                remarks=(
                    "What: Integrity gate between paper-sharp and live-fragile patterns.\n\n"
                    "Where: ``run_live_pattern_depromotion(db)`` after cycle report.\n\n"
                    "Why: Protects live book from research-overfit promotions."
                ),
                inputs=("Live vs research performance splits in DB", "Promotion policy"),
                outputs=("``report['live_depromotion']`` dict", "Updated ``active`` flags"),
            ),
            CycleStepDef(
                sid="finalize",
                label="Finalizing",
                code_ref="run_learning_cycle finalize + log_learning_event",
                runner_phase="finalizing",
                description=(
                    "Aggregates timings, writes the summary learning event, clears running "
                    "state, and exposes the cycle digest to the Brain UI."
                ),
                remarks=(
                    "What: Logs aggregate cycle summary, sets status idle, copies funnel "
                    "into ``_learning_status``.\n\n"
                    "Where: Tail of ``run_learning_cycle`` ``try`` body + ``finally`` in "
                    "``learning.py``.\n\n"
                    "Why: Single auditable line in ``learning_events`` and clean UI state."
                ),
                inputs=(
                    "Final ``report`` + ``elapsed`` + ``_provider`` string",
                    "``db``, ``user_id`` for ``log_learning_event``",
                ),
                outputs=(
                    "``learning_events`` scan row",
                    "``_learning_status`` idle + ``last_cycle_funnel``",
                    "Returned ``report`` to caller",
                ),
            ),
        ),
    ),
)


def _build_step_index() -> dict[tuple[str, str], CycleStepDef]:
    idx: dict[tuple[str, str], CycleStepDef] = {}
    for c in TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS:
        for s in c.steps:
            key = (c.id, s.sid)
            if key in idx:
                raise ValueError(f"duplicate cycle step key: {key}")
            idx[key] = s
    return idx


_CYCLE_STEP_INDEX: dict[tuple[str, str], CycleStepDef] = _build_step_index()


def get_cycle_step(cluster_id: str, step_sid: str) -> CycleStepDef:
    try:
        return _CYCLE_STEP_INDEX[(cluster_id, step_sid)]
    except KeyError as e:
        raise KeyError(f"unknown cycle step ({cluster_id!r}, {step_sid!r})") from e


def apply_learning_cycle_step_status(status_dict: dict[str, Any], cluster_id: str, step_sid: str) -> None:
    s = get_cycle_step(cluster_id, step_sid)
    status_dict["current_step"] = s.label
    status_dict["phase"] = s.runner_phase


def apply_learning_cycle_step_status_progress(
    status_dict: dict[str, Any],
    cluster_id: str,
    step_sid: str,
    done: int,
    total: int,
) -> None:
    s = get_cycle_step(cluster_id, step_sid)
    status_dict["current_step"] = f"{s.label} ({done}/{total})"
    status_dict["phase"] = s.runner_phase
