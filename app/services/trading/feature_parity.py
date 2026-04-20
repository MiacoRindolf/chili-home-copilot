"""P1.4 — runtime feature-parity assertion at entry.

Verifies at live entry time that the feature vector used by the decision
matches what :func:`indicator_core.compute_all_from_df` produces on the same
OHLCV frame. Catches regressions where the live path diverges from the
canonical backtest compute surface (missing features, rounding drift, stale
caches, type coercion differences).

Design
======
* The caller supplies two things:
    ``live_snap``
        The per-feature dict the decision actually used. In the AutoTrader v1
        path this is the last-row snap from ``_last_indicator_row`` (or an
        augmented ``passes_rule_gate`` snap); in the momentum_neural path the
        live runner already builds ``_last_indicator_row(df, needed)``.
    ``reference_df``
        The OHLCV frame the decision was computed against. We rerun
        ``compute_all_from_df`` on it and extract the last-row snap as the
        canonical reference.
* The diff is run over the intersection of keys in ``live_snap`` ∩ the
  requested feature set (``DEFAULT_FEATURES`` by default).
* Severity is one of ``"ok" | "warn" | "critical"``:
    * ``warn``    — at least one numeric mismatch > epsilon, or missing-key
                    asymmetry, but fewer than
                    ``chili_feature_parity_critical_mismatch_count`` total
                    issues AND no boolean mismatch.
    * ``critical`` — any boolean mismatch (semantic-contract violation)
                    OR total issue count ≥ the threshold.
* Persistence: every non-OK result is written to the
  ``TradingExecutionEvent`` stream with ``event_type='feature_parity_drift'``
  (reuses the P1.2 rate-limit pattern — no new table needed). Callers can
  audit drift history with a standard rolling-window query keyed on
  ``(ticker, recorded_at)``.
* Feature-flag: ``chili_feature_parity_enabled`` (default **False**) is a
  hard bypass. Ships dark until an operator opts in.
* Mode: ``chili_feature_parity_mode`` — ``"soft"`` (default) records + alerts
  without blocking entry; ``"hard"`` blocks when severity == critical.
  The 2-week shakedown runs in soft mode.
* Fail-open everywhere: any compute / DB / alert exception returns
  ``ok=True`` with a ``reason`` so one bad OHLCV row can never halt trading.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd
from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import TradingExecutionEvent
from .indicator_core import compute_all_from_df

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────
# Feature catalog
# ──────────────────────────────────────────────────────────────────────────
# Features we know are numeric / PIT-safe and universally computed by
# ``compute_all_from_df`` when called with ``needed=None``. Lazy features
# (Fibonacci, FVG) are excluded by default — they're expensive and
# conditionally loaded; callers can opt in via the ``features`` parameter.
DEFAULT_NUMERIC_FEATURES: frozenset[str] = frozenset(
    {
        "price",
        "rsi_14",
        "ema_9",
        "ema_12",
        "ema_20",
        "ema_21",
        "ema_26",
        "ema_50",
        "ema_100",
        "ema_200",
        "sma_10",
        "sma_20",
        "sma_50",
        "sma_100",
        "sma_200",
        "adx",
        "macd",
        "macd_signal",
        "macd_hist",
        "bb_upper",
        "bb_lower",
        "bb_mid",
        "bb_pct",
        "bb_width",
        "atr",
        "stoch_k",
        "stoch_d",
        "rel_vol",
        "gap_pct",
        "daily_change_pct",
        "resistance",
        "dist_to_resistance_pct",
    }
)

DEFAULT_BOOLEAN_FEATURES: frozenset[str] = frozenset(
    {
        "ema_stack",
        "stoch_bull_div",
        "stoch_bear_div",
        "bb_squeeze",
    }
)

DEFAULT_FEATURES: frozenset[str] = DEFAULT_NUMERIC_FEATURES | DEFAULT_BOOLEAN_FEATURES

# ──────────────────────────────────────────────────────────────────────────
# Mode / severity vocabulary
# ──────────────────────────────────────────────────────────────────────────
MODE_DISABLED = "disabled"
MODE_SOFT = "soft"
MODE_HARD = "hard"
_VALID_MODES = frozenset({MODE_DISABLED, MODE_SOFT, MODE_HARD})

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

EVENT_TYPE = "feature_parity_drift"


# ──────────────────────────────────────────────────────────────────────────
# Result shapes
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FeatureDelta:
    """One per-feature disagreement between live and reference vectors."""

    name: str
    live_value: Any
    reference_value: Any
    abs_delta: float | None  # None for booleans / missing-side comparisons
    rel_delta: float | None
    kind: str  # "numeric" | "bool" | "missing_live" | "missing_reference"
    severity: str  # "warn" | "critical"


@dataclass(frozen=True)
class ParityResult:
    """Returned by :func:`check_entry_feature_parity`.

    Attributes
    ----------
    ok
        ``False`` only in ``hard`` mode when severity == ``critical``. Soft
        mode always returns ``ok=True``. Disabled always returns ``ok=True``.
    severity
        ``"ok" | "warn" | "critical"``.
    mode
        ``"disabled" | "soft" | "hard"`` — effective mode for this call.
    reason
        Short tag when the gate would block or when the run was skipped
        (``"no_reference_df"``, ``"ref_compute_failed"``, etc). ``None`` on
        clean OK.
    deltas
        Tuple of every ``FeatureDelta`` found. Empty on OK.
    n_features_checked
        Size of the feature set requested.
    n_mismatches
        Length of ``deltas``.
    record_id
        Primary key of the persisted ``TradingExecutionEvent`` row when
        ``deltas`` is non-empty and a ``db`` was supplied. ``None`` otherwise.
    """

    ok: bool
    severity: str
    mode: str
    reason: str | None
    deltas: tuple[FeatureDelta, ...]
    n_features_checked: int
    n_mismatches: int
    record_id: int | None


# ──────────────────────────────────────────────────────────────────────────
# Settings resolution (re-read every call so monkeypatch / env changes land)
# ──────────────────────────────────────────────────────────────────────────
def _resolve_settings() -> dict[str, Any]:
    """Read the 6 P1.4 knobs from settings live.

    Same pattern as ``rate_limiter._settings_snapshot`` / ``venue_health`` /
    ``order_state_machine._is_enabled`` so monkeypatching in tests works
    without reimporting the module.
    """
    raw_mode = getattr(settings, "chili_feature_parity_mode", MODE_SOFT)
    mode = str(raw_mode or MODE_SOFT).strip().lower()
    if mode not in _VALID_MODES:
        mode = MODE_SOFT
    return {
        "enabled": bool(getattr(settings, "chili_feature_parity_enabled", False)),
        "mode": mode,
        "epsilon_abs": float(getattr(settings, "chili_feature_parity_epsilon_abs", 1e-6)),
        "epsilon_rel": float(getattr(settings, "chili_feature_parity_epsilon_rel", 0.005)),
        "critical_mismatch_count": int(
            getattr(settings, "chili_feature_parity_critical_mismatch_count", 3)
        ),
        "alert_on_warn": bool(getattr(settings, "chili_feature_parity_alert_on_warn", True)),
    }


# ──────────────────────────────────────────────────────────────────────────
# Core helpers
# ──────────────────────────────────────────────────────────────────────────
def extract_last_row_snapshot(
    arrays: dict[str, list],
    *,
    needed: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Pull the last value from each feature array.

    Missing / empty arrays are skipped. ``None`` last-row values (which
    :func:`indicator_core.compute_all_from_df` uses for insufficient-data
    slots) are skipped as well — we treat "not computable yet" identically
    on both sides so warmup bars never fire drift alerts.
    """
    keys = set(needed) if needed is not None else set(arrays.keys())
    snap: dict[str, Any] = {}
    for key in sorted(keys):
        vec = arrays.get(key)
        if not isinstance(vec, list) or not vec:
            continue
        v = vec[-1]
        if v is None:
            continue
        snap[key] = v
    return snap


def _is_bool(x: Any) -> bool:
    """Strict bool check — ``isinstance(True, int)`` is True in Python so
    we need the explicit type check to keep 0/1 ints in the numeric path."""
    return type(x) is bool  # noqa: E721


def diff_feature_vectors(
    live: dict[str, Any],
    reference: dict[str, Any],
    *,
    epsilon_abs: float,
    epsilon_rel: float,
    critical_mismatch_count: int = 3,
    features: Iterable[str] | None = None,
) -> tuple[tuple[FeatureDelta, ...], str]:
    """Compare two per-feature snapshots. Returns ``(deltas, overall_severity)``.

    Tolerance: a numeric difference is tolerated when *either* ``abs_d <=
    epsilon_abs`` OR ``rel_d <= epsilon_rel``. Using OR (not AND) means
    small-magnitude values pass on absolute tolerance while large-magnitude
    values pass on relative tolerance — exactly what you want when comparing
    e.g. ``bb_pct`` (0..1) alongside ``price`` (tens to thousands).

    Booleans must match exactly. A boolean mismatch is always critical
    because pattern-engine conditions like ``ema_stack`` feed rule gates and
    a silent flip changes the decision.

    Missing-key asymmetry (key present in one vector but not the other) is
    ``warn`` — schema drift worth flagging but not blocking.
    """
    if features is not None:
        keys: set[str] = set(features)
    else:
        keys = set(live.keys()) | set(reference.keys())

    deltas: list[FeatureDelta] = []
    n_bool_bad = 0

    for key in sorted(keys):
        lv = live.get(key)
        rv = reference.get(key)

        if lv is None and rv is None:
            # Both sides agree on "unavailable" — not a mismatch.
            continue

        if lv is None:
            deltas.append(
                FeatureDelta(
                    name=key,
                    live_value=None,
                    reference_value=rv,
                    abs_delta=None,
                    rel_delta=None,
                    kind="missing_live",
                    severity=SEVERITY_WARN,
                )
            )
            continue

        if rv is None:
            deltas.append(
                FeatureDelta(
                    name=key,
                    live_value=lv,
                    reference_value=None,
                    abs_delta=None,
                    rel_delta=None,
                    kind="missing_reference",
                    severity=SEVERITY_WARN,
                )
            )
            continue

        # Boolean comparison: strict equality.
        if _is_bool(lv) or _is_bool(rv):
            if bool(lv) != bool(rv):
                deltas.append(
                    FeatureDelta(
                        name=key,
                        live_value=lv,
                        reference_value=rv,
                        abs_delta=None,
                        rel_delta=None,
                        kind="bool",
                        severity=SEVERITY_CRITICAL,
                    )
                )
                n_bool_bad += 1
            continue

        # Numeric comparison.
        try:
            fl = float(lv)
            fr = float(rv)
        except (TypeError, ValueError):
            deltas.append(
                FeatureDelta(
                    name=key,
                    live_value=lv,
                    reference_value=rv,
                    abs_delta=None,
                    rel_delta=None,
                    kind="numeric",
                    severity=SEVERITY_WARN,
                )
            )
            continue

        abs_d = abs(fl - fr)
        if abs_d <= float(epsilon_abs):
            continue
        rel_d = (abs_d / abs(fr)) if fr != 0 else None
        if rel_d is not None and rel_d <= float(epsilon_rel):
            continue

        deltas.append(
            FeatureDelta(
                name=key,
                live_value=fl,
                reference_value=fr,
                abs_delta=abs_d,
                rel_delta=rel_d,
                kind="numeric",
                severity=SEVERITY_WARN,
            )
        )

    total_issues = len(deltas)
    if n_bool_bad > 0:
        return tuple(deltas), SEVERITY_CRITICAL
    if total_issues >= int(critical_mismatch_count):
        return tuple(deltas), SEVERITY_CRITICAL
    if total_issues > 0:
        return tuple(deltas), SEVERITY_WARN
    return (), SEVERITY_OK


# ──────────────────────────────────────────────────────────────────────────
# Persistence + alerting
# ──────────────────────────────────────────────────────────────────────────
def _json_safe(v: Any) -> Any:
    """Coerce a feature value to something the JSONB column will accept."""
    if v is None:
        return None
    if _is_bool(v):
        return bool(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        # Drop NaN/Inf — JSON can't represent them.
        return v if (v == v and abs(v) != float("inf")) else None
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v)


def _persist_drift_event(
    db: Session,
    *,
    ticker: str,
    severity: str,
    deltas: Iterable[FeatureDelta],
    source: str,
    scan_pattern_id: int | None,
    venue: str | None,
    n_features_checked: int,
) -> int | None:
    """Insert one ``TradingExecutionEvent`` with ``event_type='feature_parity_drift'``.

    Payload contains the per-feature deltas in a stable shape so downstream
    watchdogs can aggregate across calls by name / kind / severity.
    """
    payload = {
        "severity": severity,
        "source": source,
        "n_features_checked": int(n_features_checked),
        "deltas": [
            {
                "name": d.name,
                "kind": d.kind,
                "severity": d.severity,
                "live_value": _json_safe(d.live_value),
                "reference_value": _json_safe(d.reference_value),
                "abs_delta": _json_safe(d.abs_delta),
                "rel_delta": _json_safe(d.rel_delta),
            }
            for d in deltas
        ],
    }
    row = TradingExecutionEvent(
        ticker=((ticker or "").upper() or None),
        venue=((venue or "").lower() or None),
        event_type=EVENT_TYPE,
        status=severity,
        scan_pattern_id=scan_pattern_id,
        payload_json=payload,
    )
    db.add(row)
    db.flush()
    return row.id


def _dispatch_alert(
    *,
    ticker: str,
    severity: str,
    source: str,
    n_mismatches: int,
    scan_pattern_id: int | None,
) -> None:
    """Best-effort alert dispatch. Never raises."""
    try:
        from .alerts import dispatch_alert  # local import to avoid cycles
    except Exception:
        return
    msg = (
        f"Feature-parity drift [{severity}] on {ticker} ({source}): "
        f"{n_mismatches} feature mismatch{'es' if n_mismatches != 1 else ''}."
    )
    try:
        dispatch_alert(
            alert_type=EVENT_TYPE,
            ticker=ticker,
            message=msg,
            scan_pattern_id=scan_pattern_id,
        )
    except Exception:
        logger.exception("[feature_parity] dispatch_alert failed (continuing)")


# ──────────────────────────────────────────────────────────────────────────
# Main gate entrypoint
# ──────────────────────────────────────────────────────────────────────────
def check_entry_feature_parity(
    db: Session | None,
    *,
    ticker: str,
    live_snap: dict[str, Any],
    reference_df: pd.DataFrame | None,
    features: Iterable[str] | None = None,
    source: str = "unknown",
    scan_pattern_id: int | None = None,
    venue: str | None = None,
) -> ParityResult:
    """Runtime parity check for the entry decision.

    Parameters
    ----------
    db
        SQLAlchemy session for persistence. ``None`` skips the drift-event
        write (useful for unit tests that want to call in isolation).
    ticker
        Canonical ticker for audit routing and alert dedup.
    live_snap
        The per-feature dict the decision actually used at entry time.
    reference_df
        OHLCV DataFrame that the decision was computed against. The reference
        vector is a fresh ``compute_all_from_df(reference_df, needed=features)``
        last-row extract.
    features
        Feature allowlist. When ``None`` uses :data:`DEFAULT_FEATURES`.
    source
        Audit tag; typically ``"auto_trader_v1"`` / ``"momentum_neural"``.
    scan_pattern_id, venue
        Optional routing metadata persisted on the drift event row.

    Returns
    -------
    :class:`ParityResult`
        ``ok=False`` only when mode is ``"hard"`` AND severity is
        ``"critical"``. All other cases return ``ok=True``.
    """
    cfg = _resolve_settings()
    if not cfg["enabled"]:
        return ParityResult(
            ok=True,
            severity=SEVERITY_OK,
            mode=MODE_DISABLED,
            reason=None,
            deltas=(),
            n_features_checked=0,
            n_mismatches=0,
            record_id=None,
        )

    if reference_df is None or getattr(reference_df, "empty", True):
        return ParityResult(
            ok=True,
            severity=SEVERITY_OK,
            mode=cfg["mode"],
            reason="no_reference_df",
            deltas=(),
            n_features_checked=0,
            n_mismatches=0,
            record_id=None,
        )

    needed_set = set(features) if features is not None else set(DEFAULT_FEATURES)

    try:
        ref_arrays = compute_all_from_df(reference_df, needed=needed_set)
    except Exception:
        logger.exception("[feature_parity] compute_all_from_df failed")
        return ParityResult(
            ok=True,
            severity=SEVERITY_OK,
            mode=cfg["mode"],
            reason="ref_compute_failed",
            deltas=(),
            n_features_checked=len(needed_set),
            n_mismatches=0,
            record_id=None,
        )

    ref_snap = extract_last_row_snapshot(ref_arrays, needed=needed_set)
    # Only diff keys that were actually requested — callers supplying extra
    # non-indicator keys (``ticker``, ``alert_id``, etc) should not produce
    # spurious "missing_reference" deltas.
    live_filtered = {k: v for k, v in live_snap.items() if k in needed_set}

    deltas, severity = diff_feature_vectors(
        live_filtered,
        ref_snap,
        epsilon_abs=cfg["epsilon_abs"],
        epsilon_rel=cfg["epsilon_rel"],
        critical_mismatch_count=cfg["critical_mismatch_count"],
        features=needed_set,
    )

    record_id: int | None = None
    if db is not None and deltas:
        try:
            record_id = _persist_drift_event(
                db,
                ticker=ticker,
                severity=severity,
                deltas=deltas,
                source=source,
                scan_pattern_id=scan_pattern_id,
                venue=venue,
                n_features_checked=len(needed_set),
            )
        except Exception:
            logger.exception("[feature_parity] persist failed (continuing)")

    if severity != SEVERITY_OK and cfg["alert_on_warn"]:
        _dispatch_alert(
            ticker=ticker,
            severity=severity,
            source=source,
            n_mismatches=len(deltas),
            scan_pattern_id=scan_pattern_id,
        )

    ok = True
    reason: str | None = None
    if cfg["mode"] == MODE_HARD and severity == SEVERITY_CRITICAL:
        ok = False
        reason = f"feature_parity_critical:{len(deltas)}"
    elif severity != SEVERITY_OK:
        reason = f"feature_parity_{severity}:{len(deltas)}"

    return ParityResult(
        ok=ok,
        severity=severity,
        mode=cfg["mode"],
        reason=reason,
        deltas=deltas,
        n_features_checked=len(needed_set),
        n_mismatches=len(deltas),
        record_id=record_id,
    )
