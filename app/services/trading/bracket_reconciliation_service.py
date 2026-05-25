"""Phase G - periodic reconciliation sweep (read-only, shadow-safe).

Reads open ``Trade`` rows + their ``BracketIntent`` rows, asks the
broker for its view via an injectable ``broker_view_fn``, classifies
each (trade, broker) pair through ``bracket_reconciler.classify_discrepancy``,
and persists one ``BracketReconciliationLog`` row per comparison.

This service is **strictly read-only against the broker** in Phase G.
It never submits, cancels, or modifies any broker order. Running with
``brain_live_brackets_mode=authoritative`` falls back to shadow unless
the Phase G.2 sweep-writer flag is explicitly enabled.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from ...trading_brain.infrastructure.bracket_reconciliation_ops_log import (
    format_bracket_reconciliation_ops_line,
)
from .bracket_intent_writer import (
    bump_last_observed,
    mark_auto_reconciled_after_terminal_reject,
    mark_reconciled,
    sync_broker_stop_order_id_mirror,
)
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

# audit-missing-stop-emergency-repair (2026-05-03):
# Per-intent throttle for the emergency-repair branch in
# _invoke_writer_for_decision. The branch fires at most once per
# CHILI_BRACKET_TERMINAL_REJECT_REPAIR_THROTTLE_SECONDS per intent.
# Why 6h: long enough that a misbehaving intent doesn't churn against
# a broker-side problem (Robinhood was rate-limiting the SELL_STOP
# rejection storm at ~2-min sweep cadence); short enough that if the
# operator manually re-arms the broker stop, the next sweep within
# that day will reconcile it. Operator override:
# CHILI_BRACKET_TERMINAL_REJECT_REPAIR_THROTTLE_SECONDS env var
# (read at module import, not per-call -- consistent with other
# bracket throttles).
import os as _os
EMERGENCY_REPAIR_THROTTLE_SECONDS: float = float(
    _os.environ.get(
        "CHILI_BRACKET_TERMINAL_REJECT_REPAIR_THROTTLE_SECONDS",
        str(6 * 3600),
    )
)
# broker-truth-self-heal (2026-05-04): the prior flap-guard machinery
# (EMERGENCY_REPAIR_PHANTOM_CLOSE_MIN_CONSECUTIVE_ZERO_SWEEPS constant +
# _bump_phantom_close_zero_qty_counter helper + sub-branch 2 logic) was
# retired with the deletion of sub-branch 2. The R32 wholesale empty-
# positions guard at ``broker_service.sync_positions_to_db`` already
# owns "broker says position gone"; per-intent zero-qty closure here was
# a redundant + less-protected duplicate. The orphan column
# ``phantom_close_consecutive_zero_qty_sweeps`` (migration 223) sits at
# 0 forever; schema removal is a separate hygiene ticket.
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


def _is_working_stop_order_state(state: Optional[str]) -> bool:
    if not state:
        return False
    s = str(state).lower()
    return s in (
        "confirmed", "queued", "open", "active", "working", "pending",
        "submitted", "accepted", "partially_filled", "unconfirmed",
    )


def broker_manager_view_fn(local_rows: list[dict[str, Any]]) -> list[BrokerView]:
    """Phase G.2 broker provider: positions + working stop orders.

    Reads combined positions via ``broker_manager.get_combined_positions``
    AND open broker orders to surface resting SELL stop orders. The
    classifier needs ``stop_order_id`` populated for any open order whose
    ``trigger`` is ``stop`` and whose state is still working — otherwise
    every sweep classifies the trade as ``missing_stop`` and the writer
    re-submits a duplicate (the duplicate-submission loop bit us on
    2026-04-30 17:42-17:46 UTC: writer placed real stop 69f3947a, then
    every minute re-classified as missing_stop because this fn returned
    stop_order_id=None).

    Positions we cannot reach are flagged ``available=False`` so the
    reconciler emits ``broker_down``.
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

    # Index positions by (ticker, broker_source).
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for p in positions:
        tkr = (p.get("ticker") or p.get("symbol") or "").upper() or None
        src = p.get("broker_source")
        if not tkr or not src:
            continue
        by_key[(tkr, src)] = p

    # Phase G.2: pull working broker stop orders so the classifier can
    # see the stops the writer (or anything else) has placed. Fail-soft:
    # if the fetch raises, we treat as "no stops known" — that's the same
    # state as Phase G, just less precise.
    rh_stops_by_ticker: dict[str, dict[str, Any]] = {}
    try:
        # Only Robinhood has a working stop-order surface today; Coinbase
        # support comes when bracket_writer_g2 adds Coinbase venue.
        wanted_rh = any(
            (r.get("broker_source") or "").lower() == "robinhood"
            for r in local_rows
        )
        if wanted_rh:
            # Phase 3.2 (2026-05-01): broker SDK is encapsulated in
            # broker_service. The CI guard in
            # tests/test_no_raw_broker_sdk_imports.py forbids new
            # ``import robin_stocks`` outside the adapter set.
            from .. import broker_service as _bs
            raw_orders = _bs.get_open_stock_orders()
            for od in raw_orders:
                if not isinstance(od, dict):
                    continue
                if str(od.get("side", "")).lower() != "sell":
                    continue
                if str(od.get("trigger", "")).lower() != "stop":
                    continue
                if not _is_working_stop_order_state(od.get("state")):
                    continue
                # Resolve the ticker for this order. RH orders carry an
                # instrument URL, not a symbol — try common shapes.
                tkr = (
                    od.get("symbol")
                    or od.get("chain_symbol")
                    or ""
                ).upper() or None
                if not tkr:
                    inst_url = od.get("instrument") or od.get("instrument_url")
                    if inst_url:
                        inst = _bs.get_instrument_by_url(inst_url)
                        if isinstance(inst, dict):
                            tkr = (inst.get("symbol") or "").upper() or None
                if not tkr:
                    continue
                # If the same ticker has multiple working stops, prefer the
                # most recently created — the latest is the writer's most
                # recent intent.
                prior = rh_stops_by_ticker.get(tkr)
                if prior is None or (
                    str(od.get("created_at") or "") > str(prior.get("created_at") or "")
                ):
                    rh_stops_by_ticker[tkr] = od
    except Exception:
        logger.warning(
            f"{BRACKET_RECONCILIATION} broker_manager_view_fn: stop-order "
            "fetch failed (rh.orders.get_all_open_stock_orders); "
            "stop_order_id will stay None for this sweep",
            exc_info=True,
        )
        rh_stops_by_ticker = {}

    cb_stops_by_ticker: dict[str, Any] = {}
    cb_stop_qty_by_ticker: dict[str, float] = {}
    try:
        wanted_cb = any(
            (r.get("broker_source") or "").lower() == "coinbase"
            for r in local_rows
        )
        if wanted_cb:
            from .venue.factory import get_adapter

            adapter = get_adapter("coinbase")
            if adapter is not None:
                raw_orders, _fresh = adapter.list_open_orders(
                    product_id=None, limit=250,
                )
                for od in raw_orders or []:
                    side = str(getattr(od, "side", "") or "").lower()
                    order_type = str(getattr(od, "order_type", "") or "").upper()
                    state = str(getattr(od, "status", "") or "").lower()
                    product_id = str(getattr(od, "product_id", "") or "").upper()
                    if side != "sell" or "STOP" not in order_type:
                        continue
                    if not product_id or not _is_working_stop_order_state(state):
                        continue
                    qty = 0.0
                    raw = getattr(od, "raw", None) or {}
                    cfg = raw.get("order_configuration") if isinstance(raw, dict) else None
                    if isinstance(cfg, dict):
                        for inner in cfg.values():
                            if isinstance(inner, dict):
                                value = inner.get("base_size")
                                if value not in (None, ""):
                                    try:
                                        qty = float(value)
                                    except Exception:
                                        qty = 0.0
                                    break
                            if qty > 0:
                                break
                    if qty <= 0 and isinstance(raw, dict):
                        value = raw.get("outstanding_hold_amount")
                        if value not in (None, ""):
                            try:
                                qty = float(value)
                            except Exception:
                                qty = 0.0
                    cb_stop_qty_by_ticker[product_id] = (
                        cb_stop_qty_by_ticker.get(product_id, 0.0) + max(0.0, qty)
                    )
                    prior = cb_stops_by_ticker.get(product_id)
                    if prior is None or (
                        str(getattr(od, "created_time", "") or "")
                        > str(getattr(prior, "created_time", "") or "")
                    ):
                        cb_stops_by_ticker[product_id] = od
    except Exception:
        logger.warning(
            f"{BRACKET_RECONCILIATION} broker_manager_view_fn: Coinbase "
            "stop-order fetch failed; stop_order_id will stay None for "
            "Coinbase this sweep",
            exc_info=True,
        )
        cb_stops_by_ticker = {}
        cb_stop_qty_by_ticker = {}

    def _coinbase_stop_price(order: Any) -> Optional[float]:
        raw = getattr(order, "raw", None) or {}
        cfg = raw.get("order_configuration") if isinstance(raw, dict) else None
        if isinstance(cfg, dict):
            for inner in cfg.values():
                if not isinstance(inner, dict):
                    continue
                for key in ("stop_price", "trigger_price"):
                    value = inner.get(key)
                    if value not in (None, ""):
                        try:
                            return float(value)
                        except Exception:
                            pass
        return None

    def _stop_meta_for(tkr: Optional[str], src: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[float]]:
        if not tkr:
            return None, None, None
        src_l = (src or "").lower()
        if src_l == "coinbase":
            od = cb_stops_by_ticker.get(tkr.upper())
            if not od:
                return None, None, None
            return (
                getattr(od, "order_id", None),
                getattr(od, "status", None),
                _coinbase_stop_price(od),
            )
        if src_l != "robinhood":
            return None, None, None
        od = rh_stops_by_ticker.get(tkr.upper())
        if not od:
            return None, None, None
        try:
            sp = od.get("stop_price")
            sp_f = float(sp) if sp is not None else None
        except Exception:
            sp_f = None
        return od.get("id"), od.get("state"), sp_f

    for row in local_rows:
        tkr = (row.get("ticker") or "").upper() or None
        src = row.get("broker_source")
        p = by_key.get((tkr, src)) if tkr and src else None
        stop_oid, stop_state, stop_price = _stop_meta_for(tkr, src)
        if p is None:
            if (src or "").lower() == "coinbase":
                # Coinbase account/position snapshots can be partial or
                # unavailable while open trades still exist. Position-sync is
                # the only layer allowed to decide a Coinbase position is
                # truly gone, and it now requires a confirming sell fill.
                # Treat this sweep as broker-down/unknown so mirror writes do
                # not clear cached stop ids and the writer does not place off
                # a false broker_qty=0.
                views.append(BrokerView(
                    available=False,
                    ticker=tkr,
                    broker_source=src,
                    position_quantity=None,
                    stop_order_id=stop_oid,
                    stop_order_state=stop_state,
                    stop_order_price=stop_price,
                ))
                continue
            views.append(BrokerView(
                available=True,
                ticker=tkr,
                broker_source=src,
                position_quantity=0.0,
                stop_order_id=stop_oid,
                stop_order_state=stop_state,
                stop_order_price=stop_price,
            ))
            continue
        qty = p.get("quantity") or p.get("qty") or p.get("shares") or 0
        try:
            qty_f = float(qty or 0)
        except Exception:
            qty_f = 0.0
        if (
            (src or "").lower() == "coinbase"
            and tkr
            and stop_oid is not None
            and cb_stop_qty_by_ticker.get(tkr, 0.0) < qty_f - 1e-9
        ):
            logger.info(
                f"{BRACKET_RECONCILIATION} Coinbase stop coverage partial "
                "ticker=%s broker_qty=%s stop_qty=%s; classifying as "
                "missing_stop so writer places uncovered remainder",
                tkr, qty_f, cb_stop_qty_by_ticker.get(tkr, 0.0),
            )
            stop_oid, stop_state, stop_price = None, None, None
        views.append(BrokerView(
            available=True,
            ticker=tkr,
            broker_source=src,
            position_quantity=qty_f,
            stop_order_id=stop_oid,
            stop_order_state=stop_state,
            stop_order_price=stop_price,
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
        pending_exit_status=row.get("pending_exit_status"),
        pending_exit_reason=row.get("pending_exit_reason"),
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


# ── Stage 1.5: backfill_missing_intents ────────────────────────────────
# f-coinbase-bracket-coverage-fix (2026-05-10) — Bug B safety net.


def _stage_backfill_missing_intents(
    db: Session,
    local_rows_or_views: list[Any],
    *,
    mode: str,
) -> int:
    """Create ``trading_bracket_intents`` rows for open live trades that
    have a ``stop_loss > 0`` but no intent row.

    Why this exists:
      Pre-Bug-A, ``stop_engine._maybe_emit_bracket_intent`` was only
      called on alert events. A freshly-entered Coinbase trade whose
      price had not yet approached the stop never produced an intent
      row, so the writer-invocation gate at
      ``_invoke_writer_for_decision`` (which requires a
      bracket_intent_id) silently returned None and the position
      remained unprotected at the venue. Bug A fixes the common path;
      this stage is the safety net for trades created via code paths
      that don't run through stop_engine, or that were persisted
      before Bug A landed.

    Behavior:
      * Mode-gated by ``brain_live_brackets_mode != 'off'`` (same gate
        as ``_maybe_emit_bracket_intent``).
      * Operates on rows from ``_load_local_view`` (legacy loop) OR a
        list of ``LocalView`` (staged loop) — accessor adapts.
      * Skips paper trades (broker_source unset), non-open trades,
        rows that already have an intent, and trades with no
        ``stop_loss`` (None or <= 0). The no-magic-fallback rule
        applies: a trade without a stop_loss has no information for
        the brain to write, so we do nothing.
      * Idempotent on re-entry: ``upsert_bracket_intent`` rejects
        terminal/legacy-authoritative states and updates rather than
        inserts when a row already exists.

    Returns the number of intents *created* (not updated). A non-zero
    count is the caller's signal to re-load the local view so the
    rest of the sweep sees the new bracket_intent_id values.
    """
    bl_mode = (getattr(settings, "brain_live_brackets_mode", "off") or "off").lower()
    if bl_mode == "off":
        return 0

    # Local imports keep module-import cost low and avoid widening the
    # import surface for callers that don't trigger the backfill path.
    import json as _json
    from .bracket_intent import BracketIntentInput
    from .bracket_intent_writer import upsert_bracket_intent
    from ...models.trading import Trade as _Trade

    backfilled = 0
    for entry in local_rows_or_views:
        if isinstance(entry, dict):
            trade_id = entry.get("trade_id")
            bracket_intent_id = entry.get("bracket_intent_id")
            broker_source = entry.get("broker_source")
            trade_status = entry.get("trade_status")
        else:
            trade_id = getattr(entry, "trade_id", None)
            bracket_intent_id = getattr(entry, "bracket_intent_id", None)
            broker_source = getattr(entry, "broker_source", None)
            trade_status = getattr(entry, "trade_status", None)

        if trade_id is None:
            continue
        if bracket_intent_id is not None:
            continue
        if not broker_source:
            continue
        if (trade_status or "").lower() != "open":
            continue

        try:
            trade = db.get(_Trade, int(trade_id))
            if trade is None:
                continue
            sl = getattr(trade, "stop_loss", None)
            if sl is None or float(sl) <= 0:
                continue

            atr_val = None
            try:
                snapshot = getattr(trade, "indicator_snapshot", None)
                if isinstance(snapshot, str) and snapshot:
                    snap = _json.loads(snapshot)
                    if isinstance(snap, dict):
                        atr_val = snap.get("atr") or snap.get("ATR")
                elif isinstance(snapshot, dict):
                    atr_val = snapshot.get("atr") or snapshot.get("ATR")
            except Exception:
                atr_val = None

            bracket_input = BracketIntentInput(
                ticker=trade.ticker,
                direction=(trade.direction or "long").lower(),
                entry_price=float(trade.entry_price or 0.0),
                quantity=float(trade.quantity or 0.0),
                atr=float(atr_val) if atr_val else None,
                stop_model=getattr(trade, "stop_model", None),
                pattern_id=getattr(trade, "scan_pattern_id", None),
                lifecycle_stage=None,
                regime="cautious",
                pattern_win_rate=None,
                pattern_name=None,
            )
            upsert_result = upsert_bracket_intent(
                db,
                trade_id=int(trade.id),
                user_id=getattr(trade, "user_id", None),
                bracket_input=bracket_input,
                broker_source=str(trade.broker_source),
            )
            if upsert_result is not None and upsert_result.created:
                backfilled += 1
                logger.info(
                    f"{BRACKET_RECONCILIATION} backfill_intent "
                    "trade=%s ticker=%s broker=%s intent_id=%s stop_price=%s "
                    "(mode=%s, sweep-time backfill — bug B safety net)",
                    trade.id, trade.ticker, trade.broker_source,
                    upsert_result.intent_id, upsert_result.stop_price, mode,
                )
        except Exception:
            logger.warning(
                f"{BRACKET_RECONCILIATION} backfill_intent failed for trade %s",
                trade_id, exc_info=True,
            )
    return backfilled


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


# ── bracket-intent-stale-label-cleanup (2026-05-03) ────────────────────
# Sweep-loop hook applied AFTER the classifier returns and BEFORE
# _invoke_writer_for_decision runs. Two effects when the flag is ON:
#   (1) Mirror BrokerView.stop_order_id into the local advisory cache
#       column. The local column is NEVER read by decision-time code
#       (test #8 enforces this); it exists for admin/audit visibility.
#   (2) Auto-transition intent_state='terminal_reject' → 'reconciled'
#       when the classifier reports kind=agree on a subsequent sweep.
#       Closes the stale-label loop seen on AIDX/CCCC/CRDL/IMTX/TLS/VFS
#       on 2026-05-03 where broker truth had reconciled but local rows
#       were stuck due to the state-machine's terminal_reject cooldown.
#
# Both effects are silent (return None) when:
#   - feature flag is OFF (chili_bracket_intent_mirror_enabled)
#   - broker.available is False (no observation, no mirror write)
#   - bracket_intent_id is None (orphan trade row, nothing to mirror)


def _apply_intent_mirror_writes(
    db: Session,
    *,
    sweep_id: str,
    mode: str,
    local: LocalView,
    broker: BrokerView,
    decision: ReconciliationDecision,
) -> None:
    """Mirror broker stop-order id and auto-transition stale terminal_reject
    rows when the classifier subsequently reports kind=agree.

    Failure-tolerant: any helper exception is logged at debug and the
    sweep continues; mirror writes are advisory and an auto-transition
    rejection just leaves the row in its prior state for the next sweep.
    """
    if not getattr(settings, "chili_bracket_intent_mirror_enabled", False):
        return
    if local.bracket_intent_id is None:
        return
    if not broker.available:
        return

    intent_id = int(local.bracket_intent_id)
    ticker = local.ticker or "?"
    broker_source = local.broker_source or "?"
    broker_stop_order_id = broker.stop_order_id

    # ── Mirror write ─────────────────────────────────────────────────
    try:
        changed, prev_value = sync_broker_stop_order_id_mirror(
            db,
            intent_id,
            broker_value=broker_stop_order_id,
        )
        if changed:
            if broker_stop_order_id:
                logger.info(
                    f"{BRACKET_RECONCILIATION} mirror_write trade=%s intent=%s "
                    f"ticker=%s old=%s new=%s",
                    local.trade_id, intent_id, ticker,
                    prev_value or "NULL", broker_stop_order_id,
                )
            else:
                logger.info(
                    f"{BRACKET_RECONCILIATION} mirror_clear trade=%s intent=%s "
                    f"ticker=%s old=%s",
                    local.trade_id, intent_id, ticker,
                    prev_value or "NULL",
                )
    except Exception:  # pragma: no cover - defensive
        logger.debug(
            f"{BRACKET_RECONCILIATION} mirror sync failed for intent %s",
            intent_id, exc_info=True,
        )

    # ── Auto-transition: terminal_reject → reconciled on kind=agree ──
    if decision.kind != "agree":
        return
    if (local.intent_state or "").lower() != "terminal_reject":
        return

    try:
        transitioned = mark_auto_reconciled_after_terminal_reject(db, intent_id)
    except Exception:  # pragma: no cover - defensive
        logger.debug(
            f"{BRACKET_RECONCILIATION} auto-reconcile failed for intent %s",
            intent_id, exc_info=True,
        )
        return

    if not transitioned:
        # Idempotent path: row was no longer in terminal_reject (concurrent
        # sweep already moved it). Quiet no-op.
        return

    logger.critical(
        f"{BRACKET_RECONCILIATION} auto_reconcile trade=%s intent=%s ticker=%s "
        f"from=terminal_reject to=reconciled "
        f"(broker subsequently agrees; clearing stale label)",
        local.trade_id, intent_id, ticker,
    )

    try:
        from .bracket_writer_g2 import _g2_event
        _g2_event(
            db,
            trade_id=int(local.trade_id) if local.trade_id is not None else 0,
            bracket_intent_id=intent_id,
            ticker=ticker,
            broker_source=broker_source,
            event_type="bracket_writer_action",
            status="auto_reconcile_terminal_reject",
            decision_kind=decision.kind,
            decision_severity=decision.severity,
            extra={
                "writer": "auto_reconcile_terminal_reject",
                "sweep_id": sweep_id,
                "mode": mode,
                "broker_stop_order_id": broker_stop_order_id,
            },
        )
    except Exception:  # pragma: no cover - defensive audit
        logger.debug(
            f"{BRACKET_RECONCILIATION} auto-reconcile audit emit failed for intent %s",
            intent_id, exc_info=True,
        )

    # bracket-writer-respect-upside-targets (2026-05-04) Step 5:
    # cancelled-limit replacement viability evaluator. After a position's
    # covering limit-sell was cancelled (typically by the prior auto-cancel
    # behavior that this task retired), the position has a SELL_STOP at
    # the broker but no profit target. The brain may still consider the
    # original target viable (above current price). One-shot per intent --
    # gated by "no pending_decision exists yet" + intent_state not in
    # accepted-states. The helper itself returns None when not viable, so
    # this is a quiet no-op for positions that don't qualify.
    _try_cancelled_limit_replacement_eval(
        db, local=local, broker=broker, decision=decision,
    )


def _try_cancelled_limit_replacement_eval(
    db: Session,
    *,
    local: LocalView,
    broker: BrokerView,
    decision: ReconciliationDecision,
) -> None:
    """Wedge for the Step 5 viability evaluator. Quiet no-op for any
    intent that doesn't structurally qualify."""
    try:
        if local.bracket_intent_id is None or local.trade_id is None:
            return
        if (local.trade_status or "").lower() != "open":
            return
        if (local.intent_state or "").lower() in ("closed", "accepted_no_stop"):
            return
        if decision.kind != "agree":
            return
        # Only fire when the broker has a working stop. The cancelled-
        # limit replacement candidate is "you have downside coverage
        # but no upside target" -- broker stop must be alive.
        from .bracket_reconciler import _is_working_state
        if not _is_working_state(broker.stop_order_state):
            return
        # If broker already has a working target, no candidate to surface.
        if _is_working_state(broker.target_order_state):
            return
        # Skip if a pending_decision is already present.
        row = db.execute(
            text(
                "SELECT payload_json FROM trading_bracket_intents WHERE id=:iid"
            ),
            {"iid": int(local.bracket_intent_id)},
        ).first()
        if row and row[0] and isinstance(row[0], dict):
            if row[0].get("pending_decision") is not None:
                return
        # Need entry_price + quantity from the trade row.
        from ...models.trading import Trade as _Trade
        t = db.get(_Trade, int(local.trade_id))
        if t is None:
            return
        entry = float(t.entry_price or 0.0)
        if entry <= 0:
            return
        qty = float(local.quantity or t.quantity or 0.0)
        if qty <= 0:
            return

        from .bracket_writer_g2 import evaluate_target_replacement
        evaluate_target_replacement(
            db,
            bracket_intent_id=int(local.bracket_intent_id),
            trade_id=int(local.trade_id),
            ticker=str(local.ticker or ""),
            broker_source=str(local.broker_source or ""),
            entry_price=entry,
            quantity=qty,
            direction=str(local.direction or "long"),
            stop_model=getattr(t, "stop_model", None),
        )
    except Exception:
        logger.debug(
            f"{BRACKET_RECONCILIATION} cancelled_limit_replacement_eval failed "
            "intent=%s", local.bracket_intent_id, exc_info=True,
        )


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

        # bracket-intent-stale-label-cleanup hook (gated, no-op when flag OFF).
        # Runs BEFORE _invoke_writer_for_decision so the auto-transition can
        # land (terminal_reject → reconciled) before the next branch reads
        # intent_state on the same sweep.
        _apply_intent_mirror_writes(
            db,
            sweep_id=batch.sweep_id,
            mode=batch.mode,
            local=lv,
            broker=bv,
            decision=decision,
        )

        # Phase G.2 writer hook (Round 23). No-op unless mode=authoritative.
        writer_res = _invoke_writer_for_decision(
            db,
            mode=batch.mode,
            sweep_id=batch.sweep_id,
            local=lv,
            broker=bv,
            decision=decision,
        )
        if writer_res is not None and _ops_log_enabled():
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="writer_action",
                    mode=batch.mode,
                    sweep_id=batch.sweep_id,
                    trade_id=lv.trade_id,
                    bracket_intent_id=lv.bracket_intent_id,
                    ticker=lv.ticker,
                    broker_source=lv.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                    writer=writer_res.get("writer"),
                    ok=writer_res.get("ok"),
                    reason=writer_res.get("reason"),
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


# ── Phase G.2 writer-invocation hook (Round 23) ───────────────────────


def _try_emergency_repair_terminal_reject(
    db: Session,
    *,
    local: LocalView,
    broker: BrokerView,
    decision: ReconciliationDecision,
    sweep_id: str,
) -> dict[str, Any] | None:
    """audit-missing-stop-emergency-repair (2026-05-03).

    Escape-valve for the bracket reconciler when intent_state is
    parked at ``terminal_reject`` AND the trade is still open AND
    decision.kind == ``missing_stop``. The caller must verify those
    pre-conditions plus the feature flag.

    Returns:
      * dict (writer-action shape) when this branch handled the
        intent — caller should treat as the final outcome.
      * None when this branch deliberately skipped (broker unavailable
        or throttle active) and the caller should fall through to the
        existing ``state_gated_skip``.

    Three actionable sub-branches:

      1. ``broker.available is False`` or ``broker_qty is None`` ->
         skip silently this sweep, retry next. No throttle bump
         (this isn't an attempted repair, it's an absence of broker
         data). Returns None.

      2. ``broker_qty == 0`` -> phantom open trade. Mark the trade
         ``status='closed'`` with ``closed_reason='phantom_after_
         terminal_reject'`` and the bracket intent ``intent_state=
         'closed'`` with last_diff_reason='phantom'. Bump throttle.
         Emit CRITICAL log + audit event. Returns the writer dict.

      3. ``broker_qty > 0`` -> real exposure. Throttle-check; if
         active, return None (fall through to state_gated_skip). If
         not throttled, call FIX-51 ``place_missing_stop`` with
         min(local_qty, broker_qty). Bump throttle either way. Emit
         CRITICAL log on entry; emit success/rejection log + audit
         event on completion. Returns the writer dict.

    Any unexpected exception is caught and converts to a structured
    failure dict (caller receives an outcome row, not a raised
    exception that escapes to the sweep loop).
    """
    try:
        # Sub-branch 1: broker availability gate. Mirrors FIX-51.
        if broker is None or not broker.available:
            logger.info(
                f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR SKIPPED "
                "trade=%s intent=%s ticker=%s reason=broker_unavailable",
                local.trade_id, local.bracket_intent_id, local.ticker,
            )
            return None  # fall through to state_gated_skip
        if broker.position_quantity is None:
            logger.info(
                f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR SKIPPED "
                "trade=%s intent=%s ticker=%s reason=broker_qty_unknown",
                local.trade_id, local.bracket_intent_id, local.ticker,
            )
            return None  # fall through

        broker_qty = float(broker.position_quantity)

        # broker-truth-self-heal (2026-05-04): retired sub-branch 2.
        # The "broker reports zero qty -> phantom-close locally" path
        # is owned upstream by ``broker_service.sync_positions_to_db``
        # (with R32 wholesale empty-positions guard). This per-intent
        # close path was a redundant + less-protected duplicate that
        # introduced the 2026-05-04 09:44 cascade (5 trades closed in
        # 370ms while broker still held the positions). The wholesale
        # path observes the WHOLE account at once and refuses to mass-
        # close on a single suspicious snapshot; per-intent closes here
        # had no such guard. Falling through to state_gated_skip means
        # the intent stays parked at terminal_reject and the operator
        # (or the inverse-reconcile path in sync_positions_to_db) owns
        # the next move.
        if broker_qty <= 0.0:
            return None  # fall through to state_gated_skip

        # Sub-branch 3: real exposure. Check throttle first.
        last_attempt = _fetch_last_repair_attempt(db, int(local.bracket_intent_id))
        now_ts = _utc_now_naive()
        if last_attempt is not None:
            elapsed = (now_ts - last_attempt).total_seconds()
            if elapsed < EMERGENCY_REPAIR_THROTTLE_SECONDS:
                logger.info(
                    f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR THROTTLED "
                    "trade=%s intent=%s ticker=%s elapsed=%.0fs throttle=%.0fs",
                    local.trade_id, local.bracket_intent_id, local.ticker,
                    elapsed, EMERGENCY_REPAIR_THROTTLE_SECONDS,
                )
                return None  # fall through to state_gated_skip

        local_qty = float(local.quantity or 0.0)
        if local_qty <= 0.0 or local.stop_price is None:
            return {
                "writer": "emergency_terminal_reject_repair",
                "ok": False,
                "reason": "invalid_local_qty_or_stop_price",
                "new_stop_order_id": None,
                "qty": None,
                "stop_price": None,
            }
        placement_qty = min(local_qty, broker_qty)

        logger.critical(
            f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR trade=%s intent=%s "
            "ticker=%s broker_qty=%s local_qty=%s placing=%s stop=%s "
            "(intent was terminal_reject; attempting controlled repair)",
            local.trade_id, local.bracket_intent_id, local.ticker,
            broker_qty, local_qty, placement_qty, local.stop_price,
        )

        # Bump the throttle BEFORE placing so a crash mid-place doesn't
        # leave the throttle un-set (caller would get state_gated_skip
        # next sweep, which is the safer failure mode).
        _bump_repair_attempt(db, int(local.bracket_intent_id), now_ts)

        from .bracket_writer_g2 import place_missing_stop, _g2_event
        action = place_missing_stop(
            db,
            trade_id=int(local.trade_id),
            bracket_intent_id=int(local.bracket_intent_id),
            ticker=str(local.ticker or ""),
            broker_source=str(local.broker_source or ""),
            decision=decision,
            local_quantity=float(placement_qty),
            stop_price=float(local.stop_price),
        )
        log_fn = logger.critical if action.ok else logger.error
        log_fn(
            f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR result trade=%s "
            "intent=%s ticker=%s ok=%s reason=%s new_stop_order_id=%s "
            "qty=%s stop_price=%s",
            local.trade_id, local.bracket_intent_id, local.ticker,
            action.ok, action.reason, action.new_stop_order_id,
            action.new_stop_qty, action.new_stop_price,
        )
        try:
            _g2_event(
                db,
                trade_id=int(local.trade_id),
                bracket_intent_id=int(local.bracket_intent_id),
                ticker=str(local.ticker or ""),
                broker_source=str(local.broker_source or ""),
                event_type="emergency_terminal_reject_repair",
                status="success" if action.ok else "rejection_relock",
                new_stop_order_id=action.new_stop_order_id,
                qty=action.new_stop_qty,
                stop_price=action.new_stop_price,
                error=None if action.ok else action.reason,
                decision_kind=str(decision.kind),
                decision_severity=str(decision.severity),
                extra={
                    "sweep_id": sweep_id,
                    "broker_qty": broker_qty,
                    "local_qty": local_qty,
                    "placement_qty": placement_qty,
                },
            )
        except Exception:
            logger.warning(
                f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR audit emit failed "
                "trade=%s",
                local.trade_id, exc_info=True,
            )
        return {
            "writer": "emergency_terminal_reject_repair",
            "ok": bool(action.ok),
            "reason": action.reason,
            "new_stop_order_id": action.new_stop_order_id,
            "qty": action.new_stop_qty,
            "stop_price": action.new_stop_price,
        }
    except Exception:
        logger.exception(
            f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR unexpected error "
            "trade=%s intent=%s ticker=%s",
            local.trade_id, local.bracket_intent_id, local.ticker,
        )
        return {
            "writer": "emergency_terminal_reject_repair",
            "ok": False,
            "reason": "unexpected_error",
            "new_stop_order_id": None,
            "qty": None,
            "stop_price": None,
        }


def _utc_now_naive():
    """Naive UTC for timestamp columns in this module (DB column type
    is ``timestamp without time zone``)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fetch_last_repair_attempt(db: Session, bracket_intent_id: int):
    """Read the throttle timestamp; None if never attempted or column
    missing (older schema before migration 222)."""
    try:
        row = db.execute(
            text(
                "SELECT terminal_reject_repair_last_attempt_at "
                "FROM trading_bracket_intents WHERE id=:iid"
            ),
            {"iid": int(bracket_intent_id)},
        ).first()
    except Exception:
        return None
    if row is None or row[0] is None:
        return None
    return row[0]


def _bump_repair_attempt(db: Session, bracket_intent_id: int, ts) -> None:
    """Persist the throttle timestamp. Failures swallow + log so the
    calling repair attempt isn't aborted by an audit-side issue.
    """
    try:
        db.execute(
            text(
                "UPDATE trading_bracket_intents "
                "SET terminal_reject_repair_last_attempt_at=:ts, "
                "    updated_at=:ts "
                "WHERE id=:iid"
            ),
            {"iid": int(bracket_intent_id), "ts": ts},
        )
        db.commit()
    except Exception:
        logger.warning(
            f"{BRACKET_RECONCILIATION} EMERGENCY-REPAIR throttle bump "
            "failed intent=%s", bracket_intent_id, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass


def _resolve_pending_bracket_decision(
    db: Session,
    *,
    local: LocalView,
    broker: BrokerView,
    sweep_id: str,
) -> dict[str, Any] | None:
    """bracket-writer-respect-upside-targets (2026-05-04) -- route
    pending-decision rows to the operator's chosen resolution.

    Returns a writer-action dict if a pending_decision was present
    (resolved or deferred); ``None`` otherwise (no pending_decision, or
    resolution not applicable -- caller continues normal writer flow).

    No magic numbers. Brain target/stop come from the persisted
    pending_decision JSON (already computed by the writer when the
    decision was surfaced); current price from the same JSON. Any
    threshold tolerances reuse the writer's existing 1e-9 float-equality
    convention.
    """
    try:
        row = db.execute(
            text(
                "SELECT payload_json FROM trading_bracket_intents "
                "WHERE id = :iid"
            ),
            {"iid": int(local.bracket_intent_id)},
        ).first()
    except Exception:
        return None
    if row is None or row[0] is None:
        return None
    payload = row[0]
    if not isinstance(payload, dict):
        return None
    pending = payload.get("pending_decision")
    if not isinstance(pending, dict):
        return None

    choice = pending.get("operator_choice")
    if not choice:
        # Deferred: operator has not yet chosen. Emit a discrepancy so
        # the audit funnel surfaces the wait state on every sweep.
        if _ops_log_enabled():
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="discrepancy",
                    mode="authoritative",
                    sweep_id=sweep_id,
                    trade_id=local.trade_id,
                    bracket_intent_id=local.bracket_intent_id,
                    ticker=local.ticker,
                    broker_source=local.broker_source,
                    kind="pending_operator_decision",
                    severity="warn",
                )
            )
        return {
            "writer": "pending_decision_deferred",
            "ok": False,
            "reason": "awaiting_operator_choice",
            "new_stop_order_id": None,
            "qty": None,
            "stop_price": None,
        }

    brain_state = pending.get("brain_state") or {}
    broker_state = pending.get("broker_state") or {}

    if choice == "keep_target":
        _clear_pending_decision_and_set_state(
            db, int(local.bracket_intent_id),
            new_intent_state="reconciled",
            last_diff_reason="pending_decision_resolved:keep_target",
        )
        logger.warning(
            f"{BRACKET_RECONCILIATION} pending-decision RESOLVED "
            "trade=%s intent=%s ticker=%s choice=keep_target "
            "(operator accepted no-downside-stop; no broker action)",
            local.trade_id, local.bracket_intent_id, local.ticker,
        )
        return {
            "writer": "pending_decision_resolved",
            "ok": True,
            "reason": "keep_target",
            "new_stop_order_id": None,
            "qty": None,
            "stop_price": None,
        }

    if choice == "replace_with_stop":
        brain_stop = brain_state.get("stop_price")
        if brain_stop is None or float(brain_stop) <= 0:
            logger.error(
                f"{BRACKET_RECONCILIATION} pending-decision replace_with_stop "
                "BLOCKED trade=%s intent=%s ticker=%s reason=brain_stop_missing "
                "(operator chose replace but brain target/stop unavailable; "
                "leaving pending_decision in place for operator to revise)",
                local.trade_id, local.bracket_intent_id, local.ticker,
            )
            return {
                "writer": "pending_decision_resolved",
                "ok": False,
                "reason": "brain_stop_missing",
                "new_stop_order_id": None,
                "qty": None,
                "stop_price": None,
            }
        # Cancel covering orders + delegate to place_missing_stop with
        # brain stop_price. The writer's own covered-by-existing-sell
        # branch will not fire again because we are about to free the
        # shares. Use the writer's internal helper sequence here.
        try:
            from .. import broker_service as _bs
            cancelled = _bs.cancel_open_sell_orders_for_ticker(local.ticker)
        except Exception:
            cancelled = 0
            logger.warning(
                f"{BRACKET_RECONCILIATION} pending-decision replace_with_stop: "
                "cancel_open_sell_orders failed trade=%s intent=%s ticker=%s",
                local.trade_id, local.bracket_intent_id, local.ticker,
                exc_info=True,
            )

        if cancelled <= 0:
            # Defensive: no covering orders to cancel. The position must
            # have moved (filled, manually closed, etc.). Clear the
            # pending decision and let the next sweep re-classify.
            _clear_pending_decision_and_set_state(
                db, int(local.bracket_intent_id),
                new_intent_state=None,  # do not change intent_state
                last_diff_reason="pending_decision_resolved:replace_no_orders_to_cancel",
            )
            logger.warning(
                f"{BRACKET_RECONCILIATION} pending-decision replace_with_stop: "
                "no covering orders found trade=%s intent=%s ticker=%s "
                "(broker state changed since decision surfaced; clearing "
                "pending_decision so next sweep re-classifies)",
                local.trade_id, local.bracket_intent_id, local.ticker,
            )
            return {
                "writer": "pending_decision_resolved",
                "ok": False,
                "reason": "replace_no_orders_to_cancel",
                "new_stop_order_id": None,
                "qty": None,
                "stop_price": None,
            }

        import time as _time
        _time.sleep(2)  # broker cancel propagation; matches writer convention

        from .bracket_writer_g2 import place_missing_stop
        action = place_missing_stop(
            db,
            trade_id=int(local.trade_id),
            bracket_intent_id=int(local.bracket_intent_id),
            ticker=str(local.ticker or ""),
            broker_source=str(local.broker_source or ""),
            decision=ReconciliationDecision(
                kind="missing_stop", severity="warn", delta_payload={},
            ),
            local_quantity=float(broker_state.get("qty") or local.quantity or 0.0),
            stop_price=float(brain_stop),
        )
        _clear_pending_decision_and_set_state(
            db, int(local.bracket_intent_id),
            new_intent_state=None,
            last_diff_reason="pending_decision_resolved:replace_with_stop",
        )
        logger.warning(
            f"{BRACKET_RECONCILIATION} pending-decision RESOLVED "
            "trade=%s intent=%s ticker=%s choice=replace_with_stop "
            "cancelled=%d new_stop_ok=%s reason=%s",
            local.trade_id, local.bracket_intent_id, local.ticker,
            cancelled, action.ok, action.reason,
        )
        return {
            "writer": "pending_decision_resolved",
            "ok": bool(action.ok),
            "reason": str(action.reason or "replace_with_stop"),
            "new_stop_order_id": action.new_stop_order_id,
            "qty": action.new_stop_qty,
            "stop_price": action.new_stop_price,
        }

    if choice == "convert_to_trailing_stop":
        # No broker-side trailing-stop placement helper exists. Leave the
        # pending_decision in place and surface NOT_IMPLEMENTED so the
        # operator can pick a different option.
        logger.error(
            f"{BRACKET_RECONCILIATION} pending-decision convert_to_trailing_stop "
            "NOT_IMPLEMENTED trade=%s intent=%s ticker=%s "
            "(no broker trailing-stop helper available; operator should "
            "choose replace_with_stop or keep_target instead)",
            local.trade_id, local.bracket_intent_id, local.ticker,
        )
        return {
            "writer": "pending_decision_resolved",
            "ok": False,
            "reason": "convert_to_trailing_stop_not_implemented",
            "new_stop_order_id": None,
            "qty": None,
            "stop_price": None,
        }

    # Unknown choice -- the admin endpoint validates against the options
    # list, but if a stale row was hand-edited, we surface and leave it.
    logger.error(
        f"{BRACKET_RECONCILIATION} pending-decision UNKNOWN_CHOICE "
        "trade=%s intent=%s ticker=%s choice=%s",
        local.trade_id, local.bracket_intent_id, local.ticker, choice,
    )
    return {
        "writer": "pending_decision_resolved",
        "ok": False,
        "reason": f"unknown_choice:{choice}",
        "new_stop_order_id": None,
        "qty": None,
        "stop_price": None,
    }


def _clear_pending_decision_and_set_state(
    db: Session,
    bracket_intent_id: int,
    *,
    new_intent_state: str | None,
    last_diff_reason: str,
) -> None:
    """Remove the ``pending_decision`` key from payload_json and update
    ``last_diff_reason`` (and optionally ``intent_state``). One UPDATE.
    """
    try:
        if new_intent_state is not None:
            db.execute(
                text(
                    "UPDATE trading_bracket_intents "
                    "SET payload_json = "
                    "      COALESCE(payload_json, '{}'::jsonb) - 'pending_decision', "
                    "    intent_state = :state, "
                    "    last_diff_reason = :reason, "
                    "    updated_at = NOW() "
                    "WHERE id = :iid"
                ),
                {
                    "iid": int(bracket_intent_id),
                    "state": new_intent_state,
                    "reason": last_diff_reason,
                },
            )
        else:
            db.execute(
                text(
                    "UPDATE trading_bracket_intents "
                    "SET payload_json = "
                    "      COALESCE(payload_json, '{}'::jsonb) - 'pending_decision', "
                    "    last_diff_reason = :reason, "
                    "    updated_at = NOW() "
                    "WHERE id = :iid"
                ),
                {
                    "iid": int(bracket_intent_id),
                    "reason": last_diff_reason,
                },
            )
        db.commit()
    except Exception:
        logger.warning(
            f"{BRACKET_RECONCILIATION} clear pending_decision failed intent=%s",
            bracket_intent_id, exc_info=True,
        )
        try:
            db.rollback()
        except Exception:
            pass


def _stop_tightness(
    *,
    direction: str | None,
    local_stop: float | None,
    broker_stop: float | None,
) -> str:
    """Classify which stop is more protective for the position."""
    if local_stop is None or broker_stop is None:
        return "unknown"
    try:
        local_f = float(local_stop)
        broker_f = float(broker_stop)
    except (TypeError, ValueError):
        return "unknown"
    if abs(local_f - broker_f) <= 1e-9:
        return "equal"
    side = (direction or "long").lower()
    if side == "short":
        return "broker_tighter" if broker_f < local_f else "local_tighter"
    return "broker_tighter" if broker_f > local_f else "local_tighter"


def _adopt_broker_tighter_stop(
    db: Session,
    *,
    local: LocalView,
    broker: BrokerView,
) -> dict[str, Any]:
    """Mirror a more-protective broker stop into local trade + intent state.

    The broker already owns the live order, so no cancel/place is required.
    This removes false price-drift noise while keeping CHILI's management
    envelope honest about the stop that can actually fire at the venue.
    """
    if local.trade_id is None or local.bracket_intent_id is None:
        return {
            "writer": "adopt_broker_tighter_stop",
            "ok": False,
            "reason": "missing_local_ids",
            "new_stop_order_id": None,
            "qty": None,
            "stop_price": None,
        }
    if broker.stop_order_price is None:
        return {
            "writer": "adopt_broker_tighter_stop",
            "ok": False,
            "reason": "missing_broker_stop_price",
            "new_stop_order_id": None,
            "qty": None,
            "stop_price": None,
        }

    broker_stop = float(broker.stop_order_price)
    db.execute(
        text(
            "UPDATE trading_trades "
            "SET stop_loss = :stop "
            "WHERE id = :tid AND status = 'open'"
        ),
        {"tid": int(local.trade_id), "stop": broker_stop},
    )
    db.execute(
        text(
            "UPDATE trading_bracket_intents "
            "SET stop_price = :stop, "
            "    broker_stop_order_id = COALESCE(:order_id, broker_stop_order_id), "
            "    last_diff_reason = 'adopted_broker_tighter_stop', "
            "    updated_at = NOW() "
            "WHERE id = :iid"
        ),
        {
            "iid": int(local.bracket_intent_id),
            "stop": broker_stop,
            "order_id": broker.stop_order_id,
        },
    )
    return {
        "writer": "adopt_broker_tighter_stop",
        "ok": True,
        "reason": "broker_stop_tighter_than_engine",
        "new_stop_order_id": broker.stop_order_id,
        "qty": broker.position_quantity,
        "stop_price": broker_stop,
    }


def _invoke_writer_for_decision(
    db: Session,
    *,
    mode: str,
    sweep_id: str,
    local: LocalView,
    broker: BrokerView,
    decision: ReconciliationDecision,
) -> dict[str, Any] | None:
    """Invoke the Phase G.2 bracket writer for a classification that
    represents a repairable drift, when mode + flags allow.

    Gates (all must be True for the writer to fire):
      * ``mode == "authoritative"`` (set by sweep entry point only when
        ``chili_bracket_sweep_writer_enabled`` is True).
      * ``local.trade_id`` and ``local.bracket_intent_id`` are present.
      * ``local.broker_source`` is supported (currently Robinhood only).
      * decision.kind is one of {missing_stop, qty_drift+partial_fill}.

    Returns a dict describing the writer action (logged + summarised),
    or ``None`` if no writer was invoked. Failures inside the writer
    are surfaced via the dict's ``ok`` and ``reason`` keys, never as
    exceptions reaching the sweep loop.
    """
    if mode != "authoritative":
        # Mode-gate is sweep-cadence (every 60s in prod) and reflects
        # config, not a structural problem — debug-level is appropriate.
        logger.debug(
            f"{BRACKET_RECONCILIATION} writer SKIPPED mode-gated "
            "trade=%s intent=%s ticker=%s mode=%s",
            local.trade_id, local.bracket_intent_id, local.ticker, mode,
        )
        return None
    if local.trade_id is None or local.bracket_intent_id is None:
        # f-coinbase-bracket-coverage-fix (2026-05-10) — was a silent
        # return None pre-Bug-B. Post-Bug-B the backfill stage creates
        # missing intents at sweep load time, so this should rarely
        # fire. If it does, the intent creation failed (logged at
        # warning by _stage_backfill_missing_intents) and we want
        # operator visibility on the downstream skip.
        logger.info(
            f"{BRACKET_RECONCILIATION} writer SKIPPED no-intent-row "
            "trade=%s ticker=%s broker=%s "
            "(post-backfill this should be rare; investigate the "
            "preceding backfill_intent warning if present)",
            local.trade_id, local.ticker, local.broker_source,
        )
        return None
    # f-coinbase-bracket-coverage-fix (2026-05-10) — Bug C smoking gun.
    # The previous code had a hardcoded ``!= "robinhood"`` filter that
    # silently returned None for every Coinbase trade. Phase 4 wired
    # Coinbase into ``bracket_writer_g2._SUPPORTED_VENUES`` and
    # ``place_missing_stop`` already routes Coinbase via
    # ``place_stop_limit_order_gtc`` — but this gate prevented the
    # call from ever reaching the writer. Delegate the venue check
    # to the writer's authoritative venue list and emit a log on skip.
    from .bracket_writer_g2 import _SUPPORTED_VENUES as _SUPPORTED_VENUES_G2
    broker_lower = (local.broker_source or "").lower()
    if broker_lower not in _SUPPORTED_VENUES_G2:
        logger.info(
            f"{BRACKET_RECONCILIATION} writer SKIPPED venue-unsupported "
            "trade=%s intent=%s ticker=%s broker=%s supported=%s",
            local.trade_id, local.bracket_intent_id, local.ticker,
            local.broker_source, sorted(_SUPPORTED_VENUES_G2),
        )
        return None

    # bracket-writer-respect-upside-targets (2026-05-04): pending-decision
    # routing. If the writer's covered-by-existing-sell branch previously
    # surfaced a conflict, the operator's choice (recorded via
    # POST /api/admin/bracket-decisions/<id>) is in
    # ``payload_json.pending_decision.operator_choice``. Resolve it here
    # before the normal writer path runs.
    #
    #   * ``operator_choice is None`` -> defer; no writer call this sweep.
    #     Emit a discrepancy so the audit funnel sees the deferral.
    #   * ``keep_target`` -> clear pending_decision, mark accepted_no_stop,
    #     no broker action.
    #   * ``replace_with_stop`` -> cancel covering orders + place stop at
    #     brain stop_price + clear pending_decision.
    #   * ``convert_to_trailing_stop`` -> NOT_IMPLEMENTED (no helper); log,
    #     leave pending_decision in place for operator to pick a different
    #     option.
    pending_resolution = _resolve_pending_bracket_decision(
        db, local=local, broker=broker, sweep_id=sweep_id,
    )
    if pending_resolution is not None:
        return pending_resolution

    # Phase 3.3 (2026-05-01): gate on the intent's persistent state. If
    # the intent has been parked at terminal_reject (broker repeatedly
    # rejected, ELTX-style auto-cancel pattern, etc.) or already closed,
    # the writer must not fire. This replaces the in-process FIX 52
    # _intent_reject_cooldown dict's role for reconciler invocations
    # (the dict still exists as a fast-path inside the writer itself).
    intent_state_raw = (local.intent_state or "").lower().strip()

    # audit-missing-stop-emergency-repair (2026-05-03):
    # Additive escape valve for the case where intent_state ==
    # 'terminal_reject' AND the trade is still open AND we have a
    # missing_stop classification. Without this branch, the
    # state_gated_skip below short-circuits forever and the position
    # has no broker stop -- the failure mode that produced the 7-trade
    # incident this task fixes. Gated behind
    # chili_bracket_missing_stop_repair_enabled (default OFF). On the
    # first invocation past throttle: bump
    # terminal_reject_repair_last_attempt_at, classify by broker_qty,
    # phantom-close OR call FIX-51 place_missing_stop, audit either
    # way. Any rejection re-locks via the throttle until manual
    # operator intervention.
    if (
        intent_state_raw == "terminal_reject"
        and decision.kind == "missing_stop"
        and (local.trade_status or "").lower() == "open"
        and getattr(settings, "chili_bracket_missing_stop_repair_enabled", False)
    ):
        repair_result = _try_emergency_repair_terminal_reject(
            db,
            local=local,
            broker=broker,
            decision=decision,
            sweep_id=sweep_id,
        )
        if repair_result is not None:
            return repair_result
        # repair_result is None when broker_qty unknown OR throttle
        # active OR a structural pre-condition failed; in those cases
        # fall through to state_gated_skip below for the same logging
        # behavior the operator already sees.

    if intent_state_raw in ("terminal_reject", "closed"):
        logger.info(
            f"{BRACKET_RECONCILIATION} writer SKIPPED state-gated "
            "trade=%s intent=%s ticker=%s state=%s",
            local.trade_id, local.bracket_intent_id, local.ticker, intent_state_raw,
        )
        return {
            "writer": "state_gated_skip",
            "ok": False,
            "reason": f"state_{intent_state_raw}",
            "new_stop_order_id": None,
            "qty": None,
            "stop_price": None,
        }

    if decision.kind == "price_drift":
        payload = decision.delta_payload or {}
        if payload.get("leg") != "stop":
            return None
        tightness = _stop_tightness(
            direction=local.direction,
            local_stop=payload.get("local_price", local.stop_price),
            broker_stop=payload.get("broker_price", broker.stop_order_price),
        )
        if tightness == "broker_tighter":
            if not bool(getattr(settings, "chili_bracket_writer_g2_enabled", False)):
                return {
                    "writer": "adopt_broker_tighter_stop",
                    "ok": False,
                    "reason": "disabled",
                    "new_stop_order_id": None,
                    "qty": None,
                    "stop_price": None,
                }
            return _adopt_broker_tighter_stop(db, local=local, broker=broker)
        if tightness == "local_tighter":
            # Do not blindly cancel/replace a working broker stop from inside
            # the reconciliation loop. If the engine stop is already breached
            # (the ABTC case), the live exit lane owns the sell; otherwise this
            # needs a separate amend policy that validates session, quote, and
            # immediate-trigger risk before touching the broker order.
            return {
                "writer": "amend_stop_price_drift",
                "ok": False,
                "reason": "local_stop_tighter_amend_requires_safe_session_policy",
                "new_stop_order_id": None,
                "qty": None,
                "stop_price": local.stop_price,
            }
        return None

    try:
        from .bracket_writer_g2 import (
            place_missing_stop,
            resize_stop_for_partial_fill,
        )
    except Exception:
        logger.warning(
            f"{BRACKET_RECONCILIATION} bracket_writer_g2 import failed for sweep %s",
            sweep_id, exc_info=True,
        )
        return None

    try:
        if decision.kind == "missing_stop":
            if local.stop_price is None or local.quantity is None:
                return None
            # FIX 51 (2026-05-01) — pre-flight broker-quantity check.
            #
            # The writer was submitting SELL_STOP orders sized to
            # ``local.quantity`` (the bracket_intent's stored qty) without
            # confirming the user actually held that many shares. When the
            # broker position drifted below local.quantity (manual sell,
            # phantom trade, partial fill that didn't reconcile), every
            # 2-min sweep produced a rejection storm:
            #   "Not enough shares to sell." -> place_failed
            #   classify next sweep as missing_stop again -> retry forever
            # That looped at the broker AND triggered Robinhood reject
            # notifications to the user. Triage 2026-05-01: AIDX, CCCC,
            # CRDL, TLS, VFS were all exhibiting this for hours.
            #
            # Resolution rules (data already on hand from the BrokerView):
            #   * broker not available           -> skip (broker_down sweep)
            #   * broker_qty == 0 / None         -> skip; the position is
            #     gone, the stop is meaningless, and trying produces an
            #     immediate reject. The orphan-intent cleanup path is
            #     responsible for clearing the bracket_intent row.
            #   * broker_qty < local_qty         -> cap to broker_qty so we
            #     still protect whatever is actually held. Better partial
            #     coverage than no coverage.
            #   * broker_qty >= local_qty        -> proceed unchanged.
            local_qty = float(local.quantity)
            broker_qty = (
                float(broker.position_quantity)
                if (broker is not None and broker.available
                    and broker.position_quantity is not None)
                else None
            )
            if broker_qty is None:
                logger.info(
                    f"{BRACKET_RECONCILIATION} place_missing_stop SKIPPED "
                    "trade=%s intent=%s ticker=%s reason=broker_qty_unknown",
                    local.trade_id, local.bracket_intent_id, local.ticker,
                )
                return {
                    "writer": "place_missing_stop",
                    "ok": False,
                    "reason": "skipped_broker_qty_unknown",
                    "new_stop_order_id": None,
                    "qty": None,
                    "stop_price": None,
                }
            if broker_qty <= 0:
                logger.info(
                    f"{BRACKET_RECONCILIATION} place_missing_stop SKIPPED "
                    "trade=%s intent=%s ticker=%s reason=broker_qty_zero "
                    "local_qty=%s",
                    local.trade_id, local.bracket_intent_id, local.ticker,
                    local_qty,
                )
                return {
                    "writer": "place_missing_stop",
                    "ok": False,
                    "reason": "skipped_broker_qty_zero",
                    "new_stop_order_id": None,
                    "qty": None,
                    "stop_price": None,
                }
            placement_qty = min(local_qty, broker_qty)
            if placement_qty < local_qty:
                logger.warning(
                    f"{BRACKET_RECONCILIATION} place_missing_stop CAPPED "
                    "trade=%s intent=%s ticker=%s local_qty=%s broker_qty=%s "
                    "placing=%s (broker has fewer shares than bracket_intent)",
                    local.trade_id, local.bracket_intent_id, local.ticker,
                    local_qty, broker_qty, placement_qty,
                )
            action = place_missing_stop(
                db,
                trade_id=int(local.trade_id),
                bracket_intent_id=int(local.bracket_intent_id),
                ticker=str(local.ticker or ""),
                broker_source=str(local.broker_source or ""),
                decision=decision,
                local_quantity=float(placement_qty),
                stop_price=float(local.stop_price),
            )
            return {
                "writer": "place_missing_stop",
                "ok": bool(action.ok),
                "reason": action.reason,
                "new_stop_order_id": action.new_stop_order_id,
                "qty": action.new_stop_qty,
                "stop_price": action.new_stop_price,
            }

        if decision.kind == "qty_drift":
            payload = decision.delta_payload or {}
            if payload.get("drift_kind") != "partial_fill":
                return None
            if local.stop_price is None:
                return None
            # f-coinbase-bracket-coverage-fix (2026-05-10) — explicit
            # Coinbase skip in resize path. ``resize_stop_for_partial_fill``
            # in bracket_writer_g2 calls ``adapter.place_stop_loss_sell_order``
            # directly, which the Coinbase adapter does not implement (its
            # primitive is ``place_stop_limit_order_gtc``). Wiring Coinbase
            # into the resize path is a separate fix; for now, surface the
            # skip with an info log instead of letting it crash inside the
            # writer.
            if broker_lower == "coinbase":
                logger.info(
                    f"{BRACKET_RECONCILIATION} resize_stop SKIPPED "
                    "trade=%s intent=%s ticker=%s broker=coinbase "
                    "reason=resize_not_yet_wired_for_coinbase "
                    "(place_stop_limit_order_gtc has no resize peer; "
                    "follow-up brief required to wire it)",
                    local.trade_id, local.bracket_intent_id, local.ticker,
                )
                return {
                    "writer": "resize_stop_for_partial_fill",
                    "ok": False,
                    "reason": "resize_not_yet_wired_for_coinbase",
                    "new_stop_order_id": None,
                    "qty": None,
                    "stop_price": None,
                }
            prior_id = (
                broker.stop_order_id
                if broker is not None and broker.stop_order_id
                else None
            )
            if prior_id is None:
                return None
            action = resize_stop_for_partial_fill(
                db,
                trade_id=int(local.trade_id),
                bracket_intent_id=int(local.bracket_intent_id),
                ticker=str(local.ticker or ""),
                broker_source=str(local.broker_source or ""),
                decision=decision,
                prior_stop_order_id=str(prior_id),
                stop_price=float(local.stop_price),
            )
            return {
                "writer": "resize_stop_for_partial_fill",
                "ok": bool(action.ok),
                "reason": action.reason,
                "prior_stop_order_id": action.prior_stop_order_id,
                "new_stop_order_id": action.new_stop_order_id,
                "qty": action.new_stop_qty,
                "stop_price": action.new_stop_price,
            }
    except Exception:
        logger.warning(
            f"{BRACKET_RECONCILIATION} writer raised for trade %s sweep %s",
            local.trade_id, sweep_id, exc_info=True,
        )
        return None

    return None


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
    # f-coinbase-bracket-coverage-fix (2026-05-10) — Bug B safety net.
    # Create intent rows for open live trades that have stop_loss > 0
    # but no intent yet, then re-load so the rest of the sweep sees
    # the freshly-created bracket_intent_id values.
    backfilled = _stage_backfill_missing_intents(
        db, batch.local_views, mode=mode,
    )
    if backfilled:
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
    # f-coinbase-bracket-coverage-fix (2026-05-10) — Bug B safety net.
    # Same as the staged path: create intent rows for open live trades
    # with stop_loss > 0 but no intent, then re-load so the rest of the
    # loop sees the new bracket_intent_id values.
    backfilled = _stage_backfill_missing_intents(db, local_rows, mode=mode)
    if backfilled:
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

        # bracket-intent-stale-label-cleanup hook (gated, no-op when flag OFF).
        # See helper docstring; same wiring as the staged path in _stage_log_all.
        _apply_intent_mirror_writes(
            db,
            sweep_id=sweep_id,
            mode=mode,
            local=local,
            broker=broker,
            decision=decision,
        )

        # Phase G.2 writer hook (Round 23). No-op unless mode=authoritative.
        writer_res = _invoke_writer_for_decision(
            db,
            mode=mode,
            sweep_id=sweep_id,
            local=local,
            broker=broker,
            decision=decision,
        )
        if writer_res is not None and _ops_log_enabled():
            logger.info(
                format_bracket_reconciliation_ops_line(
                    event="writer_action",
                    mode=mode,
                    sweep_id=sweep_id,
                    trade_id=local.trade_id,
                    bracket_intent_id=local.bracket_intent_id,
                    ticker=local.ticker,
                    broker_source=local.broker_source,
                    kind=decision.kind,
                    severity=decision.severity,
                    writer=writer_res.get("writer"),
                    ok=writer_res.get("ok"),
                    reason=writer_res.get("reason"),
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
    * Authoritative mode → falls back to shadow unless the sweep-writer
      flag is enabled.
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
        # Phase G.2 - authoritative mode is gated by the sweep-side
        # writer flag. When the flag is OFF we fall back to shadow + log
        # a loud warning; when ON we let the sweep run and invoke the
        # G2 writer in the post-classify hook. The bracket_writer_g2
        # module is responsible for its own per-action flags and venue
        # check on top of this gate.
        if not bool(getattr(settings, "chili_bracket_sweep_writer_enabled", False)):
            logger.warning(
                f"{BRACKET_RECONCILIATION} mode=authoritative requested but "
                "chili_bracket_sweep_writer_enabled=False; falling back to shadow"
            )
            mode = "shadow"

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
            t.pending_exit_status,
            t.pending_exit_reason,
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
            "pending_exit_status": r[6],
            "pending_exit_reason": r[7],
            "broker_source": r[8],
            "bracket_intent_id": int(r[9]) if r[9] is not None else None,
            "intent_state": r[10],
            "stop_price": float(r[11]) if r[11] is not None else None,
            "target_price": float(r[12]) if r[12] is not None else None,
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
        "pending_exit_status": local.pending_exit_status,
        "pending_exit_reason": local.pending_exit_reason,
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
