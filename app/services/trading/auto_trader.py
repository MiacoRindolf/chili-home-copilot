"""AutoTrader v1 orchestrator: pattern-imminent alerts → gates → paper or RH live."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import or_, text
from sqlalchemy.orm import Session, aliased

from ...config import settings
from ...models.trading import AutoTraderRun, BreakoutAlert, ScanPattern, Trade
from .auto_trader_llm import run_revalidation_llm
from .auto_trader_rules import (
    RuleGateContext,
    autotrader_paper_realized_pnl_today_et,
    autotrader_realized_pnl_today_et,
    breakout_alert_already_processed,
    count_autotrader_v1_open,
    count_autotrader_v1_open_by_lane,
    passes_rule_gate,
)
from .autotrader_desk import effective_autotrader_runtime
from .autopilot_scope import (
    AUTOPILOT_AUTO_TRADER_V1,
    check_autopilot_entry_gate,
)
from .auto_trader_synergy import (
    find_open_autotrader_paper,
    find_open_autotrader_trade,
    maybe_scale_in,
)
from .management_scope import MANAGEMENT_SCOPE_AUTO_TRADER_V1
from .ops_log_prefixes import CHILI_MARKET_DATA

logger = logging.getLogger(__name__)

AUTOTRADER_VERSION = "v1"


def _autotrader_tick_note(
    out: dict[str, Any],
    *,
    kind: str,
    reason: str,
    alert: BreakoutAlert | None = None,
) -> None:
    """Record the latest tick outcome for a single INFO summary line."""
    out["tick_last_kind"] = kind
    out["tick_last_reason"] = (reason or "")[:500]
    if alert is not None:
        out["tick_last_alert_id"] = int(alert.id)
        out["tick_last_ticker"] = (alert.ticker or "").upper()

# Namespace byte for advisory locks so we can't collide with other
# subsystems that also use pg_advisory_lock on alert-shaped ints. The
# lock key is (NAMESPACE << 32) | breakout_alert_id — fits a signed
# bigint and is deterministic per alert.
_ALERT_CLAIM_LOCK_NAMESPACE = 0x4154  # "AT"


def _alert_claim_lock_key(alert_id: int) -> int:
    return (_ALERT_CLAIM_LOCK_NAMESPACE << 32) | (int(alert_id) & 0xFFFFFFFF)


def _try_claim_alert(db: Session, alert_id: int) -> bool:
    """Acquire a Postgres advisory lock on this alert so only one worker
    processes it. Released when the session closes (or explicitly via
    :func:`_release_alert_claim`). Returns False if another session holds
    the lock — caller should skip.

    Safer than TOCTOU around ``breakout_alert_already_processed``: that
    check reads the AutoTraderRun audit table, but two workers can both
    pass it concurrently before either writes. SQLite (tests) doesn't
    have advisory locks, so we fail open on non-Postgres dialects — the
    existing audit-row dedupe still covers the single-process case the
    test fixtures use.
    """
    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return True
    try:
        got = db.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _alert_claim_lock_key(alert_id)},
        ).scalar()
        return bool(got)
    except Exception:
        # DB-level failure must not block the loop; treat as 'claimed'
        # and rely on the audit-row check + idempotency_store as fallback.
        logger.warning(
            "[autotrader] advisory lock acquire failed for alert=%s; falling back",
            alert_id, exc_info=True,
        )
        return True


def _release_alert_claim(db: Session, alert_id: int) -> None:
    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return
    try:
        db.execute(
            text("SELECT pg_advisory_unlock(:k)"),
            {"k": _alert_claim_lock_key(alert_id)},
        )
    except Exception:
        logger.debug(
            "[autotrader] advisory unlock failed for alert=%s; will release on session close",
            alert_id, exc_info=True,
        )


# AAA -- janitor: kill leaked autotrader advisory-lock holders.
#
# When XX's outer wall-clock budget abandons a hung worker thread, the
# thread's DB session stays alive (Python can't safely kill a thread).
# That orphan session keeps any pg_advisory_lock it acquired -- which
# means every subsequent tick fails to claim the same alert with
# advisory_lock_busy. Diagnosed via pg_stat_activity + pg_locks:
# sessions stuck "idle in transaction" with state_change_age > N seconds,
# holding a lock in our 0x4154 namespace, are leaked.
#
# This janitor runs at the START of every autotrader tick, before any
# work. It's cheap (one indexed query) and idempotent -- pg_terminate_backend
# is a no-op on a session that has already finished. Threshold is
# generous (default 120s) so we don't fight legitimate slow ticks; the
# tick budget is 45s so anything older is definitely orphaned.

def _cleanup_leaked_advisory_locks(db: Session) -> int:
    """Terminate orphan sessions holding autotrader advisory locks.

    Returns count of sessions terminated. Best-effort: any failure logs
    and returns 0 -- never raises into the tick.
    """
    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return 0
    try:
        threshold_s = max(60, int(getattr(settings, "chili_autotrader_leak_cleanup_threshold_s", 120)))
    except Exception:
        threshold_s = 120
    try:
        rows = db.execute(
            text(
                "SELECT pa.pid, "
                "       EXTRACT(EPOCH FROM (NOW() - pa.state_change))::int AS age_s, "
                "       pa.state "
                "FROM pg_stat_activity pa "
                "JOIN pg_locks l ON l.pid = pa.pid "
                "WHERE l.locktype = 'advisory' "
                "  AND l.classid::int = :ns "
                "  AND pa.state IN ('idle in transaction', 'idle in transaction (aborted)') "
                "  AND EXTRACT(EPOCH FROM (NOW() - pa.state_change)) > :thr "
            ),
            {"ns": _ALERT_CLAIM_LOCK_NAMESPACE, "thr": threshold_s},
        ).fetchall()
        killed = 0
        for r in rows or []:
            pid, age_s, state = int(r[0]), int(r[1] or 0), r[2]
            try:
                db.execute(
                    text("SELECT pg_terminate_backend(:p)"),
                    {"p": pid},
                )
                killed += 1
                logger.warning(
                    "[autotrader] AAA janitor: terminated leaked session "
                    "pid=%s state=%s age=%ss (orphan lock from prior abandoned tick)",
                    pid, state, age_s,
                )
            except Exception as e:
                logger.debug("[autotrader] AAA janitor terminate pid=%s failed: %s", pid, e)
        if killed:
            db.commit()
        return killed
    except Exception as e:
        logger.debug("[autotrader] AAA janitor pass failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _resolve_user_id() -> Optional[int]:
    return getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )


def _audit(
    db: Session,
    *,
    user_id: Optional[int],
    alert: BreakoutAlert,
    decision: str,
    reason: str,
    rule_snapshot: dict[str, Any] | None = None,
    llm_snapshot: dict[str, Any] | None = None,
    trade_id: Optional[int] = None,
) -> None:
    row = AutoTraderRun(
        user_id=user_id,
        breakout_alert_id=alert.id,
        scan_pattern_id=alert.scan_pattern_id,
        ticker=(alert.ticker or "").upper(),
        decision=decision,
        reason=reason[:2000] if reason else None,
        rule_snapshot=rule_snapshot,
        llm_snapshot=llm_snapshot,
        management_scope=MANAGEMENT_SCOPE_AUTO_TRADER_V1,
        trade_id=trade_id,
    )
    db.add(row)
    db.commit()

    # Q1.T3 phase 2 — shadow-consume unified_signals.
    # No-op when chili_unified_signal_consumer_enabled is False (default).
    # When True, looks up the matching unified_signals row and logs any
    # parity discrepancies. Does NOT change the decision in any way.
    try:
        from .contracts.signal_consumer import maybe_shadow_consume
        _entry_price = None
        try:
            _entry_price = float(alert.entry_price) if alert.entry_price is not None else None
        except (TypeError, ValueError, AttributeError):
            _entry_price = None
        maybe_shadow_consume(
            db,
            alert_id=int(alert.id),
            alert_ticker=(alert.ticker or "").upper(),
            alert_entry_price=_entry_price,
            decision=decision,
            decision_reason=reason,
        )
    except Exception:  # pragma: no cover - never raise from audit hook
        logger.debug("[autotrader] shadow_consume failed; ignored", exc_info=True)


def _pattern_name(db: Session, scan_pattern_id: Optional[int]) -> str | None:
    if not scan_pattern_id:
        return None
    p = db.query(ScanPattern).filter(ScanPattern.id == int(scan_pattern_id)).first()
    return p.name if p else None


# ── Market-data fetches (Phase B) ────────────────────────────────────
#
# These two helpers are on the auto-trader hot path: a bad quote here sizes
# a live order. Previously both sites swallowed every exception silently,
# meaning a transient timeout from the quote provider returned None and
# the gate logic blocked the alert as ``no_quote`` — indistinguishable
# from the ticker simply not quoting. That blinded ops to provider
# outages and made every missed entry a noop investigation.
#
# Phase B behavior:
#   - Up to 3 attempts per call, exponential backoff (0.5s, 1.0s).
#   - Timeouts (asyncio/sync) logged as ``kind=timeout`` → retry.
#   - Transport / network errors logged as ``kind=transport`` → retry.
#   - Empty results logged as ``kind=empty`` → retry.
#   - Unexpected exceptions logged as ``kind=upstream`` with exc_info → retry.
#   - Exhausted attempts log a final ``kind=exhausted`` line at WARNING.
#   - Every outcome is prefixed ``[chili_market_data]`` so the phase-C
#     observability registry can index it.
#
# Contract unchanged: both still return None on failure — no exception
# escapes to callers. The difference is visibility. The kill switch +
# drawdown breaker still gate the downstream execution regardless of
# what we return here.

_MARKET_DATA_MAX_ATTEMPTS = 3
_MARKET_DATA_BACKOFF_BASE_SEC = 0.5


def _classify_market_data_exc(exc: BaseException) -> str:
    """Map a raised exception to a short log-kind token.

    The upstream ``fetch_*`` layer does not raise a structured taxonomy
    today — this mapping gives the ops log a consistent vocabulary
    without requiring a contract change to ``market_data.py``.
    """
    if isinstance(exc, TimeoutError):
        return "timeout"
    # OSError covers ConnectionError / socket timeouts / refused / reset
    if isinstance(exc, OSError):
        return "transport"
    return "upstream"


def _ohlcv_summary(ticker: str) -> str | None:
    """Fetch a short OHLCV summary for LLM revalidation; retry + structured log."""
    from .market_data import fetch_ohlcv_df

    last_kind = "unknown"
    last_err: str | None = None
    for attempt in range(1, _MARKET_DATA_MAX_ATTEMPTS + 1):
        try:
            df = fetch_ohlcv_df(ticker, "5m", period="5d")
            if df is None or df.empty:
                last_kind = "empty"
                last_err = None
                logger.info(
                    f"{CHILI_MARKET_DATA} source=ohlcv kind=empty ticker=%s attempt=%d",
                    ticker, attempt,
                )
            else:
                logger.debug(
                    f"{CHILI_MARKET_DATA} source=ohlcv kind=ok ticker=%s attempt=%d rows=%d",
                    ticker, attempt, len(df),
                )
                tail = df.tail(15)
                if "Close" in tail.columns:
                    return tail[["Close"]].to_string(max_rows=20)[:3500]
                return tail.to_string(max_rows=10)[:3500]
        except Exception as e:  # noqa: BLE001 — classified below, re-logged
            last_kind = _classify_market_data_exc(e)
            last_err = repr(e)
            logger.warning(
                f"{CHILI_MARKET_DATA} source=ohlcv kind=%s ticker=%s attempt=%d err=%s",
                last_kind, ticker, attempt, last_err,
                exc_info=(last_kind == "upstream"),
            )
        if attempt < _MARKET_DATA_MAX_ATTEMPTS:
            time.sleep(_MARKET_DATA_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
    logger.warning(
        f"{CHILI_MARKET_DATA} source=ohlcv kind=exhausted ticker=%s attempts=%d last_kind=%s last_err=%s",
        ticker, _MARKET_DATA_MAX_ATTEMPTS, last_kind, last_err,
    )
    return None


def _current_price(ticker: str) -> float | None:
    """Fetch the current price for gate sizing; retry + structured log."""
    from .market_data import fetch_quote

    last_kind = "unknown"
    last_err: str | None = None
    for attempt in range(1, _MARKET_DATA_MAX_ATTEMPTS + 1):
        try:
            q = fetch_quote(ticker)
            if not q:
                last_kind = "empty"
                last_err = None
                logger.info(
                    f"{CHILI_MARKET_DATA} source=quote kind=empty ticker=%s attempt=%d",
                    ticker, attempt,
                )
            else:
                raw_p = q.get("price") or q.get("last_price")
                try:
                    price = float(raw_p) if raw_p is not None else None
                except (TypeError, ValueError):
                    price = None
                if price is None:
                    last_kind = "empty"
                    last_err = f"unparseable price={raw_p!r}"
                    logger.info(
                        f"{CHILI_MARKET_DATA} source=quote kind=empty ticker=%s attempt=%d note=unparseable",
                        ticker, attempt,
                    )
                else:
                    logger.debug(
                        f"{CHILI_MARKET_DATA} source=quote kind=ok ticker=%s attempt=%d price=%s",
                        ticker, attempt, price,
                    )
                    return price
        except Exception as e:  # noqa: BLE001 — classified below, re-logged
            last_kind = _classify_market_data_exc(e)
            last_err = repr(e)
            logger.warning(
                f"{CHILI_MARKET_DATA} source=quote kind=%s ticker=%s attempt=%d err=%s",
                last_kind, ticker, attempt, last_err,
                exc_info=(last_kind == "upstream"),
            )
        if attempt < _MARKET_DATA_MAX_ATTEMPTS:
            time.sleep(_MARKET_DATA_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
    logger.warning(
        f"{CHILI_MARKET_DATA} source=quote kind=exhausted ticker=%s attempts=%d last_kind=%s last_err=%s",
        ticker, _MARKET_DATA_MAX_ATTEMPTS, last_kind, last_err,
    )
    return None


def run_auto_trader_tick(db: Session) -> dict[str, Any]:
    """Process a small batch of unprocessed pattern-imminent BreakoutAlerts."""
    if not getattr(settings, "chili_autotrader_enabled", False):
        return {"ok": True, "skipped": True, "reason": "disabled"}

    from .governance import is_kill_switch_active

    if is_kill_switch_active():
        return {"ok": True, "skipped": True, "reason": "kill_switch"}

    rt = effective_autotrader_runtime(db)
    if not rt.get("tick_allowed"):
        return {"ok": True, "skipped": True, "reason": "paused_or_disabled", "runtime": rt}

    # AAA -- janitor pass: kill any leaked autotrader advisory-lock holders
    # from previous abandoned ticks. Cheap, idempotent. Default threshold
    # 120s -- well past the 45s tick budget so legitimate slow ticks never
    # get killed by us.
    _cleanup_leaked_advisory_locks(db)

    uid = _resolve_user_id()
    if uid is None:
        logger.debug("[autotrader] No user id (chili_autotrader_user_id / brain_default_user_id)")
        return {"ok": False, "error": "no_user_id"}

    # Match alerts scoped to this autotrader user AND system-generated
    # (``user_id IS NULL``) pattern-imminent alerts. The imminent generator
    # writes alerts without a specific owner; treating them as processable by
    # the configured autotrader user is the intended behavior (single-tenant
    # deployment). Use ``OR`` so explicit-user alerts are still honored.
    ar = aliased(AutoTraderRun)
    candidate_base = (
        db.query(BreakoutAlert)
        .outerjoin(ar, ar.breakout_alert_id == BreakoutAlert.id)
        .filter(
            BreakoutAlert.alert_tier == "pattern_imminent",
            or_(BreakoutAlert.user_id == uid, BreakoutAlert.user_id.is_(None)),
            ar.id.is_(None),
        )
    )
    candidate_pool = int(candidate_base.count())
    candidates = (
        candidate_base.order_by(BreakoutAlert.id.asc()).limit(5).all()
    )

    out: dict[str, Any] = {
        "processed": 0,
        "placed": 0,
        "scaled_in": 0,
        "skipped": 0,
        "tick_last_kind": None,
        "tick_last_reason": None,
        "tick_last_alert_id": None,
        "tick_last_ticker": None,
    }

    for alert in candidates:
        # P0.2 — acquire advisory lock before the TOCTOU window. Without
        # this, two ticks (different scheduler replicas) can both pass the
        # audit-row check and both call place_market_order. The lock is
        # held until we explicitly unlock or the session closes.
        if not _try_claim_alert(db, int(alert.id)):
            _autotrader_tick_note(
                out,
                kind="unclaimed",
                reason="advisory_lock_busy",
                alert=alert,
            )
            continue

        try:
            # Re-check race (another worker may have inserted between the
            # outer candidate query and our claim).
            db.expire_all()
            if breakout_alert_already_processed(db, int(alert.id)):
                _autotrader_tick_note(
                    out,
                    kind="skipped",
                    reason="already_processed_race",
                    alert=alert,
                )
                continue

            try:
                _process_one_alert(db, uid, alert, out, rt)
            except Exception as e:
                logger.exception("[autotrader] alert %s failed: %s", alert.id, e)
                _audit(db, user_id=uid, alert=alert, decision="error", reason=str(e)[:500])
                _autotrader_tick_note(
                    out, kind="error", reason=str(e)[:500], alert=alert
                )
            out["processed"] += 1
        finally:
            _release_alert_claim(db, int(alert.id))

    logger.info(
        "[autotrader] tick uid=%s candidate_pool=%d batch=%d processed=%d placed=%d "
        "scaled_in=%d skipped=%d last_kind=%s last_reason=%s last_alert_id=%s last_ticker=%s",
        uid,
        candidate_pool,
        len(candidates),
        out["processed"],
        out["placed"],
        out["scaled_in"],
        out["skipped"],
        out.get("tick_last_kind") or "-",
        out.get("tick_last_reason") or "-",
        out.get("tick_last_alert_id") if out.get("tick_last_alert_id") is not None else "-",
        out.get("tick_last_ticker") or "-",
    )

    return {"ok": True, **out}


def _maybe_substitute_with_options(db: Session, alert: BreakoutAlert, spot: float) -> None:
    """Phase 3 — when the substitute flag is on, translate a bullish
    equity alert into a long-call entry by writing option_meta into
    ``alert.indicator_snapshot`` and flipping ``alert.asset_type`` to
    'options'. Mutates the in-memory alert; doesn't touch the DB row.

    Synthesis tunables (DTE target, max spread, etc.) come from the
    StrategyParameter ledger so the brain's learning loop adapts them
    from realized outcomes — no hardcoded values.

    Skips silently (leaves the alert as equity) when:
      - Flag is OFF
      - Alert isn't a bullish stock alert
      - Option chain is illiquid or no tradable contract exists
      - Synthesis raises (broker hiccup, etc.)
    """
    try:
        if not bool(getattr(settings, "chili_autotrader_options_substitute_enabled", False)):
            return
        if (alert.asset_type or "").lower() != "stock":
            return
        # Bullish-only check: target above entry
        ent = float(alert.entry_price or 0)
        tgt = float(alert.target_price or 0)
        if not (ent > 0 and tgt > ent):
            return

        from .options.synthesis import synthesize_option_meta
        notional = float(getattr(settings, "chili_autotrader_per_trade_notional_usd", 300.0))
        opt_meta = synthesize_option_meta(
            db=db,
            underlying=str(alert.ticker),
            spot=float(spot),
            notional_usd=notional,
        )
        if not opt_meta:
            return

        snap = alert.indicator_snapshot if isinstance(alert.indicator_snapshot, dict) else {}
        snap = dict(snap)  # copy so we don't mutate ORM state inadvertently
        snap["option_meta"] = opt_meta
        snap["original_asset_type"] = alert.asset_type
        alert.indicator_snapshot = snap
        alert.asset_type = "options"
        # Override entry_price to the option premium so downstream code
        # using alert.entry_price as "limit" stays consistent.
        alert.entry_price = float(opt_meta.get("limit_price") or alert.entry_price)
        logger.info(
            "[autotrader_options_substitute] %s -> %s %s %s%s qty=%d limit=%.2f",
            alert.ticker, alert.ticker,
            opt_meta["expiration"], opt_meta["strike"], opt_meta["option_type"],
            opt_meta["quantity"], opt_meta["limit_price"],
        )
    except Exception:
        logger.debug("[autotrader_options_substitute] failed; falling back to equity", exc_info=True)


def _eligible_lifecycle_stages() -> set[str]:
    """The lifecycle_stage values that are allowed to enter new live trades."""
    raw = (getattr(settings, "chili_autotrader_eligible_lifecycle_stages", "promoted,live") or "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _process_one_alert(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    out: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    # 2026-04-28 lifecycle gate. Evidence audit demotes patterns to 'challenged'
    # but the entry funnel had been ignoring lifecycle_stage, so 32 of 34 entries
    # last week landed on demoted patterns (driving most of the bleed). Enforce
    # the audit's intent at trade-placement. Override via
    # CHILI_AUTOTRADER_ELIGIBLE_LIFECYCLE_STAGES env var to widen the set.
    if alert.scan_pattern_id:
        _pat = db.query(ScanPattern).filter(ScanPattern.id == int(alert.scan_pattern_id)).first()
        if _pat is not None:
            _stage = (_pat.lifecycle_stage or "").strip().lower()
            _allowed = _eligible_lifecycle_stages()
            if _stage not in _allowed:
                _reason = f"pattern_lifecycle_not_eligible:{_stage or 'none'}"
                _audit(db, user_id=uid, alert=alert, decision="skipped", reason=_reason)
                out["skipped"] += 1
                _autotrader_tick_note(out, kind="skipped", reason=_reason, alert=alert)
                return
    px = _current_price(alert.ticker)
    if px is None:
        _audit(db, user_id=uid, alert=alert, decision="skipped", reason="no_quote")
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="skipped", reason="no_quote", alert=alert)
        return

    # Phase 3: when the substitute flag is on, mutate this in-memory
    # equity alert into an options alert. The rule gate's options_path
    # branch then picks it up just like an explicitly-queued option
    # alert. No-op when flag is off (leaves the alert as equity).
    _maybe_substitute_with_options(db, alert, px)

    live = bool(runtime.get("live_orders_effective"))
    open_n = count_autotrader_v1_open(db, uid, paper_mode=not live)
    open_by_lane = count_autotrader_v1_open_by_lane(db, uid, paper_mode=not live)
    loss_today = (
        autotrader_paper_realized_pnl_today_et(db, uid)
        if not live
        else autotrader_realized_pnl_today_et(db, uid)
    )
    ctx = RuleGateContext(
        current_price=px,
        autotrader_open_count=open_n,
        realized_loss_today_usd=loss_today,
        autotrader_open_count_by_lane=open_by_lane,
    )

    existing_trade = None
    existing_paper = None
    if live:
        existing_trade = find_open_autotrader_trade(db, user_id=uid, ticker=alert.ticker)
    else:
        existing_paper = find_open_autotrader_paper(db, user_id=uid, ticker=alert.ticker)

    scale_plan = None
    if live and existing_trade is not None:
        scale_plan = maybe_scale_in(
            db,
            user_id=uid,
            ticker=alert.ticker,
            new_scan_pattern_id=alert.scan_pattern_id,
            new_stop=float(alert.stop_loss) if alert.stop_loss is not None else None,
            new_target=float(alert.target_price) if alert.target_price is not None else None,
            current_price=px,
            settings=settings,
        )

    if existing_trade is not None:
        if int(existing_trade.scan_pattern_id or 0) == int(alert.scan_pattern_id or 0):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="duplicate_pattern_already_open")
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="skipped", reason="duplicate_pattern_already_open", alert=alert
            )
            return
        if scale_plan is None:
            reason = (
                "synergy_disabled_second_signal"
                if not getattr(settings, "chili_autotrader_synergy_enabled", False)
                else "synergy_not_applicable"
            )
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason=reason)
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="skipped", reason=reason, alert=alert)
            return

    if not live and existing_paper is not None:
        if int(existing_paper.scan_pattern_id or 0) == int(alert.scan_pattern_id or 0):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="duplicate_pattern_paper_open")
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="skipped", reason="duplicate_pattern_paper_open", alert=alert
            )
            return
        if getattr(settings, "chili_autotrader_synergy_enabled", False):
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="paper_synergy_not_supported")
            skip_reason = "paper_synergy_not_supported"
        else:
            _audit(db, user_id=uid, alert=alert, decision="skipped", reason="synergy_disabled_second_signal")
            skip_reason = "synergy_disabled_second_signal"
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="skipped", reason=skip_reason, alert=alert)
        return

    for_new = scale_plan is None

    # P1.2 — venue health circuit breaker. Cheaper than autopilot_mutex
    # (one rolling-window aggregate) and the more fundamental signal: if
    # the venue is observably sick we shouldn't fire ANY new orders there
    # regardless of ownership. Fails open: if the feature flag is off or
    # we can't compute a summary, treat as healthy. Only gates live paths
    # — paper writes nothing to the event stream so would always show
    # insufficient_data anyway.
    if live:
        try:
            from .venue.venue_health import is_venue_degraded, venue_degraded_reason
            if is_venue_degraded(db, venue="robinhood"):
                reason_detail = venue_degraded_reason(db, venue="robinhood") or "unknown"
                rsn = f"venue_degraded:robinhood:{reason_detail}"[:255]
                _audit(
                    db,
                    user_id=uid,
                    alert=alert,
                    decision="blocked",
                    reason=rsn,
                )
                out["skipped"] += 1
                _autotrader_tick_note(out, kind="blocked", reason=rsn, alert=alert)
                return
        except Exception:
            # Defensive: venue-health module failure must never block the
            # autotrader loop. Fall through to the remaining gates, but
            # log loudly — a silent check failure defeats the circuit breaker.
            logger.warning(
                "[autotrader] venue_health check failed for alert=%s ticker=%s; failing open",
                alert.id, alert.ticker, exc_info=True,
            )

    # P0.4 — autopilot mutual exclusion. Only gate LIVE orders: the lease
    # signal for momentum_neural is a mode="live" TradingAutomationSession,
    # so paper v1 can't contend on the schema level. For live v1:
    #   * scale-in (scale_plan != None) → our own existing Trade is the lease,
    #     gate returns owner_self → allowed.
    #   * new entry → gate blocks if momentum_neural already owns the symbol.
    if live:
        gate = check_autopilot_entry_gate(
            db,
            candidate=AUTOPILOT_AUTO_TRADER_V1,
            symbol=alert.ticker,
            user_id=uid,
        )
        if not gate.get("allowed"):
            rsn = f"autopilot_mutex:{gate.get('reason')}:owner={gate.get('owner') or 'none'}"
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=rsn,
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason=rsn, alert=alert)
            return

    ok, reason, snap = passes_rule_gate(
        db, alert, settings=settings, ctx=ctx, for_new_entry=for_new, fallback_user_id=uid,
    )
    if not ok:
        _audit(db, user_id=uid, alert=alert, decision="skipped", reason=reason, rule_snapshot=snap)
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="skipped", reason=str(reason), alert=alert)
        return

    llm_snap: dict[str, Any] | None = None
    if getattr(settings, "chili_autotrader_llm_revalidation_enabled", True):
        ohlcv = _ohlcv_summary(alert.ticker)
        viable, llm_snap = run_revalidation_llm(
            alert,
            current_price=px,
            ohlcv_summary=ohlcv,
            pattern_name=_pattern_name(db, alert.scan_pattern_id),
            trace_id=f"autotrader-{alert.id}",
        )
        if not viable:
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason="llm_not_viable",
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason="llm_not_viable", alert=alert)
            return

    # P1.4 — runtime feature-parity assertion at entry. Fetches a fresh
    # OHLCV frame, computes the live indicator snapshot, and verifies it
    # matches the canonical compute_all_from_df output on the same frame.
    # Fails open (flag off / no frame / compute exception) so an unwired
    # environment behaves unchanged. In ``soft`` mode always allows through
    # — just records a TradingExecutionEvent with event_type='feature_parity_drift'
    # and emits an alert. In ``hard`` mode blocks entry on critical drift.
    # Only gates live paths: paper skip is cheap and paper drift still
    # records for auditing below.
    if live:
        _parity_blocked = _maybe_check_feature_parity(
            db,
            ticker=alert.ticker,
            scan_pattern_id=alert.scan_pattern_id,
            venue="robinhood",
            source="auto_trader_v1",
        )
        if _parity_blocked is not None:
            rsn = _parity_blocked[:255]
            _audit(
                db,
                user_id=uid,
                alert=alert,
                decision="blocked",
                reason=rsn,
                rule_snapshot=snap,
                llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason=rsn, alert=alert)
            return

    if scale_plan is not None:
        _execute_scale_in(db, uid, alert, scale_plan, px, snap, llm_snap, live, out)
        return

    _execute_new_entry(db, uid, alert, px, snap, llm_snap, live, out)


def _maybe_check_feature_parity(
    db: Session,
    *,
    ticker: str,
    scan_pattern_id: int | None,
    venue: str,
    source: str,
) -> str | None:
    """Run the P1.4 parity check. Returns a ``reason`` string when hard-mode
    blocks entry, ``None`` otherwise (including soft-mode drift, disabled,
    compute failures). Never raises.

    **Short-circuits on the feature flag BEFORE any OHLCV fetch / compute
    work.** The flag is off by default, so this function must be near-zero
    cost when unwired — otherwise every live alert pays a network fetch for
    nothing, which in a Windows test environment has been observed to exhaust
    the ephemeral socket pool (WinError 10055).
    """
    if not bool(getattr(settings, "chili_feature_parity_enabled", False)):
        return None

    try:
        from .feature_parity import (
            DEFAULT_FEATURES,
            check_entry_feature_parity,
        )
        from .indicator_core import compute_all_from_df
        from .market_data import fetch_ohlcv_df
    except Exception:
        logger.warning(
            "[autotrader] feature_parity imports unavailable; failing open for %s",
            ticker, exc_info=True,
        )
        return None

    try:
        df = fetch_ohlcv_df(ticker, "1d", "6mo")
    except Exception:
        logger.warning(
            "[autotrader] feature_parity OHLCV fetch failed for %s; failing open",
            ticker, exc_info=True,
        )
        return None
    if df is None or df.empty:
        return None

    try:
        arrays = compute_all_from_df(df, needed=set(DEFAULT_FEATURES))
    except Exception:
        logger.warning(
            "[autotrader] feature_parity indicator compute failed for %s; failing open",
            ticker, exc_info=True,
        )
        return None
    live_snap: dict[str, Any] = {}
    for key, vec in arrays.items():
        if not isinstance(vec, list) or not vec:
            continue
        v = vec[-1]
        if v is None:
            continue
        live_snap[key] = v

    try:
        result = check_entry_feature_parity(
            db,
            ticker=ticker,
            live_snap=live_snap,
            reference_df=df,
            features=DEFAULT_FEATURES,
            source=source,
            scan_pattern_id=scan_pattern_id,
            venue=venue,
        )
    except Exception:
        logger.warning(
            "[autotrader] feature_parity check raised for %s; failing open",
            ticker, exc_info=True,
        )
        return None
    if result.ok:
        return None
    # Hard-mode critical block.
    return f"feature_parity:{result.reason or result.severity}"


def _execute_broker_buy(
    db: Session,
    *,
    uid: int,
    alert: BreakoutAlert,
    qty: float,
    client_order_id: str,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    out: dict[str, Any],
) -> dict[str, Any] | None:
    """Place a live buy via Robinhood with the full safety envelope.

    Phase D (tech-debt): previously this exact sequence was duplicated
    between ``_execute_scale_in`` and ``_execute_new_entry`` — a
    kill-switch fix applied to one path but not the other was
    always-one-edit away. Centralized here so both callers share the
    same gate order: kill-switch recheck → adapter enabled → broker
    place → error surface.

    Returns the broker result dict on success (caller writes the trade
    row), or ``None`` if the path short-circuited. Every short-circuit
    path also writes an ``AutoTraderRun`` audit row and increments
    ``out["skipped"]`` so the caller can return immediately.
    """
    from .governance import is_kill_switch_active
    from .venue.factory import get_adapter

    # P0.5 — re-check kill switch immediately before submitting. The
    # initial check at tick entry can go stale if an operator flips the
    # switch while gates are evaluating (feature_parity / LLM can take
    # seconds). Cheap (in-memory lock + bool), so no reason not to.
    if is_kill_switch_active():
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason="kill_switch_activated_mid_flight",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out, kind="blocked", reason="kill_switch_activated_mid_flight", alert=alert
        )
        return None

    # Task MM Phase 2 — when this is an options alert, branch to the
    # options venue adapter instead of the spot adapter. The rule gate
    # has already validated the option metadata exists, so we just
    # extract it and call place_option_buy. snap['option_meta'] is set
    # by the gate when options_path=True.
    if snap.get("options_path") and snap.get("option_meta"):
        opt_meta = snap["option_meta"]
        from .venue.robinhood_options import RobinhoodOptionsAdapter
        opt_ad = RobinhoodOptionsAdapter()
        if not opt_ad.is_enabled():
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked", reason="rh_options_adapter_off",
                rule_snapshot=snap, llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(out, kind="blocked", reason="rh_options_adapter_off", alert=alert)
            return None
        # Phase 4 — multi-leg branch. When option_meta carries `legs`
        # (a list of >1 leg dicts), submit as a spread atomically via
        # the spread adapter method instead of single-leg place_option_buy.
        # The strategy layer (Q2.T1 vertical_spread / iron_condor /
        # etc.) emits the legs + direction; the autotrader just routes.
        legs = opt_meta.get("legs")
        try:
            if isinstance(legs, list) and len(legs) > 1:
                res = opt_ad.place_spread(
                    underlying=str(alert.ticker),
                    legs=legs,
                    quantity=int(qty),
                    limit_price=float(opt_meta.get("limit_price") or alert.entry_price or 0),
                    direction=str(opt_meta.get("direction", "debit")),
                )
            else:
                # qty here represents number of CONTRACTS (each = 100
                # underlying shares). The rule gate's notional sizing
                # already converted cash → contract count.
                res = opt_ad.place_option_buy(
                    underlying=str(alert.ticker),
                    expiration=str(opt_meta["expiration"]),
                    strike=float(opt_meta["strike"]),
                    option_type=str(opt_meta["option_type"]),
                    quantity=int(qty),
                    limit_price=float(opt_meta.get("limit_price") or alert.entry_price or 0),
                )
        except Exception as exc:
            res = {"ok": False, "error": f"options_adapter_exception:{exc}"}
        if not res.get("ok"):
            _audit(
                db, user_id=uid, alert=alert,
                decision="blocked", reason=f"broker:{res.get('error')}",
                rule_snapshot=snap, llm_snapshot=llm_snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="blocked", reason=f"broker:{res.get('error')}", alert=alert,
            )
            return None
        return res

    ad = get_adapter("robinhood")
    if ad is None or not ad.is_enabled():
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason="rh_adapter_off",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="blocked", reason="rh_adapter_off", alert=alert)
        return None
    res = ad.place_market_order(
        product_id=alert.ticker,
        side="buy",
        base_size=str(qty),
        client_order_id=client_order_id,
    )
    if not res.get("ok"):
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason=f"broker:{res.get('error')}",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(
            out,
            kind="blocked",
            reason=f"broker:{res.get('error')}",
            alert=alert,
        )
        return None
    return res


def _execute_scale_in(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    plan: Any,
    px: float,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    live: bool,
    out: dict[str, Any],
) -> None:
    t = plan.trade
    add_q = float(plan.added_quantity)
    if live:
        res = _execute_broker_buy(
            db,
            uid=uid,
            alert=alert,
            qty=add_q,
            client_order_id=f"atv1-{alert.id}-scale",
            snap=snap,
            llm_snap=llm_snap,
            out=out,
        )
        if res is None:
            return

    t.entry_price = float(plan.new_avg_entry)
    t.quantity = float(t.quantity) + add_q
    t.stop_loss = float(plan.new_stop)
    t.take_profit = float(plan.new_target)
    t.scale_in_count = int(t.scale_in_count or 0) + 1
    if t.indicator_snapshot is None:
        t.indicator_snapshot = {}
    if isinstance(t.indicator_snapshot, dict):
        t.indicator_snapshot = {
            **t.indicator_snapshot,
            "autotrader_scale_in_alert_ids": (t.indicator_snapshot.get("autotrader_scale_in_alert_ids") or [])
            + [alert.id],
        }
    db.add(t)
    db.commit()
    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="scaled_in",
        reason="ok",
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
        trade_id=t.id,
    )
    out["scaled_in"] += 1
    _autotrader_tick_note(out, kind="scaled_in", reason="ok", alert=alert)


def _execute_new_entry(
    db: Session,
    uid: int,
    alert: BreakoutAlert,
    px: float,
    snap: dict[str, Any],
    llm_snap: dict[str, Any] | None,
    live: bool,
    out: dict[str, Any],
) -> None:
    if px <= 0:
        _audit(db, user_id=uid, alert=alert, decision="skipped", reason="bad_px", rule_snapshot=snap)
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="skipped", reason="bad_px", alert=alert)
        return

    # Phase 3: notional = min(dial-scaled equity slice, env fallback).
    # The flat ``chili_autotrader_per_trade_notional_usd=300`` was blind to
    # equity and to pattern quality. Prefer a percent-of-equity sizing that
    # scales with the risk dial, falling back to the env dollar amount only
    # when live equity is unreachable.
    from .auto_trader_rules import (
        resolve_brain_risk_context,
        resolve_effective_capital,
    )

    env_notional = float(getattr(settings, "chili_autotrader_per_trade_notional_usd", 300.0))
    per_trade_pct = float(getattr(settings, "chili_autotrader_per_trade_risk_pct", 1.0))
    equity, cap_source = resolve_effective_capital(db, settings)
    brain_ctx = resolve_brain_risk_context(db, user_id=uid)
    dial = float(brain_ctx.get("dial_value", 1.0))

    if equity > 0 and per_trade_pct > 0:
        dyn_notional = equity * (per_trade_pct / 100.0) * dial
        notional = dyn_notional
        snap["notional_source"] = "equity_pct_dial"
    else:
        notional = env_notional * dial
        snap["notional_source"] = "env_dollar_dial"
    snap["notional_env"] = env_notional
    snap["notional_dial"] = dial
    snap["notional_capital_source"] = cap_source

    # ─── HARDCODED NOTIONAL FLOOR (TEMP — operator request 2026-04-21) ───
    # With equity $10k, dial 0.5, per_trade_pct 1% → ~$50 notional, too
    # small for meaningful capture. Floor at $300 target / $350 per-share
    # upsize ceiling so mid-priced stocks (WGS @ $70, GH @ $92, ACN @ $197)
    # can buy 1–4 whole shares instead of sub-1 fractional (which most
    # tickers reject server-side).
    # REMOVE when the dial / per_trade_pct can natively produce this sizing
    # (i.e. once equity grows or per_trade_pct is raised). Until then,
    # these constants override the dial-derived small notional.
    _TEMP_MIN_NOTIONAL_USD = 300.0
    _TEMP_MAX_PER_SHARE_USD = 350.0
    if notional < _TEMP_MIN_NOTIONAL_USD:
        snap["notional_floored"] = True
        snap["notional_before_floor"] = round(notional, 2)
        snap["notional_floor_applied"] = _TEMP_MIN_NOTIONAL_USD
        notional = _TEMP_MIN_NOTIONAL_USD
    snap["notional_effective"] = round(notional, 2)
    # ─────────────────────────────────────────────────────────────────────

    # Q1.T5 — HRP shadow sizing (and live override when flag ON).
    # Always logged for shadow comparison; the chosen_sizing field of the
    # decision tells us which to honor. Naive fallback when HRP is
    # unavailable (insufficient history etc.) so flag-flip is safe.
    try:
        from .hrp_sizing import decide_position_size as _hrp_decide
        _hrp_decision = _hrp_decide(
            db,
            symbol=(alert.ticker or "").upper(),
            account_equity_usd=float(equity if equity > 0 else env_notional / 0.02),
            user_id=uid,
        )
        snap["hrp_naive_size_usd"] = _hrp_decision.naive_size_usd
        snap["hrp_size_usd"] = _hrp_decision.hrp_size_usd
        snap["hrp_weight"] = _hrp_decision.hrp_weight
        snap["hrp_chosen_sizing"] = _hrp_decision.chosen_sizing
        snap["hrp_n_active_positions"] = _hrp_decision.n_active_positions
        if _hrp_decision.chosen_sizing == "hrp" and _hrp_decision.hrp_size_usd:
            # Flag is ON and HRP succeeded — override notional with HRP value
            # (still subject to floor + per-share-cap below).
            snap["notional_before_hrp"] = round(notional, 2)
            notional = float(_hrp_decision.hrp_size_usd)
            if notional < _TEMP_MIN_NOTIONAL_USD:
                notional = _TEMP_MIN_NOTIONAL_USD
            snap["notional_effective"] = round(notional, 2)
            snap["notional_source"] = "hrp_allocated"
    except Exception as _hrp_e:
        snap["hrp_error"] = str(_hrp_e)[:200]

    # K Phase 3 S.4 — survival-classifier sizing multiplier.
    # Composes AFTER HRP (so HRP allocates risk-parity weight, then K
    # nudges based on per-pattern survival probability). Always called,
    # logs to pattern_survival_decision_log; returns no_op when any of
    # the gates is OFF or no prediction exists. Failures are
    # deliberately swallowed — sizing must never crash the entry path.
    try:
        from .pattern_survival.decisions import compute_decision as _ps_decide
        _ps_result = _ps_decide(
            db,
            scan_pattern_id=int(alert.scan_pattern_id),
            consumer="sizing",
            input_context={"input_notional": float(notional)},
        )
        snap["ps_sizing_decision"] = _ps_result["decision"]
        snap["ps_sizing_predicted"] = _ps_result.get("predicted_survival")
        if _ps_result["decision"] == "apply":
            mult = float(_ps_result["details"]["multiplier"])
            snap["notional_before_ps_sizing"] = round(notional, 2)
            snap["ps_sizing_multiplier"] = mult
            notional = float(_ps_result["details"]["output_notional"])
            if notional < _TEMP_MIN_NOTIONAL_USD:
                notional = _TEMP_MIN_NOTIONAL_USD
                snap["ps_sizing_floored_to_min"] = True
            snap["notional_effective"] = round(notional, 2)
            snap["notional_source"] = (
                snap.get("notional_source", "unknown") + "+ps_sizing"
            )
        else:
            # no_op — multiplier was 1.0 effectively. Still surface
            # the skip_reason so the operator can confirm gating.
            snap["ps_sizing_skip_reason"] = (
                _ps_result.get("details") or {}
            ).get("skip_reason")
    except Exception as _ps_e:
        # Hard rule: sizing must never crash entry. Fall back to the
        # HRP-allocated notional unchanged.
        snap["ps_sizing_error"] = str(_ps_e)[:200]

    # HARDCODED (TEMP 2026-04-21): floor to whole shares rather than
    # fractional. Most mid/large-cap RH tickers (ACN, WGS, GH, BA…)
    # don't support fractional orders, and server-side rejection wastes
    # a tick. Whole-share sizing sacrifices some precision but succeeds
    # universally. REMOVE with the rest of the TEMP block when the
    # brain-driven fractional-eligibility check ships.
    # CCC -- options sizing bypass. Operator-driven option entry already
    # encoded qty in option_meta.quantity (default 1 contract). Equity
    # math (qty = notional / px where px = UNDERLYING price) gives
    # qty=0 for SPY at $714 / $300 notional and trips the per-share
    # cap as "symbol_too_expensive_for_notional", which is wrong by
    # construction. For options, set qty from option_meta and use
    # premium*100*qty as the effective notional (skipping the per-share
    # ceiling check below since qty>=1 is now guaranteed).
    if snap.get("options_path") and snap.get("option_meta"):
        _opt_meta = snap["option_meta"]
        try:
            qty = int(_opt_meta.get("quantity") or 1)
            if qty < 1:
                qty = 1
        except Exception:
            qty = 1
        try:
            _premium = float(_opt_meta.get("limit_price") or alert.entry_price or 0)
        except Exception:
            _premium = 0.0
        qty_raw = float(qty)  # populate for snapshot logging
        snap["qty_source"] = "options_meta"
        snap["notional_effective"] = round(_premium * 100.0 * qty, 2)
    else:
        qty_raw = notional / px
        qty = int(qty_raw)  # whole shares only for now

    if qty < 1 and px > 0:
        if px <= _TEMP_MAX_PER_SHARE_USD:
            snap["qty_upsized_reason"] = "fractional_not_supported_fallback"
            snap["qty_before_upsize"] = round(qty_raw, 8)
            qty = 1
            snap["notional_effective"] = round(px, 2)
        else:
            # px exceeds the TEMP per-share ceiling — even 1 share is
            # too expensive for the operator's intended trade size.
            _audit(
                db, user_id=uid, alert=alert,
                decision="skipped",
                reason="symbol_too_expensive_for_notional",
                rule_snapshot=snap,
            )
            out["skipped"] += 1
            _autotrader_tick_note(
                out, kind="skipped", reason="symbol_too_expensive_for_notional", alert=alert
            )
            return
    else:
        # qty >= 1 case: update notional_effective to the integer-share
        # actual cost (may be slightly under target — e.g. GH at $91.75
        # gives 3 shares = $275.25, below $300 target but close enough
        # for whole-share sizing).
        snap["notional_effective"] = round(qty * px, 2)
        snap["qty_raw"] = round(qty_raw, 8)

    if live:
        res = _execute_broker_buy(
            db,
            uid=uid,
            alert=alert,
            qty=qty,
            client_order_id=f"atv1-{alert.id}-buy",
            snap=snap,
            llm_snap=llm_snap,
            out=out,
        )
        if res is None:
            return
        raw = res.get("raw") or {}
        try:
            fill = float(raw.get("average_price") or raw.get("price") or px)
        except (TypeError, ValueError):
            fill = px

        tr = Trade(
            user_id=uid,
            ticker=alert.ticker.upper(),
            direction="long",
            entry_price=fill,
            quantity=float(qty),
            entry_date=datetime.utcnow(),
            status="open",
            stop_loss=float(alert.stop_loss) if alert.stop_loss is not None else None,
            take_profit=float(alert.target_price) if alert.target_price is not None else None,
            scan_pattern_id=alert.scan_pattern_id,
            related_alert_id=alert.id,
            broker_source="robinhood",
            management_scope=MANAGEMENT_SCOPE_AUTO_TRADER_V1,
            broker_order_id=str(res.get("order_id") or ""),
            indicator_snapshot={
                "breakout_alert": alert.indicator_snapshot,
                "signals": alert.signals_snapshot,
            },
            tags="autotrader_v1",
            auto_trader_version=AUTOTRADER_VERSION,
            scale_in_count=0,
        )
        db.add(tr)
        db.commit()
        db.refresh(tr)
        # Phase 2C: emit trade_lifecycle entry event and save correlation_id
        # on the Trade. On close, plasticity uses this to look up the path log
        # and reinforce/attenuate the edges that carried the signal.
        try:
            from .brain_neural_mesh.publisher import publish_trade_lifecycle

            entry_corr = publish_trade_lifecycle(
                db,
                trade_id=int(tr.id),
                ticker=tr.ticker,
                transition="entry",
                broker_source="robinhood",
                quantity=float(tr.quantity),
                price=float(fill),
            )
            if entry_corr:
                tr.mesh_entry_correlation_id = entry_corr
                db.commit()
        except Exception:
            # Post-entry plasticity / mesh correlation is best-effort — a
            # failure must not undo a successful order placement. Log at
            # DEBUG so the silent-swallow audit stays clean while still
            # leaving a trail in the app log for ops follow-up.
            logger.debug(
                "[autotrader] plasticity mesh correlation post-entry failed "
                "(non-fatal) for trade_id=%s",
                getattr(tr, "id", None),
                exc_info=True,
            )
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="placed",
            reason="ok",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
            trade_id=tr.id,
        )
        out["placed"] += 1
        _autotrader_tick_note(out, kind="placed", reason="live_robinhood", alert=alert)
        return

    # Paper
    from .paper_trading import open_paper_trade

    iq = max(1, int(qty))
    sig = {
        "auto_trader_v1": True,
        "breakout_alert_id": alert.id,
        "projected": snap.get("projected_profit_pct"),
    }
    pt = open_paper_trade(
        db,
        uid,
        alert.ticker,
        px,
        scan_pattern_id=alert.scan_pattern_id,
        stop_price=float(alert.stop_loss) if alert.stop_loss is not None else None,
        target_price=float(alert.target_price) if alert.target_price is not None else None,
        direction="long",
        quantity=iq,
        signal_json=sig,
    )
    if pt is None:
        _audit(
            db,
            user_id=uid,
            alert=alert,
            decision="blocked",
            reason="paper_open_failed",
            rule_snapshot=snap,
            llm_snapshot=llm_snap,
        )
        out["skipped"] += 1
        _autotrader_tick_note(out, kind="blocked", reason="paper_open_failed", alert=alert)
        return

    _audit(
        db,
        user_id=uid,
        alert=alert,
        decision="placed",
        reason="paper",
        rule_snapshot=snap,
        llm_snapshot=llm_snap,
        trade_id=None,
    )
    out["placed"] += 1
    _autotrader_tick_note(out, kind="placed", reason="paper", alert=alert)
