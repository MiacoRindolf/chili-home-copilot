"""Canonical Trading Brain learning-cycle architecture (single source of truth).

Used by ``get_trading_brain_network_graph`` for the Network tab and by
``run_learning_cycle`` for ``current_step`` / ``phase`` strings.

When you add, remove, or reorder phases, edit **this module only** (then bump
``graph_version`` in ``brain_network_graph`` if consumers care).

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
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


@dataclass(frozen=True)
class CycleClusterDef:
    id: str
    label: str
    phase_summary: str
    description: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    steps: tuple[CycleStepDef, ...]


@dataclass(frozen=True)
class TradingBrainRootMetadata:
    """Documentation for the graph root (orchestrator)."""

    description: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


TRADING_BRAIN_ROOT_METADATA = TradingBrainRootMetadata(
    description=(
        "Top-level orchestrator for one full learning cycle: narrows the universe, "
        "refreshes market memory, mines and validates patterns, evolves hypotheses, "
        "runs optional secondary miners, journals results, trains meta-models, "
        "generates proposals and reports, then finalizes state."
    ),
    inputs=(
        "PostgreSQL session and CHILI config (providers, feature flags)",
        "User / household context for scoping and watchlists",
        "External market data APIs (Massive, Polygon, or yfinance)",
    ),
    outputs=(
        "Cycle report dict (counts, timings, funnel snapshot)",
        "Updated DB rows: snapshots, patterns, insights, signals, events",
        "Learning status and logs visible in the Brain UI",
    ),
)

TRADING_BRAIN_LEARNING_CYCLE_CLUSTERS: tuple[CycleClusterDef, ...] = (
    CycleClusterDef(
        id="c_universe",
        label="Universe & scoring",
        phase_summary="pre-filtering → scanning",
        description=(
            "Reduces thousands of symbols to a tractable set, then deep-scores candidates "
            "so later phases work on the most relevant tickers."
        ),
        inputs=("Configured universe and provider connectivity", "Prior cycle state (optional)"),
        outputs=("Prescreened candidate list", "Ranked scan results with scores"),
        steps=(
            CycleStepDef(
                sid="prefilter",
                label="Pre-filtering market",
                code_ref="prescreener.get_prescreened_candidates",
                runner_phase="pre-filtering",
                description=(
                    "Applies fast screens (liquidity, price, lists) to shrink the raw universe "
                    "before expensive scoring."
                ),
                inputs=("Raw symbol universe", "Screener rules and provider data"),
                outputs=("Candidate ticker list", "Prescreen status / source breakdown"),
            ),
            CycleStepDef(
                sid="scan",
                label="Scanning market",
                code_ref="scanner.run_full_market_scan",
                runner_phase="scanning",
                description=(
                    "Runs the main market scan over prescreened names to produce scores and "
                    "metadata used by snapshots and mining."
                ),
                inputs=("Prescreened candidates", "OHLCV and quote data"),
                outputs=("Scan result rows per ticker", "Aggregate scan counters"),
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
        inputs=("Scored tickers and watchlist", "Historical bar data"),
        outputs=("Market snapshots", "Backfilled returns/scores", "Decay/prune stats"),
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
                inputs=("Ticker list from scan + watchlist", "Bar intervals and provider"),
                outputs=("market_snapshots rows", "Per-ticker indicator snapshots"),
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
                inputs=("Existing snapshots and price history", "Prediction pipeline"),
                outputs=("Updated snapshot rows", "Backfill counts for returns and scores"),
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
                inputs=("TradingInsight and related rows", "Decay thresholds"),
                outputs=("Decayed or pruned insight counts", "Updated confidence fields"),
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
        inputs=("Snapshots and OHLCV", "Existing pattern catalog"),
        outputs=("New or updated ScanPattern candidates", "Seek/boost statistics"),
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
                inputs=("Market snapshots and bar data", "Mining configuration"),
                outputs=("Discovered pattern records", "Discovery count for the cycle"),
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
                inputs=("Active patterns with low coverage", "Ticker and bar availability"),
                outputs=("Boosted / sought counts", "Updated pattern evidence paths"),
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
        inputs=("Patterns and insights queued for testing", "Backtest engine settings"),
        outputs=("Backtest results", "Queue depth and exploration metrics"),
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
                inputs=("Open insights eligible for backtest", "Spread and commission model"),
                outputs=("Insight backtest counts", "Skip flag when disabled"),
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
                inputs=("Priority queue of patterns", "OHLCV windows per pattern"),
                outputs=(
                    "Patterns processed and pending counts",
                    "Backtest result rows",
                    "Queue exploration additions",
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
        inputs=("Patterns with variants", "Hypothesis engine state", "Alert/outcome history"),
        outputs=("Evolved weights and variants", "Hypothesis test stats", "Breakout learnings"),
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
                inputs=("Parent patterns and backtest history", "Variant taxonomy"),
                outputs=("Evolution statistics dict", "New or updated child patterns"),
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
                inputs=("Hypothesis registry", "Performance and risk metrics"),
                outputs=(
                    "Hypotheses tested/challenged counts",
                    "Weight evolution and spawned patterns",
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
                inputs=("Resolved breakout alerts", "Linked patterns"),
                outputs=("Patterns learned count", "Adjustment metadata in report"),
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
        inputs=("Patterns and bar data", "Resource budget for the cycle"),
        outputs=("Specialized pattern stats", "Refinements and tuning adjustments"),
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
                inputs=("Intraday OHLCV slices", "High-vol regime labels"),
                outputs=("Intraday and high-vol discovery counts", "Rows mined metrics"),
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
                inputs=("Patterns eligible for refinement", "Search grid / limits"),
                outputs=("Refined pattern count", "Updated parameters"),
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
                inputs=("Trade and alert history", "Exit configs per pattern"),
                outputs=("Exit adjustment count", "Updated exit metadata"),
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
                inputs=("Price action around failed breaks", "Existing pattern library"),
                outputs=("Fakeout pattern count", "New pattern candidates"),
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
                inputs=("Position and PnL history", "Risk settings"),
                outputs=("Sizing adjustment count", "Updated sizing hints"),
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
                inputs=("Historical alerts timeline", "Pattern linkage graph"),
                outputs=("Inter-alert insight count", "Correlation features"),
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
                inputs=("Backtests and trades by interval", "Pattern tags"),
                outputs=("Timeframe insight count", "Horizon preference signals"),
            ),
            CycleStepDef(
                sid="synergy",
                label="Mining signal synergies",
                code_ref="learning.mine_signal_synergies",
                runner_phase="synergy_mining",
                description=(
                    "Finds pairs or groups of signals that outperform together versus alone."
                ),
                inputs=("Alert co-occurrence data", "Outcome labels"),
                outputs=("Synergy count", "Combined signal candidates"),
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
        inputs=("Cycle aggregates and context", "Open positions and alerts"),
        outputs=("Journal entry", "Signal event list and counts"),
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
                inputs=("DB market summary inputs", "User scope"),
                outputs=("Journal record or skip marker", "Narrative text blob"),
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
                inputs=("Active signals and thresholds", "Latest prices"),
                outputs=("Event list", "Event count for reporting"),
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
        inputs=("Full cycle report dict so far", "Active patterns and ML features"),
        outputs=(
            "ML training metrics",
            "Proposals",
            "Pattern engine stats",
            "Stored markdown report",
            "Depromotion results",
            "Final learning event log",
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
                inputs=("Labeled pattern feature rows", "Train/validation split config"),
                outputs=("CV accuracy and training ok flag", "Boosted/penalised pattern counts"),
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
                inputs=("Patterns passing gates", "User risk preferences"),
                outputs=("Proposal list", "Proposal count"),
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
                inputs=("Engine config", "Pattern and insight stores"),
                outputs=("Hypotheses generated", "Patterns tested/evolved counts"),
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
                inputs=("Complete in-memory cycle report", "LLM / template configuration"),
                outputs=("Stored report id", "Markdown body in DB"),
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
                inputs=("Live and research performance splits", "Promotion rules"),
                outputs=("Depromotion outcome dict", "Updated pattern active flags"),
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
                inputs=("Final report dict", "Elapsed time and provider name"),
                outputs=("learning_events row", "Idle learning status", "Client-visible digest"),
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
