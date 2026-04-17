"""Phase I: pure capital re-weight model (no DB, no I/O).

Computes the proposed per-bucket capital allocation for a single user
at a point in time. The model is intentionally simple - it ships
**inverse-volatility weights** as the default allocation strategy,
optionally tilted by the current risk dial, and enforces a single-
bucket hard cap. A covariance-matrix provider is accepted as an
optional parameter so Phase I.2 can plug in a full Markowitz / ERC
allocator without rewriting callers.

Like :mod:`risk_dial_model`, this module is **pure** - it does not
read a database, touch a broker, or log. It is called from
``capital_reweight_service`` which supplies open-book and active-
pattern context from the database.

Phase I persists the model's proposal to
``trading_capital_reweight_log`` in shadow mode only; no open position
is resized. Phase I.2 will promote the proposal to authoritative and
generate rebalance orders.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

BucketName = str
CovMatrixProvider = Callable[[Iterable[str]], dict[tuple[str, str], float]]


@dataclass(frozen=True)
class CapitalReweightConfig:
    max_single_bucket_pct: float = 35.0  # hard cap per bucket, % of capital
    min_weight_pct: float = 0.0  # below this we clip to 0 instead of tiny weights
    regime_tilt_enabled: bool = True  # scale target weights by dial_value


@dataclass(frozen=True)
class BucketContext:
    """A single correlation / asset-class bucket context.

    ``volatility`` is the bucket's realized volatility proxy (ATR ratio
    or std-of-returns); a higher value reduces the bucket's weight in
    the inverse-vol schema.
    """

    name: BucketName
    current_notional: float = 0.0
    volatility: float = 1.0  # sensible default so zero-open-book works


@dataclass(frozen=True)
class CapitalReweightInput:
    user_id: int | None
    as_of_date: str  # YYYY-MM-DD (passed as string for determinism)
    total_capital: float
    regime: str | None
    dial_value: float
    buckets: tuple[BucketContext, ...]


@dataclass(frozen=True)
class BucketAllocation:
    bucket: BucketName
    current_notional: float
    current_weight_pct: float
    target_notional: float
    target_weight_pct: float
    drift_bps: float
    cap_triggered: bool
    rationale: str


@dataclass(frozen=True)
class CapitalReweightOutput:
    reweight_id: str
    user_id: int | None
    as_of_date: str
    total_capital: float
    regime: str | None
    dial_value: float
    allocations: tuple[BucketAllocation, ...]
    mean_drift_bps: float
    p90_drift_bps: float
    cap_triggers: dict = field(default_factory=dict)
    reasoning: dict = field(default_factory=dict)


def _abs_drift_bps(target: float, current: float) -> float:
    denom = max(abs(target), 1e-6)
    return float(abs(target - current) / denom * 1e4)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = max(0, min(len(xs) - 1, int(round((pct / 100.0) * (len(xs) - 1)))))
    return float(xs[k])


def compute_reweight(
    input: CapitalReweightInput,
    *,
    config: CapitalReweightConfig,
    cov_matrix_provider: CovMatrixProvider | None = None,
) -> CapitalReweightOutput:
    """Compute proposed per-bucket allocations deterministically.

    Algorithm (Phase I default; Phase I.2 will override via
    ``cov_matrix_provider``):

    1. Base weight for each bucket = ``1 / max(vol, epsilon)``.
    2. Normalize base weights to sum to 1.
    3. Scale target notional = ``weight * total_capital * dial_value``
       (if ``regime_tilt_enabled``) else ``weight * total_capital``.
    4. Enforce single-bucket cap
       (``max_single_bucket_pct * total_capital / 100``).
    5. If a cap fires, redistribute the excess proportionally across
       the uncapped buckets; if every bucket caps, hold at the cap.
    6. Compute per-bucket drift in bps against current notional.
    """
    total_capital = max(0.0, float(input.total_capital))
    dial = max(0.0, float(input.dial_value))

    if cov_matrix_provider is not None:
        try:
            _ = cov_matrix_provider([b.name for b in input.buckets])
        except Exception:
            pass

    raw_weights: dict[BucketName, float] = {}
    for b in input.buckets:
        vol = max(1e-6, float(b.volatility))
        raw_weights[b.name] = 1.0 / vol

    total_raw = sum(raw_weights.values())
    if total_raw <= 0 or not input.buckets:
        out_allocs: list[BucketAllocation] = []
        for b in input.buckets:
            drift = _abs_drift_bps(0.0, b.current_notional)
            out_allocs.append(BucketAllocation(
                bucket=b.name,
                current_notional=float(b.current_notional),
                current_weight_pct=0.0,
                target_notional=0.0,
                target_weight_pct=0.0,
                drift_bps=drift,
                cap_triggered=False,
                rationale="empty_or_degenerate_input",
            ))
        drifts = [a.drift_bps for a in out_allocs]
        return CapitalReweightOutput(
            reweight_id=_compute_reweight_id(input),
            user_id=input.user_id,
            as_of_date=input.as_of_date,
            total_capital=total_capital,
            regime=input.regime,
            dial_value=dial,
            allocations=tuple(out_allocs),
            mean_drift_bps=(sum(drifts) / len(drifts)) if drifts else 0.0,
            p90_drift_bps=_percentile(drifts, 90.0),
            cap_triggers={
                "single_bucket": 0,
                "concentration": 0,
            },
            reasoning={"algorithm": "inverse_vol_default", "degenerate": True},
        )

    normalized: dict[BucketName, float] = {
        k: v / total_raw for k, v in raw_weights.items()
    }

    scale = dial if config.regime_tilt_enabled else 1.0
    scale = max(0.0, min(1.0, scale))
    tilted_total = total_capital * scale

    target_notional: dict[BucketName, float] = {
        k: w * tilted_total for k, w in normalized.items()
    }

    single_bucket_cap = (
        config.max_single_bucket_pct / 100.0 * total_capital
    )
    cap_fired: dict[BucketName, bool] = {k: False for k in target_notional}
    if single_bucket_cap > 0:
        over = 0.0
        uncapped: list[BucketName] = []
        for k, v in target_notional.items():
            if v > single_bucket_cap:
                cap_fired[k] = True
                over += v - single_bucket_cap
                target_notional[k] = single_bucket_cap
            else:
                uncapped.append(k)

        while over > 1e-9 and uncapped:
            bucket_cap_slack_total = sum(
                single_bucket_cap - target_notional[k] for k in uncapped
            )
            if bucket_cap_slack_total <= 0:
                break
            distributed = 0.0
            still_uncapped: list[BucketName] = []
            for k in uncapped:
                slack = single_bucket_cap - target_notional[k]
                share = over * (slack / bucket_cap_slack_total)
                if target_notional[k] + share >= single_bucket_cap - 1e-9:
                    distributed += single_bucket_cap - target_notional[k]
                    target_notional[k] = single_bucket_cap
                    cap_fired[k] = True
                else:
                    target_notional[k] += share
                    distributed += share
                    still_uncapped.append(k)
            over = max(0.0, over - distributed)
            uncapped = still_uncapped
            if not still_uncapped:
                break

    allocs: list[BucketAllocation] = []
    total_current = sum(max(0.0, b.current_notional) for b in input.buckets)
    for b in input.buckets:
        tgt = target_notional.get(b.name, 0.0)
        if total_capital > 0 and tgt / total_capital * 100.0 < config.min_weight_pct:
            tgt = 0.0
        cur = float(b.current_notional)
        current_pct = (cur / total_current * 100.0) if total_current > 0 else 0.0
        target_pct = (tgt / total_capital * 100.0) if total_capital > 0 else 0.0
        drift = _abs_drift_bps(tgt, cur)
        rationale = _rationale_for(
            cap_fired.get(b.name, False),
            scale,
            tilted_total,
            total_capital,
        )
        allocs.append(BucketAllocation(
            bucket=b.name,
            current_notional=cur,
            current_weight_pct=current_pct,
            target_notional=tgt,
            target_weight_pct=target_pct,
            drift_bps=drift,
            cap_triggered=cap_fired.get(b.name, False),
            rationale=rationale,
        ))

    drifts = [a.drift_bps for a in allocs if a.drift_bps > 0]
    mean = (sum(drifts) / len(drifts)) if drifts else 0.0
    p90 = _percentile(drifts, 90.0)

    single_cap_count = sum(1 for v in cap_fired.values() if v)
    concentration_triggered = 0
    if total_capital > 0:
        max_current_pct = max(
            (a.current_weight_pct for a in allocs),
            default=0.0,
        )
        if max_current_pct > config.max_single_bucket_pct:
            concentration_triggered = 1

    return CapitalReweightOutput(
        reweight_id=_compute_reweight_id(input),
        user_id=input.user_id,
        as_of_date=input.as_of_date,
        total_capital=total_capital,
        regime=input.regime,
        dial_value=dial,
        allocations=tuple(allocs),
        mean_drift_bps=float(mean),
        p90_drift_bps=float(p90),
        cap_triggers={
            "single_bucket": int(single_cap_count),
            "concentration": int(concentration_triggered),
        },
        reasoning={
            "algorithm": "inverse_vol_default",
            "regime_tilt_enabled": bool(config.regime_tilt_enabled),
            "scale_factor": scale,
            "tilted_total": tilted_total,
            "total_capital": total_capital,
            "single_bucket_cap": single_bucket_cap,
        },
    )


def _rationale_for(
    cap_triggered: bool,
    scale: float,
    tilted_total: float,
    total_capital: float,
) -> str:
    if cap_triggered:
        return "single_bucket_cap"
    if scale < 0.999 and total_capital > 0:
        return "dial_tilt_applied"
    if tilted_total <= 0:
        return "no_capital_to_deploy"
    return "inverse_vol_default"


def _compute_reweight_id(input: CapitalReweightInput) -> str:
    """Deterministic idempotency key per ``(user_id, as_of_date)``.

    Used by callers to skip re-writing a sweep that already exists for
    the same day.
    """
    parts = [
        str(int(input.user_id)) if input.user_id is not None else "global",
        str(input.as_of_date),
    ]
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return h[:32]


__all__ = [
    "BucketAllocation",
    "BucketContext",
    "CapitalReweightConfig",
    "CapitalReweightInput",
    "CapitalReweightOutput",
    "CovMatrixProvider",
    "compute_reweight",
]
