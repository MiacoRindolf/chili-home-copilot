"""Post-exit excursion + shake-out classification — CORRECT learning labels.

A momentum trade's raw PnL is a SHALLOW, often WRONG label for the learner. A
position stopped out by a too-tight stop (KAIO: -0.72% then it ran PAST target)
records identically to a thesis-invalidated loss, so the brain wrongly penalises a
GOOD setup and learns to avoid it. This module decomposes the outcome: given the
price path AFTER the exit, it answers "would the thesis have worked?" and labels a
SHAKE-OUT separately, so the learner fixes the STOP (widen it) instead of the
SETUP (abandon it). docs/DESIGN/MOMENTUM_LANE.md ; see [[project_momentum_lane]].

Pure + side-effect-free; the scheduler job feeds it the lookforward High/Low.
"""
from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

# Exit reasons that are LOSING/stop-style cuts (a shake-out can only happen on these).
_STOP_STYLE_EXITS = frozenset(
    {"stop", "stop_loss", "stoploss", "bailout", "max_loss", "per_trade_loss", "trail_stop"}
)
# Exit reasons that are deliberate target/profit takes.
_TARGET_STYLE_EXITS = frozenset({"target", "take_profit", "takeprofit", "scale_out", "trail"})


def _pct(numer: float, denom: float) -> float:
    if denom <= 0 or not math.isfinite(denom):
        return 0.0
    return (numer / denom) * 100.0


def classify_exit_kind(exit_reason: str | None, realized_pnl: float | None) -> str:
    r = str(exit_reason or "").strip().lower()
    if r in _STOP_STYLE_EXITS or (realized_pnl is not None and float(realized_pnl) < 0 and r not in _TARGET_STYLE_EXITS):
        return "loss_cut"
    if r in _TARGET_STYLE_EXITS or (realized_pnl is not None and float(realized_pnl) > 0):
        return "profit_take"
    return "neutral"


def compute_post_exit_excursion(
    *,
    entry_price: float,
    exit_price: float,
    original_target: float | None,
    original_stop: float | None,
    side_long: bool,
    future_high: float,
    future_low: float,
    exit_reason: str | None,
    realized_pnl: float | None = None,
    reversal_capture_frac: float = 0.5,
) -> dict[str, Any]:
    """Decompose a closed momentum trade against its post-exit price path.

    ``future_high`` / ``future_low`` are the max High / min Low over the lookforward
    window AFTER the exit. Returns excursion metrics + a decomposed outcome class +
    a ``setup_quality`` score (did the move happen, independent of whether our stop
    captured it) that the selection learner should use INSTEAD of raw PnL sign.
    """
    e = float(entry_price or 0.0)
    x = float(exit_price or 0.0)
    if e <= 0 or x <= 0 or not (math.isfinite(e) and math.isfinite(x)):
        return {"ok": False, "reason": "invalid_prices"}

    hi = float(future_high or 0.0)
    lo = float(future_low or 0.0)
    # Favorable / adverse excursion AFTER the exit, in the trade's direction.
    if side_long:
        post_exit_mfe_pct = _pct(hi - x, x)          # how far it ran up after we sold
        post_exit_mae_pct = _pct(x - lo, x)          # how far down after we sold
        cf_target_hit = original_target is not None and hi >= float(original_target)
        ran_back_through_entry = hi >= e
        ran_back_frac = _pct(hi - x, max(e - x, 1e-12)) / 100.0 if e > x else 1.0
    else:
        post_exit_mfe_pct = _pct(x - lo, x)
        post_exit_mae_pct = _pct(hi - x, x)
        cf_target_hit = original_target is not None and lo <= float(original_target)
        ran_back_through_entry = lo <= e
        ran_back_frac = _pct(x - lo, max(x - e, 1e-12)) / 100.0 if x > e else 1.0

    kind = classify_exit_kind(exit_reason, realized_pnl)
    was_loss_cut = kind == "loss_cut"

    # Decompose:
    #  - shakeout: a loss-cut, but the thesis WOULD have hit target afterwards.
    #  - premature_stop: a loss-cut that didn't reach target but reversed >= frac
    #    of the way back from the exit to the entry (stop somewhat too tight).
    #  - thesis_invalidated: a loss-cut with no meaningful favorable reversal.
    #  - target_win / clean_win / neutral otherwise.
    if was_loss_cut and cf_target_hit:
        outcome_class = "shakeout"
        setup_quality = 1.0
    elif was_loss_cut and ran_back_through_entry and ran_back_frac >= float(reversal_capture_frac):
        outcome_class = "premature_stop"
        setup_quality = 0.6
    elif was_loss_cut:
        outcome_class = "thesis_invalidated"
        setup_quality = 0.0
    elif kind == "profit_take":
        outcome_class = "target_win" if (realized_pnl or 0) > 0 else "clean_win"
        setup_quality = 1.0
    else:
        outcome_class = "neutral"
        setup_quality = 0.5

    # stop_too_tight is the actionable signal for the STOP learner (distinct from
    # the setup signal above): the setup worked but our stop didn't let us hold it.
    stop_too_tight = outcome_class in ("shakeout", "premature_stop")

    return {
        "ok": True,
        "post_exit_mfe_pct": round(post_exit_mfe_pct, 4),
        "post_exit_mae_pct": round(post_exit_mae_pct, 4),
        "counterfactual_target_hit": bool(cf_target_hit),
        "ran_back_through_entry": bool(ran_back_through_entry),
        "outcome_class": outcome_class,
        "setup_quality": setup_quality,
        "stop_too_tight": bool(stop_too_tight),
        "exit_kind": kind,
    }


def _parse_iso(value: Any):
    from datetime import datetime
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _persist_pending(db, sess, snap: dict, le: dict, pend: dict) -> None:
    le["post_exit_excursion_pending"] = pend
    snap["momentum_live_execution"] = le
    sess.risk_snapshot_json = dict(snap)
    # `snap` here is the LIVE attribute (run_post_exit_excursion_pass reads
    # sess.risk_snapshot_json directly), and the column is a plain JSONB with no
    # MutableDict tracking — so the nested in-place mutation above is NOT detected
    # as a change and the UPDATE is silently dropped (markers stay 'pending'
    # forever, reprocessed every cycle). Force the flush explicitly.
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        pass  # non-ORM instance (tests) — the direct reassignment above suffices
    db.add(sess)
    db.commit()


def _patch_outcome_label(db, session_id: int, label: dict) -> None:
    """Best-effort: stamp the decomposed label onto the session's outcome row so the
    SELECTION learner can use setup_quality (don't penalise a shaken-out good setup)
    instead of raw PnL sign."""
    try:
        from ....models.trading import MomentumAutomationOutcome

        row = (
            db.query(MomentumAutomationOutcome)
            .filter(MomentumAutomationOutcome.session_id == int(session_id))
            .order_by(MomentumAutomationOutcome.id.desc())
            .first()
        )
        if row is None:
            return
        summary = dict(row.extracted_summary_json) if isinstance(row.extracted_summary_json, dict) else {}
        summary["post_exit_label"] = {
            "outcome_class": label.get("outcome_class"),
            "setup_quality": label.get("setup_quality"),
            "stop_too_tight": label.get("stop_too_tight"),
            "counterfactual_target_hit": label.get("counterfactual_target_hit"),
            "post_exit_mfe_pct": label.get("post_exit_mfe_pct"),
        }
        row.extracted_summary_json = dict(summary)
        db.add(row)
        db.commit()
    except Exception:
        logger.debug("[post_exit] outcome-row patch skipped session=%s", session_id, exc_info=True)


def run_post_exit_excursion_pass(db, *, now=None) -> dict[str, Any]:
    """Label recently-closed momentum live trades against their post-exit price path.

    For each closed session carrying a `post_exit_excursion_pending` marker whose
    horizon has elapsed, fetch the lookforward bars, classify (shakeout vs
    thesis_invalidated vs win), persist the decomposed label, and stamp the outcome
    row so the learner sees CORRECT data — not a shallow loss. Returns a summary.
    """
    from datetime import datetime, timedelta, timezone

    from ....config import settings
    from ....models.trading import TradingAutomationSession

    from .live_fsm import STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING

    # Sessions currently HOLDING a position: they re-entered after the exit that
    # set this marker, so the live runner owns `momentum_live_execution` and
    # rewrites it every tick — any label we persist would be clobbered, and an
    # open trade has not truly exited. Skip them; the marker resolves on the next
    # real exit. (Without this the labeler fights the runner and re-labels the
    # same open position every cycle.)
    _holding_states = frozenset({STATE_LIVE_ENTERED, STATE_LIVE_TRAILING, STATE_LIVE_SCALING_OUT})

    out: dict[str, Any] = {"checked": 0, "labeled": 0, "shakeouts": 0, "waiting": 0, "errors": 0, "skipped_open": 0}
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    horizon_default = int(getattr(settings, "chili_momentum_post_exit_horizon_seconds", 1800) or 1800)
    lookback = timedelta(seconds=horizon_default * 4 + 3600)
    try:
        rows = (
            db.query(TradingAutomationSession)
            .filter(
                TradingAutomationSession.mode == "live",
                TradingAutomationSession.updated_at >= now - lookback,
            )
            .order_by(TradingAutomationSession.updated_at.desc())
            .limit(200)
            .all()
        )
    except Exception:
        return out

    for sess in rows:
        snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
        le = snap.get("momentum_live_execution")
        if not isinstance(le, dict):
            continue
        pend = le.get("post_exit_excursion_pending")
        if not isinstance(pend, dict) or pend.get("state") != "pending":
            continue
        if getattr(sess, "state", None) in _holding_states:
            out["skipped_open"] += 1
            continue
        out["checked"] += 1
        exit_t = _parse_iso(pend.get("exit_time_utc"))
        horizon = int(pend.get("horizon_seconds") or horizon_default)
        if exit_t is None:
            pend["state"] = "error"
            _persist_pending(db, sess, snap, le, pend)
            out["errors"] += 1
            continue
        if (now - exit_t).total_seconds() < horizon:
            out["waiting"] += 1
            continue
        try:
            from ..market_data import fetch_ohlcv_df

            end_t = exit_t + timedelta(seconds=horizon)
            df = fetch_ohlcv_df(
                str(pend.get("symbol") or ""),
                interval="1m",
                start=exit_t.isoformat()[:19],
                end=end_t.isoformat()[:19],
            )
        except Exception:
            df = None
        if df is None or getattr(df, "empty", True):
            attempts = int(pend.get("attempts") or 0) + 1
            pend["attempts"] = attempts
            if attempts >= 5:
                pend["state"] = "no_data"
            _persist_pending(db, sess, snap, le, pend)
            out["errors"] += 1
            continue
        try:
            fut_hi = float(df["High"].max())
            fut_lo = float(df["Low"].min())
        except Exception:
            pend["state"] = "error"
            _persist_pending(db, sess, snap, le, pend)
            out["errors"] += 1
            continue

        label = compute_post_exit_excursion(
            entry_price=float(pend.get("entry_price") or 0.0),
            exit_price=float(pend.get("exit_price") or 0.0),
            original_target=pend.get("original_target"),
            original_stop=pend.get("original_stop"),
            side_long=bool(pend.get("side_long", True)),
            future_high=fut_hi,
            future_low=fut_lo,
            exit_reason=pend.get("exit_reason"),
            realized_pnl=pend.get("realized_pnl"),
        )
        le["post_exit_excursion"] = label
        pend["state"] = "done"
        _persist_pending(db, sess, snap, le, pend)
        out["labeled"] += 1
        if label.get("outcome_class") == "shakeout":
            out["shakeouts"] += 1
            logger.warning(
                "[post_exit] SHAKEOUT session=%s %s — setup was right, stop too tight "
                "(post_exit_mfe=%.2f%% cf_target_hit=%s). Learner: don't penalise the setup.",
                sess.id, pend.get("symbol"), label.get("post_exit_mfe_pct"),
                label.get("counterfactual_target_hit"),
            )
        _patch_outcome_label(db, int(sess.id), label)

    return out
