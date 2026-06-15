"""Lane-health FROZEN alert for the momentum/trading lane.

A tripped safety breaker silently empties the lane: the auto-arm pass short-circuits
(``skipped="kill_switch"`` / a per-broker daily-loss block) and the only visible trace
is a 6ms ``phase=ok`` tick with no ``[scheduler] auto_arm:`` line. On 2026-06-15 the
global daily-loss kill switch tripped at 05:18 ET and the momentum lane sat empty all
day before the operator caught it — a frozen SAFETY state is silent.

This module makes a frozen lane LOUD:

* :func:`evaluate_lane_health` — pure, read-only assessment (no side effects). Reused by
  the cockpit P&L rollup so the autopilot band shows the freeze live.
* :func:`run_lane_health_check` — the periodic scheduler hook. When the lane has been
  frozen past the (adaptive) grace window it emits ``logger.critical("[lane_health]
  FROZEN ...")`` AND writes an audit row to ``trading_alerts`` — change-only with a
  re-remind cooldown so a long freeze keeps nagging without spamming.

Frozen conditions:
  (a) the GLOBAL kill switch has been active longer than the grace window;
  (b) a PER-BROKER daily-loss block is set (and held past grace);
  (c) the lane is enabled + expected to be trading, but the auto-arm pass / scheduler
      is not actually executing (job wedged or scheduler-worker down) — distinct from a
      legitimately quiet market, where the pass keeps firing every tick (it just finds
      no setup) and the heartbeats stay fresh.

Threshold N is ADAPTIVE: it derives from the lane's OWN watch cadence (auto_arm
max_watch + watch_extend) — no separate magic number — with a single optional override
knob. Reversible: ``CHILI_LANE_HEALTH_ALERT_ENABLED=0`` disables the alert entirely.

See [[project_per_broker_daily_loss]] (the 06-15 incident) and
[[project_autopilot_money_cockpit]] (where the operator actually looks).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from ....config import settings

logger = logging.getLogger(__name__)

# Process-start floor: a fresh scheduler process has no in-process auto-arm heartbeat
# yet, so "stalled" must be measured from when this module loaded, not from epoch.
_MODULE_LOADED_MONOTONIC = time.monotonic()

# In-process heartbeat: the auto-arm scheduler job stamps this every time it actually
# executes a pass. A healthy-but-quiet lane keeps stamping; a wedged/dead job goes
# stale. Lives in the SAME process as the lane-health job (both are scheduler jobs).
_auto_arm_last_run_monotonic: float | None = None
_auto_arm_last_run_wall: datetime | None = None
_heartbeat_lock = threading.Lock()

# Change-only / cooldown state for the loud side effects.
_alert_lock = threading.Lock()
_last_alert_signature: str | None = None
_last_alert_at_monotonic: float | None = None


def record_auto_arm_run() -> None:
    """Stamp the auto-arm execution heartbeat. Called by the auto-arm scheduler job at
    the top of each pass so lane-health can tell a wedged job from a quiet market."""
    global _auto_arm_last_run_monotonic, _auto_arm_last_run_wall
    with _heartbeat_lock:
        _auto_arm_last_run_monotonic = time.monotonic()
        _auto_arm_last_run_wall = datetime.utcnow()


def _auto_arm_heartbeat_age_seconds() -> float | None:
    """Seconds since the auto-arm pass last executed in THIS process, or None if it has
    never run yet (then the caller uses the module-load floor instead)."""
    with _heartbeat_lock:
        mono = _auto_arm_last_run_monotonic
    if mono is None:
        return None
    return max(0.0, time.monotonic() - mono)


def _alert_enabled() -> bool:
    return bool(getattr(settings, "chili_lane_health_alert_enabled", True))


def freeze_grace_seconds() -> float:
    """Adaptive grace before a held safety state counts as FROZEN.

    A positive ``chili_lane_health_freeze_alert_seconds`` overrides. Otherwise derive
    from the lane's own patience: one full auto-arm watch-and-extend cycle (max_watch +
    watch_extend). If a breaker has held longer than the lane would wait on a single
    candidate, the lane has skipped a complete arming cycle — it is frozen, not paused.
    No separate magic number; the threshold tracks the lane's configured cadence.
    """
    override = float(getattr(settings, "chili_lane_health_freeze_alert_seconds", 0.0) or 0.0)
    if override > 0:
        return override
    max_watch = float(getattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 300) or 300)
    extend = float(getattr(settings, "chili_momentum_auto_arm_watch_extend_seconds", 600) or 600)
    return max(60.0, max_watch + extend)


def _lane_enabled() -> bool:
    """The lane is CONFIGURED to be running (so a silent empty lane is a freeze, not an
    intentional off). Mirrors the auto-arm scheduler registration condition."""
    return (
        bool(getattr(settings, "chili_momentum_live_runner_enabled", False))
        and bool(getattr(settings, "chili_momentum_live_runner_scheduler_enabled", True))
        and bool(getattr(settings, "chili_momentum_auto_arm_live_enabled", True))
        and bool(getattr(settings, "chili_momentum_auto_arm_live_scheduler_enabled", True))
    )


def _expected_trading_window_open() -> bool:
    """Is the lane expected to be able to arm RIGHT NOW? Crypto-inclusive lanes trade
    24/7; an equity-only lane only during tradeable equity hours. Fail-open (assume
    expected) so a profile/clock hiccup never SUPPRESSES a real freeze."""
    try:
        equity_only = bool(getattr(settings, "chili_momentum_auto_arm_equity_only", False))
        crypto_only = bool(getattr(settings, "chili_momentum_auto_arm_crypto_only", True))
        if crypto_only or not equity_only:
            return True  # crypto present -> always expected
        from .market_profile import is_tradeable_now

        return bool(is_tradeable_now("SPY"))
    except Exception:
        return True


def _auto_arm_user_id() -> int | None:
    return getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )


def _human(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m" + (f"{sec}s" if sec else "")
    h, m = divmod(m, 60)
    return f"{h}h" + (f"{m}m" if m else "")


def _parse_iso(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "").replace("+00:00", "")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def evaluate_lane_health(db, *, user_id: int | None = None) -> dict[str, Any]:
    """Read-only assessment of whether the momentum lane is frozen.

    Returns a structured dict (always — never raises): ``frozen``, ``severity``,
    ``headline``, ``detail``, ``conditions`` (each with kind/since/elapsed), plus the
    grace window. Conditions active-but-within-grace are reported with ``frozen=False``
    so the cockpit can show "armed to alert" without crying wolf.
    """
    now = datetime.utcnow()
    grace = freeze_grace_seconds()
    enabled = _alert_enabled()
    out: dict[str, Any] = {
        "enabled": enabled,
        "frozen": False,
        "severity": "ok",
        "headline": None,
        "detail": None,
        "conditions": [],
        "grace_seconds": round(grace, 1),
        "as_of_utc": now.isoformat() + "Z",
    }
    if not enabled:
        return out

    conditions: list[dict[str, Any]] = []

    # (a) Global kill switch.
    try:
        from ..governance import get_kill_switch_status

        ks = get_kill_switch_status()
        if ks.get("active"):
            since = _parse_iso(ks.get("set_at"))
            elapsed = (now - since).total_seconds() if since else None
            conditions.append({
                "kind": "kill_switch",
                "reason": ks.get("reason") or "kill_switch_active",
                "since_utc": (since.isoformat() + "Z") if since else None,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
                "elapsed_human": _human(elapsed),
                # No set_at (e.g. manual switch before restore) -> treat as frozen now,
                # erring loud: a held global halt is exactly the silent-freeze case.
                "frozen": (elapsed is None) or (elapsed >= grace),
            })
    except Exception:
        logger.debug("[lane_health] kill-switch probe failed", exc_info=True)

    # (b) Per-broker daily-loss blocks (the 06-15 Coinbase-sized cap incident).
    try:
        from ..governance import (
            REAL_DAILY_LOSS_FAMILIES,
            get_broker_daily_loss_block,
            is_broker_daily_loss_blocked,
        )

        for fam in REAL_DAILY_LOSS_FAMILIES:
            if not is_broker_daily_loss_blocked(fam):
                continue
            blk = get_broker_daily_loss_block(fam) or {}
            since = _parse_iso(blk.get("set_at"))
            elapsed = (now - since).total_seconds() if since else None
            conditions.append({
                "kind": "broker_block",
                "family": fam,
                "reason": blk.get("reason") or f"broker_daily_loss_block_{fam}",
                "realized": blk.get("realized"),
                "limit": blk.get("limit"),
                "since_utc": (since.isoformat() + "Z") if since else None,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
                "elapsed_human": _human(elapsed),
                "frozen": (elapsed is None) or (elapsed >= grace),
            })
    except Exception:
        logger.debug("[lane_health] per-broker block probe failed", exc_info=True)

    # (c) The lane is enabled + expected to trade, but the pass/scheduler is not
    #     executing. Only meaningful when NO safety breaker is already the reason (a
    #     breaker SHOULD leave the lane idle). Distinguishes a wedged job/dead worker
    #     from a legitimately quiet market (which keeps the heartbeats fresh).
    if not conditions and _lane_enabled() and _expected_trading_window_open():
        # (c1) Whole scheduler-worker down (durable, cross-process).
        try:
            from .automation_query import _latest_scheduler_heartbeat_at

            hb = _latest_scheduler_heartbeat_at(db)
            hb_age = (now - hb).total_seconds() if hb else None
            if hb is None or (hb_age is not None and hb_age >= grace):
                conditions.append({
                    "kind": "scheduler_down",
                    "reason": "scheduler_worker_heartbeat_missing" if hb is None else "scheduler_worker_stale",
                    "since_utc": (hb.isoformat() + "Z") if hb else None,
                    "elapsed_seconds": round(hb_age, 1) if hb_age is not None else None,
                    "elapsed_human": _human(hb_age),
                    "frozen": True,
                })
        except Exception:
            logger.debug("[lane_health] scheduler-heartbeat probe failed", exc_info=True)

        # (c2) Auto-arm pass specifically wedged (scheduler alive, job not firing).
        if not conditions:
            aa_age = _auto_arm_heartbeat_age_seconds()
            if aa_age is None:
                # Never ran this process yet — only a freeze if the process has been up
                # long enough that the 40s-delayed first pass should have fired.
                uptime = time.monotonic() - _MODULE_LOADED_MONOTONIC
                if uptime >= grace:
                    conditions.append({
                        "kind": "auto_arm_stalled",
                        "reason": "auto_arm_never_ran",
                        "since_utc": None,
                        "elapsed_seconds": round(uptime, 1),
                        "elapsed_human": _human(uptime),
                        "frozen": True,
                    })
            elif aa_age >= grace:
                conditions.append({
                    "kind": "auto_arm_stalled",
                    "reason": "auto_arm_pass_not_executing",
                    "since_utc": None,
                    "elapsed_seconds": round(aa_age, 1),
                    "elapsed_human": _human(aa_age),
                    "frozen": True,
                })

    out["conditions"] = conditions
    frozen = [c for c in conditions if c.get("frozen")]
    if frozen:
        out["frozen"] = True
        out["severity"] = "critical"
        out["headline"] = _headline_for(frozen)
        out["detail"] = _detail_for(frozen)
    return out


def _headline_for(frozen: list[dict[str, Any]]) -> str:
    c = frozen[0]
    kind = c.get("kind")
    extra = f" (+{len(frozen) - 1} more)" if len(frozen) > 1 else ""
    if kind == "kill_switch":
        return f"MOMENTUM LANE FROZEN — global kill switch active {c.get('elapsed_human')}{extra}"
    if kind == "broker_block":
        return f"MOMENTUM LANE FROZEN — {c.get('family')} daily-loss block {c.get('elapsed_human')}{extra}"
    if kind == "scheduler_down":
        return f"MOMENTUM LANE FROZEN — scheduler worker not running ({c.get('elapsed_human')}){extra}"
    if kind == "auto_arm_stalled":
        return f"MOMENTUM LANE FROZEN — auto-arm pass not executing ({c.get('elapsed_human')}){extra}"
    return f"MOMENTUM LANE FROZEN ({len(frozen)} condition(s))"


def _detail_for(frozen: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for c in frozen:
        kind = c.get("kind")
        if kind == "kill_switch":
            parts.append(f"kill_switch[{c.get('reason')}] {c.get('elapsed_human')}")
        elif kind == "broker_block":
            r, lim = c.get("realized"), c.get("limit")
            money = (
                f" realized=${float(r):.2f} vs -${float(lim):.0f}"
                if isinstance(r, (int, float)) and isinstance(lim, (int, float))
                else ""
            )
            parts.append(f"{c.get('family')} block[{c.get('reason')}]{money} {c.get('elapsed_human')}")
        elif kind == "scheduler_down":
            parts.append(f"scheduler {c.get('reason')} {c.get('elapsed_human')}")
        elif kind == "auto_arm_stalled":
            parts.append(f"auto_arm {c.get('reason')} {c.get('elapsed_human')}")
    tail = (
        " — new entries halted (exits stay live); reset via the kill-switch runbook "
        "or wait for the ET-day roll if it is a daily-loss cap."
    )
    return "; ".join(parts) + tail


def _signature(frozen: list[dict[str, Any]]) -> str:
    """Identity of the frozen state (kind+reason+family), stable across the elapsed
    growing — so we re-emit on a STATE change, not on every tick."""
    return "|".join(
        sorted(f"{c.get('kind')}:{c.get('family', '')}:{c.get('reason')}" for c in frozen)
    )


def _write_alert_row(db, *, headline: str, detail: str, signature: str) -> None:
    """Append a durable audit row to trading_alerts (the notification log). A bare
    insert — does NOT itself dispatch SMS/email (that is a separate delivery path)."""
    try:
        from ....models.trading import AlertHistory

        row = AlertHistory(
            user_id=_auto_arm_user_id(),
            alert_type="lane_health_frozen",
            ticker=None,
            message=(headline + " — " + detail)[:4000],
            sent_via="cockpit",
            success=True,
            content_signature=("lane_health:" + signature)[:512],
        )
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("[lane_health] could not write audit row", exc_info=True)


def run_lane_health_check(db) -> dict[str, Any]:
    """Periodic hook: evaluate + emit the LOUD signal when frozen.

    Change-only with a re-remind cooldown (reuses the grace window): emit when the
    frozen state first appears, when its identity changes, or when the cooldown elapses
    while still frozen — so an 8h freeze keeps nagging instead of going silent after
    one line. Logs a recovery line (and resets) when the lane un-freezes.
    """
    global _last_alert_signature, _last_alert_at_monotonic

    result = evaluate_lane_health(db)
    if not result.get("enabled"):
        return result

    frozen = [c for c in result.get("conditions", []) if c.get("frozen")]
    if not frozen:
        with _alert_lock:
            was = _last_alert_signature
            _last_alert_signature = None
            _last_alert_at_monotonic = None
        if was is not None:
            logger.warning("[lane_health] RECOVERED — momentum lane no longer frozen (was: %s)", was)
        return result

    sig = _signature(frozen)
    cooldown = freeze_grace_seconds()
    now_mono = time.monotonic()
    with _alert_lock:
        changed = sig != _last_alert_signature
        cooled = (
            _last_alert_at_monotonic is None
            or (now_mono - _last_alert_at_monotonic) >= cooldown
        )
        should_emit = changed or cooled
        if should_emit:
            _last_alert_signature = sig
            _last_alert_at_monotonic = now_mono

    if should_emit:
        headline = result.get("headline") or "MOMENTUM LANE FROZEN"
        detail = result.get("detail") or ""
        logger.critical("[lane_health] FROZEN %s | %s", headline, detail)
        _write_alert_row(db, headline=headline, detail=detail, signature=sig)
        result["emitted"] = True
    else:
        result["emitted"] = False
    return result
