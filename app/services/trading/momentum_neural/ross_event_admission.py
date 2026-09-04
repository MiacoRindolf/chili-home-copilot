"""Event-driven Ross small-cap admission into the guarded live runner.

This module does not place orders. It turns a proven Ross event into the same
``begin_live_arm -> confirm_live_arm`` flow used by operator/auto-arm code, then
ticks the newly queued watcher so the live runner can evaluate tick-level entry
state immediately instead of waiting for the next scheduler pass.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationEvent, TradingAutomationSession
from .live_fsm import LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY, LIVE_RUNNER_RUNNABLE_STATES
from .tick_scalp import (
    independent_smallcap_a_plus_evidence_ok,
    ross_signal_for_symbol,
    ross_tick_scalp_evidence_ok,
)
from .universe import (
    EQUITY_ROSS_SMALLCAP,
    _f as _universe_float,
    _iqfeed_dollar_volumes,
    _premarket_change_pct,
    _snapshot_adv_shares,
    _snapshot_basis_rejected,
    _snapshot_today_shares,
    ross_smallcap_profile_evidence,
)

logger = logging.getLogger(__name__)

_LAST_ATTEMPT_MONOTONIC: dict[tuple[str, str], float] = {}
_COOLDOWN_LOCK = threading.Lock()


def _sym(value: Any) -> str:
    return str(value or "").strip().upper()


def _event_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_ross_event_admission_enabled", True))


def _cooldown_seconds() -> float:
    try:
        return max(
            0.0,
            float(getattr(settings, "chili_momentum_ross_event_admission_cooldown_seconds", 2.0) or 0.0),
        )
    except (TypeError, ValueError):
        return 2.0


def _tick_count() -> int:
    try:
        return max(
            0,
            min(5, int(getattr(settings, "chili_momentum_ross_event_admission_tick_count", 1) or 1)),
        )
    except (TypeError, ValueError):
        return 1


def _utc_naive(now: datetime | None = None) -> datetime:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _auto_user_id() -> int | None:
    from .auto_arm import _auto_arm_user_id

    return _auto_arm_user_id()


def _symbol_market_open(symbol: str) -> bool:
    from .auto_arm import _symbol_market_open as _auto_arm_symbol_market_open

    return bool(_auto_arm_symbol_market_open(symbol))


def _equity_execution_family() -> str:
    from ..execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP

    return EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP


def _fetch_snapshot_row(symbol: str) -> dict[str, Any] | None:
    try:
        from ...massive_client import get_full_market_snapshot

        snapshot = get_full_market_snapshot(
            max_age_seconds=EQUITY_ROSS_SMALLCAP.snapshot_max_age_seconds
        ) or []
    except Exception:
        logger.debug("[ross_event_admission] snapshot fetch failed symbol=%s", symbol, exc_info=True)
        return None
    sym = _sym(symbol)
    for row in snapshot:
        if isinstance(row, dict) and _sym(row.get("ticker") or row.get("symbol")) == sym:
            return row
    return None


# ── IGNITION SIGNAL (2026-09-04) ─────────────────────────────────────────────
# THE MEASURED PROBLEM. The 79-video forensic cross-reference of Ross Cameron's
# trades against this DB found ADMISSION LATENCY to be the largest structural
# class of missed winners: the median symbol this lane arms is already +15.6%
# (p90 +44.7%) when it is FIRST admitted to the live universe. On 2026-07-27
# Ross's entire VTIX round trip ran 13:17:15-13:19:15Z; CHILI's universe did not
# contain the symbol until 13:31:36Z.
#
# The discovery loop is closed: every other event source can only fire on a
# symbol ALREADY inside ``build_equity_universe`` (a poll of a market snapshot).
# ``admit_ross_event`` is the ONE path that can create a viability row for a
# symbol with none — and it produced 0 ``ross_event_admitted`` events across
# 3,379 live sessions in 14 days. The reason is visible in the call: the
# ignition NOTIFY carries ``last_price`` / ``pct_change_60s`` / ``dollar_vol_60s``
# from the host bridge's own tape, and the loop threw all three away
# (``signal=None``), so the universe proof fell back entirely on the Massive
# snapshot row and failed closed with ``ross_universe_missing_price`` /
# ``ross_universe_missing_dollar_volume`` — exactly for the newly-igniting names
# the snapshot has not caught up with yet.
#
# THIS BUILDER IS NOT A RELAXATION. Price AND dollar volume must still both be
# proven or the symbol is still refused; what changes is WHERE the proof may come
# from. Fail-closed properties, each covered by a test:
#   * no usable last print price  -> None (no signal; the caller behaves as today)
#   * no dollar-volume source     -> the signal carries NO dollar volume, so
#                                    ``ross_universe_missing_dollar_volume`` still
#                                    fires. The $1M floor is the tradability bar
#                                    and is never faked from a 60-second number.
#   * rejected day-change basis   -> change_pct is None, never synthesised. A
#                                    since-subscription move is NOT a day change
#                                    (#1284 burned this repo once already).
_IGNITION_SIGNAL_SOURCE = "iqfeed_ignition_tick"
# Every token here is one the EXISTING recognisers already match — see
# ``_independent_smallcap_a_plus_source`` / ``_live_tape_signal_from_universe``
# above and ``tick_scalp._source_text`` consumers. Deliberately contains no
# "ross"/"warrior" token so ``_explicit_ross_source`` stays False and the
# independent small-cap A+ proof remains available to a tape-only nomination.
_IGNITION_SIGNAL_SCANNER_SOURCE = "iqfeed ignition_tick tape_delta_ignite"


def _positive(value: Any) -> float | None:
    """The value as a positive finite float, else None."""
    num = _universe_float(value)
    if num is None or not math.isfinite(num) or num <= 0:
        return None
    return num


def ignition_signal_from_payload(
    payload: dict[str, Any] | None,
    *,
    tape_dollar_volume: float | None,
    snapshot_row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """PURE: the Ross signal an ignition NOTIFY payload can prove on its own.

    ``payload`` is the validated ``chili.iqfeed-ignition-nominate.v1`` dict from
    ``live_runner_loop._validated_ignition_notify``. ``tape_dollar_volume`` is
    the since-midnight $-volume for this symbol from the trade tape (see
    ``universe._iqfeed_dollar_volumes``; may be None — the tape query fails open).
    ``snapshot_row`` is the Massive full-market row when one exists, else None.

    Returns the signal dict, or None when the payload cannot even name a price.
    No I/O, no clock, no settings — every input is an argument.
    """
    if not isinstance(payload, dict):
        return None
    symbol = _sym(payload.get("symbol"))
    price = _positive(payload.get("last_price"))
    if not symbol or price is None:
        return None
    snap = snapshot_row if isinstance(snapshot_row, dict) else None

    # DOLLAR VOLUME — the same MONOTONIC MAX rule ``build_equity_universe`` uses
    # at universe.py:1023 (``max(price * vol, _iqfeed_dvols.get(...))``): a leg
    # can only ADD a missed mover, never remove a current one. Three legs, all
    # day-scale turnover:
    #   1. the trade tape's since-midnight $-volume (ground truth premarket, when
    #      the Massive day/min aggregates are still zero),
    #   2. the snapshot's TODAY shares x price (day.v, else min.av which counts
    #      extended-hours prints — ``_snapshot_today_shares``),
    #   3. the snapshot's baseline average shares x price (prevDay.v —
    #      ``_snapshot_adv_shares``), the name's ordinary daily turnover.
    # ``dollar_vol_60s`` from the payload is deliberately NOT a leg: it is a
    # SIXTY-SECOND number and the $1M profile floor asks whether a full position
    # can be exited today. Feeding it in would fake the tradability bar.
    dv_legs: dict[str, float] = {}
    tape_dv = _positive(tape_dollar_volume)
    if tape_dv is not None:
        dv_legs["tape_since_midnight"] = tape_dv
    if snap is not None:
        today_shares = _positive(_snapshot_today_shares(snap))
        if today_shares is not None:
            dv_legs["snapshot_today_volume"] = price * today_shares
        adv_shares = _positive(_snapshot_adv_shares(snap))
        if adv_shares is not None:
            dv_legs["snapshot_average_volume"] = price * adv_shares
    dollar_volume: float | None = None
    dollar_volume_leg: str | None = None
    if dv_legs:
        dollar_volume_leg = max(dv_legs, key=lambda key: dv_legs[key])
        dollar_volume = dv_legs[dollar_volume_leg]

    # TODAY'S SHARE VOLUME is reported ONLY from today-scale legs. The average
    # -volume leg above may prove tradability, but yesterday's share count is not
    # today's volume and must never be published as one (the downstream
    # independent-A+ proof has its own share-volume floor).
    today_dv_legs = [
        dv_legs[key]
        for key in ("tape_since_midnight", "snapshot_today_volume")
        if key in dv_legs
    ]
    day_volume = (max(today_dv_legs) / price) if today_dv_legs else None

    # DAY CHANGE — snapshot only, and only when the day basis survives the
    # existing guard. ``_premarket_change_pct`` is the same fallback
    # ``build_equity_universe`` uses when the vendor field is null (it carries its
    # own basis guard and its own kill switch), which is what lets a 09:17 ET
    # igniter be graded at all. When neither is available the value stays None and
    # the caller lands on ``ross_universe_missing_change_pct`` — fail-closed.
    change_pct: float | None = None
    change_source: str | None = None
    basis_rejected = _snapshot_basis_rejected(snap) if snap is not None else None
    if snap is not None and not basis_rejected:
        vendor_change = _universe_float(snap.get("todaysChangePerc"))
        if vendor_change is not None:
            change_pct, change_source = vendor_change, "snapshot_todays_change_perc"
        else:
            premarket_change = _premarket_change_pct(snap)
            if premarket_change is not None:
                change_pct, change_source = premarket_change, "snapshot_premarket_basis"

    # VELOCITY — the honest short-horizon axis. The producer reports
    # ``pct_change_60s`` as a FRACTION (IgnitionConfig.pct_base = 0.05 means +5%);
    # the velocity leg of ``ross_smallcap_profile_evidence`` compares
    # ``velocity_pct`` against ``chili_momentum_velocity_intake_min_pct``, which is
    # expressed in PERCENT (7.0 = +7%). The x100 is the unit conversion, and it is
    # the whole reason this axis is usable instead of a fabricated day change.
    velocity_fraction = _universe_float(payload.get("pct_change_60s"))
    velocity_pct = velocity_fraction * 100.0 if velocity_fraction is not None else None

    signal: dict[str, Any] = {
        "ticker": symbol,
        "symbol": symbol,
        "price": price,
        "last_price": price,
        "source": _IGNITION_SIGNAL_SOURCE,
        "scanner_source": _IGNITION_SIGNAL_SCANNER_SOURCE,
        "ignition_dollar_vol_60s": _universe_float(payload.get("dollar_vol_60s")),
        "ignition_prints_10s": payload.get("prints_10s"),
        "ignition_fired_at": payload.get("fired_at"),
        "ignition_dollar_volume_leg": dollar_volume_leg,
        "ignition_change_source": change_source,
        "ignition_snapshot_basis_rejected": basis_rejected,
    }
    if dollar_volume is not None:
        signal["dollar_volume"] = dollar_volume
    if day_volume is not None:
        signal["day_volume"] = day_volume
        signal["volume"] = day_volume
    if change_pct is not None:
        signal["daily_change_pct"] = change_pct
        signal["todays_change_perc"] = change_pct
        signal["change_pct"] = change_pct
    if velocity_pct is not None:
        signal["velocity_pct"] = velocity_pct
    return signal


def ignition_admission_signal(
    payload: dict[str, Any] | None,
    *,
    snapshot_provider: Callable[[str], dict[str, Any] | None] | None = None,
    tape_dollar_volume_provider: Callable[[list[str]], dict[str, float]] | None = None,
) -> dict[str, Any] | None:
    """Gather the two external facts the pure builder needs, then build.

    Thin I/O shell so ``ignition_signal_from_payload`` stays pure and testable.
    Both providers fail open: the tape helper already returns ``{}`` on any error
    or on its own 3s statement timeout, and a missing snapshot row is the normal
    case for exactly the symbols this change exists to admit.
    """
    if not isinstance(payload, dict):
        return None
    symbol = _sym(payload.get("symbol"))
    if not symbol:
        return None
    tape_dollar_volume: float | None = None
    try:
        fetch_dvols = tape_dollar_volume_provider or _iqfeed_dollar_volumes
        tape_dollar_volume = (fetch_dvols([symbol]) or {}).get(symbol)
    except Exception:
        logger.debug(
            "[ross_event_admission] ignition tape $-volume failed symbol=%s",
            symbol,
            exc_info=True,
        )
    snapshot_row: dict[str, Any] | None = None
    try:
        snapshot_row = (snapshot_provider or _fetch_snapshot_row)(symbol)
    except Exception:
        logger.debug(
            "[ross_event_admission] ignition snapshot fetch failed symbol=%s",
            symbol,
            exc_info=True,
        )
    return ignition_signal_from_payload(
        payload,
        tape_dollar_volume=tape_dollar_volume,
        snapshot_row=snapshot_row,
    )


def _merged_signal(symbol: str, signal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(signal, dict):
        return None
    out = dict(signal)
    out.setdefault("ticker", symbol)
    out.setdefault("symbol", symbol)
    return out


def _prove_ross_universe(
    symbol: str,
    *,
    signal: dict[str, Any] | None,
    snapshot_row: dict[str, Any] | None,
    snapshot_provider: Callable[[str], dict[str, Any] | None] | None,
) -> tuple[bool, str, dict[str, Any], dict[str, Any] | None]:
    ok, reason, debug = ross_smallcap_profile_evidence(
        symbol,
        signal=signal,
        snapshot_row=snapshot_row,
    )
    if ok:
        return True, str(reason), dict(debug or {}), snapshot_row
    if snapshot_row is None and str(reason).startswith("ross_universe_missing_"):
        provider = snapshot_provider or _fetch_snapshot_row
        row = provider(symbol)
        if row is not None:
            ok2, reason2, debug2 = ross_smallcap_profile_evidence(
                symbol,
                signal=signal,
                snapshot_row=row,
            )
            return bool(ok2), str(reason2), dict(debug2 or {}), row
    return False, str(reason), dict(debug or {}), snapshot_row


def _live_tape_signal_from_universe(
    symbol: str,
    *,
    source: str,
    universe_debug: dict[str, Any],
) -> dict[str, Any] | None:
    """Build setup evidence from live tape after universe proof succeeded.

    IQFeed/quote events can arrive before the symbol has a persisted
    ``extra.ross_signals`` payload. Once the hard Ross universe gate has already
    proven price, liquidity, and in-play change, use those facts as the scalp
    evidence seed so the event-driven path does not fall back to a stale DB row.
    """
    source_text = str(source or "").strip().lower()
    if not any(token in source_text for token in ("iqfeed", "tape", "ignition", "nbbo")):
        return None
    price = universe_debug.get("price")
    change_pct = universe_debug.get("change_pct")
    dollar_volume = universe_debug.get("dollar_volume")
    if price is None or change_pct is None or dollar_volume is None:
        return None
    sig: dict[str, Any] = {
        "ticker": symbol,
        "symbol": symbol,
        "price": price,
        "last_price": price,
        "daily_change_pct": change_pct,
        "todays_change_perc": change_pct,
        "change_pct": change_pct,
        "dollar_volume": dollar_volume,
        "source": source or "live_tape",
        "scanner_source": f"{source or 'live_tape'} tape_delta_ignite ross_universe",
    }
    try:
        px = float(price)
        dv = float(dollar_volume)
        if px > 0 and dv > 0:
            sig["day_volume"] = dv / px
            sig["volume"] = dv / px
    except (TypeError, ValueError):
        pass
    return sig


def _independent_smallcap_a_plus_source(source: str, signal: dict[str, Any] | None) -> bool:
    source_text = " ".join(
        str(part or "")
        for part in (
            source,
            (signal or {}).get("source") if isinstance(signal, dict) else "",
            (signal or {}).get("scanner_source") if isinstance(signal, dict) else "",
            (signal or {}).get("signal_type") if isinstance(signal, dict) else "",
        )
    ).lower()
    return any(
        token in source_text
        for token in (
            "iqfeed",
            "tape",
            "nbbo",
            "ignition",
            "ws_ignition",
            "running_up",
            "running up",
        )
    )


def _explicit_ross_source(signal: dict[str, Any] | None) -> bool:
    if not isinstance(signal, dict):
        return False
    source_text = " ".join(
        str(signal.get(key) or "")
        for key in ("source", "scanner_source", "signal_type", "alert_name", "strategy")
    ).lower()
    return any(
        token in source_text
        for token in (
            "ross",
            "warrior",
            "5 pillar",
            "5 pillars",
            "five pillar",
            "ross_audio_transcript",
        )
    )


def _transcript_context_invalid(signal: dict[str, Any] | None) -> bool:
    if not isinstance(signal, dict):
        return False
    signal_type = str(signal.get("signal_type") or "").strip().lower()
    source = " ".join(
        str(signal.get(k) or "")
        for k in ("source", "scanner_source")
    ).lower()
    if signal_type != "ross_transcript_mention" and "ross_audio_transcript" not in source:
        return False
    text = str(signal.get("transcript_text") or "").strip()
    if not text:
        return True
    try:
        from .ross_transcript_bridge import has_trading_context

        return not bool(has_trading_context(text))
    except Exception:
        return True


def _demote_invalid_transcript_candidate(candidate: Any) -> None:
    if candidate is None:
        return
    try:
        candidate.live_eligible = False
    except Exception:
        pass
    try:
        candidate.paper_eligible = False
    except Exception:
        pass
    try:
        explain = getattr(candidate, "explain_json", None)
        if not isinstance(explain, dict):
            explain = {}
        explain["demoted_reason"] = "ross_transcript_context_rejected"
        explain["demoted_by"] = "ross_event_admission"
        candidate.explain_json = explain
    except Exception:
        pass


def _fresh_live_candidate(
    db: Session,
    symbol: str,
    *,
    now: datetime | None = None,
) -> MomentumSymbolViability | None:
    max_age = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
    cutoff = _utc_naive(now) - timedelta(seconds=max_age)
    try:
        rows = (
            db.query(MomentumSymbolViability)
            .filter(
                MomentumSymbolViability.scope == "symbol",
                MomentumSymbolViability.symbol == symbol,
                MomentumSymbolViability.live_eligible.is_(True),
                MomentumSymbolViability.freshness_ts >= cutoff,
            )
            .order_by(
                MomentumSymbolViability.viability_score.desc(),
                MomentumSymbolViability.updated_at.desc(),
            )
            .limit(25)
            .all()
        )
        demoted = False
        for candidate in rows:
            signal = ross_signal_for_symbol(getattr(candidate, "execution_readiness_json", None), symbol)
            if _transcript_context_invalid(signal):
                _demote_invalid_transcript_candidate(candidate)
                demoted = True
                continue
            return candidate
        if demoted:
            try:
                db.flush()
            except Exception:
                pass
        return None
    except Exception:
        logger.debug("[ross_event_admission] candidate read failed symbol=%s", symbol, exc_info=True)
        return None


def _active_live_session(
    db: Session,
    symbol: str,
    *,
    user_id: int | None,
) -> TradingAutomationSession | None:
    try:
        q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.symbol == symbol,
            TradingAutomationSession.state.in_(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY),
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        return q.order_by(TradingAutomationSession.updated_at.desc()).first()
    except Exception:
        logger.debug("[ross_event_admission] active session read failed symbol=%s", symbol, exc_info=True)
        return None


def _recent_pre_submit_terminal_block(
    db: Session,
    symbol: str,
    *,
    user_id: int | None,
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Block immediate re-admission after a no-order pre-submit terminal.

    The block window reuses the same viability-freshness horizon that made the
    candidate actionable. If a runner just decided "do not submit" for this
    still-fresh idea, IQFeed should not recreate the watcher on the next tick.
    """
    try:
        horizon = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
    except (TypeError, ValueError):
        horizon = 600.0
    cutoff = _utc_naive(now) - timedelta(seconds=max(0.0, horizon))
    terminal_states = ("live_cancelled", "live_error", "live_finished")
    try:
        q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.symbol == symbol,
            TradingAutomationSession.state.in_(terminal_states),
            TradingAutomationSession.updated_at >= cutoff,
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        recent = q.order_by(TradingAutomationSession.updated_at.desc()).limit(8).all()
        for sess in recent:
            snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
            le = snap.get("momentum_live_execution") if isinstance(snap.get("momentum_live_execution"), dict) else {}
            if le.get("entry_submitted") or le.get("position"):
                continue
            ev = (
                db.query(TradingAutomationEvent)
                .filter(
                    TradingAutomationEvent.session_id == int(sess.id),
                    TradingAutomationEvent.event_type == "live_entry_wait_late_window",
                    TradingAutomationEvent.ts >= cutoff,
                )
                .order_by(TradingAutomationEvent.ts.desc())
                .first()
            )
            payload = ev.payload_json if ev is not None and isinstance(ev.payload_json, dict) else {}
            if ev is not None and bool(payload.get("pre_submit_no_order", False)):
                return True, {
                    "session_id": int(sess.id),
                    "state": str(sess.state),
                    "event_ts": str(ev.ts),
                    "horizon_s": horizon,
                    "reason": "recent_pre_submit_late_window",
                }
    except Exception:
        logger.debug("[ross_event_admission] recent terminal block read failed symbol=%s", symbol, exc_info=True)
    return False, {}


def _cooldown_allows(symbol: str, source: str, *, ignore_cooldown: bool = False) -> bool:
    if ignore_cooldown:
        return True
    cd = _cooldown_seconds()
    if cd <= 0:
        return True
    key = (_sym(source) or "ROSS_EVENT", _sym(symbol))
    now = time.monotonic()
    with _COOLDOWN_LOCK:
        last = _LAST_ATTEMPT_MONOTONIC.get(key, 0.0)
        if now - last < cd:
            return False
        _LAST_ATTEMPT_MONOTONIC[key] = now
    return True


def _tick_session(
    db: Session,
    session_id: int,
    *,
    tick_live_session_fn: Callable[..., dict[str, Any]] | None,
    count: int,
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    if tick_live_session_fn is None:
        from .live_runner import tick_live_session as tick_live_session_fn

    results: list[dict[str, Any]] = []
    for _ in range(count):
        try:
            result = tick_live_session_fn(db, int(session_id))
            results.append(dict(result or {}))
            try:
                db.flush()
            except Exception:
                pass
            state = str((result or {}).get("state") or "")
            skipped = (result or {}).get("skipped")
            if skipped in ("not_runnable", "concurrent_tick") or (
                state and state not in LIVE_RUNNER_RUNNABLE_STATES
            ):
                break
        except Exception as exc:
            results.append({"ok": False, "error": str(exc)[:160]})
            break
    return results


def _append_event_safe(
    db: Session,
    *,
    session_id: int | None,
    payload: dict[str, Any],
    append_event_fn: Callable[..., Any] | None,
) -> None:
    if session_id is None:
        return
    try:
        if append_event_fn is None:
            from .persistence import append_trading_automation_event as append_event_fn

        append_event_fn(
            db,
            int(session_id),
            "ross_event_admitted",
            payload,
            source_node_id="ross_event_admission",
        )
    except Exception:
        logger.debug(
            "[ross_event_admission] append audit event failed session=%s",
            session_id,
            exc_info=True,
        )


def _source_owns_commit_fence(source: object) -> bool:
    """L9-C4 (2026-08-03): TRUE para sa mga source na dumadaan sa
    generation-fenced IQFeed loop commit — 'iqfeed*' AT ang ignition tag.
    Ang lumang bare 'iqfeed' substring ay HINDI tumutugma sa
    ``source='ignition_tick'`` kaya ang ignition admissions ay nag-i-immediate-
    tick nang WALA pang commit (latent bug; zero epekto sa sealed paper lane —
    ang ignition consumer doon ay hindi pa bukas — pero kailangang tama bago
    i-soak ang ordinary-loop ignition path)."""
    s = str(source or "").strip().lower()
    return "iqfeed" in s or "ignition" in s


def admit_ross_event(
    db: Session,
    *,
    symbol: str,
    signal: dict[str, Any] | None = None,
    source: str = "ross_event",
    snapshot_row: dict[str, Any] | None = None,
    refresh_viability: bool = True,
    run_momentum_tick_fn: Callable[..., dict[str, Any]] | None = None,
    begin_live_arm_fn: Callable[..., dict[str, Any]] | None = None,
    confirm_live_arm_fn: Callable[..., dict[str, Any]] | None = None,
    tick_live_session_fn: Callable[..., dict[str, Any]] | None = None,
    append_event_fn: Callable[..., Any] | None = None,
    candidate_provider: Callable[..., Any] | None = None,
    snapshot_provider: Callable[[str], dict[str, Any] | None] | None = None,
    market_open_fn: Callable[[str], bool] | None = None,
    now: datetime | None = None,
    ignore_cooldown: bool = False,
    dry_run: bool = False,
    defer_live_ticks_until_commit: bool = False,
) -> dict[str, Any]:
    """Admit one Ross-qualified symbol into the live watcher path.

    Rejections are explicit and fail closed on missing universe evidence. The
    function returns a structured summary for logs/tests; callers own commit.
    """
    started = time.monotonic()
    sym = _sym(symbol)
    out: dict[str, Any] = {
        "ok": True,
        "symbol": sym,
        "source": source,
        "admitted": False,
        "armed": 0,
        "ticked": 0,
        "skipped": None,
    }
    # The IQFeed loop owns a generation-fenced DB commit before any runner tick.
    # Other callers retain the existing immediate-tick behavior even if they pass
    # this option accidentally.
    defer_iqfeed_ticks = bool(
        defer_live_ticks_until_commit and _source_owns_commit_fence(source)
    )
    admission_tick_count = 0 if defer_iqfeed_ticks else _tick_count()
    if defer_iqfeed_ticks:
        out["ticks_deferred_until_commit"] = True
        out["admission_tick_count"] = 0
    if not sym:
        out["skipped"] = "invalid_symbol"
        return out
    if not _event_enabled():
        out["skipped"] = "flag_off"
        return out
    if not bool(getattr(settings, "chili_momentum_live_runner_enabled", False)):
        out["skipped"] = "live_runner_off"
        return out

    uid = _auto_user_id()
    if uid is None:
        out["skipped"] = "no_user"
        return out

    market_open = market_open_fn or _symbol_market_open
    try:
        if not bool(market_open(sym)):
            out["skipped"] = "market_closed"
            return out
    except Exception:
        out["skipped"] = "market_closed_unknown"
        return out

    if not _cooldown_allows(sym, source, ignore_cooldown=ignore_cooldown):
        out["skipped"] = "cooldown"
        return out

    sig = _merged_signal(sym, signal)
    candidate = None
    if sig is None and candidate_provider is not None:
        candidate = candidate_provider(db, sym, now=_utc_naive(now))
        sig = _merged_signal(
            sym,
            ross_signal_for_symbol(getattr(candidate, "execution_readiness_json", None), sym),
        )
    elif sig is None:
        candidate = _fresh_live_candidate(db, sym, now=now)
        sig = _merged_signal(
            sym,
            ross_signal_for_symbol(getattr(candidate, "execution_readiness_json", None), sym),
        )
    if sig is None and candidate is None and not refresh_viability:
        out["skipped"] = "no_fresh_live_eligible_candidate"
        return out
    if _transcript_context_invalid(sig):
        _demote_invalid_transcript_candidate(candidate)
        try:
            db.flush()
        except Exception:
            pass
        out["skipped"] = "ross_transcript_context_rejected"
        return out

    universe_ok, universe_reason, universe_debug, snapshot_row = _prove_ross_universe(
        sym,
        signal=sig,
        snapshot_row=snapshot_row,
        snapshot_provider=snapshot_provider,
    )
    out["ross_universe_reason"] = universe_reason
    out["ross_universe_debug"] = universe_debug
    if not universe_ok:
        out["skipped"] = "ross_universe_rejected"
        logger.info(
            "[ross_event_admission] reject %s source=%s universe=%s debug=%s",
            sym,
            source,
            universe_reason,
            universe_debug,
        )
        return out

    evidence_ok, evidence_reason, evidence_debug = ross_tick_scalp_evidence_ok(sig)
    independent_tape_source = _independent_smallcap_a_plus_source(source, sig) and not _explicit_ross_source(sig)
    if not evidence_ok and evidence_reason == "no_ross_signal":
        live_tape_sig = _live_tape_signal_from_universe(
            sym,
            source=source,
            universe_debug=universe_debug,
        )
        if live_tape_sig is not None:
            sig = _merged_signal(sym, live_tape_sig)
            evidence_ok, evidence_reason, evidence_debug = ross_tick_scalp_evidence_ok(sig)
            independent_tape_source = _independent_smallcap_a_plus_source(source, sig) and not _explicit_ross_source(sig)
    if (
        (not evidence_ok or independent_tape_source)
        and _independent_smallcap_a_plus_source(source, sig)
        and bool(getattr(settings, "chili_momentum_independent_smallcap_a_plus_enabled", True))
    ):
        independent_ok, independent_reason, independent_debug = independent_smallcap_a_plus_evidence_ok(sig)
        out["independent_smallcap_a_plus_reason"] = independent_reason
        out["independent_smallcap_a_plus_debug"] = independent_debug
        if independent_ok:
            evidence_ok, evidence_reason, evidence_debug = (
                True,
                independent_reason,
                independent_debug,
            )
    out["ross_evidence_reason"] = evidence_reason
    out["ross_evidence_debug"] = evidence_debug
    if not evidence_ok:
        out["skipped"] = "ross_evidence_rejected"
        logger.info(
            "[ross_event_admission] reject %s source=%s evidence=%s debug=%s",
            sym,
            source,
            evidence_reason,
            evidence_debug,
        )
        return out

    active = _active_live_session(db, sym, user_id=int(uid))
    if active is not None:
        session_id = int(getattr(active, "id", 0) or 0)
        ticks = _tick_session(
            db,
            session_id,
            tick_live_session_fn=tick_live_session_fn,
            count=admission_tick_count,
        )
        out.update(
            {
                "skipped": "already_active",
                "session_id": session_id,
                "state": getattr(active, "state", None),
                "tick_results": ticks,
                "ticked": len(ticks),
            }
        )
        return out

    blocked, block_detail = _recent_pre_submit_terminal_block(
        db,
        sym,
        user_id=int(uid),
        now=now,
    )
    if blocked:
        out["skipped"] = "recent_pre_submit_terminal"
        out["terminal_block"] = block_detail
        logger.info(
            "[ross_event_admission] skip %s source=%s recent_terminal=%s",
            sym,
            source,
            block_detail,
        )
        return out

    if refresh_viability:
        try:
            if run_momentum_tick_fn is None:
                from .pipeline import run_momentum_neural_tick as run_momentum_tick_fn

            out["pipeline"] = run_momentum_tick_fn(
                db,
                meta={
                    "tickers": [sym],
                    "ross_signals": {sym: sig},
                    "ross_event_admission": True,
                    "ross_event_source": source,
                },
            )
            try:
                db.flush()
            except Exception:
                pass
        except Exception as exc:
            out["ok"] = False
            out["skipped"] = "viability_refresh_failed"
            out["error"] = str(exc)[:200]
            logger.warning(
                "[ross_event_admission] viability refresh failed symbol=%s source=%s: %s",
                sym,
                source,
                exc,
            )
            return out

    if candidate is None:
        if candidate_provider is not None:
            candidate = candidate_provider(db, sym, now=_utc_naive(now))
        else:
            candidate = _fresh_live_candidate(db, sym, now=now)
    if candidate is None:
        out["skipped"] = "no_fresh_live_eligible_candidate"
        return out

    out["variant_id"] = int(getattr(candidate, "variant_id"))
    out["viability_score"] = float(getattr(candidate, "viability_score", 0.0) or 0.0)
    if dry_run:
        out.update(
            {
                "skipped": "dry_run",
                "would_admit": True,
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
        )
        return out

    if begin_live_arm_fn is None or confirm_live_arm_fn is None:
        from .operator_actions import begin_live_arm, confirm_live_arm

        begin_live_arm_fn = begin_live_arm_fn or begin_live_arm
        confirm_live_arm_fn = confirm_live_arm_fn or confirm_live_arm

    begin = begin_live_arm_fn(
        db,
        user_id=int(uid),
        symbol=sym,
        variant_id=int(out["variant_id"]),
        execution_family=_equity_execution_family(),
    )
    out["begin"] = begin
    if not begin.get("ok"):
        out["skipped"] = "begin_blocked"
        out["begin_error"] = begin.get("error")
        return out

    if begin.get("deduped"):
        session_id = int(begin.get("session_id") or 0)
        ticks = _tick_session(
            db,
            session_id,
            tick_live_session_fn=tick_live_session_fn,
            count=admission_tick_count,
        )
        out.update(
            {
                "skipped": "already_active",
                "session_id": session_id,
                "state": begin.get("state"),
                "tick_results": ticks,
                "ticked": len(ticks),
            }
        )
        return out

    confirm = confirm_live_arm_fn(
        db,
        user_id=int(uid),
        arm_token=begin.get("arm_token"),
        confirm=True,
    )
    out["confirm"] = confirm
    if not confirm.get("ok"):
        out["skipped"] = "confirm_blocked"
        out["confirm_error"] = confirm.get("error")
        return out

    session_id = int(begin.get("session_id") or confirm.get("session_id") or 0)
    ticks = _tick_session(
        db,
        session_id,
        tick_live_session_fn=tick_live_session_fn,
        count=admission_tick_count,
    )
    out.update(
        {
            "admitted": True,
            "armed": 1,
            "session_id": session_id,
            "state": confirm.get("state"),
            "tick_results": ticks,
            "ticked": len(ticks),
            "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
        }
    )
    _append_event_safe(
        db,
        session_id=session_id,
        payload={
            "symbol": sym,
            "source": source,
            "variant_id": out.get("variant_id"),
            "ross_universe_reason": universe_reason,
            "ross_evidence_reason": evidence_reason,
            "ticked": len(ticks),
            "latency_ms": out["latency_ms"],
        },
        append_event_fn=append_event_fn,
    )
    logger.warning(
        "[ross_event_admission] admitted %s source=%s session=%s ticks=%s latency_ms=%.1f",
        sym,
        source,
        session_id,
        len(ticks),
        out["latency_ms"],
    )
    return out
