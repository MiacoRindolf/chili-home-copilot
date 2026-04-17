"""Phase M.2-autopilot: pure decision model for the auto-advance engine.

No I/O, no SQLAlchemy, no ``settings`` access. Inputs are explicit
dataclasses; outputs are explicit dataclasses. Unit-testable in
isolation.

The engine cycles daily. For each M.2 slice (``tilt`` / ``promotion``
/ ``killswitch``) it:

1. Gathers evidence (current mode, days-in-stage, decision counts,
   slice-specific safety envelope metrics, approval-live flag,
   anomaly flags).
2. Feeds evidence into :func:`evaluate_slice_gates` which returns a
   single :class:`AutopilotDecision` (advance / hold / revert /
   blocked_by_order_lock).
3. The service applies the decision by writing to
   ``trading_brain_runtime_modes`` (and, for compare->authoritative,
   inserting a governance approval row).

Ordering contract (preserves the M.2 cutover order):

* ``tilt`` never gated by order lock (first slice).
* ``killswitch`` may not advance beyond ``shadow`` until ``tilt``
  is ``authoritative``.
* ``promotion`` may not advance beyond ``shadow`` until ``killswitch``
  is ``authoritative``.

Rate limit: at most one advance per slice per UTC day. The service
enforces this; the pure model simply records the last-advance date
it was given.

Anomaly / revert contract:

* If ``slice.anomaly_refused_authoritative`` is true (any
  ``*_refused_authoritative`` line in the last 24h) and current mode
  is authoritative, revert to compare.
* If ``slice.authoritative_approval_missing`` is true and current
  mode is authoritative, revert to compare (belt-and-suspenders:
  the slice already fail-closes, but we also downshift the stage so
  the approval contract stays visible in audit).
* If ``slice.diagnostics_stale_hours`` > max threshold OR
  ``slice.release_blocker_failed`` is true, revert one stage
  (authoritative->compare or compare->shadow; shadow->off is NOT
  allowed by the autopilot — master-kill is the only path down to
  off).

Gate short names (stable strings for ops log / audit):

* ``days_in_stage``
* ``total_decisions``
* ``diagnostics_healthy``
* ``release_blocker_clean``
* ``scan_status_frozen``
* ``envelope_tilt_multiplier``
* ``envelope_promotion_block_ratio``
* ``envelope_killswitch_mean_fires``
* ``approval_live``
* ``order_lock``
* ``rate_limit``
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

ALLOWED_STAGES: tuple[str, ...] = ("off", "shadow", "compare", "authoritative")
ADVANCEABLE_STAGES: tuple[str, ...] = ("shadow", "compare", "authoritative")

SLICE_TILT = "tilt"
SLICE_PROMOTION = "promotion"
SLICE_KILLSWITCH = "killswitch"
SLICE_NAMES: tuple[str, ...] = (SLICE_TILT, SLICE_KILLSWITCH, SLICE_PROMOTION)


@dataclass(frozen=True)
class AutopilotConfig:
    """Tunables — values come from ``settings`` but the pure model
    only sees these explicit fields."""

    shadow_days: int = 5
    compare_days: int = 10
    min_decisions: int = 100

    tilt_mult_min: float = 0.85
    tilt_mult_max: float = 1.25

    promo_block_max_ratio: float = 0.10

    ks_max_fires_per_day: float = 1.0

    approval_days: int = 30
    diagnostics_max_stale_hours: float = 24.0

    def __post_init__(self) -> None:
        if self.shadow_days < 1:
            raise ValueError("shadow_days must be >= 1")
        if self.compare_days < 1:
            raise ValueError("compare_days must be >= 1")
        if self.min_decisions < 0:
            raise ValueError("min_decisions must be >= 0")
        if not (0.0 < self.tilt_mult_min <= 1.0 <= self.tilt_mult_max):
            raise ValueError(
                "tilt envelope must satisfy 0 < min <= 1 <= max"
            )
        if not (0.0 <= self.promo_block_max_ratio <= 1.0):
            raise ValueError(
                "promo_block_max_ratio must be in [0.0, 1.0]"
            )
        if self.ks_max_fires_per_day < 0.0:
            raise ValueError("ks_max_fires_per_day must be >= 0")
        if self.approval_days < 1:
            raise ValueError("approval_days must be >= 1")


@dataclass(frozen=True)
class SliceEvidence:
    """All inputs the pure model needs for one slice evaluation."""

    slice_name: str
    current_mode: str
    days_in_stage: int
    total_decisions: int
    last_advance_date: Optional[date] = None
    today_utc: date = field(default_factory=date.today)

    diagnostics_healthy: bool = True
    diagnostics_stale_hours: float = 0.0
    release_blocker_clean: bool = True
    scan_status_frozen_ok: bool = True

    anomaly_refused_authoritative: bool = False
    authoritative_approval_missing: bool = False
    approval_live: bool = False

    # Slice-specific envelope metrics (only relevant for the owning slice).
    tilt_mean_multiplier: Optional[float] = None
    promotion_block_ratio: Optional[float] = None
    killswitch_mean_fires_per_day: Optional[float] = None

    def __post_init__(self) -> None:
        if self.slice_name not in SLICE_NAMES:
            raise ValueError(f"unknown slice_name: {self.slice_name!r}")
        if self.current_mode not in ALLOWED_STAGES:
            raise ValueError(
                f"current_mode must be one of {ALLOWED_STAGES!r}; "
                f"got {self.current_mode!r}"
            )
        if self.days_in_stage < 0:
            raise ValueError("days_in_stage must be >= 0")
        if self.total_decisions < 0:
            raise ValueError("total_decisions must be >= 0")


@dataclass(frozen=True)
class OrderLockState:
    """Cross-slice stage awareness for the order lock.

    ``tilt_mode`` must equal ``authoritative`` before the killswitch
    slice is allowed to advance beyond shadow. ``killswitch_mode``
    must equal ``authoritative`` before the promotion slice is allowed
    to advance beyond shadow. Modes are the POST-revert, PRE-advance
    snapshot so the order lock never "sees its own advance" within
    a single evaluation tick.
    """

    tilt_mode: str
    killswitch_mode: str
    promotion_mode: str

    def __post_init__(self) -> None:
        for name, mode in (
            ("tilt_mode", self.tilt_mode),
            ("killswitch_mode", self.killswitch_mode),
            ("promotion_mode", self.promotion_mode),
        ):
            if mode not in ALLOWED_STAGES:
                raise ValueError(f"{name} must be a valid stage; got {mode!r}")

    def can_advance_beyond_shadow(self, slice_name: str) -> bool:
        if slice_name == SLICE_TILT:
            return True
        if slice_name == SLICE_KILLSWITCH:
            return self.tilt_mode == "authoritative"
        if slice_name == SLICE_PROMOTION:
            return self.killswitch_mode == "authoritative"
        return False


@dataclass(frozen=True)
class GateEvaluation:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class AutopilotDecision:
    """Single-tick decision for a slice."""

    slice_name: str
    action: str  # "advance" | "hold" | "revert" | "blocked_by_order_lock" | "skipped"
    from_mode: str
    to_mode: str
    reason_code: str
    gates: tuple[GateEvaluation, ...]
    requires_approval_insert: bool = False

    def changed(self) -> bool:
        return self.action in ("advance", "revert") and self.from_mode != self.to_mode


def _next_stage(current: str) -> Optional[str]:
    """Linear progression off -> shadow -> compare -> authoritative."""
    idx = ALLOWED_STAGES.index(current)
    if idx + 1 >= len(ALLOWED_STAGES):
        return None
    return ALLOWED_STAGES[idx + 1]


def _prev_stage(current: str) -> Optional[str]:
    """Reverse of :func:`_next_stage`. Shadow never downshifts to off
    via autopilot (master kill is the only path to off)."""
    idx = ALLOWED_STAGES.index(current)
    if idx - 1 <= 0:
        # current is off or shadow; autopilot does not go to off.
        return None
    return ALLOWED_STAGES[idx - 1]


def _envelope_gate(
    slice_name: str,
    evidence: SliceEvidence,
    config: AutopilotConfig,
) -> GateEvaluation:
    """Slice-specific safety envelope for the compare->authoritative step.

    Returns a GateEvaluation. For slices that are not evaluating that
    specific transition, callers should ignore the return value.
    """
    if slice_name == SLICE_TILT:
        m = evidence.tilt_mean_multiplier
        if m is None or math.isnan(m):
            return GateEvaluation(
                name="envelope_tilt_multiplier",
                ok=False,
                detail="no mean multiplier observed",
            )
        ok = (config.tilt_mult_min <= m <= config.tilt_mult_max)
        return GateEvaluation(
            name="envelope_tilt_multiplier",
            ok=ok,
            detail=f"mean={m:.4f} bounds=[{config.tilt_mult_min:.2f},{config.tilt_mult_max:.2f}]",
        )
    if slice_name == SLICE_PROMOTION:
        r = evidence.promotion_block_ratio
        if r is None or math.isnan(r):
            return GateEvaluation(
                name="envelope_promotion_block_ratio",
                ok=False,
                detail="no block ratio observed",
            )
        ok = r <= config.promo_block_max_ratio
        return GateEvaluation(
            name="envelope_promotion_block_ratio",
            ok=ok,
            detail=f"ratio={r:.4f} max={config.promo_block_max_ratio:.4f}",
        )
    if slice_name == SLICE_KILLSWITCH:
        f = evidence.killswitch_mean_fires_per_day
        if f is None or math.isnan(f):
            return GateEvaluation(
                name="envelope_killswitch_mean_fires",
                ok=False,
                detail="no fire rate observed",
            )
        ok = f <= config.ks_max_fires_per_day
        return GateEvaluation(
            name="envelope_killswitch_mean_fires",
            ok=ok,
            detail=f"mean={f:.4f}/day max={config.ks_max_fires_per_day:.4f}",
        )
    return GateEvaluation(
        name="envelope_unknown", ok=False, detail="unknown slice"
    )


def _consider_revert(
    evidence: SliceEvidence,
) -> Optional[tuple[str, str]]:
    """Return (to_mode, reason_code) for a one-step revert, or None.

    Called BEFORE advance logic on every tick.
    """
    current = evidence.current_mode

    # Belt-and-suspenders: authoritative with no approval
    if current == "authoritative" and not evidence.approval_live:
        target = _prev_stage(current)
        if target is not None:
            return (target, "authoritative_approval_missing")

    # Authoritative that saw a refused line in the last window
    if current == "authoritative" and evidence.anomaly_refused_authoritative:
        target = _prev_stage(current)
        if target is not None:
            return (target, "anomaly_refused_authoritative")

    # Release blocker failed -> step back one stage (any non-shadow)
    if not evidence.release_blocker_clean and current in ("compare", "authoritative"):
        target = _prev_stage(current)
        if target is not None:
            return (target, "release_blocker_failed")

    # Diagnostics stale > threshold -> step back one stage (any non-shadow)
    if current in ("compare", "authoritative") and not evidence.diagnostics_healthy:
        target = _prev_stage(current)
        if target is not None:
            return (target, "diagnostics_unhealthy")

    return None


def evaluate_slice_gates(
    evidence: SliceEvidence,
    config: AutopilotConfig,
    order_lock: OrderLockState,
) -> AutopilotDecision:
    """Evaluate one slice on one tick and return a single decision.

    Decision priority (first-match wins):

    1. Revert (anomaly / approval missing / unhealthy).
    2. Rate limit (already advanced today -> hold).
    3. Current mode == ``off`` -> hold (autopilot does not wake a
       slice from off; that's a manual operation).
    4. Order lock -> ``blocked_by_order_lock`` if attempting to move
       beyond shadow without the prior slice authoritative.
    5. Gates -> advance if all green; else hold.
    """
    current = evidence.current_mode

    # 1. Revert?
    revert = _consider_revert(evidence)
    if revert is not None:
        to_mode, reason = revert
        gates: list[GateEvaluation] = [
            GateEvaluation(name="revert_trigger", ok=False, detail=reason),
        ]
        return AutopilotDecision(
            slice_name=evidence.slice_name,
            action="revert",
            from_mode=current,
            to_mode=to_mode,
            reason_code=reason,
            gates=tuple(gates),
            requires_approval_insert=False,
        )

    # 2. Rate limit: one advance per UTC day.
    if (
        evidence.last_advance_date is not None
        and evidence.last_advance_date == evidence.today_utc
    ):
        return AutopilotDecision(
            slice_name=evidence.slice_name,
            action="hold",
            from_mode=current,
            to_mode=current,
            reason_code="rate_limit_same_day",
            gates=(
                GateEvaluation(
                    name="rate_limit",
                    ok=False,
                    detail=f"last_advance_date={evidence.last_advance_date.isoformat()} today={evidence.today_utc.isoformat()}",
                ),
            ),
        )

    # 3. Autopilot does not wake slices from off.
    if current == "off":
        return AutopilotDecision(
            slice_name=evidence.slice_name,
            action="hold",
            from_mode=current,
            to_mode=current,
            reason_code="off_stays_off",
            gates=(
                GateEvaluation(
                    name="current_off",
                    ok=False,
                    detail="autopilot does not wake off-mode slices",
                ),
            ),
        )

    # 4. Nothing to advance past authoritative.
    if current == "authoritative":
        return AutopilotDecision(
            slice_name=evidence.slice_name,
            action="hold",
            from_mode=current,
            to_mode=current,
            reason_code="terminal_authoritative",
            gates=(
                GateEvaluation(
                    name="terminal",
                    ok=True,
                    detail="slice already authoritative",
                ),
            ),
        )

    # 5. Order lock. Advancing FROM shadow means "to compare" — this
    #    is the first move beyond shadow. If the prior slice is not
    #    authoritative yet, block.
    target = _next_stage(current)
    if target is None:
        return AutopilotDecision(
            slice_name=evidence.slice_name,
            action="hold",
            from_mode=current,
            to_mode=current,
            reason_code="no_next_stage",
            gates=(GateEvaluation(name="no_next_stage", ok=True, detail=""),),
        )

    if current == "shadow" and not order_lock.can_advance_beyond_shadow(evidence.slice_name):
        return AutopilotDecision(
            slice_name=evidence.slice_name,
            action="blocked_by_order_lock",
            from_mode=current,
            to_mode=current,
            reason_code="order_lock_prior_slice_not_authoritative",
            gates=(
                GateEvaluation(
                    name="order_lock",
                    ok=False,
                    detail=(
                        f"prior slice not authoritative "
                        f"(tilt={order_lock.tilt_mode} "
                        f"killswitch={order_lock.killswitch_mode})"
                    ),
                ),
            ),
        )

    # 6. Common gates (shadow->compare AND compare->authoritative).
    required_days = (
        config.shadow_days if current == "shadow" else config.compare_days
    )

    gates: list[GateEvaluation] = []
    gates.append(
        GateEvaluation(
            name="days_in_stage",
            ok=evidence.days_in_stage >= required_days,
            detail=f"observed={evidence.days_in_stage} required={required_days}",
        )
    )
    gates.append(
        GateEvaluation(
            name="total_decisions",
            ok=evidence.total_decisions >= config.min_decisions,
            detail=f"observed={evidence.total_decisions} required={config.min_decisions}",
        )
    )
    gates.append(
        GateEvaluation(
            name="diagnostics_healthy",
            ok=evidence.diagnostics_healthy,
            detail=f"stale_hours={evidence.diagnostics_stale_hours:.2f}",
        )
    )
    gates.append(
        GateEvaluation(
            name="release_blocker_clean",
            ok=evidence.release_blocker_clean,
            detail="",
        )
    )
    gates.append(
        GateEvaluation(
            name="scan_status_frozen",
            ok=evidence.scan_status_frozen_ok,
            detail="",
        )
    )

    # 7. For compare->authoritative also require envelope + approval.
    requires_approval_insert = False
    if current == "compare":
        gates.append(_envelope_gate(evidence.slice_name, evidence, config))
        # Approval contract: we are about to auto-insert a governance
        # approval row as part of the advance, so we do NOT require
        # one to already be live. But if one is live already, that's
        # fine. This flag indicates the insert step is needed.
        requires_approval_insert = True

    all_ok = all(g.ok for g in gates)
    if not all_ok:
        failed_names = [g.name for g in gates if not g.ok]
        return AutopilotDecision(
            slice_name=evidence.slice_name,
            action="hold",
            from_mode=current,
            to_mode=current,
            reason_code=f"gates_not_ready:{','.join(failed_names)}",
            gates=tuple(gates),
        )

    return AutopilotDecision(
        slice_name=evidence.slice_name,
        action="advance",
        from_mode=current,
        to_mode=target,
        reason_code=f"advance_to_{target}",
        gates=tuple(gates),
        requires_approval_insert=requires_approval_insert,
    )


def compute_order_lock_state(
    *,
    tilt_mode: str,
    killswitch_mode: str,
    promotion_mode: str,
) -> OrderLockState:
    return OrderLockState(
        tilt_mode=tilt_mode,
        killswitch_mode=killswitch_mode,
        promotion_mode=promotion_mode,
    )


def business_days_between(start: date, end: date) -> int:
    """Inclusive-count of business days (Mon-Fri) between ``start`` and
    ``end``. Returns 0 if ``end <= start``. Used as a helper for
    ``days_in_stage`` computation by the service."""
    if end <= start:
        return 0
    days = 0
    cursor = start
    step = timedelta(days=1)
    while cursor < end:
        cursor += step
        if cursor.weekday() < 5:
            days += 1
    return days


__all__ = [
    "ALLOWED_STAGES",
    "SLICE_TILT",
    "SLICE_PROMOTION",
    "SLICE_KILLSWITCH",
    "SLICE_NAMES",
    "AutopilotConfig",
    "SliceEvidence",
    "OrderLockState",
    "GateEvaluation",
    "AutopilotDecision",
    "compute_order_lock_state",
    "evaluate_slice_gates",
    "business_days_between",
]
