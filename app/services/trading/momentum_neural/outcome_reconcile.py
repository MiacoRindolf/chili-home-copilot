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
from .outcome_labels import NEVER_ENTERED_OUTCOMES

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
#
# HARDENING (2026-09-02, adversarial review of the first cut):
#   * A no-fill row with ZERO entry evidence never spends a broker read, and a
#     `no_owned_fills` verdict backs off — otherwise ~115 cancelled-pre-entry rows
#     re-read every 60 s pass against a 20-read budget and STARVE the newly
#     terminal FILLED session the loss guard needs (the 85-minute arming outage
#     shape of project_loss_guard_broker_recon_landmine_0902.md). The batch is
#     ordered loss-guard-first, never heap order.
#   * A listing that does not contain the session's OWN ledger legs never
#     certifies anything (wrong account generation / wrong window / empty page).
#   * OCO child legs carry NO client_order_id — the stop leg's fill lives in
#     raw.legs[0] under a parent that reads canceled/filled 0. Walk them, or every
#     stop-leg exit is a permanent `residual_open` and the account's loss history
#     goes unavailable.
#   * Ownership is by broker_order_id too (orphan-repair `orphrec-*` closes and
#     broker-cid replace successors), and a terminal label is never demoted
#     without POSITIVE evidence of an owned fill the ledger never saw.
#   * The unpriced-leg fallback is time-ordered, skips legs already attributed by
#     cid, and refuses a broker_order_id another outcome already claims.
ATTRIBUTION_VERSION = 2
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
# The listing is readable but provably does not cover this session (it is missing
# the session's own ledger legs) → nothing it says may certify anything.
ATTR_LISTING_INCOMPLETE = "listing_incomplete"
# No broker read was spent: the session has no entry evidence AND one readable
# listing already proved the broker owns nothing for it (budget protection, not a
# verdict about the broker). NEVER asserted before that proof read — see
# `broker_read_plan`: an envelope that LOST its order id is exactly the shape this
# PR exists to fix, and a permanent skip would make its loss invisible forever.
ATTR_SKIPPED_NO_ENTRY_EVIDENCE = "skipped_no_entry_evidence"
ATTR_SKIPPED_BACKOFF = "skipped_no_owned_fills_backoff"
# No broker read was spent: the session window cannot be derived at all, so the
# reader would be called with a null bound and the branch would bail WITHOUT an
# HTTP call. Charging a budget slot for a read that provably cannot happen is a
# pure leak, and the resulting `unreadable` verdict stamps no version — i.e. the
# row repeats that leak every 60 s forever.
ATTR_SKIPPED_NO_WINDOW = "skipped_no_session_window"
_EPOCH_NAIVE = datetime(1970, 1, 1)
# The listing is readable and complete, but a whole cycle's envelope anchors (an
# entry-side AND an exit-side order id the session recorded) are absent from it —
# the only shape that can fabricate a FLAT verdict out of a partial listing.
ATTR_ANCHORS_MISSING = "envelope_anchors_missing"

# ── DURABLE ATTRIBUTION MARKERS (2026-09-02) ──────────────────────────────────
# These keys are STATE, not a per-pass audit snapshot: `broker_read_plan` reads
# them to decide whether this row may skip its broker listing, and
# `_emit_divergence_event` reads one of them for its once-per-outcome idempotency.
# Every writer of ``broker_recon_detail_json`` REBUILDS the dict from scratch
# (`reconcile_one_outcome` starts from `{"reconciled_at_utc": ...}`;
# `alpaca_reconcile._apply_cancelled_pre_entry_orphan_truth` writes `{**truth, ...}`), so a
# single write from any path that does not hand-carry them erases the whole
# skip/backoff state — measured 2026-09-02: seven consecutive passes cycled
# 0 → 20 → 40 → 60 → 0 → 20 → 40 `skipped_no_broker_read_needed` instead of
# converging, each pass spending its full 20-read budget on rows a readable
# listing had ALREADY proved own nothing.
#
# Persistence is therefore structural, not per-branch: `stamp_recon_detail` is
# THE write site and it MERGES these forward from whatever is already on the row.
# A pass that must genuinely retire one says so explicitly (`cleared=`), so a
# converged row still drops its stale horizon.
#
# ⚠️ `attribution_version` is deliberately NOT in this set and must never be. It
# is what makes a TERMINAL status immutable (`needs_reconcile`), so carrying it
# across a pass that attributed nothing is exactly the CANF-19471 undercount
# (−78.13 instead of −186.98) documented at the skip branch below. Same for
# `broker_attribution`: it is the LAST READ's verdict, and a stale one presented
# as this pass's finding would mislead the operator reading the row.
STICKY_RECON_DETAIL_KEYS: tuple[str, ...] = (
    "attribution_no_entry_evidence_proven_empty",
    "attribution_terminal_at",
    "attribution_next_retry_utc",
    "attribution_attempts",
    "attribution_retry_blocking",
    # One-shot latch for the `ledger_settled_terminal` proof release. Durable
    # because its whole job is to make that release happen EXACTLY once per row.
    "attribution_ledger_release_done",
    "divergence_event_emitted",
)


def merge_recon_detail(
    prior: Any,
    new: dict,
    *,
    cleared: Optional[set] = None,
) -> dict:
    """Carry the durable attribution markers from ``prior`` into ``new`` (pure).

    ``new`` always wins where it sets a key — this only fills keys it left out.
    ``cleared`` names markers this pass RETIRED on purpose (a converged read
    dropping its retry horizon); those are never resurrected.

    Genuinely pure: the caller's dict is COPIED, never mutated. That is not
    hygiene, it is what makes ``stamp_recon_detail`` safe for the obvious
    read-modify-write shape (``d = row.broker_recon_detail_json; d[k] = v;
    stamp_recon_detail(row, d)``). ``broker_recon_detail_json`` is a plain
    ``Column(JSONB)`` with no ``MutableDict`` wrapper, so SQLAlchemy flags the
    attribute dirty only when a NEW object is assigned — mutating in place and
    assigning the same object back emits no UPDATE, and the write is lost with no
    error and no log line while the caller's own read-back still sees it.
    """
    if not isinstance(new, dict):
        return {}
    new = dict(new)
    if not isinstance(prior, dict):
        return new
    skip = cleared or set()
    for key in STICKY_RECON_DETAIL_KEYS:
        if key in skip or key in new:
            continue
        if prior.get(key) is not None:
            new[key] = prior[key]
    return new


def stamp_recon_detail(outcome: Any, detail: dict, *, cleared: Optional[set] = None) -> dict:
    """THE single write site for ``broker_recon_detail_json``.

    Every path that stamps the column goes through here so no writer — present or
    future, in this module or another — can silently erase the markers that gate
    the broker-read budget. Returns the merged dict actually persisted.
    """
    merged = merge_recon_detail(getattr(outcome, "broker_recon_detail_json", None), detail, cleared=cleared)
    outcome.broker_recon_detail_json = merged
    return merged

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


_ENTRY_EVIDENCE_KEYS = (
    "entry_order_id",
    "entry_client_order_id",
    "entry_reconcile_pending_client_order_id",
    "last_exit_order_id",
    "exit_order_id",
    "scale_limit_order_id",
)


def _has_entry_evidence(le: dict) -> bool:
    """True when this session can POSSIBLY have a broker fill.

    Every order the lane (or an operator, or an orphan repair) ever placed for a
    session leaves at least one of these markers on the envelope. A row carrying
    NONE of them cannot have a fill at the broker, so listing its symbol is a
    guaranteed-empty read — and ~115 such rows per pass are exactly what starved
    the 20-read budget and left the loss guard blind for 85 minutes.
    """
    if not isinstance(le, dict):
        return False
    for key in _ENTRY_EVIDENCE_KEYS:
        if str(le.get(key) or "").strip():
            return True
    ids_all = le.get("entry_order_ids_all")
    if isinstance(ids_all, (list, tuple, set)) and any(str(x or "").strip() for x in ids_all):
        return True
    resolved = le.get("entry_orders_resolved")
    if isinstance(resolved, dict) and resolved:
        return True
    pos = le.get("position")
    if isinstance(pos, dict) and (_f(pos.get("quantity")) or 0.0) > 0:
        return True
    for key in ("emergency_exit_authority", "orphan_reconcile_truth", "emergency_position_truth"):
        if isinstance(le.get(key), dict) and le.get(key):
            return True
    if _unpriced_emergency_legs(le):
        return True
    return False


def _owned_order_ids_from_le(le: dict) -> set:
    """Broker order ids this session PROVABLY owns, independent of the cid.

    An orphan-repair close (`orphrec-<symbol>-<digest>`) and a broker-cid replace
    successor carry a client_order_id the session-cid regex cannot own; without
    this set the attribution sees a non-owned close, lands `residual_open`, and
    demotes an already-labelled row.
    """
    out: set[str] = set()
    if not isinstance(le, dict):
        return out

    def _add(v: Any) -> None:
        s = str(v or "").strip()
        if s:
            out.add(s)

    for key in ("entry_order_id", "last_exit_order_id", "exit_order_id", "scale_limit_order_id"):
        _add(le.get(key))
    ids_all = le.get("entry_order_ids_all")
    if isinstance(ids_all, (list, tuple, set)):
        for v in ids_all:
            _add(v)
    resolved = le.get("entry_orders_resolved")
    if isinstance(resolved, dict):
        for v in resolved.keys():
            _add(v)
    truth = le.get("orphan_reconcile_truth")
    if isinstance(truth, dict):
        _add(truth.get("exit_order_id"))
        _add(truth.get("entry_order_id"))
    auth = le.get("emergency_exit_authority")
    if isinstance(auth, dict):
        _add(auth.get("order_id"))
        _add(auth.get("broker_order_id"))
    for leg in _unpriced_emergency_legs(le):
        _add((leg or {}).get("broker_order_id"))
    return out


def _envelope_anchor_ids(le: dict) -> tuple[set, set]:
    """``(entry_side_anchors, exit_side_anchors)`` — order ids the session itself
    recorded, split by which side of the round-trip they opened or closed.

    These are the completeness anchors for a session whose FSM ledger is EMPTY
    (the ledger-derived guard has nothing to work with there). Every one of them
    was submitted for this symbol inside the session's own life, so a readable
    ``status=all`` listing for [started_at−120 s, ended_at+grace] MUST contain it.

    Deliberately EXCLUDES ``orphan_reconcile_truth`` and ``emergency_exit_authority``:
    an orphan repair by construction references an order from an EARLIER window,
    so demanding it would fabricate a permanent incompleteness — and a row that can
    never certify pins the whole account at `loss_guard_history_unavailable`,
    which is a worse outage than the mispriced label it would prevent."""
    entry: set[str] = set()
    exit_: set[str] = set()
    if not isinstance(le, dict):
        return entry, exit_

    def _add(dst: set, v: Any) -> None:
        s = str(v or "").strip()
        if s:
            dst.add(s)

    _add(entry, le.get("entry_order_id"))
    ids_all = le.get("entry_order_ids_all")
    if isinstance(ids_all, (list, tuple, set)):
        for v in ids_all:
            _add(entry, v)
    resolved = le.get("entry_orders_resolved")
    if isinstance(resolved, dict):
        for v in resolved.keys():
            _add(entry, v)
    for key in ("last_exit_order_id", "exit_order_id", "scale_limit_order_id"):
        _add(exit_, le.get(key))
    for leg in _unpriced_emergency_legs(le):
        _add(exit_, (leg or {}).get("broker_order_id"))
    return entry, exit_


def _loss_guard_label_usable(outcome: Any) -> bool:
    """Does THIS row satisfy the loss guard's broker-truth admission? (pure)

    Mirrors ``risk_policy._alpaca_loss_history_broker_truth`` minus its feature
    flag. A row that fails this — for an entered session — makes the guard emit
    ``loss_guard_history_unavailable`` for the WHOLE account, which is the
    85-minute arming outage of 2026-09-02. It is therefore the only correct
    definition of "the loss guard is waiting on this row", and it is what the
    batch ordering and the retry horizon must both key on.
    """
    if str(getattr(outcome, "broker_recon_status", "") or "").strip().lower() != STATUS_RECONCILED:
        return False
    if not isinstance(getattr(outcome, "broker_reconciled_at", None), datetime):
        return False
    if _f(getattr(outcome, "broker_realized_pnl_usd", None)) is None:
        return False
    notional = _f(getattr(outcome, "broker_notional_basis_usd", None))
    return notional is not None and notional > 0.0


def _loss_guard_can_block(outcome: Any, sess: Any, *, entry_event_seen: bool = False) -> bool:
    """Can an unresolved label on this row block the WHOLE account? (fail-closed)

    ``risk_policy.load_current_live_loss_history`` skips exactly ONE class of row:
    the one ``_loss_history_entry_classification`` calls ``not_entered`` (a
    never-entered outcome class with no durable and no economic evidence).
    Everything else — ``entered``, ``unknown``, ``conflict`` — gaps the day and
    disarms the lane.

    ⚠️ This DELEGATES to that classifier rather than re-deriving it. A hand-written
    mirror was wrong in three ways the first time, ALL of them in the direction
    that SILENCES the alarm on a row that genuinely disarms the account:

      * ``broker_notional_basis_usd`` PRESENT but ``<= 0`` / non-finite / a bool is
        ``conflict`` at risk_policy.py:1474, checked BEFORE the never-entered
        branch; the mirror only treated ``> 0.0`` as blocking;
      * ``_loss_history_snapshot_entry_proof`` (risk_policy.py:1352) proves a
        durable entry from ``le["realized_pnl_usd"]`` / ``le["last_exit_entry_price"]``
        — two keys the mirror never read;
      * ``entry_event_seen`` — a ``live_entry_filled`` / ``live_exit_filled`` /
        ``live_partial_exit_filled`` row in ``trading_automation_events`` — which a
        pure function cannot see at all. That is exactly the CANF-19471
        lost-adoption shape this whole one-proof-read design exists to defend, and
        it sorts into the LOWEST priority class, so it is the first row the budget
        drops. It is supplied by the caller (see ``_entry_event_session_ids``).

    Anything the classifier cannot answer (an import/attribute failure) is treated
    as blocking, so the alarm can never go quiet on a genuinely blind row.
    """
    try:
        from .risk_policy import _loss_history_entry_classification

        classification = _loss_history_entry_classification(
            outcome, sess, entry_event_seen=bool(entry_event_seen)
        )
    except Exception:  # pragma: no cover - fail-closed on any classifier failure
        logger.debug("[broker_truth_recon] loss-guard classification failed", exc_info=True)
        return True
    return str(classification) != "not_entered"


def _entry_event_session_ids(db: Any, session_ids: Any) -> Optional[set]:
    """Sessions with a durable entry EVENT — the half of the classifier a pure
    function cannot see. ONE bounded query for the whole batch, never per row.

    Returns ``None`` when the read fails, which callers must treat as "assume every
    row could block" (fail-closed): not knowing is not the same as knowing there is
    no event, and the failure mode of guessing wrong here is a silent alarm on the
    85-minute-arming-outage shape.

    Deliberately UNBOUNDED in time where the guard bounds the event to the session
    window and the availability frontier: a superset can only over-report "this row
    could block", which is the safe direction for an alarm gate.
    """
    ids = [int(s) for s in session_ids if s is not None]
    if not ids:
        return set()
    try:
        from ....models.trading import TradingAutomationEvent
        from .risk_policy import _LOSS_HISTORY_ENTRY_EVENTS

        rows = (
            db.query(TradingAutomationEvent.session_id)
            .filter(
                TradingAutomationEvent.session_id.in_(tuple(ids)),
                TradingAutomationEvent.event_type.in_(tuple(sorted(_LOSS_HISTORY_ENTRY_EVENTS))),
            )
            .distinct()
            .all()
        )
    except Exception:
        logger.debug("[broker_truth_recon] entry-event probe failed", exc_info=True)
        return None
    out = set()
    for row in rows:
        sid = row[0] if isinstance(row, (tuple, list)) else getattr(row, "session_id", row)
        try:
            out.add(int(sid))
        except (TypeError, ValueError):
            continue
    return out


def _attribution_backoff_seconds(*, blocking: bool = False) -> int:
    """Seconds until this row may spend another broker read.

    TWO horizons, because one number cannot serve both shapes:
      * ``blocking`` (the loss guard is disarmed behind this row) — SHORT. A long
        backoff here converts a transient invisibility (the fill has not
        propagated into the listing yet; the operator sell landed after the grace
        window) into a GUARANTEED account-wide arming outage of exactly that
        length. The 09-02 landmine was 85 minutes; a 30-minute backoff on a
        blocking row re-arms it with a 30-minute fuse.
      * everything else (a cancelled-pre-entry row the guard skips outright) —
        LONG, because re-listing an immutable empty answer every 60 s is pure
        budget burn.
    """
    key = (
        "chili_momentum_outcome_recon_broker_attribution_blocking_retry_seconds"
        if blocking
        else "chili_momentum_outcome_recon_broker_attribution_no_fill_backoff_seconds"
    )
    default = 120 if blocking else 1800
    try:
        return max(0, int(getattr(settings, key, default) or 0))
    except (TypeError, ValueError):
        return default


def _session_attribution_window(sess: Any, outcome: Any) -> tuple:
    """The ``[after, until]`` bound the broker listing is read with, or ``(None, None)``.

    Shared by ``broker_read_plan`` and ``reconcile_one_outcome`` so the budget is
    never charged for a read the reconciler will refuse to make: a row with no
    derivable window used to burn one of the 20 slots every single pass and land
    ``unreadable`` (no ``attribution_version``) forever.
    """
    w_start = _naive_utc(getattr(sess, "started_at", None))
    w_end = _naive_utc(getattr(sess, "ended_at", None)) or _naive_utc(getattr(outcome, "terminal_at", None))
    if w_start is None or w_end is None:
        return None, None
    try:
        grace = int(getattr(settings, "chili_momentum_outcome_recon_broker_attribution_grace_seconds", 900) or 0)
    except (TypeError, ValueError):
        grace = 900
    return w_start - timedelta(seconds=120), w_end + timedelta(seconds=grace)


def broker_read_plan(outcome: Any, sess: Any, *, now: Optional[datetime] = None) -> dict:
    """Should THIS pass spend a broker listing read on this row? (pure)

    Used by the batch loop (to charge the budget) AND by ``reconcile_one_outcome``
    (to call the reader) so the two can never disagree. ``{"read": bool, "reason": str}``.
    """
    if not (_attribution_enabled() and _is_alpaca_family(sess)):
        return {"read": False, "reason": "not_alpaca_attribution"}
    le = _le_of(sess)
    detail = getattr(outcome, "broker_recon_detail_json", None)
    detail = detail if isinstance(detail, dict) else {}
    prior = detail.get("broker_attribution")
    prior = prior if isinstance(prior, dict) else {}
    w_start, w_end = _session_attribution_window(sess, outcome)
    if w_start is None or w_end is None:
        # `reconcile_one_outcome` would bail with `unreadable` WITHOUT calling the
        # reader — charging a budget slot for a read that provably cannot happen.
        return {"read": False, "reason": ATTR_SKIPPED_NO_WINDOW}
    if not _has_entry_evidence(le):
        # The envelope shows nothing that could have filled — but "the envelope
        # shows nothing" is exactly what an entry whose adoption write was LOST
        # looks like (CANF 19471 cycle 2), and such a session is classified
        # `entered` by the loss guard, so a permanent skip pins the whole account
        # at `loss_guard_history_unavailable` forever. Spend ONE proof read; only
        # a readable listing that owned nothing earns the permanent skip.
        clock = now or datetime.utcnow()
        if detail.get("attribution_no_entry_evidence_proven_empty"):
            stamped_terminal = _naive_utc(detail.get("attribution_terminal_at"))
            cur_terminal = _naive_utc(getattr(outcome, "terminal_at", None))
            if stamped_terminal is not None and cur_terminal is not None and stamped_terminal != cur_terminal:
                return {"read": True, "reason": "terminal_at_changed"}
            return {"read": False, "reason": ATTR_SKIPPED_NO_ENTRY_EVIDENCE}
        retry_at = _naive_utc(detail.get("attribution_next_retry_utc"))
        if retry_at is not None and clock < retry_at:
            return {"read": False, "reason": ATTR_SKIPPED_BACKOFF, "next_retry_utc": retry_at.isoformat()}
        return {"read": True, "reason": "no_entry_evidence_proof_read"}
    # RETRY HORIZON — honoured for EVERY armed verdict, not just `no_owned_fills`.
    # `unreadable` / `truncated` / `listing_incomplete` / `residual_open` /
    # `oversold` never stamp an attribution_version, so under the first cut each
    # one re-listed the broker every 60 s FOREVER while sitting in the top
    # ordering class — an unbounded set of permanently stuck rows that holds the
    # whole 20-read budget and starves every row below it. The horizon is written
    # by `_arm_attribution_retry` (short when the loss guard is blocked behind the
    # row, long when it is not) and carried forward verbatim by the skips, so a
    # skip can never push its own horizon out.
    retry_at = _naive_utc(detail.get("attribution_next_retry_utc"))
    if retry_at is not None:
        stamped_terminal = _naive_utc(detail.get("attribution_terminal_at"))
        cur_terminal = _naive_utc(getattr(outcome, "terminal_at", None))
        if stamped_terminal is not None and cur_terminal is not None and stamped_terminal != cur_terminal:
            return {"read": True, "reason": "terminal_at_changed"}
        clock = now or datetime.utcnow()
        if clock < retry_at:
            return {"read": False, "reason": ATTR_SKIPPED_BACKOFF, "next_retry_utc": retry_at.isoformat()}
    return {"read": True, "reason": "attribute"}


def _attribution_priority(outcome: Any, sess: Any) -> int:
    """Batch ordering class — the budget is spent strictly in this order.

    The class must be "how badly is the LOSS GUARD waiting on this row", not "how
    old is the attribution stamp". The first cut ranked by the stamp, so:
      * a backfill row the guard can ALREADY use (`reconciled` with a finite pnl
        and a positive notional, merely missing an ``attribution_version``) sat in
        the top class — an unbounded queue of them on a cold deploy; while
      * a newly terminal FILLED row that lost its first read to a transient Alpaca
        error dropped to `unreconciled_broker_unavailable` and therefore BELOW
        every one of them, with no way back up. That row is precisely the one the
        guard emits ``loss_guard_history_unavailable`` for, account-wide.

      0  the guard cannot use this row and it has never been attributed  (blind, new)
      1  the guard cannot use this row and it HAS been attributed        (blind, converged)
      2  the guard can already use this row (backfill / version upgrade only)
      3  no entry evidence: the one-time proof read (see `broker_read_plan`) —
         it must happen, but never before a row that could carry a fill.
    """
    if not _has_entry_evidence(_le_of(sess)):
        return 3
    if _loss_guard_label_usable(outcome):
        return 2
    detail = getattr(outcome, "broker_recon_detail_json", None)
    detail = detail if isinstance(detail, dict) else {}
    try:
        attributed = int(detail.get("attribution_version") or 0) >= ATTRIBUTION_VERSION
    except (TypeError, ValueError):
        attributed = False
    return 1 if attributed else 0


def _oco_legs_of(o: Any) -> list[dict]:
    """Child legs whose side is PROVABLY the parent's side — i.e. OCO only.

    The normalized leg dict (alpaca_spot._normalize_order) carries no ``side``, so
    the walker can only infer it from the parent. That inference holds for
    ``order_class=oco`` (take-profit limit + stop-loss, both reducing the same
    position, both the parent's side) and is FALSE for ``bracket``/``oto``, whose
    parent is the ENTRY and whose legs are the opposite side. Walking a bracket
    would book a protective SELL as a second opening BUY — measured: a 165-share
    entry with a filled 165-share stop leg came out ``open_qty=330, close_qty=0``
    and a fabricated −$1,415.70. CHILI only ever submits ``OrderClass.OCO``
    (alpaca_spot.py:3964), so anything else is either a hand-placed order or a
    future code path: fail closed and let the parent stand on its own economics.
    """
    raw = _order_field(o, "raw") if isinstance(o, dict) else getattr(o, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    if str(raw.get("order_class") or "").strip().lower() != "oco":
        return []
    legs = raw.get("legs")
    return [l for l in (legs or []) if isinstance(l, dict)]


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
    owned_order_ids: Optional[set] = None,
    ledger_owned_order_ids: Optional[set] = None,
    expected_listing_order_ids: Optional[set] = None,
    entry_anchor_ids: Optional[set] = None,
    exit_anchor_ids: Optional[set] = None,
) -> dict:
    """PURE attribution of a session's broker fills (no DB, no HTTP).

    ``orders`` are NormalizedOrder objects (or equivalent dicts) for the session's
    symbol. A filled order is attributed when
      (a) its client_order_id carries THIS session id (any CHILI prefix), or
      (b) its broker order id is one the session itself recorded — on its envelope
          (``owned_order_ids``) or, decisively, in its OWN FSM ledger
          (``ledger_owned_order_ids``, i.e. ``momentum_fill_outcomes.session_id``),
          or
      (c) it is a closing-side fill NOT owned by any CHILI session id that
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
    # OWNERSHIP, two independent records. The envelope half is WEAKER than it
    # looks: `last_exit_order_id` has no writer anywhere in the repo, and
    # `exit_order_id` is POPPED off the envelope by the runner on the repeg and
    # terminal-no-fill paths (live_runner.py:17731/17896/17918). The session's own
    # FSM ledger (`momentum_fill_outcomes.session_id`) is therefore the reliable
    # record of which broker orders a session closed with. Without it, a close
    # whose client_order_id is BROKER-generated (a replace successor, a UI sell the
    # FSM later adopted) is seen as non-owned → `residual_open` → a row the ledger
    # alone would have labelled `reconciled` is DEMOTED, and every demoted Alpaca
    # row pins the whole account at `loss_guard_history_unavailable`
    # (risk_policy.py:1432) — the 85-minute arming outage of 2026-09-02.
    ledger_owned = {str(x) for x in (ledger_owned_order_ids or set()) if str(x or "").strip()}
    owned_ids = {str(x) for x in (owned_order_ids or set()) if str(x or "").strip()} | ledger_owned
    legs: list[dict] = []
    seen: set[str] = set()
    listing_ids: set[str] = set()
    candidates_non_owned: list[dict] = []
    foreign_owned = 0

    for o in orders:
        oid = str(_order_field(o, "order_id") or _order_field(o, "id") or "")
        if not oid or oid in seen:
            continue
        o_sym = str(_order_field(o, "product_id") or _order_field(o, "symbol") or "").strip().upper()
        if o_sym and sym and o_sym != sym:
            continue
        listing_ids.add(oid)
        side = str(_order_field(o, "side") or "").strip().lower()
        cid = _order_field(o, "client_order_id")
        owner = session_id_from_client_order_id(cid)
        # Ownership is cid FIRST (a foreign session's cid is never ours, whatever
        # the id set says), then the session's own recorded broker order ids.
        owned = (owner == int(session_id)) or (owner is None and oid in owned_ids)
        t = _order_fill_time(o)
        in_window = True
        if t is not None:
            if window_start is not None and t < window_start:
                in_window = False
            if window_end is not None and t > window_end:
                in_window = False

        # ── OCO CHILD LEGS ──────────────────────────────────────────────────
        # Alpaca gives NO client_order_id to a child leg: the parent
        # (`chili_ml_toco_<sid>_…`) is the take-profit limit, and when the STOP
        # leg fires the parent reads canceled with filled_qty 0 while the fill
        # lives in raw.legs[0]. Both legs of an OCO are the SAME side as the
        # parent. Missing them makes every stop-leg exit a permanent
        # `residual_open` → loss-guard history unavailable account-wide.
        if owned and side in ("buy", "sell"):
            for child in _oco_legs_of(o):
                leg_id = str(child.get("id") or "")
                if not leg_id or leg_id in seen or leg_id in listing_ids:
                    continue
                listing_ids.add(leg_id)
                lq = _f(child.get("filled_qty")) or 0.0
                lpx = _f(child.get("filled_avg_price"))
                if lq <= 1e-12 or lpx is None or lpx <= 0:
                    continue
                child_leg = {
                    "broker_order_id": leg_id,
                    "client_order_id": None,
                    "parent_broker_order_id": oid,
                    "parent_client_order_id": str(cid) if cid else None,
                    "side": side,
                    "qty": float(lq),
                    "price": float(lpx),
                    # The leg payload has no clock of its own; this is the
                    # PARENT's. Marked so nothing downstream mistakes it for the
                    # leg's real fill time (it is excluded from `earliest_open`).
                    "filled_at_utc": t.isoformat() if t is not None else None,
                    "filled_at_source": "parent_order",
                    "status": str(child.get("status") or ""),
                }
                if in_window:
                    child_leg["attribution"] = "session_cid_oco_leg"
                else:
                    child_leg["note"] = "owned_cid_outside_window"
                legs.append(child_leg)
                seen.add(leg_id)

        filled = _f(_order_field(o, "filled_size", "filled_qty")) or 0.0
        px = _f(_order_field(o, "average_filled_price", "filled_avg_price"))
        if filled <= 1e-12 or px is None or px <= 0:
            continue  # unfilled / cancelled-unfilled orders carry no economics
        if side not in ("buy", "sell"):
            continue
        leg = {
            "broker_order_id": oid,
            "client_order_id": str(cid) if cid else None,
            "side": side,
            "qty": float(filled),
            "price": float(px),
            "filled_at_utc": t.isoformat() if t is not None else None,
            "status": str(_order_field(o, "status") or ""),
        }
        if owned:
            if not in_window:
                leg["note"] = "owned_cid_outside_window"
                legs.append(leg)
                seen.add(oid)
                continue
            if owner == int(session_id):
                leg["attribution"] = "session_cid"
            elif oid in ledger_owned:
                leg["attribution"] = "session_ledger_order_id"
            else:
                leg["attribution"] = "session_broker_order_id"
            legs.append(leg)
            seen.add(oid)
        elif owner is not None:
            foreign_owned += 1
        elif side == close_side and in_window:
            candidates_non_owned.append(leg)

    # Earliest OWNED opening fill: a close that filled BEFORE this session ever
    # opened cannot be this session's close (overlapping same-symbol sessions are
    # routine — ADXN 08-21 had 30 overlapping pairs).
    owned_open_times = [
        _naive_utc(l["filled_at_utc"])
        for l in legs
        if l.get("attribution") and l["side"] == open_side and l.get("filled_at_utc")
        and l.get("filled_at_source") != "parent_order"  # inherited clock, not leg truth
    ]
    owned_open_times = [t for t in owned_open_times if t is not None]
    earliest_open = min(owned_open_times) if owned_open_times else None
    owned_cids_seen = {
        str(l.get("client_order_id") or "")
        for l in legs
        if l.get("attribution") and l.get("client_order_id")
    }

    ambiguous = False
    for uleg in unpriced_legs or []:
        uq = _f((uleg or {}).get("quantity")) or 0.0
        if uq <= 1e-12:
            continue
        # This unpriced leg already has a priced, attributed twin in the listing
        # (its own cid was found) — matching a stranger's same-qty sell on top of
        # it would double count. Live rows carry `chili_ml_x_<sid>_` here.
        uleg_cid = str((uleg or {}).get("client_order_id") or "").strip()
        if uleg_cid and uleg_cid in owned_cids_seen:
            continue
        uleg_oid = str((uleg or {}).get("broker_order_id") or "").strip()
        if uleg_oid and uleg_oid in seen:
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
            if earliest_open is not None and ct is not None and ct < earliest_open:
                continue  # closed before this session opened → another session's
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
    # A missing CLOSING fill is the only thing that positively proves an existing
    # terminal label's economics are wrong. A missing opening fill (or simply not
    # seeing a close) is an ABSENCE of evidence — never grounds to knock a
    # labelled row down to a non-terminal status.
    missing_closing = [
        l["broker_order_id"] for l in attributed
        if l["side"] == close_side and l["broker_order_id"] not in ledger_ids
    ]
    # COMPLETENESS (the converse direction, previously never computed): a ledger
    # leg the listing does not contain proves the listing does NOT cover this
    # session — wrong bound account generation, wrong window, empty page. Nothing
    # such a read says may certify a label.
    expected = {str(x) for x in (expected_listing_order_ids if expected_listing_order_ids is not None else ledger_ids)}
    ledger_missing_from_broker = sorted(expected - listing_ids)
    # ENVELOPE ANCHORS: the ledger-derived guard above is blind for a session whose
    # FSM ledger is EMPTY — precisely the sessions this attribution exists for.
    # A listing that drops ONE side lands residual_open/oversold on its own; the
    # only way a partial listing fabricates a FLAT is by dropping a WHOLE cycle,
    # i.e. an entry-side AND an exit-side anchor together. That paired condition is
    # the gate; a single stale anchor is reported but never blocks (a row that can
    # never certify pins the account at `loss_guard_history_unavailable`).
    entry_anchors = {str(x) for x in (entry_anchor_ids or set()) if str(x or "").strip()}
    exit_anchors = {str(x) for x in (exit_anchor_ids or set()) if str(x or "").strip()}
    entry_anchors_missing = sorted(entry_anchors - listing_ids)
    exit_anchors_missing = sorted(exit_anchors - listing_ids)
    anchors_missing_both_sides = bool(entry_anchors_missing) and bool(exit_anchors_missing)

    if ledger_missing_from_broker:
        status = ATTR_LISTING_INCOMPLETE
    elif ambiguous:
        status = ATTR_AMBIGUOUS
    elif not attributed:
        status = ATTR_NO_OWNED_FILLS
    elif abs(open_qty - close_qty) <= 1e-9:
        status = ATTR_ANCHORS_MISSING if anchors_missing_both_sides else ATTR_FLAT
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
        "closing_legs_missing_from_ledger": missing_closing,
        "ledger_ids_missing_from_broker": ledger_missing_from_broker,
        "entry_anchors_missing_from_broker": entry_anchors_missing,
        "exit_anchors_missing_from_broker": exit_anchors_missing,
        "listing_order_ids_seen": len(listing_ids),
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


def _ledger_legs_for_attribution(db: Session, session_id: int) -> list:
    """``[(broker_order_id, fill_ts_naive_utc|None), …]`` for the session's ledger.

    The fill time is what makes the completeness guard precise: only a ledger leg
    whose fill falls INSIDE the queried window is expected in the listing, so a
    legitimately out-of-window leg never fabricates an "incomplete read"."""
    try:
        rows = db.execute(
            _text(
                "SELECT broker_order_id, fill_ts FROM momentum_fill_outcomes "
                "WHERE session_id = :sid AND broker_order_id IS NOT NULL"
            ),
            {"sid": int(session_id)},
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows or []:
        if not r or not r[0]:
            continue
        ts = _naive_utc(r[1]) if len(r) > 1 else None
        out.append((str(r[0]), ts))
    return out


def _ledger_order_ids(db: Session, session_id: int) -> set:
    return {oid for oid, _ts in _ledger_legs_for_attribution(db, session_id)}


def _broker_order_ids_attributed_elsewhere(
    db: Session,
    *,
    session_id: int,
    symbol: str,
    order_ids: list,
    terminal_at: Optional[datetime],
) -> dict:
    """ONE bounded read: does another outcome already attribute these order ids?

    Two overlapping same-symbol sessions each holding an equal-qty unpriced leg
    would otherwise BOTH claim one UI/orphan sell and the loss guard would double
    count it. Unreadable ⇒ ``readable=False`` ⇒ the caller fails closed."""
    ids = [str(x) for x in (order_ids or []) if str(x or "").strip()]
    if not ids:
        return {"readable": True, "collisions": {}}
    anchor = terminal_at if isinstance(terminal_at, datetime) else datetime.utcnow()
    lo = anchor - timedelta(days=2)
    hi = anchor + timedelta(days=2)
    try:
        rows = db.execute(
            _text(
                "SELECT o.session_id, l ->> 'broker_order_id' AS oid FROM ("
                "  SELECT session_id, broker_recon_detail_json -> 'broker_attribution' -> 'legs' AS legs"
                "  FROM momentum_automation_outcomes"
                "  WHERE session_id <> :sid AND symbol = :symbol"
                "    AND terminal_at >= :lo AND terminal_at < :hi"
                "    AND jsonb_typeof(broker_recon_detail_json -> 'broker_attribution' -> 'legs') = 'array'"
                ") o CROSS JOIN LATERAL jsonb_array_elements(o.legs) AS l"
                " WHERE l ->> 'broker_order_id' = ANY(:oids) LIMIT 20"
            ),
            {"sid": int(session_id), "symbol": str(symbol or ""), "lo": lo, "hi": hi, "oids": ids},
        ).fetchall()
    except Exception as ex:
        logger.warning(
            "[broker_truth_recon] unpriced-leg collision probe unreadable session=%s: %s",
            session_id, ex,
        )
        return {"readable": False, "collisions": {}}
    collisions = {str(r[1]): int(r[0]) for r in (rows or []) if r and r[1] is not None}
    return {"readable": True, "collisions": collisions}


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


_DIVERGENCE_EVENT = "broker_truth_attribution_divergence"


def _emit_divergence_event(
    db: Session,
    outcome: Any,
    sess: Any,
    *,
    detail: dict,
    divergence: Optional[float],
) -> None:
    """Append ONE `broker_truth_attribution_divergence` event per outcome.

    Fires only when the attribution actually found broker fills the FSM ledger
    never held. Best-effort: an audit event must never fail a reconcile pass."""
    attr = detail.get("broker_attribution")
    attr = attr if isinstance(attr, dict) else {}
    missing = attr.get("legs_missing_from_ledger") or []
    if not missing:
        return
    prior = getattr(outcome, "broker_recon_detail_json", None)
    prior = prior if isinstance(prior, dict) else {}
    if prior.get("divergence_event_emitted") or detail.get("divergence_event_emitted"):
        return
    detail["divergence_event_emitted"] = True
    try:
        from .persistence import append_trading_automation_event

        append_trading_automation_event(
            db,
            int(outcome.session_id),
            _DIVERGENCE_EVENT,
            {
                "outcome_id": getattr(outcome, "id", None),
                "symbol": getattr(sess, "symbol", None),
                "attr_status": attr.get("attr_status"),
                "legs_missing_from_ledger": list(missing)[:20],
                "broker_pnl_usd": attr.get("broker_pnl_usd"),
                "ledger_pnl_usd": attr.get("ledger_pnl_usd"),
                "divergence_usd": divergence,
            },
            correlation_id=getattr(sess, "correlation_id", None),
            source_node_id="outcome_reconcile_broker_attribution",
        )
    except Exception as ex:
        detail["divergence_event_emitted"] = False
        logger.debug("[broker_truth_recon] divergence event not appended session=%s: %s",
                     getattr(outcome, "session_id", None), ex)


# ── per-session reconcile (computes the label; no commit) ──────────────────────
def reconcile_one_outcome(
    db: Session,
    outcome: MomentumAutomationOutcome,
    sess: TradingAutomationSession,
    *,
    broker_orders_reader: Optional[BrokerOrdersReader] = None,
    read_plan: Optional[dict] = None,
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
    # STICKY across every pass: the divergence event's idempotency marker. A pass
    # that finds no missing legs (an unreadable broker, a backoff skip) returns
    # early from `_emit_divergence_event` and would otherwise drop the marker from
    # the json it writes — the pass after that would then emit a SECOND event for
    # the same outcome. Carry it before anything else can overwrite `detail`.
    _prior_detail_in = getattr(outcome, "broker_recon_detail_json", None)
    if isinstance(_prior_detail_in, dict) and _prior_detail_in.get("divergence_event_emitted"):
        detail["divergence_event_emitted"] = True
    # Markers this pass RETIRES on purpose. `stamp_recon_detail` merges every
    # other sticky marker forward; these are the ones that must NOT come back.
    sticky_cleared: set[str] = set()

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
        prior_status = getattr(outcome, "broker_recon_status", None)
        prior_was_terminal = prior_status in _TERMINAL_RECON_STATUSES
        prior_pnl = _f(getattr(outcome, "broker_realized_pnl_usd", None))
        prior_notional = _f(getattr(outcome, "broker_notional_basis_usd", None))
        ledger_status = status
        ledger_pnl_snapshot = broker_pnl
        ledger_notional_snapshot = broker_notional
        unpriced = _unpriced_emergency_legs(le)
        # ONE definition of the window, shared with `broker_read_plan`, so the
        # budget is never charged for a read this branch would refuse to make.
        w_start, w_end = _session_attribution_window(sess, outcome)
        reader = broker_orders_reader or _default_alpaca_orders_reader
        # The batch loop already charged (or refused) the budget from THIS plan —
        # reuse it verbatim so the two can never disagree across a clock tick.
        plan = read_plan if isinstance(read_plan, dict) else broker_read_plan(outcome, sess)
        attr: dict[str, Any]
        if not plan.get("read"):
            # NO broker read is spent on this row this pass (see broker_read_plan).
            attr = {"attr_status": str(plan.get("reason") or ATTR_SKIPPED_NO_ENTRY_EVIDENCE), "broker_read": False}
        elif w_start is None or w_end is None:
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
                ledger_legs = _ledger_legs_for_attribution(db, int(outcome.session_id))
                ledger_ids = {oid for oid, _ts in ledger_legs}
                expected_ids = {
                    oid for oid, ts in ledger_legs
                    if ts is None or (w_start <= ts <= w_end)
                }
                entry_anchors, exit_anchors = _envelope_anchor_ids(le)
                attr = attribute_session_broker_orders(
                    session_id=int(outcome.session_id),
                    symbol=str(sess.symbol or ""),
                    side_long=side_long,
                    orders=list(listing.get("orders") or []),
                    unpriced_legs=unpriced,
                    window_start=w_start,
                    window_end=w_end,
                    ledger_order_ids=ledger_ids,
                    owned_order_ids=_owned_order_ids_from_le(le),
                    ledger_owned_order_ids=ledger_ids,
                    expected_listing_order_ids=expected_ids,
                    entry_anchor_ids=entry_anchors,
                    exit_anchor_ids=exit_anchors,
                )
                # A readable listing that owns NOTHING while the session's ledger
                # holds broker-confirmed legs is the same inconsistency as a
                # missing id: the read does not cover the session.
                if attr.get("attr_status") == ATTR_NO_OWNED_FILLS and expected_ids:
                    attr["attr_status"] = ATTR_LISTING_INCOMPLETE
                    attr["ledger_ids_missing_from_broker"] = sorted(expected_ids)
                    attr["no_owned_fills_with_ledger_legs"] = True
                # CROSS-SESSION GUARD: never stamp a NON-CID attribution on a
                # broker order id another outcome already claims. A cid names
                # exactly one session and is exempt; an unpriced-leg qty match and
                # an envelope-id match are both guesses about WHICH session an
                # anonymous fill belongs to, and two overlapping same-symbol
                # sessions can each hold the same id (an emergency leg or an
                # orphan-repair close recorded on both envelopes) — attributing it
                # twice double counts it straight into the loss guard's day total.
                # `session_ledger_order_id` is included: `momentum_fill_outcomes`
                # is per-session, but an orphan repair can record ONE broker order
                # under two sessions, and that would double count it into the day
                # total exactly like an envelope-id collision.
                _NON_CID_ATTRIBUTIONS = (
                    "unpriced_emergency_leg_match",
                    "session_broker_order_id",
                    "session_ledger_order_id",
                )
                fallback_ids = [
                    l["broker_order_id"] for l in attr.get("legs") or []
                    if l.get("attribution") in _NON_CID_ATTRIBUTIONS
                ]
                if fallback_ids:
                    probe = _broker_order_ids_attributed_elsewhere(
                        db,
                        session_id=int(outcome.session_id),
                        symbol=str(sess.symbol or ""),
                        order_ids=fallback_ids,
                        terminal_at=_naive_utc(getattr(outcome, "terminal_at", None)),
                    )
                    if not probe.get("readable"):
                        attr["attr_status"] = ATTR_AMBIGUOUS
                        attr["unpriced_collision_probe"] = "unreadable"
                    elif probe.get("collisions"):
                        attr["attr_status"] = ATTR_AMBIGUOUS
                        attr["unpriced_collision"] = probe["collisions"]
        attr["window_start_utc"] = w_start.isoformat() if w_start else None
        attr["window_end_utc"] = w_end.isoformat() if w_end else None
        attr["ledger_pnl_usd"] = ledger_pnl_snapshot
        attr["ledger_notional_usd"] = ledger_notional_snapshot
        attr["ledger_status"] = ledger_status
        detail["broker_attribution"] = attr
        a_status = attr.get("attr_status")
        if plan.get("read"):
            # A read was actually SPENT this pass. Whatever it says supersedes an
            # older proof, so the inherited "one readable listing proved this
            # session owns nothing" claim is retired here and re-earned below ONLY
            # by another `no_owned_fills` verdict. Without this the merge would
            # carry the marker across an `unreadable` re-read (a re-read the
            # changed `terminal_at` forced) and pin the row on a permanent skip
            # that no readable empty listing ever justified.
            sticky_cleared.add("attribution_no_entry_evidence_proven_empty")
        # POSITIVE evidence that the ledger label is wrong: an owned CLOSING fill
        # the ledger never recorded, or an unpriced emergency leg still unbooked.
        # "I could not see a close" is an absence of evidence and never demotes.
        evidence_of_missing_legs = bool(unpriced) or bool(attr.get("closing_legs_missing_from_ledger"))

        def _demotion_allowed() -> bool:
            """A terminal label (`reconciled` / `fee_unconfirmed` — the orphan-repair
            shape) is never knocked down to a non-terminal status on a re-touch
            unless something positively says it is wrong."""
            return (not prior_was_terminal) or evidence_of_missing_legs

        def _keep_prior_label(marker: str) -> None:
            nonlocal status, broker_pnl, broker_notional, fees_status, source
            status = str(prior_status)
            broker_pnl = prior_pnl if prior_pnl is not None else ledger_pnl_snapshot
            broker_notional = prior_notional if prior_notional is not None else ledger_notional_snapshot
            source = "prior_label_preserved"
            fees_status = "prior_label_preserved"
            detail[marker] = True

        def _arm_attribution_retry() -> None:
            """Every verdict that did NOT converge must say when it may read again.

            Without this, `unreadable` / `truncated` / `listing_incomplete` /
            `residual_open` / `oversold` re-listed the broker every 60 s forever
            (they never stamp an `attribution_version`, so `needs_reconcile` keeps
            admitting them). An unbounded set of such rows — one credential
            rotation, one delisted symbol, one never-closing residual — holds the
            entire 20-read budget every pass and starves everything below it.

            The horizon is SHORT while the loss guard is disarmed behind the row
            (retrying is the only way the account gets its arming back) and LONG
            when the guard skips the row outright.
            """
            # NOTE: no `entry_event_seen` here on purpose. The delegation already
            # corrects this consumer's notional / snapshot-proof classification,
            # and the remaining lost-adoption case would cost a DB read inside a
            # helper the batch loop needs to stay cheap. The horizon is bounded
            # either way; the ALARM gate below is where the events probe belongs.
            blocking = _loss_guard_can_block(outcome, sess)
            try:
                attempts = int(
                    (getattr(outcome, "broker_recon_detail_json", None) or {}).get("attribution_attempts") or 0
                )
            except (TypeError, ValueError, AttributeError):
                attempts = 0
            attempts += 1
            base = _attribution_backoff_seconds(blocking=blocking)
            if not blocking:
                # A row the guard ignores may escalate; capped at an hour so the
                # backoff can never become an effectively permanent stop.
                base = min(base * max(1, min(attempts, 4)), 3600)
            detail["attribution_attempts"] = attempts
            detail["attribution_retry_blocking"] = blocking
            detail["attribution_next_retry_utc"] = (
                datetime.utcnow() + timedelta(seconds=base)
            ).isoformat()
            t_at = _naive_utc(getattr(outcome, "terminal_at", None))
            detail["attribution_terminal_at"] = t_at.isoformat() if t_at else None

        if a_status == ATTR_FLAT:
            broker_pnl = _f(attr.get("broker_pnl_usd"))
            broker_notional = _f(attr.get("open_notional_usd"))
            source = "broker_orders_attributed"
            # Alpaca order objects carry no per-fill fee field; the account is
            # commission-free and the ledger path already labels its 0.0 fees
            # `known`. Same posture here — gross == net for this venue.
            fees_status = "alpaca_commission_free_gross"
            if prior_status == STATUS_FEE_UNCONFIRMED:
                # An orphan-repair / fee-unconfirmed row keeps its EXCLUDED label;
                # attribution only sharpens its numbers, it never promotes a row
                # whose fee truth was deliberately marked unsettled.
                status = STATUS_FEE_UNCONFIRMED
                detail["prior_label_preserved_fee_unconfirmed"] = True
            else:
                status = STATUS_RECONCILED
            detail["attribution_version"] = ATTRIBUTION_VERSION
            # Converged: drop any horizon a previous non-converged pass armed so a
            # stale one can never hold a settled row out of a later re-touch.
            for _k in ("attribution_next_retry_utc", "attribution_attempts", "attribution_retry_blocking"):
                detail.pop(_k, None)
                sticky_cleared.add(_k)
            if attr.get("legs_missing_from_ledger"):
                logger.warning(
                    "[broker_truth_recon] session=%s symbol=%s: %d broker fill(s) missing from "
                    "the FSM ledger attributed by session cid/oco leg/unpriced-leg match; broker "
                    "pnl=%.2f vs ledger pnl=%s",
                    outcome.session_id, sess.symbol, len(attr["legs_missing_from_ledger"]),
                    broker_pnl or 0.0, attr.get("ledger_pnl_usd"),
                )
        elif a_status in (ATTR_LISTING_INCOMPLETE, ATTR_ANCHORS_MISSING):
            # HEADLINE: the listing is missing this session's OWN ledger legs (or a
            # whole cycle's envelope anchors) → wrong bound account generation /
            # wrong window / empty page. Never certify, never stamp
            # attribution_version (retry next pass).
            logger.warning(
                "[broker_truth_recon] ledger ids missing from listing session=%s symbol=%s "
                "reason=%s ledger_ids=%s entry_anchors=%s exit_anchors=%s window=%s..%s — read "
                "does NOT cover the session; refusing to certify",
                outcome.session_id, sess.symbol, a_status,
                attr.get("ledger_ids_missing_from_broker"),
                attr.get("entry_anchors_missing_from_broker"),
                attr.get("exit_anchors_missing_from_broker"),
                attr.get("window_start_utc"), attr.get("window_end_utc"),
            )
            if _demotion_allowed():
                status = STATUS_BROKER_UNAVAILABLE
                broker_pnl = None
                broker_notional = None
                source = "broker_orders_attributed"
            else:
                _keep_prior_label("attribution_listing_incomplete_label_preserved")
            _arm_attribution_retry()
        elif a_status == ATTR_RESIDUAL_OPEN:
            if _demotion_allowed():
                status = STATUS_RESIDUAL_OPEN
                broker_pnl = None
                broker_notional = None
                source = "broker_orders_attributed"
                detail["attribution_version"] = ATTRIBUTION_VERSION
            else:
                _keep_prior_label("attribution_residual_open_label_preserved")
                detail["attribution_version"] = ATTRIBUTION_VERSION
            # `residual_open` is NOT converged — it is "the close is not visible
            # yet" and the row is re-read until it is. Bound that: an OCO leg the
            # walker cannot see, or a genuinely never-closed session, would
            # otherwise hold a budget slot every 60 s for the whole lookback.
            _arm_attribution_retry()
        elif a_status in (ATTR_OVERSOLD, ATTR_AMBIGUOUS):
            if _demotion_allowed():
                status = STATUS_AMBIGUOUS_TRADE
                broker_pnl = None
                broker_notional = None
                source = "broker_orders_attributed"
                detail["attribution_version"] = ATTRIBUTION_VERSION
            else:
                _keep_prior_label("attribution_ambiguous_label_preserved")
                detail["attribution_version"] = ATTRIBUTION_VERSION
            _arm_attribution_retry()
        elif a_status == ATTR_NO_OWNED_FILLS:
            # Broker READABLE and the ledger has no legs to contradict it: nothing
            # was ever filled for this session. Stamp the version AND a retry
            # horizon so the row stops burning a broker read every 60 s.
            detail["attribution_version"] = ATTRIBUTION_VERSION
            _arm_attribution_retry()
            # Envelope shows nothing AND the broker owned nothing: two independent
            # negatives. THAT earns the permanent skip — never the envelope alone.
            if not _has_entry_evidence(le):
                detail["attribution_no_entry_evidence_proven_empty"] = True
        elif a_status in (ATTR_SKIPPED_NO_ENTRY_EVIDENCE, ATTR_SKIPPED_BACKOFF, ATTR_SKIPPED_NO_WINDOW):
            # No read spent. Keep the ledger verdict exactly.
            detail["attribution_read_skipped"] = a_status
            # The skip reasons carry their sticky markers forward verbatim.
            # `attribution_no_entry_evidence_proven_empty` in particular: dropping
            # it would make the next pass read the row again, re-stamp it, and skip
            # again — an every-other-pass read loop, which is the budget churn the
            # skip exists to prevent. The retry horizon is carried, never
            # recomputed, so the skips can never push it further out.
            #
            # ⚠️ `attribution_version` is NOT in that list, and must never be. A
            # skip attributes NOTHING, and the version is what makes a TERMINAL
            # status immutable (`needs_reconcile`). Carrying it meant: arm a
            # 30-minute backoff off an empty listing while the ledger is still
            # empty → the exit leg settles inside that window → the next skip
            # writes `reconciled` (from the ledger alone) AND the inherited
            # version → the row is terminal forever, never attributed. That is the
            # CANF-19471 −78.13-instead-of-−186.98 undercount, minted by the very
            # mechanism added to protect the budget.
            prior_detail = getattr(outcome, "broker_recon_detail_json", None)
            prior_detail = prior_detail if isinstance(prior_detail, dict) else {}
            prior_attr = prior_detail.get("broker_attribution")
            prior_attr = prior_attr if isinstance(prior_attr, dict) else {}
            # Only a FLAT verdict earns its version off ACTUALLY attributed broker
            # legs. Any other stamped version is a "nothing to attribute (yet)"
            # verdict and must not survive the ledger becoming a closed round-trip.
            earned_on_broker_legs = prior_attr.get("attr_status") == ATTR_FLAT
            ledger_now_terminal = status in _TERMINAL_RECON_STATUSES
            already_attributed = earned_on_broker_legs
            already_released = bool(prior_detail.get("attribution_ledger_release_done"))
            if ledger_now_terminal and not earned_on_broker_legs and not already_released:
                # The ledger settled into a closed round-trip while the row was
                # backed off. That is exactly the state that MUST be checked
                # against broker truth, so release the horizon and read next pass.
                #
                # ⚠️ The PROOF is released too, not just the horizon.
                # `broker_read_plan` tests `attribution_no_entry_evidence_proven_empty`
                # BEFORE it ever looks at `attribution_next_retry_utc`, so carrying
                # the proof through here made this whole branch a NO-OP for the one
                # shape it was written for: the row said "read next pass" and then
                # never read again. A settled closed round-trip in the ledger is a
                # direct contradiction of "a readable listing proved this session
                # owned nothing", so the proof has to be re-earned.
                detail["attribution_backoff_released"] = "ledger_settled_terminal"
                if prior_detail.get("attribution_terminal_at") is not None:
                    detail["attribution_terminal_at"] = prior_detail["attribution_terminal_at"]
                for k in (
                    "attribution_next_retry_utc",
                    "attribution_attempts",
                    "attribution_retry_blocking",
                    "attribution_no_entry_evidence_proven_empty",
                ):
                    detail.pop(k, None)
                    sticky_cleared.add(k)
                # ONE-SHOT, and durable so it survives every later write. Without
                # it a re-read that comes back `no_owned_fills` re-stamps the proof,
                # the next skip finds the ledger still terminal and releases AGAIN
                # — a broker read every other pass forever, which is precisely the
                # budget churn the permanent skip exists to prevent.
                detail["attribution_ledger_release_done"] = True
            else:
                for k in (
                    "attribution_next_retry_utc",
                    "attribution_terminal_at",
                    "attribution_attempts",
                    "attribution_retry_blocking",
                    "attribution_no_entry_evidence_proven_empty",
                ):
                    if prior_detail.get(k) is not None:
                        detail[k] = prior_detail[k]
                if already_attributed and prior_detail.get("attribution_version") is not None:
                    detail["attribution_version"] = prior_detail["attribution_version"]
        else:
            # unreadable / truncated: transient. If there is evidence of legs the
            # ledger never saw, the ledger label is KNOWN-wrong → do not certify
            # it; retry next pass. Otherwise keep the ledger verdict (no
            # attribution_version → re-attempted after the armed horizon).
            if evidence_of_missing_legs and status in _TERMINAL_RECON_STATUSES:
                status = STATUS_BROKER_UNAVAILABLE
                broker_pnl = None
                broker_notional = None
            _arm_attribution_retry()

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

    # GAP B: ONE audit event per outcome when broker truth supersedes the lane's
    # self-report with fills the FSM ledger never saw. Idempotency guard: the
    # marker rides in the detail json the same pass writes, so a re-touch (or a
    # replayed pass) can never emit a second event for the same outcome.
    _emit_divergence_event(db, outcome, sess, detail=detail, divergence=divergence)

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
    # Structural persistence of the skip/backoff markers — see
    # STICKY_RECON_DETAIL_KEYS. A branch that reached this point without
    # re-deriving a marker keeps the one already on the row instead of erasing it.
    detail = stamp_recon_detail(outcome, detail, cleared=sticky_cleared)
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
            .order_by(MomentumAutomationOutcome.terminal_at.desc())
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
    proof_reads = 0
    skipped_no_read_needed = 0
    starved_blind = 0
    # Budget-deferred rows the loss guard provably SKIPS (never-entered class, no
    # economic evidence). They drain over a few passes and are not an alarm.
    deferred_unblocking = 0
    # ORDERING (never heap order): the rows the LOSS GUARD is waiting on take the
    # budget FIRST, newest first. The unordered `.all()` let ~115 no-fill rows
    # shuffle a newly terminal FILLED session out of the 20-read budget.
    #
    # The key must be TOTAL — the previous `try/except: pass` around the sort meant
    # any single bad row silently reverted the whole pass to SQL order, i.e. the
    # prioritisation this fix exists for could vanish with no log line.
    # `datetime.timestamp()` on a naive datetime is exactly such a hazard on
    # Windows (it goes through the platform localtime conversion), so the key is
    # computed by subtraction instead.
    def _sort_key(pair):
        outcome, sess = pair
        t = getattr(outcome, "terminal_at", None)
        if isinstance(t, datetime):
            if t.tzinfo is not None:
                t = t.astimezone(timezone.utc).replace(tzinfo=None)
            t_key = (t - _EPOCH_NAIVE).total_seconds()
        else:
            t_key = 0.0
        return (_attribution_priority(outcome, sess), -t_key)

    rows = sorted(rows, key=_sort_key)
    # The half of `_loss_history_entry_classification` that is not derivable from
    # the ORM objects in hand. One bounded query for the batch; `None` = unknown,
    # and unknown must count as blocking (see `_entry_event_session_ids`).
    entry_event_ids = _entry_event_session_ids(db, [o.session_id for o, _ in rows])
    for outcome, sess in rows:
        checked += 1
        if not needs_reconcile(outcome, sess):
            skipped_terminal += 1
            by_status[str(outcome.broker_recon_status)] = by_status.get(str(outcome.broker_recon_status), 0) + 1
            continue
        plan = broker_read_plan(outcome, sess)
        if plan.get("read"):
            if broker_reads >= budget:
                skipped_budget += 1
                # THE gauge that matters: a row the loss guard cannot use that
                # lost its read. `skipped_broker_budget` alone is benign (the
                # backfill class queues there by design); this counter is not.
                #
                # `not _loss_guard_label_usable` ALONE is not that gauge. It is
                # true for every class-3 no-entry-evidence row too, and those the
                # guard SKIPS outright (`_loss_history_entry_classification` →
                # `not_entered`), so they can never gap the day or disarm the
                # lane. Measured 2026-09-02: 95 `cancelled_pre_entry` rows queued
                # behind the budget fired "LOSS-GUARD ROWS STARVED" on every
                # single pass — alarm fatigue on the one alarm that matters (the
                # 85-minute arming outage). `_loss_guard_can_block` DELEGATES to
                # `_loss_history_entry_classification` itself — never a
                # hand-written mirror of it — and anything it cannot answer counts
                # as blocking, so the alarm can never go quiet on a genuinely
                # blind row.
                if not _loss_guard_label_usable(outcome):
                    seen = True if entry_event_ids is None else (int(outcome.session_id) in entry_event_ids)
                    if _loss_guard_can_block(outcome, sess, entry_event_seen=seen):
                        starved_blind += 1
                    else:
                        deferred_unblocking += 1
                continue
            broker_reads += 1
            if plan.get("reason") == "no_entry_evidence_proof_read":
                # One-time, lowest priority. Visible so the operator can watch it
                # drain instead of mistaking it for the starvation it replaced.
                proof_reads += 1
        elif plan.get("reason") not in ("not_alpaca_attribution",):
            # Alpaca row that spends NO broker read this pass (no entry evidence /
            # backoff). It is still reconciled from the ledger — it just does not
            # compete for the budget.
            skipped_no_read_needed += 1
        try:
            detail = reconcile_one_outcome(db, outcome, sess, read_plan=plan)
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
        "loss_guard_blind_starved": starved_blind,
        "skipped_budget_guard_skips_row": deferred_unblocking,
        "skipped_no_broker_read_needed": skipped_no_read_needed,
        "broker_reads": broker_reads,
        "broker_proof_reads": proof_reads,
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
    if starved_blind:
        # HEADLINE: rows the loss guard cannot use lost their broker read to the
        # per-pass budget. Every one of them is an account-wide
        # `loss_guard_history_unavailable` for as long as it stays unresolved —
        # the 85-minute arming outage of 2026-09-02.
        logger.warning(
            "[broker_truth_recon] LOSS-GUARD ROWS STARVED BY THE READ BUDGET: %d row(s) the guard "
            "cannot use AND cannot skip lost their broker read (budget=%d, reads=%d; a further %d "
            "budget-deferred row(s) are ones the guard skips outright and are NOT part of this "
            "alarm). Raise chili_momentum_outcome_recon_broker_attribution_max_per_pass or "
            "investigate the rows holding the slots.",
            starved_blind, budget, broker_reads, deferred_unblocking,
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
