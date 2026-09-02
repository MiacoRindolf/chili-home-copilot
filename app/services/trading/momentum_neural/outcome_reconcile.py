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
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import text as _text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumAutomationOutcome, TradingAutomationSession

logger = logging.getLogger(__name__)

# ── BROKER-TRUTH ATTRIBUTION (2026-09-02, CANF 19471) ─────────────────────────
# The ledger path above knows ONLY the fills the FSM itself adopted. Session 19471
# traded two cycles at the broker (355 @ 4.34 → 4.119915, then 165 @ 4.62 →
# 3.960303) but its ledger holds cycle 1 only: the cycle-2 entry fill was never
# adopted (auto-arm reaper race → no `live_entry_filled` → Hook A never wrote an
# entry leg) and the cycle-2 sell was an operator order placed OUTSIDE the FSM
# (`chili_ops_flat_19471_…`), booked only as an UNPRICED emergency leg in
# le["emergency_exit_accounting_pending"] — which `_record_emergency_unpriced_fill`
# never writes to momentum_fill_outcomes. `_aggregate_ledger` therefore saw a
# clean 355/355 round-trip and stamped `reconciled` at −78.13 while the broker's
# truth for the session is −186.98. The loss guard reads broker_realized_pnl_usd
# (risk_policy.load_current_live_loss_history) and undercounted the day by −108.85.
#
# For Alpaca families the pass now lists the symbol's broker orders inside the
# session window and attributes EVERY filled order whose client_order_id carries
# the session id (chili_ml_e_<sid>_, chili_ml_s_<sid>_, chili_dm_<sid>_,
# chili_ml_bw_<sid>_, chili_ops_flat_<sid>_, …) plus any closing fill that
# uniquely matches an unpriced emergency leg by symbol / qty / time. broker_* then
# reflects the WHOLE session; broker_divergence_usd = broker − lane self-report.
ATTRIBUTION_VERSION = 1
_ALPACA_ATTRIBUTION_FAMILIES = frozenset({"alpaca_spot", "alpaca_short"})
# First numeric segment after the alphabetic prefix tokens is the session id:
#   chili_ml_e_19471_a7c3e32c_9738f4d973 → 19471
#   chili_dm_19471_1_ce039811d2          → 19471 (the `_1_` is the deadman generation)
#   chili_ops_flat_19471_d8394610fc      → 19471
_SESSION_CID_RE = re.compile(r"^chili_(?:[a-z]+_)+(\d+)_")
ATTR_FLAT = "flat"
ATTR_RESIDUAL_OPEN = "residual_open"
ATTR_OVERSOLD = "oversold"
ATTR_AMBIGUOUS = "ambiguous_unpriced_match"
ATTR_NO_OWNED_FILLS = "no_owned_fills"
ATTR_UNREADABLE = "unreadable"
ATTR_TRUNCATED = "truncated"

BrokerOrdersReader = Callable[[str, datetime, datetime], dict]

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


def session_id_from_client_order_id(cid: Any) -> Optional[int]:
    """The session id a CHILI client_order_id belongs to, or None (foreign /
    broker-generated / malformed). Pure; used for attribution ownership."""
    m = _SESSION_CID_RE.match(str(cid or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _naive_utc(v: Any) -> Optional[datetime]:
    """Broker timestamps (aware, ISO / str(datetime)) → naive UTC to compare with
    the naive-UTC session clocks. None when absent/unparseable — never invented."""
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _order_field(o: Any, name: str, raw_name: Optional[str] = None) -> Any:
    """Read a NormalizedOrder attribute or a plain-dict order (tests / fixtures)."""
    if isinstance(o, dict):
        if name in o:
            return o.get(name)
        raw = o.get("raw") if isinstance(o.get("raw"), dict) else {}
        return raw.get(raw_name or name)
    v = getattr(o, name, None)
    if v is None and raw_name is not None:
        raw = getattr(o, "raw", None)
        if isinstance(raw, dict):
            v = raw.get(raw_name)
    return v


def _order_fill_time(o: Any) -> Optional[datetime]:
    raw = _order_field(o, "raw") if isinstance(o, dict) else getattr(o, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    return _naive_utc(raw.get("filled_at")) or _naive_utc(raw.get("submitted_at")) or _naive_utc(
        _order_field(o, "created_time")
    )


def _unpriced_emergency_legs(le: dict) -> list[dict]:
    pend = le.get("emergency_exit_accounting_pending")
    if not isinstance(pend, dict):
        return []
    out = []
    for leg in pend.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        q = _f(leg.get("quantity")) or 0.0
        if q <= 1e-12:
            continue
        if _f(leg.get("fill_price")) is not None:
            continue  # priced legs are booked by the normal exit path
        out.append(leg)
    return out


def attribute_session_broker_orders(
    *,
    session_id: int,
    symbol: str,
    side_long: bool,
    orders: list,
    unpriced_legs: Optional[list] = None,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    ledger_order_ids: Optional[set] = None,
) -> dict:
    """PURE attribution of a session's broker fills (no DB, no HTTP).

    ``orders`` are NormalizedOrder objects (or equivalent dicts) for the session's
    symbol. A filled order is attributed when
      (a) its client_order_id carries THIS session id (any CHILI prefix), or
      (b) it is a closing-side fill NOT owned by any CHILI session id that
          UNIQUELY matches an unpriced emergency leg by qty inside the window
          (an operator/UI sell with a broker-generated cid).
    Foreign-session cids are never attributed. Returns the leg list, the summed
    opening notional, the broker-true pnl (Σ closing proceeds − Σ opening cost for
    a long; sign-symmetric for a short) and an ``attr_status``:
      flat            → opening qty == closing qty (label usable)
      residual_open   → opening qty > closing qty (still open / sell not visible)
      oversold        → closing qty > opening qty (foreign shares; not this session)
      ambiguous_unpriced_match → >1 non-owned candidates for one unpriced leg
      no_owned_fills  → nothing attributable
    """
    sym = str(symbol or "").strip().upper()
    open_side = "buy" if side_long else "sell"
    close_side = "sell" if side_long else "buy"
    legs: list[dict] = []
    seen: set[str] = set()
    candidates_non_owned: list[dict] = []
    foreign_owned = 0

    for o in orders:
        oid = str(_order_field(o, "order_id") or _order_field(o, "id") or "")
        if not oid or oid in seen:
            continue
        o_sym = str(_order_field(o, "product_id") or _order_field(o, "symbol") or "").strip().upper()
        if o_sym and sym and o_sym != sym:
            continue
        filled = _f(_order_field(o, "filled_size", "filled_qty")) or 0.0
        px = _f(_order_field(o, "average_filled_price", "filled_avg_price"))
        if filled <= 1e-12 or px is None or px <= 0:
            continue  # unfilled / cancelled-unfilled orders carry no economics
        side = str(_order_field(o, "side") or "").strip().lower()
        if side not in ("buy", "sell"):
            continue
        cid = _order_field(o, "client_order_id")
        owner = session_id_from_client_order_id(cid)
        t = _order_fill_time(o)
        in_window = True
        if t is not None:
            if window_start is not None and t < window_start:
                in_window = False
            if window_end is not None and t > window_end:
                in_window = False
        leg = {
            "broker_order_id": oid,
            "client_order_id": str(cid) if cid else None,
            "side": side,
            "qty": float(filled),
            "price": float(px),
            "filled_at_utc": t.isoformat() if t is not None else None,
            "status": str(_order_field(o, "status") or ""),
        }
        if owner == int(session_id):
            if not in_window:
                leg["note"] = "owned_cid_outside_window"
                legs.append(leg)
                seen.add(oid)
                continue
            leg["attribution"] = "session_cid"
            legs.append(leg)
            seen.add(oid)
        elif owner is not None:
            foreign_owned += 1
        elif side == close_side and in_window:
            candidates_non_owned.append(leg)

    ambiguous = False
    for uleg in unpriced_legs or []:
        uq = _f((uleg or {}).get("quantity")) or 0.0
        if uq <= 1e-12:
            continue
        rec_at = _naive_utc((uleg or {}).get("recorded_at_utc"))
        matches = []
        for c in candidates_non_owned:
            if c["broker_order_id"] in seen:
                continue
            if abs(c["qty"] - uq) > 1e-9:
                continue
            ct = _naive_utc(c["filled_at_utc"])
            if rec_at is not None and ct is not None and ct > rec_at + timedelta(seconds=120):
                continue
            matches.append(c)
        if len(matches) == 1:
            m = dict(matches[0])
            m["attribution"] = "unpriced_emergency_leg_match"
            m["unpriced_leg_reason"] = (uleg or {}).get("reason")
            legs.append(m)
            seen.add(m["broker_order_id"])
        elif len(matches) > 1:
            ambiguous = True

    attributed = [l for l in legs if l.get("attribution")]
    open_qty = sum(l["qty"] for l in attributed if l["side"] == open_side)
    close_qty = sum(l["qty"] for l in attributed if l["side"] == close_side)
    open_notional = sum(l["qty"] * l["price"] for l in attributed if l["side"] == open_side)
    close_notional = sum(l["qty"] * l["price"] for l in attributed if l["side"] == close_side)
    pnl = (close_notional - open_notional) if side_long else (open_notional - close_notional)
    ledger_ids = {str(x) for x in (ledger_order_ids or set())}
    missing = [l["broker_order_id"] for l in attributed if l["broker_order_id"] not in ledger_ids]

    if ambiguous:
        status = ATTR_AMBIGUOUS
    elif not attributed:
        status = ATTR_NO_OWNED_FILLS
    elif abs(open_qty - close_qty) <= 1e-9:
        status = ATTR_FLAT
    elif open_qty > close_qty:
        status = ATTR_RESIDUAL_OPEN
    else:
        status = ATTR_OVERSOLD

    return {
        "attr_status": status,
        "legs": legs,
        "opening_orders": sum(1 for l in attributed if l["side"] == open_side),
        "closing_orders": sum(1 for l in attributed if l["side"] == close_side),
        "open_qty": open_qty,
        "close_qty": close_qty,
        "open_notional_usd": open_notional if open_notional > 0 else None,
        "close_notional_usd": close_notional,
        "broker_pnl_usd": pnl if attributed else None,
        "legs_missing_from_ledger": missing,
        "foreign_session_orders_ignored": foreign_owned,
        "unpriced_legs_considered": len(unpriced_legs or []),
    }


def _default_alpaca_orders_reader(symbol: str, after: datetime, until: datetime) -> dict:
    """Read-only broker GET through the app adapter (never place/cancel)."""
    try:
        from ..venue.alpaca_spot import AlpacaSpotAdapter

        adapter = AlpacaSpotAdapter()
        if not adapter.is_enabled():
            return {"readable": False, "orders": [], "error": "adapter_disabled"}
        return adapter.list_symbol_orders_truth(symbol, after=after, until=until)
    except Exception as ex:  # pragma: no cover - defensive
        return {"readable": False, "orders": [], "error": str(ex)[:200]}


def _ledger_order_ids(db: Session, session_id: int) -> set:
    try:
        rows = db.execute(
            _text(
                "SELECT broker_order_id FROM momentum_fill_outcomes "
                "WHERE session_id = :sid AND broker_order_id IS NOT NULL"
            ),
            {"sid": int(session_id)},
        ).fetchall()
    except Exception:
        return set()
    return {str(r[0]) for r in rows if r and r[0]}


def _attribution_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_outcome_recon_broker_attribution_enabled", True))


def _is_alpaca_family(sess: Any) -> bool:
    return str(getattr(sess, "execution_family", "") or "") in _ALPACA_ATTRIBUTION_FAMILIES


def needs_reconcile(outcome: Any, sess: Any) -> bool:
    """Batch-pass admission. Terminal statuses are immutable EXCEPT for a one-time
    attribution upgrade of Alpaca rows stamped before ATTRIBUTION_VERSION (the
    203734 case: `reconciled` on cycle 1 only, never re-touched at line ~414 of the
    pre-fix pass). Once detail carries attribution_version the row is terminal."""
    status = getattr(outcome, "broker_recon_status", None)
    if status not in _TERMINAL_RECON_STATUSES:
        return True
    if not (_attribution_enabled() and _is_alpaca_family(sess)):
        return False
    detail = getattr(outcome, "broker_recon_detail_json", None)
    detail = detail if isinstance(detail, dict) else {}
    try:
        return int(detail.get("attribution_version") or 0) < ATTRIBUTION_VERSION
    except (TypeError, ValueError):
        return True


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
    *,
    broker_orders_reader: Optional[BrokerOrdersReader] = None,
) -> dict:
    """Compute the broker-truth label for one closed session and stamp the mig309
    columns on ``outcome`` (caller commits). Returns the audit dict written to
    broker_recon_detail_json. NEVER touches realized_pnl_usd / return_bps.

    ``broker_orders_reader(symbol, after, until) -> {"readable", "orders", ...}``
    is the read-only broker listing used for Alpaca attribution (default: the
    app adapter's ``list_symbol_orders_truth``; tests inject a fake)."""
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

    # ── BROKER-TRUTH ATTRIBUTION (Alpaca families) ──
    # The ledger label above is the FSM's own view. Supersede it with the broker's
    # order list scoped to this session: every session-cid fill + unpriced-leg
    # matches. Whole-session broker_* is what the loss guard must consume.
    if _attribution_enabled() and _is_alpaca_family(sess):
        unpriced = _unpriced_emergency_legs(le)
        grace = int(getattr(settings, "chili_momentum_outcome_recon_broker_attribution_grace_seconds", 900) or 0)
        w_start = _naive_utc(getattr(sess, "started_at", None))
        w_end = _naive_utc(getattr(sess, "ended_at", None)) or _naive_utc(getattr(outcome, "terminal_at", None))
        if w_start is not None:
            w_start = w_start - timedelta(seconds=120)
        if w_end is not None:
            w_end = w_end + timedelta(seconds=grace)
        reader = broker_orders_reader or _default_alpaca_orders_reader
        attr: dict[str, Any]
        if w_start is None or w_end is None:
            attr = {"attr_status": ATTR_UNREADABLE, "error": "session_window_unavailable"}
        else:
            try:
                listing = reader(str(sess.symbol or ""), w_start, w_end) or {}
            except Exception as ex:
                listing = {"readable": False, "orders": [], "error": str(ex)[:200]}
            if not listing.get("readable"):
                attr = {"attr_status": ATTR_UNREADABLE, "error": listing.get("error")}
            elif listing.get("truncated"):
                attr = {"attr_status": ATTR_TRUNCATED}
            else:
                side_long = (str(getattr(sess, "execution_family", "") or "") != "alpaca_short") and (
                    le.get("side_long") is not False
                )
                attr = attribute_session_broker_orders(
                    session_id=int(outcome.session_id),
                    symbol=str(sess.symbol or ""),
                    side_long=side_long,
                    orders=list(listing.get("orders") or []),
                    unpriced_legs=unpriced,
                    window_start=w_start,
                    window_end=w_end,
                    ledger_order_ids=_ledger_order_ids(db, int(outcome.session_id)),
                )
        attr["window_start_utc"] = w_start.isoformat() if w_start else None
        attr["window_end_utc"] = w_end.isoformat() if w_end else None
        attr["ledger_pnl_usd"] = broker_pnl
        attr["ledger_notional_usd"] = broker_notional
        attr["ledger_status"] = status
        detail["broker_attribution"] = attr
        a_status = attr.get("attr_status")
        evidence_of_missing_legs = bool(unpriced) or bool(attr.get("legs_missing_from_ledger"))
        if a_status == ATTR_FLAT:
            broker_pnl = _f(attr.get("broker_pnl_usd"))
            broker_notional = _f(attr.get("open_notional_usd"))
            source = "broker_orders_attributed"
            # Alpaca order objects carry no per-fill fee field; the account is
            # commission-free and the ledger path already labels its 0.0 fees
            # `known`. Same posture here — gross == net for this venue.
            fees_status = "alpaca_commission_free_gross"
            status = STATUS_RECONCILED
            detail["attribution_version"] = ATTRIBUTION_VERSION
            if attr.get("legs_missing_from_ledger"):
                logger.warning(
                    "[broker_truth_recon] session=%s symbol=%s: %d broker fill(s) missing from "
                    "the FSM ledger attributed by session cid/unpriced-leg match; broker pnl=%.2f "
                    "vs ledger pnl=%s",
                    outcome.session_id, sess.symbol, len(attr["legs_missing_from_ledger"]),
                    broker_pnl or 0.0, attr.get("ledger_pnl_usd"),
                )
        elif a_status == ATTR_RESIDUAL_OPEN:
            status = STATUS_RESIDUAL_OPEN
            broker_pnl = None
            broker_notional = None
            source = "broker_orders_attributed"
            detail["attribution_version"] = ATTRIBUTION_VERSION
        elif a_status in (ATTR_OVERSOLD, ATTR_AMBIGUOUS):
            status = STATUS_AMBIGUOUS_TRADE
            broker_pnl = None
            broker_notional = None
            source = "broker_orders_attributed"
            detail["attribution_version"] = ATTRIBUTION_VERSION
        elif a_status == ATTR_NO_OWNED_FILLS:
            # Broker readable, nothing owned. Keep the ledger verdict; if the
            # ledger claimed broker-confirmed legs this is a real inconsistency
            # (recorded, and the row stays terminal so it does not spin).
            detail["attribution_version"] = ATTRIBUTION_VERSION
            if status in _USABLE_FOR_LEARNING and agg is not None and agg["entry_legs"] > 0:
                detail["attribution_inconsistent_with_ledger"] = True
        else:
            # unreadable / truncated / no window: transient. If there is evidence
            # of legs the ledger never saw, the ledger label is KNOWN-wrong → do
            # not certify it; retry next pass. Otherwise keep the ledger verdict
            # (no attribution_version → re-attempted next pass).
            if evidence_of_missing_legs and status in _TERMINAL_RECON_STATUSES:
                status = STATUS_BROKER_UNAVAILABLE
                broker_pnl = None
                broker_notional = None

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
    skipped_budget = 0
    by_status: dict[str, int] = {}
    legacy_sum = 0.0
    broker_sum = 0.0
    # Broker GET budget per pass (one listing per Alpaca session attributed).
    try:
        budget = int(getattr(settings, "chili_momentum_outcome_recon_broker_attribution_max_per_pass", 20) or 20)
    except (TypeError, ValueError):
        budget = 20
    broker_reads = 0
    for outcome, sess in rows:
        checked += 1
        if not needs_reconcile(outcome, sess):
            skipped_terminal += 1
            by_status[str(outcome.broker_recon_status)] = by_status.get(str(outcome.broker_recon_status), 0) + 1
            continue
        if _attribution_enabled() and _is_alpaca_family(sess):
            if broker_reads >= budget:
                skipped_budget += 1
                continue
            broker_reads += 1
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
        "skipped_broker_budget": skipped_budget,
        "broker_reads": broker_reads,
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
