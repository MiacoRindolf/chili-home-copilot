"""Phase G - periodic reconciliation sweep (read-only, shadow-safe).

Reads open ``Trade`` rows + their ``BracketIntent`` rows, asks the
broker for its view via an injectable ``broker_view_fn``, classifies
each (trade, broker) pair through ``bracket_reconciler.classify_discrepancy``,
and persists one ``BracketReconciliationLog`` row per comparison.

This service is **strictly read-only against the broker** in Phase G.
It never submits, cancels, or modifies any broker order. Running with
``brain_live_brackets_mode=authoritative`` raises immediately so that
Phase G.2 must explicitly wire a writer path before enabling it.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.bracket_reconciliation_ops_log import (
    format_bracket_reconciliation_ops_line,
)
from .bracket_intent_writer import bump_last_observed, mark_reconciled
from .bracket_reconciler import (
    BrokerView,
    LocalView,
    ReconciliationDecision,
    Tolerances,
    classify_discrepancy,
)

# Structured log prefixes. Kept local — the shared ``ops_log_prefixes``
# module landed on ``main`` after this branch diverged; duplicating the
# literal keeps the staged-sweep refactor self-contained and matches
# pattern-matched prod alerts on ``[bracket_reconciliation]`` /
# ``[bracket_watchdog]``.
BRACKET_RECONCILIATION = "[bracket_reconciliation]"
BRACKET_WATCHDOG = "[bracket_watchdog]"

logger = logging.getLogger(__name__)
_ALLOWED_MODES = ("off", "shadow", "compare", "authoritative")


def _effective_mode(override: str | None = None) -> str:
    m = (override or getattr(settings, "brain_live_brackets_mode", "off") or "off").lower()
    return m if m in _ALLOWED_MODES else "off"


def _ops_log_enabled() -> bool:
    return bool(getattr(settings, "brain_live_brackets_ops_log_enabled", True))


def _staged_sweep_enabled() -> bool:
    return bool(getattr(settings, "brain_live_brackets_staged_sweep_enabled", False))


def _tolerances_from_settings() -> Tolerances:
    """Load reconciliation tolerances from settings with safe defaults.

    The defaults below were calibrated during the Phase G (live-brackets
    reconciliation) rollout against broker-truth samples from Robinhood:

    * **price_drift_bps = 25.0** (0.25%). Broker-truth stop prices round
      to instrument-specific tick sizes (e.g. $0.01 for most equities
      above $1). Over 100 sampled stops, the observed drift between
      CHILI's intended stop and RH's returned stop clustered under
      ~10 bps, with outliers around 20 bps on low-priced volatile
      tickers. 25 bps gives ~2x headroom without blurring real drift —
      anything above 25 bps is a genuinely mis-priced stop and
      classifies as ``price_drift``.
    * **qty_drift_abs = 1e-6**. A pure numerical-noise threshold: SQL
      stores quantities as floats, broker responds with strings, round-
      tripping through ``Decimal -> float`` produces ULP-level diffs.
      1e-6 is below the smallest fractional share RH supports (5 decimal
      places) and above the observed float noise. Any real mismatch is
      many orders of magnitude larger.

    Change the defaults only with broker-truth resampling — a wider
    bound masks real drift; a tighter one floods the watchdog with false
    positives. Env overrides
    (``BRAIN_LIVE_BRACKETS_PRICE_DRIFT_BPS`` /
    ``BRAIN_LIVE_BRACKETS_QTY_DRIFT_ABS``) are available for incident
    response but prefer updating the default constant after rollback.
    """
    return Tolerances(
        price_drift_bps=float(getattr(settings, "brain_live_brackets_price_drift_bps", 25.0)),
        qty_drift_abs=float(getattr(settings, "brain_live_brackets_qty_drift_abs", 1e-6)),
    )


# ── Broker view provider (injectable) ──────────────────────────────────


BrokerViewFn = Callable[[list[dict[str, Any]]], list[BrokerView]]


def _noop_broker_view_fn(local_rows: list[dict[str, Any]]) -> list[BrokerView]:
    """Default broker provider: flags every ticker as ``available=False``.

    The scheduler job supplies a real provider that reads open orders +
    positions from ``broker_manager``; tests supply synthetic providers.
    Returning ``broker_down`` here by default means the sweep is safe
    even if the scheduler wires things up in the wrong order.
    """
    return [
        BrokerView(available=False, ticker=r.get("ticker"), broker_source=r.get("broker_source"))
        for r in local_rows
    ]


def broker_manager_view_fn(local_rows: list[dict[str, Any]]) -> list[BrokerView]:
    """Phase G default broker provider: reads combined positions across
    Robinhood + Coinbase via ``broker_manager.get_combined_positions``.

    In Phase G the broker has no bracket / stop primitives wired (that's
    Phase G.2), so ``stop_order_id`` / ``target_order_id`` are always
    ``None``. A live trade with a local ``BracketIntent`` will therefore
    classify as ``missing_stop`` (truthful: Phase G never places a
    server-side stop). Positions we cannot reach are flagged
    ``available=False`` so the reconciler emits ``broker_down``.
    """
    views: list[BrokerView] = []
    try:
        from ..broker_manager import get_combined_positions  # local import
        positions = get_combined_positions() or []
    except Exception:  # pragma: no cover - defensive
        logger.warning(f"{BRACKET_RECONCILIATION} broker_manager_view_fn: unavailable", exc_info=True)
        positions = None

    if positions is None:
        return [
            BrokerView(
                available=False,
                ticker=r.get("ticker"),
                broker_source=r.get("broker_source"),
            )
            for r in local_rows
        ]

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for p in positions:
        tkr = (p.get("ticker") or p.get("symbol") or "").upper() or None
        src = p.get("broker_source")
        if not tkr or not src:
            continue
        by_key[(tkr, src)] = p

    for row in local_rows:
        tkr = (row.get("ticker") or "").upper() or None
        src = row.get("broker_source")
        p = by_key.get((tkr, src)) if tkr and src else None
        if p is None:
            views.append(BrokerView(
                available=True,
                ticker=tkr,
                broker_source=src,
                position_quantity=0.0,
            ))
            continue
        qty = p.get("quantity") or p.get("qty") or p.get("shares") or 0
        try:
            qty_f = float(qty or 0)
        except Exception:
            qty_f = 0.0
        views.append(BrokerView(
            available=True,
            ticker=tkr,
            broker_source=src,
            position_quantity=qty_f,
        ))
    return views


# ── Result shape ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepSummary:
    sweep_id: str
    mode: str
    trades_scanned: int
    brackets_checked: int
    agree: int
    orphan_stop: int
    missing_stop: int
    qty_drift: int
    state_drift: int
    price_drift: int
    broker_down: int
    unreconciled: int
    took_ms: float
    rows_written: int = 0
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sweep_id": self.sweep_id,
            "mode": self.mode,
            "trades_scanned": self.trades_scanned,
            "brackets_checked": self.brackets_checked,
            "agree": self.agree,
            "orphan_stop": self.orphan_stop,
            "missing_stop": self.missing_stop,
            "qty_drift": self.qty_drift,
            "state_drift": self.state_drift,
            "price_drift": self.price_drift,
            "broker_down": self.broker_down,
            "unreconciled": self.unreconciled,
            "took_ms": self.took_ms,
            "rows_written": self.rows_written,
        }


# ── Staged-sweep data carrier ──────────────────────────────────────────


@dataclass
class SweepBatch:
    """Progressive batch state passed between the four sweep stages.

    Each stage reads the fields it needs and appends/assigns its own output
    (``local_views`` → ``broker_views`` → ``decisions`` → ``rows_written``).
    The orchestrator summarizes the populated batch at the end. Stages
    downstream of ``classify_all`` must not touch the broker or run the
    classifier again; log_all is the only stage that writes to the DB.
    """

    sweep_id: str
    mode: str
    tolerances: Tolerances
    local_views: list[LocalView] = field(default_factory=list)
    broker_views: list[BrokerView] = field(default_factory=list)
    decisions: list[ReconciliationDecision] = field(default_factory=list)
    rows_written: int = 0


def _row_to_local_view(row: dict[str, Any]) -> LocalView:
    return LocalView(
        trade_id=row.get("trade_id"),
        bracket_intent_id=row.get("bracket_intent_id"),
        ticker=row.get("ticker"),
        direction=row.get("direction"),
        quantity=row.get("quantity"),
        intent_state=row.get("intent_state"),
        stop_price=row.get("stop_price"),
        target_price=row.get("target_price"),
        broker_source=row.get("broker_source"),
        trade_status=row.get("trade_status"),
    )


# ── Stage 1: load_local ────────────────────────────────────────────────


def _stage_load_local(
    db: Session, batch: SweepBatch, *, user_id: int | None = None
) -> None:
    """Populate ``batch.local_views`` from the live-trade + intent join.

    Read-only DB query; no broker calls. See ``_load_local_view`` for the
    SELECT shape and scope (open live trades + orphan-candidate intents).
    """
    rows = _load_local_view(db, user_id=user_id)
    batch.local_views = [_row_to_local_view(r) for r in rows]


# ── Stage 2: fetch_broker ──────────────────────────────────────────────


def _stage_fetch_broker(batch: SweepBatch, broker_view_fn: BrokerViewFn) -> None:
    """Ask the broker for its view of each (ticker, broker_source) key.

    Writes a broker view for every local view, parallel-indexed. Missing
    broker entries are backfilled as ``available=False`` so the classifier
    attributes them to ``broker_down`` rather than false-positive
    ``missing_stop``.
    """
    broker_input: list[dict[str, Any]] = [
        {"ticker": lv.ticker, "broker_source": lv.broker_source}
        for lv in batch.local_views
    ]
    raw_views = broker_view_fn(broker_input)
    by_key = {(bv.ticker, bv.broker_source): bv for bv in raw_views}
    aligned: list[BrokerView] = []
    for lv in batch.local_views:
        bv = by_key.get((lv.ticker, lv.broker_source))
        if bv is None:
            bv = BrokerView(
                available=False,
                ticker=lv.ticker,
                broker_source=lv.broker_source,
            )
        aligned.append(bv)
    batch.broker_views = aligned


# ── Stage 3: classify_all (pure — no DB, no broker) ────────────────────


def _stage_classify_all(batch: SweepBatch) -> list[ReconciliationDecision]:
    """Classify every (local, broker) pair. Pure: safe to call with no DB.

    Runs ``classify_discrepancy`` for each parallel-indexed pair. A classifier
    exception is trapped into an ``unreconciled`` decision so a single bad
    row can't abort the sweep. The batch is mutated to hold the decisions
    *and* returned for convenience (tests).
    """
    out: list[ReconciliationDecision] = []
    for lv, bv in zip(batch.local_views, batch.broker_views):
        try:
            out.append(
                classify_discrepancy(lv, bv, tolerances=batch.tolerances)
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"{BRACKET_RECONCILIATION} classify_discrepancy failed for trade %s: %s",
                lv.trade_id, exc,
            )
            out.append(ReconciliationDecision(
                kind="unreconciled", severity="error",
                delta_payload={"error": str(exc)},
            ))
    batch.decisions = out
    return out


# ── Stage 4: log_all (DB writes + intent bumps + ops-log emission) ─────


def _stage_log_all(db: Session, batch: SweepBatch) -> int:
    """Persist one reconciliation-log row per decision + bump intents.

    Writes a ``trading_bracket_reconciliation_log`` row, calls
    ``mark_reconciled`` on agree or ``bump_last_observed`` on non-agree
    (P0.5 crash-recovery signal), and emits a ``[bracket_reconciliation_ops]``
    ``event=discrepancy`` line on any non-agree outcome. All writes stay on
    the passed-in session; the orchestrator owns the commit.
    """
    rows_written = 0
    for lv, bv, decision in zip(
        batch.local_views, batch.broker_views, batch.decisions
    ):
        try:
            _write_reconciliation_row(
                db,
                sweep_id=batch.sweep_id,
                mode=batch.mode,
                local=lv,
                broker=bv,
                decision=decision,
            )
            rows_written += 1
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                f"{BRACKET_RECONCILIATION} failed to write log row for trade %s",
                lv.trade_id, exc_info=True,
            )

        if decision.kind == "agree" and lv.bracket_intent_id is not None:
            try:
                mark_reconciled(
                    db,
                    int(lv.bracket_intent_id),
                    reason="agree",
                    mode_override=batch.mode,
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    f"{BRACKET_RECONCILIATION} mark_reconciled failed for intent %s",
                    lv.bracket_intent_id,
                )
        elif lv.bracket_intent_id is not None:
            try:
                bump_last_observed(
                    db,
                    int(lv.bracket_intent_id),
                    diff_reason=f"{decision.kind}:{decision.severity}",
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    f"{BRACKET_RECONCILIATION} bump_last_observed failed for intent %s",
                    lv.bracket_intent_id,
                )

        if _ops_log_enabled() and decision.kind != "agree":
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="discrepancy",
                    mode=batch.mode,
                    sweep_id=batch.sweep_id,
                    trade_id=lv.trade_id,
                    bracket_intent_id=lv.bracket_intent_id,
                    ticker=lv.ticker,
                    broker_source=lv.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                )
            )
    batch.rows_written = rows_written
    return rows_written


# ── Summary builders + legacy interleaved loop ─────────────────────────


_COUNT_KINDS = (
    "agree",
    "orphan_stop",
    "missing_stop",
    "qty_drift",
    "state_drift",
    "price_drift",
    "broker_down",
    "unreconciled",
)


def _empty_off_summary(sweep_id: str) -> SweepSummary:
    return SweepSummary(
        sweep_id=sweep_id,
        mode="off",
        trades_scanned=0,
        brackets_checked=0,
        agree=0,
        orphan_stop=0,
        missing_stop=0,
        qty_drift=0,
        state_drift=0,
        price_drift=0,
        broker_down=0,
        unreconciled=0,
        took_ms=0.0,
        rows_written=0,
    )


def _emit_sweep_summary_ops_log(summary: SweepSummary) -> None:
    if not _ops_log_enabled():
        return
    logger.info(
        format_bracket_reconciliation_ops_line(
            event="sweep_summary",
            mode=summary.mode,
            sweep_id=summary.sweep_id,
            trades_scanned=summary.trades_scanned,
            brackets_checked=summary.brackets_checked,
            agree_count=summary.agree,
            orphan_stop=summary.orphan_stop,
            missing_stop=summary.missing_stop,
            qty_drift=summary.qty_drift,
            state_drift=summary.state_drift,
            price_drift=summary.price_drift,
            broker_down=summary.broker_down,
            unreconciled=summary.unreconciled,
            took_ms=summary.took_ms,
        )
    )


def _summarize_from_batch(batch: SweepBatch, *, took_ms: float) -> SweepSummary:
    counts = {k: 0 for k in _COUNT_KINDS}
    brackets_checked = 0
    for lv, decision in zip(batch.local_views, batch.decisions):
        counts[decision.kind] = counts.get(decision.kind, 0) + 1
        if lv.bracket_intent_id is not None:
            brackets_checked += 1
    decisions_payload = [
        {
            "trade_id": lv.trade_id,
            "bracket_intent_id": lv.bracket_intent_id,
            "ticker": lv.ticker,
            "broker_source": lv.broker_source,
            "kind": d.kind,
            "severity": d.severity,
            "delta_payload": d.delta_payload,
        }
        for lv, d in zip(batch.local_views, batch.decisions)
    ]
    return SweepSummary(
        sweep_id=batch.sweep_id,
        mode=batch.mode,
        trades_scanned=len(batch.local_views),
        brackets_checked=brackets_checked,
        agree=counts["agree"],
        orphan_stop=counts["orphan_stop"],
        missing_stop=counts["missing_stop"],
        qty_drift=counts["qty_drift"],
        state_drift=counts["state_drift"],
        price_drift=counts["price_drift"],
        broker_down=counts["broker_down"],
        unreconciled=counts["unreconciled"],
        took_ms=took_ms,
        rows_written=batch.rows_written,
        decisions=decisions_payload,
    )


def _run_sweep_staged(
    db: Session,
    *,
    mode: str,
    sweep_id: str,
    tolerances: Tolerances,
    user_id: int | None,
    broker_view_fn: BrokerViewFn,
) -> SweepSummary:
    """Staged pipeline: load_local → fetch_broker → classify_all → log_all.

    Gated behind ``brain_live_brackets_staged_sweep_enabled`` (default
    False). Tests assert SweepSummary parity against the legacy loop
    before the flag is flipped in a follow-up PR.
    """
    start = time.perf_counter()
    batch = SweepBatch(sweep_id=sweep_id, mode=mode, tolerances=tolerances)
    _stage_load_local(db, batch, user_id=user_id)
    _stage_fetch_broker(batch, broker_view_fn)
    _stage_classify_all(batch)
    _stage_log_all(db, batch)
    took_ms = (time.perf_counter() - start) * 1000.0
    return _summarize_from_batch(batch, took_ms=took_ms)


def _run_sweep_legacy(
    db: Session,
    *,
    mode: str,
    sweep_id: str,
    tolerances: Tolerances,
    user_id: int | None,
    broker_view_fn: BrokerViewFn,
) -> SweepSummary:
    """Legacy interleaved loop: classify + persist + log in one pass."""
    start = time.perf_counter()

    local_rows = _load_local_view(db, user_id=user_id)
    broker_input: list[dict[str, Any]] = [
        {"ticker": r["ticker"], "broker_source": r["broker_source"]}
        for r in local_rows
    ]
    broker_views = broker_view_fn(broker_input)
    broker_by_ticker = {(bv.ticker, bv.broker_source): bv for bv in broker_views}

    counts = {k: 0 for k in _COUNT_KINDS}
    brackets_checked = 0
    rows_written = 0
    decisions: list[dict[str, Any]] = []

    for row in local_rows:
        local = _row_to_local_view(row)
        broker = broker_by_ticker.get((local.ticker, local.broker_source))
        if broker is None:
            broker = BrokerView(
                available=False,
                ticker=local.ticker,
                broker_source=local.broker_source,
            )

        try:
            decision: ReconciliationDecision = classify_discrepancy(
                local, broker, tolerances=tolerances,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"{BRACKET_RECONCILIATION} classify_discrepancy failed for trade %s: %s",
                local.trade_id, exc,
            )
            decision = ReconciliationDecision(
                kind="unreconciled", severity="error",
                delta_payload={"error": str(exc)},
            )

        counts[decision.kind] = counts.get(decision.kind, 0) + 1
        if local.bracket_intent_id is not None:
            brackets_checked += 1

        try:
            _write_reconciliation_row(
                db,
                sweep_id=sweep_id,
                mode=mode,
                local=local,
                broker=broker,
                decision=decision,
            )
            rows_written += 1
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                f"{BRACKET_RECONCILIATION} failed to write log row for trade %s",
                local.trade_id, exc_info=True,
            )

        if decision.kind == "agree" and local.bracket_intent_id is not None:
            try:
                mark_reconciled(
                    db,
                    int(local.bracket_intent_id),
                    reason="agree",
                    mode_override=mode,
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    f"{BRACKET_RECONCILIATION} mark_reconciled failed for intent %s",
                    local.bracket_intent_id,
                )
        elif local.bracket_intent_id is not None:
            # P0.5 crash-recovery signal: bump last_observed_at on every
            # non-agree scan too, so the watchdog can distinguish
            # "reconciler saw this and it's still broken" from
            # "reconciler never ran / crashed before reaching this intent."
            try:
                bump_last_observed(
                    db,
                    int(local.bracket_intent_id),
                    diff_reason=f"{decision.kind}:{decision.severity}",
                )
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    f"{BRACKET_RECONCILIATION} bump_last_observed failed for intent %s",
                    local.bracket_intent_id,
                )

        decisions.append({
            "trade_id": local.trade_id,
            "bracket_intent_id": local.bracket_intent_id,
            "ticker": local.ticker,
            "broker_source": local.broker_source,
            "kind": decision.kind,
            "severity": decision.severity,
            "delta_payload": decision.delta_payload,
        })

        if _ops_log_enabled() and decision.kind != "agree":
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="discrepancy",
                    mode=mode,
                    sweep_id=sweep_id,
                    trade_id=local.trade_id,
                    bracket_intent_id=local.bracket_intent_id,
                    ticker=local.ticker,
                    broker_source=local.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                )
            )

    took_ms = (time.perf_counter() - start) * 1000.0
    return SweepSummary(
        sweep_id=sweep_id,
        mode=mode,
        trades_scanned=len(local_rows),
        brackets_checked=brackets_checked,
        agree=counts["agree"],
        orphan_stop=counts["orphan_stop"],
        missing_stop=counts["missing_stop"],
        qty_drift=counts["qty_drift"],
        state_drift=counts["state_drift"],
        price_drift=counts["price_drift"],
        broker_down=counts["broker_down"],
        unreconciled=counts["unreconciled"],
        took_ms=took_ms,
        rows_written=rows_written,
        decisions=decisions,
    )


# ── Main entry point ───────────────────────────────────────────────────


def run_reconciliation_sweep(
    db: Session,
    *,
    user_id: int | None = None,
    broker_view_fn: BrokerViewFn | None = None,
    mode_override: str | None = None,
) -> SweepSummary:
    """Run a single reconciliation sweep across open live trades.

    * Off mode → returns an empty summary without touching the DB or the
      broker.
    * Authoritative mode → raises ``RuntimeError``; Phase G.2 will wire
      a dedicated writer path and flip this gate.
    * Shadow / compare → dispatches to either ``_run_sweep_staged`` (when
      ``brain_live_brackets_staged_sweep_enabled`` is True) or
      ``_run_sweep_legacy`` (default). Both paths return a byte-identical
      ``SweepSummary`` — the flag only changes the internal pipeline
      shape (interleaved vs four discrete stages).
    """
    mode = _effective_mode(mode_override)
    sweep_id = str(uuid.uuid4())

    if mode == "off":
        return _empty_off_summary(sweep_id)

    if mode == "authoritative":
        raise RuntimeError(
            "bracket_reconciliation_service.run_reconciliation_sweep refuses to run "
            "in authoritative mode during Phase G; that cutover belongs to Phase G.2."
        )

    broker_view_fn = broker_view_fn or _noop_broker_view_fn
    tolerances = _tolerances_from_settings()
    runner = _run_sweep_staged if _staged_sweep_enabled() else _run_sweep_legacy
    summary = runner(
        db,
        mode=mode,
        sweep_id=sweep_id,
        tolerances=tolerances,
        user_id=user_id,
        broker_view_fn=broker_view_fn,
    )

    try:
        db.commit()
    except Exception:  # pragma: no cover - defensive
        db.rollback()
        logger.warning(f"{BRACKET_RECONCILIATION} failed to commit sweep %s", sweep_id)

    _emit_sweep_summary_ops_log(summary)
    return summary


# ── Local view loader ──────────────────────────────────────────────────


def _load_local_view(
    db: Session,
    *,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    """Load one row per live ``Trade`` + its bracket intent that is in the
    reconciliation sweep's scan scope.

    Scope (P0.5 — orphan-stop coverage expansion):

    * Always: every open live trade (``status='open'``,
      ``broker_source IS NOT NULL``) — the classical reconciliation path.
    * Also: any trade whose ``BracketIntent`` has NOT yet reached a
      terminal state (i.e. not ``reconciled`` and not
      ``authoritative_closed``) — including trades that are
      ``cancelled`` / ``expired`` / ``closed``. Without this, a
      cancelled entry that left a working stop at the broker (orphan)
      would never be scanned and never classified as ``orphan_stop``.

    Paper trades (``broker_source IS NULL``) are excluded on purpose:
    paper state is authoritative locally and needs no broker check.
    """
    params: dict[str, Any] = {}
    # Two disjoint scopes joined with OR:
    #   scope A — the classical "open live trade" scope.
    #   scope B — the orphan candidate scope: the Trade is no longer
    #             open, but its BracketIntent still thinks it should be
    #             protected. These rows are exactly the ones at risk of
    #             leaving a stop working at the broker for a position
    #             we no longer hold.
    scope_clause = (
        "( (t.status = 'open' AND t.broker_source IS NOT NULL)"
        " OR ("
        "     bi.id IS NOT NULL"
        "     AND t.broker_source IS NOT NULL"
        "     AND t.status <> 'open'"
        "     AND bi.intent_state NOT IN ('reconciled', 'authoritative_closed')"
        "   )"
        " )"
    )
    filters = [scope_clause]
    if user_id is not None:
        filters.append("t.user_id = :uid")
        params["uid"] = int(user_id)

    sql = text(f"""
        SELECT
            t.id AS trade_id,
            t.user_id,
            t.ticker,
            t.direction,
            t.quantity,
            t.status AS trade_status,
            t.broker_source,
            bi.id AS bracket_intent_id,
            bi.intent_state,
            bi.stop_price,
            bi.target_price
        FROM trading_trades AS t
        LEFT JOIN trading_bracket_intents AS bi
          ON bi.trade_id = t.id
        WHERE {' AND '.join(filters)}
        ORDER BY t.id
    """)
    rows = db.execute(sql, params).fetchall()
    return [
        {
            "trade_id": int(r[0]),
            "user_id": r[1],
            "ticker": r[2],
            "direction": r[3],
            "quantity": float(r[4]) if r[4] is not None else None,
            "trade_status": r[5],
            "broker_source": r[6],
            "bracket_intent_id": int(r[7]) if r[7] is not None else None,
            "intent_state": r[8],
            "stop_price": float(r[9]) if r[9] is not None else None,
            "target_price": float(r[10]) if r[10] is not None else None,
        }
        for r in rows
    ]


def _write_reconciliation_row(
    db: Session,
    *,
    sweep_id: str,
    mode: str,
    local: LocalView,
    broker: BrokerView,
    decision: ReconciliationDecision,
) -> None:
    local_payload: dict[str, Any] = {
        "intent_state": local.intent_state,
        "stop_price": local.stop_price,
        "target_price": local.target_price,
        "quantity": local.quantity,
        "trade_status": local.trade_status,
    }
    broker_payload: dict[str, Any] = {
        "available": broker.available,
        "position_quantity": broker.position_quantity,
        "stop_order_id": broker.stop_order_id,
        "stop_order_state": broker.stop_order_state,
        "stop_order_price": broker.stop_order_price,
        "target_order_id": broker.target_order_id,
        "target_order_state": broker.target_order_state,
        "target_order_price": broker.target_order_price,
    }

    db.execute(text("""
        INSERT INTO trading_bracket_reconciliation_log (
            sweep_id, trade_id, bracket_intent_id, ticker, broker_source,
            kind, severity, local_payload, broker_payload, delta_payload,
            mode, observed_at
        ) VALUES (
            :sweep_id, :trade_id, :bracket_intent_id, :ticker, :broker_source,
            :kind, :severity,
            CAST(:local_payload AS JSONB),
            CAST(:broker_payload AS JSONB),
            CAST(:delta_payload AS JSONB),
            :mode, NOW()
        )
    """), {
        "sweep_id": sweep_id,
        "trade_id": local.trade_id,
        "bracket_intent_id": local.bracket_intent_id,
        "ticker": local.ticker,
        "broker_source": local.broker_source,
        "kind": decision.kind,
        "severity": decision.severity,
        "local_payload": _json_dumps(local_payload),
        "broker_payload": _json_dumps(broker_payload),
        "delta_payload": _json_dumps(decision.delta_payload),
        "mode": mode,
    })


# ── Diagnostics summary ────────────────────────────────────────────────


def bracket_reconciliation_summary(
    db: Session,
    *,
    lookback_hours: int = 24,
    recent_sweeps: int = 20,
) -> dict[str, Any]:
    """Frozen-shape summary for ``/brain/bracket-reconciliation/diagnostics``.

    Keys (stable):
        mode, lookback_hours, recent_sweeps_requested,
        rows_total, by_kind, by_severity,
        last_sweep_id, last_observed_at, sweeps_recent
    """
    mode = _effective_mode()

    rows = db.execute(text("""
        SELECT kind, COUNT(*)
        FROM trading_bracket_reconciliation_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY kind
    """), {"lh": int(lookback_hours)}).fetchall()
    by_kind = {r[0]: int(r[1]) for r in rows}
    rows_total = sum(by_kind.values())

    sev_rows = db.execute(text("""
        SELECT severity, COUNT(*)
        FROM trading_bracket_reconciliation_log
        WHERE observed_at >= (NOW() - (:lh || ' hours')::INTERVAL)
        GROUP BY severity
    """), {"lh": int(lookback_hours)}).fetchall()
    by_severity = {r[0]: int(r[1]) for r in sev_rows}

    last_sweep = db.execute(text("""
        SELECT sweep_id, MAX(observed_at)
        FROM trading_bracket_reconciliation_log
        GROUP BY sweep_id
        ORDER BY MAX(observed_at) DESC
        LIMIT 1
    """)).fetchone()

    last_sweep_id = last_sweep[0] if last_sweep else None
    last_observed_at = (
        last_sweep[1].isoformat() if last_sweep and last_sweep[1] else None
    )

    sweeps_rows = db.execute(text("""
        SELECT sweep_id, MAX(observed_at) AS ts, COUNT(*) AS rows
        FROM trading_bracket_reconciliation_log
        GROUP BY sweep_id
        ORDER BY MAX(observed_at) DESC
        LIMIT :lim
    """), {"lim": int(recent_sweeps)}).fetchall()
    sweeps_recent = [
        {
            "sweep_id": r[0],
            "observed_at": r[1].isoformat() if r[1] else None,
            "rows": int(r[2]),
        }
        for r in sweeps_rows
    ]

    return {
        "mode": mode,
        "lookback_hours": int(lookback_hours),
        "recent_sweeps_requested": int(recent_sweeps),
        "rows_total": rows_total,
        "by_kind": by_kind,
        "by_severity": by_severity,
        "last_sweep_id": last_sweep_id,
        "last_observed_at": last_observed_at,
        "sweeps_recent": sweeps_recent,
    }


def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, default=str, separators=(",", ":"))


@dataclass(frozen=True)
class WatchdogHit:
    """One flagged trade from ``run_missing_stop_watchdog``."""

    trade_id: int
    ticker: str | None
    broker_source: str | None
    kind: str                   # 'missing_stop' | 'orphan_stop' | 'never_observed'
    severity: str
    age_seconds: float
    last_observed_at: str | None
    alert_sent: bool
    alert_skip_reason: str | None = None


@dataclass(frozen=True)
class WatchdogSummary:
    checked_at: str
    enabled: bool
    stale_after_sec: int
    open_trades_scanned: int
    hits: list[WatchdogHit] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at,
            "enabled": self.enabled,
            "stale_after_sec": self.stale_after_sec,
            "open_trades_scanned": self.open_trades_scanned,
            "hits": [h.__dict__ for h in self.hits],
        }


def _watchdog_enabled() -> bool:
    return bool(getattr(settings, "chili_bracket_watchdog_enabled", False))


def _watchdog_stale_after_sec() -> int:
    raw = getattr(settings, "chili_bracket_watchdog_stale_after_sec", 300) or 300
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 300
    return max(30, n)


def run_missing_stop_watchdog(
    db: Session,
    *,
    user_id: int | None = None,
    stale_after_sec: int | None = None,
    enabled_override: bool | None = None,
    alert_dispatcher: Any = None,
) -> WatchdogSummary:
    """P0.5 — scan open live trades and alert on stale unprotected positions.

    For each open live trade with a ``BracketIntent``:

    1. If no reconciliation row has been written in the lookback window,
       classify as ``never_observed`` (crash-recovery signal — the sweep
       hasn't run or crashed before reaching this trade).
    2. Else, look at the *most recent* reconciliation decision for the
       trade. If it's ``missing_stop`` or ``orphan_stop`` *and* the
       ``observed_at`` is older than ``stale_after_sec``, the position is
       considered unprotected; fire an alert.

    Alerts are routed through :func:`alerts.dispatch_alert` (rate-limited
    per ticker by that module). ``alert_dispatcher`` lets tests inject a
    spy without touching the real alert path.

    Returns a :class:`WatchdogSummary`. The watchdog is read-only.
    """
    from datetime import datetime as _dt

    enabled = enabled_override if enabled_override is not None else _watchdog_enabled()
    stale_sec = int(stale_after_sec) if stale_after_sec is not None else _watchdog_stale_after_sec()
    checked_at = _dt.utcnow().isoformat()

    if not enabled:
        return WatchdogSummary(
            checked_at=checked_at,
            enabled=False,
            stale_after_sec=stale_sec,
            open_trades_scanned=0,
            hits=[],
        )

    # Latest reconciliation row per trade within a generous lookback,
    # joined to the live open-trade set. Paper trades are excluded
    # (``broker_source IS NOT NULL``) for the same reason as the sweep.
    params: dict[str, Any] = {"stale_sec": int(stale_sec)}
    user_filter = ""
    if user_id is not None:
        user_filter = " AND t.user_id = :uid"
        params["uid"] = int(user_id)

    sql = text(f"""
        WITH last_rec AS (
            SELECT DISTINCT ON (trade_id)
                trade_id, kind, severity, observed_at
            FROM trading_bracket_reconciliation_log
            WHERE observed_at >= (NOW() - INTERVAL '24 hours')
            ORDER BY trade_id, observed_at DESC
        )
        SELECT
            t.id AS trade_id,
            t.ticker,
            t.broker_source,
            bi.id AS bracket_intent_id,
            bi.last_observed_at,
            r.kind,
            r.severity,
            r.observed_at,
            EXTRACT(EPOCH FROM (NOW() - COALESCE(r.observed_at, bi.created_at))) AS age_sec
        FROM trading_trades AS t
        JOIN trading_bracket_intents AS bi ON bi.trade_id = t.id
        LEFT JOIN last_rec AS r ON r.trade_id = t.id
        WHERE t.status = 'open'
          AND t.broker_source IS NOT NULL
          AND bi.intent_state NOT IN ('reconciled', 'authoritative_closed')
          {user_filter}
        ORDER BY t.id
    """)
    rows = db.execute(sql, params).fetchall()

    hits: list[WatchdogHit] = []
    for row in rows:
        trade_id = int(row[0])
        ticker = row[1]
        broker_source = row[2]
        last_observed_at = row[4]
        kind = row[5]
        severity = row[6]
        observed_at = row[7]
        age_sec = float(row[8]) if row[8] is not None else 0.0

        hit_kind: str | None = None
        hit_severity: str = severity or "warn"
        if kind is None:
            # No recent classification at all — reconciler hasn't reached
            # this intent. Only a hit once the age crosses the threshold.
            if age_sec >= stale_sec:
                hit_kind = "never_observed"
                hit_severity = "error"
        elif kind in ("missing_stop", "orphan_stop"):
            if age_sec >= stale_sec:
                hit_kind = kind
        if hit_kind is None:
            continue

        alert_sent = False
        alert_skip_reason: str | None = None
        try:
            dispatcher = alert_dispatcher
            if dispatcher is None:
                from .alerts import dispatch_alert as dispatcher  # type: ignore
            message = (
                f"{BRACKET_WATCHDOG} {hit_kind} on {ticker or '?'} "
                f"(trade_id={trade_id}, age={int(age_sec)}s, "
                f"severity={hit_severity})"
            )
            alert_sent = bool(
                dispatcher(
                    db=db,
                    user_id=None,
                    alert_type=f"bracket_watchdog_{hit_kind}",
                    ticker=ticker,
                    message=message,
                    skip_throttle=False,
                )
            )
            if not alert_sent:
                alert_skip_reason = "throttled_or_log_only"
        except Exception as exc:  # pragma: no cover - defensive
            alert_skip_reason = f"dispatch_error:{type(exc).__name__}"

        hits.append(WatchdogHit(
            trade_id=trade_id,
            ticker=ticker,
            broker_source=broker_source,
            kind=hit_kind,
            severity=hit_severity,
            age_seconds=age_sec,
            last_observed_at=(
                observed_at.isoformat() if hasattr(observed_at, "isoformat") else (
                    last_observed_at.isoformat()
                    if hasattr(last_observed_at, "isoformat") else None
                )
            ),
            alert_sent=alert_sent,
            alert_skip_reason=alert_skip_reason,
        ))

    return WatchdogSummary(
        checked_at=checked_at,
        enabled=True,
        stale_after_sec=stale_sec,
        open_trades_scanned=len(rows),
        hits=hits,
    )


__all__ = [
    "SweepBatch",
    "SweepSummary",
    "WatchdogHit",
    "WatchdogSummary",
    "bracket_reconciliation_summary",
    "run_missing_stop_watchdog",
    "run_reconciliation_sweep",
]
