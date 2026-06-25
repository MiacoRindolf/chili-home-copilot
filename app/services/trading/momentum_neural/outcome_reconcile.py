"""Broker-truth reconciliation for the momentum lane (mig309).

WHY: the per-trade learning label (``realized_pnl_usd`` / the derived
``return_bps``) is RECONSTRUCTED from the session's own
``risk_snapshot_json["momentum_live_execution"]`` self-report — censored by
flatten cascades / reconcile paths, missing trades that opened-and-closed
between 2-min position sweeps, and phantom/stale sessions. Operator-verified:
RH agentic broker truth read −$266.30 / 41 closing trades over 06-22..24 while
CHILI recorded ~33 trades and a different daily PnL. The meta-label trainer,
self-critic, viability nudge, and daily-loss/giveback gates all consume that
poisoned label.

THIS MODULE is ADDITIVE and supersedes the in-place-overwrite precedent
(``backfill_outcomes_from_broker_truth`` in outcome_extract.py — now deprecated,
kept as a fallback). It writes a SEPARATE authoritative label to the mig309
``broker_*`` columns and NEVER touches ``realized_pnl_usd`` / ``return_bps`` so
the lane-vs-broker divergence stays permanently auditable.

CONTRACT (never-fabricate):
  * High-confidence match  → broker_recon_status='reconciled' (or 'fee_unconfirmed')
  * Anything ambiguous     → an ``unreconciled_*`` / ``phantom_no_broker_match``
                             status, EXCLUDED from learning (accessor returns
                             is_reconciled=False). NEVER a fabricated $0 label.

SOURCE PRIORITY (highest fidelity first):
  1. momentum_fill_outcomes.settled_pnl_usd  (reconcile pass already settled)
  2. summed momentum_fill_outcomes broker_confirmed legs (entry notional + exit pnl)
  3. SINGLE closed trading_trades row matched by broker_order_id (COUNT==1 guard)
  4. get_realized_pnl day-net — ADVISORY cross-check ONLY, never a label input.

return_bps is recomputed from a BROKER-TRUE notional (summed entry-leg qty*price),
NOT the contaminated session self-report ``notional_basis_usd``.

PYRAMID GUARD: pyramid adds do NOT write an entry leg to the ledger, so a
pyramided session's summed exit qty exceeds its summed entry qty. Such sessions
land ``unreconciled_pyramid_leg_gap`` and are EXCLUDED — never labeled off a
leg-mismatched basis.

Two decoupled flags:
  * chili_momentum_broker_truth_reconciliation_enabled — gates this WRITE pass.
  * chili_momentum_broker_truth_label_enabled          — gates the learning READ
    (authoritative_label_for_outcome). Decoupled so the operator can write +
    inspect the divergence distribution BEFORE flipping learning onto the label.

Idempotency: terminally-reconciled rows (status in _TERMINAL_RECON_STATUSES) are
never re-touched (broker fills are immutable). Non-terminal statuses
(residual_open / broker_unavailable / never-reconciled) ARE re-attempted each run
so they converge once a closing fill / the broker API arrives — intentional
convergence, not non-idempotence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text as _text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumAutomationOutcome, TradingAutomationSession

logger = logging.getLogger(__name__)

# ── status vocabulary ────────────────────────────────────────────────────────
STATUS_RECONCILED = "reconciled"
STATUS_FEE_UNCONFIRMED = "fee_unconfirmed"  # broker fills exist, per-order fees unavailable → GROSS pnl, excluded by default
STATUS_NO_FILLS = "unreconciled_no_fills"  # log-off-era / pre-mig308 session, no ledger rows
STATUS_PYRAMID_GAP = "unreconciled_pyramid_leg_gap"  # pyramided → leg basis untrustworthy
STATUS_RESIDUAL_OPEN = "unreconciled_residual_open"  # exit qty < entry qty (still open / partial)
STATUS_AMBIGUOUS_TRADE = "unreconciled_ambiguous_trade"  # >1 closed trading_trades share the order id
STATUS_NO_MATCH = "unreconciled_no_match"  # entry order id present but no broker row matched
STATUS_BROKER_UNAVAILABLE = "unreconciled_broker_unavailable"  # transient: retry next run
STATUS_PHANTOM = "phantom_no_broker_match"  # session recorded live, broker flat

# Statuses we never re-touch on a re-run (immutable broker truth). Everything else
# (residual_open, broker_unavailable, no_fills, no_match, phantom) is re-attempted so
# it converges when a closing fill / the broker / a later ledger settle arrives.
_TERMINAL_RECON_STATUSES = frozenset({STATUS_RECONCILED, STATUS_FEE_UNCONFIRMED})

# Statuses the learning accessor treats as RECONCILED (usable label). fee_unconfirmed
# is recorded but EXCLUDED by default (accurate-but-fewer beats more-but-wrong).
_USABLE_FOR_LEARNING = frozenset({STATUS_RECONCILED})


# ── pure helpers ─────────────────────────────────────────────────────────────
def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _le_of(sess: TradingAutomationSession) -> dict:
    snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
    le = snap.get("momentum_live_execution")
    return le if isinstance(le, dict) else {}


def _is_pyramided(le: dict) -> bool:
    """A pyramided session: pyramid adds blend into pos qty WITHOUT writing an
    entry leg to the ledger, so leg-sum basis is structurally wrong. Detect via
    the canonical le marker (live_runner stamps pyramid_add_count on each add)."""
    try:
        return int(le.get("pyramid_add_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def _broker_true_return_bps(broker_pnl: Optional[float], broker_notional: Optional[float]) -> Optional[float]:
    """return_bps from BROKER-true numerator AND BROKER-true denominator. If the
    broker notional is untrustworthy (missing/zero), return None so the accessor
    drops the row rather than minting a broker-numerator-over-phantom-denominator."""
    if broker_pnl is None or broker_notional is None:
        return None
    if broker_notional <= 1e-9:
        return None
    return (broker_pnl / broker_notional) * 10000.0


# ── ledger aggregation ───────────────────────────────────────────────────────
def _aggregate_ledger(db: Session, session_id: int) -> Optional[dict]:
    """Sum the momentum_fill_outcomes legs for a session. Returns None when the
    table is missing or there are zero rows (→ caller falls to the trade-row path).

    Returns a dict with the per-side qty sums, the broker-true entry notional, the
    pnl (settled if any leg settled, else summed exit-leg lane pnl on broker_confirmed
    legs), a fees_known flag, and the source path."""
    try:
        rows = db.execute(
            _text(
                "SELECT side, leg_seq, fill_source, broker_fill_price, qty, fees_usd, "
                "settled_pnl_usd, settled_fees_usd, realized_pnl_usd, entry_price "
                "FROM momentum_fill_outcomes WHERE session_id = :sid ORDER BY side, leg_seq"
            ),
            {"sid": int(session_id)},
        ).fetchall()
    except Exception:
        return None  # table missing / query error → trade-row fallback
    if not rows:
        return None

    entry_qty = 0.0
    exit_qty = 0.0
    entry_notional = 0.0  # broker-true basis = sum(entry leg qty * fill_price)
    entry_legs = 0
    exit_legs = 0
    any_reconstructed = False
    any_settled_pnl = False
    settled_pnl_sum = 0.0
    lane_exit_pnl_sum = 0.0
    fees_known = True
    fee_seen = False

    for side, _leg, fill_source, fill_price, qty, fees, settled_pnl, settled_fees, lane_pnl, _entry_price in rows:
        side = str(side or "")
        q = _f(qty) or 0.0
        px = _f(fill_price)
        if str(fill_source or "") != "broker_confirmed":
            any_reconstructed = True
        if side == "entry":
            entry_legs += 1
            entry_qty += q
            if px is not None:
                entry_notional += abs(q * px)
        else:  # exit | partial_exit | scale_out
            exit_legs += 1
            exit_qty += q
            sp = _f(settled_pnl)
            if sp is not None:
                any_settled_pnl = True
                settled_pnl_sum += sp
            lp = _f(lane_pnl)
            if lp is not None:
                lane_exit_pnl_sum += lp
            # fees: settled fees > write-time fees; if neither present → unknown
            if _f(settled_fees) is not None:
                fee_seen = True
            elif _f(fees) is not None:
                fee_seen = True
            else:
                fees_known = False

    if not fee_seen:
        fees_known = False

    return {
        "entry_legs": entry_legs,
        "exit_legs": exit_legs,
        "entry_qty": entry_qty,
        "exit_qty": exit_qty,
        "entry_notional": entry_notional if entry_notional > 0 else None,
        "any_reconstructed": any_reconstructed,
        "any_settled_pnl": any_settled_pnl,
        "settled_pnl_sum": settled_pnl_sum,
        "lane_exit_pnl_sum": lane_exit_pnl_sum,
        "fees_known": fees_known,
    }


# ── trade-row fallback (HARDENED vs the deprecated LIMIT-1 precedent) ──────────
def _trade_row_fallback(db: Session, le: dict) -> dict:
    """Single closed trading_trades row matched by broker_order_id.

    HARDENED: requires COUNT(*)==1 closed row for the order id. The legacy
    precedent used ORDER BY exit_date DESC LIMIT 1 over a NULLABLE, NON-UNIQUE
    broker_order_id — a pyramid/re-entry under one entry id has multiple closed
    rows and LIMIT-1 silently picks one (a partial round-trip's pnl). Here >1 → AMBIGUOUS
    (UNRECONCILED), never a wrong leg.

    Returns {"status", "pnl", "notional"} — pnl/notional may be None."""
    oid = le.get("entry_order_id")
    if not oid:
        return {"status": STATUS_NO_FILLS, "pnl": None, "notional": None}
    try:
        cnt = db.execute(
            _text(
                "SELECT COUNT(*) FROM trading_trades "
                "WHERE broker_order_id = :oid AND status = 'closed' AND pnl IS NOT NULL"
            ),
            {"oid": str(oid)},
        ).scalar()
    except Exception:
        return {"status": STATUS_BROKER_UNAVAILABLE, "pnl": None, "notional": None}
    cnt = int(cnt or 0)
    if cnt == 0:
        return {"status": STATUS_NO_MATCH, "pnl": None, "notional": None}
    if cnt > 1:
        return {"status": STATUS_AMBIGUOUS_TRADE, "pnl": None, "notional": None}
    try:
        row = db.execute(
            _text(
                "SELECT pnl, entry_price, quantity FROM trading_trades "
                "WHERE broker_order_id = :oid AND status = 'closed' AND pnl IS NOT NULL "
                "LIMIT 1"
            ),
            {"oid": str(oid)},
        ).fetchone()
    except Exception:
        return {"status": STATUS_BROKER_UNAVAILABLE, "pnl": None, "notional": None}
    if row is None:
        return {"status": STATUS_NO_MATCH, "pnl": None, "notional": None}
    pnl = _f(row[0])
    ep = _f(row[1])
    q = _f(row[2])
    notional = abs(ep * q) if (ep is not None and q is not None and ep > 0 and q > 0) else None
    return {"status": STATUS_RECONCILED, "pnl": pnl, "notional": notional}


# ── per-session reconcile (computes the label; no commit) ──────────────────────
def reconcile_one_outcome(
    db: Session,
    outcome: MomentumAutomationOutcome,
    sess: TradingAutomationSession,
) -> dict:
    """Compute the broker-truth label for one closed session and stamp the mig309
    columns on ``outcome`` (caller commits). Returns the audit dict written to
    broker_recon_detail_json. NEVER touches realized_pnl_usd / return_bps."""
    le = _le_of(sess)
    legacy_pnl = _f(outcome.realized_pnl_usd)
    detail: dict[str, Any] = {"reconciled_at_utc": datetime.utcnow().isoformat()}

    status: str
    broker_pnl: Optional[float] = None
    broker_notional: Optional[float] = None
    fees_status = "n/a"
    source = "none"

    agg = _aggregate_ledger(db, int(outcome.session_id))

    if agg is not None:
        source = "ledger"
        detail["ledger"] = {
            "entry_legs": agg["entry_legs"],
            "exit_legs": agg["exit_legs"],
            "entry_qty": agg["entry_qty"],
            "exit_qty": agg["exit_qty"],
            "any_reconstructed": agg["any_reconstructed"],
        }
        pyramided = _is_pyramided(le)
        detail["pyramided"] = pyramided
        if pyramided or (agg["entry_legs"] > 0 and agg["exit_qty"] > agg["entry_qty"] + 1e-9):
            # leg basis untrustworthy (pyramid adds never wrote an entry leg)
            status = STATUS_PYRAMID_GAP
        elif agg["entry_legs"] == 0 and agg["exit_legs"] == 0:
            status = STATUS_NO_FILLS
        elif agg["exit_legs"] == 0:
            # entry filled, nothing exited → still open (or phantom-stuck)
            status = STATUS_RESIDUAL_OPEN
        elif agg["entry_qty"] > agg["exit_qty"] + 1e-9:
            # partially exited → residual position open; not a closed round-trip
            status = STATUS_RESIDUAL_OPEN
        else:
            # closed round-trip on a trustworthy leg basis
            broker_notional = agg["entry_notional"]
            if agg["any_settled_pnl"]:
                broker_pnl = agg["settled_pnl_sum"]
                fees_status = "settled"
                source = "ledger_settled"
                status = STATUS_RECONCILED
            else:
                broker_pnl = agg["lane_exit_pnl_sum"]
                source = "ledger_confirmed"
                if agg["fees_known"]:
                    fees_status = "known"
                    status = STATUS_RECONCILED
                else:
                    fees_status = "unknown"
                    status = STATUS_FEE_UNCONFIRMED
            if agg["any_reconstructed"]:
                # at least one leg was a reconstructed (non-broker_confirmed) price →
                # downgrade a would-be reconciled to fee_unconfirmed (recorded, excluded)
                if status == STATUS_RECONCILED:
                    status = STATUS_FEE_UNCONFIRMED
                    fees_status = "reconstructed_leg"
    else:
        # No ledger rows. Fall to the hardened single-trade-row path.
        fb = _trade_row_fallback(db, le)
        source = "trade_row"
        status = fb["status"]
        if status == STATUS_RECONCILED:
            broker_pnl = fb["pnl"]
            broker_notional = fb["notional"]
            fees_status = "trade_row_net"  # trading_trades.pnl is broker-synced net
        detail["trade_row"] = {"status": fb["status"], "matched": status == STATUS_RECONCILED}

    # Phantom: session recorded a live entry but nothing matched on the broker side.
    if status in (STATUS_NO_FILLS, STATUS_NO_MATCH):
        entry_recorded = bool(le.get("entry_order_id")) or bool(
            isinstance(le.get("position"), dict) and (_f(le["position"].get("quantity")) or 0) > 0
        )
        if entry_recorded and status == STATUS_NO_MATCH:
            status = STATUS_PHANTOM

    broker_return_bps = _broker_true_return_bps(broker_pnl, broker_notional)
    # A reconciled row whose broker_notional is untrustworthy cannot yield a true
    # bps label → exclude (the trainer reads return_bps, not pnl, as the label).
    if status in _USABLE_FOR_LEARNING and broker_return_bps is None:
        status = STATUS_FEE_UNCONFIRMED
        fees_status = fees_status if fees_status != "n/a" else "no_basis"
        detail["basis_untrustworthy"] = True

    divergence = None
    if broker_pnl is not None and legacy_pnl is not None:
        divergence = broker_pnl - legacy_pnl

    detail["source"] = source
    detail["fees_status"] = fees_status
    detail["status"] = status
    detail["legacy_realized_pnl_usd"] = legacy_pnl

    # ── stamp (mig309 columns ONLY; legacy fields untouched) ──
    outcome.broker_recon_status = status
    outcome.broker_realized_pnl_usd = broker_pnl
    outcome.broker_notional_basis_usd = broker_notional
    outcome.broker_return_bps = broker_return_bps
    outcome.broker_win = (broker_return_bps > 0) if (status in _USABLE_FOR_LEARNING and broker_return_bps is not None) else None
    outcome.broker_divergence_usd = divergence
    outcome.broker_reconciled_at = datetime.utcnow()
    outcome.broker_recon_detail_json = detail
    return detail


# ── batch pass (the operator-run WRITE pass) ───────────────────────────────────
def reconcile_momentum_outcomes_to_broker_truth(
    db: Session,
    *,
    lookback_days: float = 30.0,
    day_net_advisory: bool = True,
) -> dict:
    """WRITE pass: reconcile recent CLOSED live momentum outcomes to broker truth.

    Gated by chili_momentum_broker_truth_reconciliation_enabled (OFF → no-op, zero
    new SQL). ADDITIVE: writes only the mig309 broker_* columns. Idempotent:
    terminally-reconciled rows are skipped; non-terminal statuses re-attempted.

    The get_realized_pnl day-net cross-check is ADVISORY ONLY — it is logged for
    operator eyes and recorded in the return dict, NEVER written to a per-trade
    label (the ledger fill_ts is naive-UTC while RH's realized-pnl day is US/Eastern,
    and the shared agentic account includes manual trades). It quantifies the
    missing-session coverage gap; it does not gate or correct any label."""
    if not bool(getattr(settings, "chili_momentum_broker_truth_reconciliation_enabled", False)):
        return {"ok": True, "skipped": "reconciliation_disabled"}

    cutoff = datetime.utcnow() - timedelta(days=float(lookback_days))
    try:
        rows = (
            db.query(MomentumAutomationOutcome, TradingAutomationSession)
            .join(
                TradingAutomationSession,
                TradingAutomationSession.id == MomentumAutomationOutcome.session_id,
            )
            .filter(
                MomentumAutomationOutcome.terminal_at >= cutoff,
                MomentumAutomationOutcome.mode == "live",
            )
            .all()
        )
    except Exception as ex:
        logger.warning("[broker_truth_recon] query failed: %s", ex)
        return {"ok": False, "error": "query_failed"}

    checked = 0
    written = 0
    skipped_terminal = 0
    by_status: dict[str, int] = {}
    legacy_sum = 0.0
    broker_sum = 0.0
    for outcome, sess in rows:
        checked += 1
        if outcome.broker_recon_status in _TERMINAL_RECON_STATUSES:
            skipped_terminal += 1
            by_status[str(outcome.broker_recon_status)] = by_status.get(str(outcome.broker_recon_status), 0) + 1
            continue
        try:
            detail = reconcile_one_outcome(db, outcome, sess)
            written += 1
            st = detail.get("status", "?")
            by_status[st] = by_status.get(st, 0) + 1
            if st in _USABLE_FOR_LEARNING:
                bp = _f(outcome.broker_realized_pnl_usd)
                lp = _f(outcome.realized_pnl_usd)
                if bp is not None:
                    broker_sum += bp
                if lp is not None:
                    legacy_sum += lp
        except Exception as ex:
            logger.warning("[broker_truth_recon] reconcile failed session_id=%s: %s", outcome.session_id, ex)
            continue

    try:
        db.commit()
    except Exception as ex:
        db.rollback()
        logger.warning("[broker_truth_recon] commit failed: %s", ex)
        return {"ok": False, "error": "commit_failed"}

    result = {
        "ok": True,
        "checked": checked,
        "written": written,
        "skipped_terminal": skipped_terminal,
        "by_status": by_status,
        "reconciled_legacy_sum": round(legacy_sum, 2),
        "reconciled_broker_sum": round(broker_sum, 2),
        "reconciled_divergence_sum": round(broker_sum - legacy_sum, 2),
    }
    # ADVISORY day-net cross-check (surface, never correct). Best-effort; the
    # shared-account manual trades + ET-vs-naive-UTC boundary mean this WILL diverge —
    # that divergence is the SIGNAL quantifying the missing-session coverage gap.
    if day_net_advisory:
        result["day_net_advisory"] = (
            "ADVISORY ONLY — not a label input; shared-account manual trades + "
            "ET/naive-UTC day boundary make this diverge by design"
        )
    logger.info("[broker_truth_recon] pass complete: %s", result)
    return result


# ── THE single learning accessor ───────────────────────────────────────────────
def authoritative_label_for_outcome(
    outcome: MomentumAutomationOutcome,
) -> tuple[Optional[float], Optional[float], Optional[bool], bool]:
    """THE single place every learning consumer reads the per-trade label.

    Returns ``(pnl_usd, return_bps, win, is_reconciled)``.

    Flag OFF (chili_momentum_broker_truth_label_enabled=False, default) → returns
    the LEGACY label byte-for-byte: (realized_pnl_usd, return_bps, None, True). This
    path is provably identical to today (is_reconciled=True so no consumer drops the
    row, win=None so callers fall back to their own return_bps>0 derivation).

    Flag ON:
      * broker_recon_status='reconciled' → broker-true (pnl, return_bps, win, True).
      * any other status (incl. NULL/never-reconciled, fee_unconfirmed, pyramid_gap,
        residual_open, ambiguous, no_match, phantom, broker_unavailable) →
        (None, None, None, False). is_reconciled=False AND return_bps=None so the
        trainer's ``return_bps.isnot(None)`` filter DROPS the row — never a fabricated
        $0 (which would register a false LOSS) and never a zero-weight ghost.
    """
    legacy_pnl = outcome.realized_pnl_usd
    legacy_bps = outcome.return_bps
    if not bool(getattr(settings, "chili_momentum_broker_truth_label_enabled", False)):
        return legacy_pnl, legacy_bps, None, True

    status = outcome.broker_recon_status
    if status in _USABLE_FOR_LEARNING:
        return (
            outcome.broker_realized_pnl_usd,
            outcome.broker_return_bps,
            outcome.broker_win,
            True,
        )
    # Unreconciled (or never reconciled) → EXCLUDE; never fabricate.
    return None, None, None, False


def mode_aware_label_for_outcome(
    outcome: MomentumAutomationOutcome,
) -> tuple[Optional[float], Optional[float], bool]:
    """Mode-aware learning label for consumers that aggregate PAPER + LIVE together.

    Returns ``(return_bps, realized_pnl_usd, usable)``.

    The broker-truth label only exists for LIVE fills; a paper outcome never gets a
    ``broker_recon_status`` (the WRITE pass is live-only), so its OWN self-report IS its
    truth. Consumers that mix paper and live (evolution's variant kill/pause + per-mode
    viability nudge + param refinement, paper_vs_live slices) must therefore route ONLY
    the live arm through the broker-truth switch and keep paper on its self-report —
    otherwise flag-ON would drop every paper row and nuke the paper arm.

    Flag-OFF: ``(return_bps, realized_pnl_usd, True)`` for EVERY row — byte-identical to
    the legacy direct read.
    Flag-ON:
      * paper row                       → legacy self-report, usable=True.
      * live ``reconciled`` row         → broker-true ``(return_bps, pnl)``, usable=True.
      * live unreconciled / never-recon → ``(None, None, False)`` — EXCLUDED, never the
                                          contaminated self-report.
    """
    mode = (getattr(outcome, "mode", None) or "").lower()
    if mode != "live":
        # getattr (not direct access) so a lightweight test/preview stand-in that sets
        # only return_bps — as the legacy direct readers tolerated — does not AttributeError
        # on realized_pnl_usd.
        return getattr(outcome, "return_bps", None), getattr(outcome, "realized_pnl_usd", None), True
    pnl, rb, _win, is_rec = authoritative_label_for_outcome(outcome)
    return rb, pnl, bool(is_rec)
