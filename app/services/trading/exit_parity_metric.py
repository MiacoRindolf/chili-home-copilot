"""Pure helpers for the f-exit-parity-metric-v2 (Migration 230) decomposition.

Both the live exit-engine adapter (``live_exit_engine.py``) and the
backtest adapter (``backtest_service.py::_phase_b_bt_shadow_parity``)
call into ``compute_parity_v2_fields`` to derive the four
ExitParityLog v2 columns from the legacy + canonical engine outputs.

The function is pure (no DB, no HTTP, no logging) so it can be
exhaustively tested at the helper level without spinning up the
truncate-per-test fixture cycle.

Sign convention for ``exit_price_drift_bps``: positive ALWAYS means
canonical produced better realized P/L relative to legacy, regardless
of trade direction. The function takes ``direction`` to apply the
direction-aware sign flip for shorts.
"""
from __future__ import annotations

import hashlib
from typing import NamedTuple, Optional

PARITY_SAMPLE_HASH_DIGEST_SIZE_BYTES = 8
PARITY_SAMPLE_BUCKET_DENOMINATOR = float(
    1 << (PARITY_SAMPLE_HASH_DIGEST_SIZE_BYTES * 8)
)
BACKTEST_PARITY_ASYMMETRIC_ACTION_CLASSES = frozenset({
    "canonical_only_close",
    "legacy_only_close",
})


class ParityV2Fields(NamedTuple):
    """The four columns added by Migration 230."""

    action_class: str
    label_match: Optional[bool]
    exit_price_drift_bps: Optional[float]
    priority_winner: Optional[str]


def compute_parity_v2_fields(
    *,
    legacy_action: str,
    canonical_action: str,
    legacy_exit_price: Optional[float],
    canonical_exit_price: Optional[float],
    canonical_reason_code: Optional[str],
    direction: str = "long",
) -> ParityV2Fields:
    """Derive the v2 metric fields from the two engines' outputs.

    Parameters
    ----------
    legacy_action / canonical_action
        The action string each engine emitted (``"hold"`` means no exit;
        any other value means a close decision keyed by reason).
    legacy_exit_price / canonical_exit_price
        The exit price each engine would have used. Required for the
        ``exit_price_drift_bps`` computation; if either is None the
        drift is None (skip).
    canonical_reason_code
        Used as ``priority_winner`` when canonical exits but legacy
        doesn't, or when both close with disagreeing labels.
    direction
        ``"long"`` (default) or ``"short"``. For shorts, the sign on
        ``exit_price_drift_bps`` is flipped so the convention "positive
        = canonical did better" still holds.
    """
    legacy_closes = legacy_action != "hold"
    canonical_closes = canonical_action != "hold"
    if not legacy_closes and not canonical_closes:
        action_class = "both_hold"
    elif legacy_closes and canonical_closes:
        action_class = "both_close"
    elif canonical_closes and not legacy_closes:
        action_class = "canonical_only_close"
    else:
        action_class = "legacy_only_close"

    label_match: Optional[bool] = (
        (legacy_action == canonical_action)
        if action_class == "both_close"
        else None
    )

    sign = 1.0 if (direction or "long").lower() != "short" else -1.0
    exit_price_drift_bps: Optional[float] = None
    if (
        action_class == "both_close"
        and legacy_exit_price is not None
        and canonical_exit_price is not None
    ):
        try:
            lxp = float(legacy_exit_price)
            cxp = float(canonical_exit_price)
        except (TypeError, ValueError):
            lxp = 0.0
            cxp = 0.0
        if lxp > 0:
            exit_price_drift_bps = sign * (cxp - lxp) / lxp * 10000.0

    priority_winner: Optional[str] = None
    if action_class == "both_close" and label_match is False:
        priority_winner = canonical_reason_code
    elif action_class == "canonical_only_close":
        priority_winner = canonical_reason_code
    elif action_class == "legacy_only_close":
        priority_winner = legacy_action

    return ParityV2Fields(
        action_class=action_class,
        label_match=label_match,
        exit_price_drift_bps=exit_price_drift_bps,
        priority_winner=priority_winner,
    )


def should_persist_parity_row(
    *,
    sample_pct: float,
    action_class: str | None,
    agree_bool: bool,
    legacy_action: str,
    canonical_action: str,
    source: str,
    ticker: str,
    position_id: int | None = None,
    scan_pattern_id: int | None = None,
    bar_idx: int | None = None,
    config_hash: str | None = None,
    sample_salt: str | None = None,
) -> bool:
    """Return whether a parity row should be persisted.

    Disagreements and actual exits are always kept. Sampling only applies to
    low-information ``hold``/``hold`` rows where both engines agree, which are
    the source of most table growth and are not used by the cutover drift
    statistics.
    """
    boring_agreed_hold = (
        bool(agree_bool)
        and (action_class in (None, "", "both_hold"))
        and legacy_action == "hold"
        and canonical_action == "hold"
    )
    if not boring_agreed_hold:
        return True

    try:
        pct = float(sample_pct)
    except (TypeError, ValueError):
        pct = 1.0
    if pct >= 1.0:
        return True
    if pct <= 0.0:
        return False

    return _sample_pct_allows(
        sample_pct,
        (
            source,
            ticker,
            position_id,
            scan_pattern_id,
            bar_idx,
            config_hash,
            sample_salt,
            legacy_action,
            canonical_action,
        ),
    )


def should_persist_backtest_parity_row(
    *,
    hold_sample_pct: float,
    close_agreement_sample_pct: float,
    interesting_drift_bps: float,
    action_class: str | None,
    agree_bool: bool,
    label_match: bool | None,
    exit_price_drift_bps: float | None,
    priority_winner: str | None,
    legacy_action: str,
    canonical_action: str,
    ticker: str,
    scan_pattern_id: int | None = None,
    bar_idx: int | None = None,
    config_hash: str | None = None,
) -> bool:
    """Return whether a backtest parity row is worth persisting.

    Live parity keeps every actual exit row because live volume is naturally
    bounded. Backtests can emit one row per synthetic bar across hundreds of
    queued retests, so they need a richer evidence buffer: keep divergences
    and meaningful drift, sample routine agreements deterministically.
    """
    normalized_action_class = (action_class or "").strip()
    if not bool(agree_bool):
        return True
    if normalized_action_class in BACKTEST_PARITY_ASYMMETRIC_ACTION_CLASSES:
        return True
    if label_match is False:
        return True
    if priority_winner:
        return True

    if _drift_is_interesting(
        exit_price_drift_bps=exit_price_drift_bps,
        interesting_drift_bps=interesting_drift_bps,
    ):
        return True

    if normalized_action_class == "both_close":
        return _sample_pct_allows(
            close_agreement_sample_pct,
            (
                "backtest_close_agreement",
                ticker,
                scan_pattern_id,
                bar_idx,
                config_hash,
                legacy_action,
                canonical_action,
            ),
        )

    return should_persist_parity_row(
        sample_pct=hold_sample_pct,
        action_class=action_class,
        agree_bool=agree_bool,
        legacy_action=legacy_action,
        canonical_action=canonical_action,
        source="backtest",
        ticker=ticker,
        scan_pattern_id=scan_pattern_id,
        bar_idx=bar_idx,
        config_hash=config_hash,
    )


def _drift_is_interesting(
    *,
    exit_price_drift_bps: float | None,
    interesting_drift_bps: float,
) -> bool:
    try:
        threshold = float(interesting_drift_bps)
        drift = abs(float(exit_price_drift_bps))
    except (TypeError, ValueError):
        return False
    return threshold > 0.0 and drift >= threshold


def _sample_pct_allows(sample_pct: float, key_parts: tuple[object, ...]) -> bool:
    try:
        pct = float(sample_pct)
    except (TypeError, ValueError):
        pct = 1.0
    if pct >= 1.0:
        return True
    if pct <= 0.0:
        return False

    key = "|".join(
        str(part)
        for part in key_parts
    )
    digest = hashlib.blake2b(
        key.encode("utf-8"),
        digest_size=PARITY_SAMPLE_HASH_DIGEST_SIZE_BYTES,
    ).digest()
    bucket = int.from_bytes(digest, "big") / PARITY_SAMPLE_BUCKET_DENOMINATOR
    return bucket < pct


__all__ = [
    "compute_parity_v2_fields",
    "should_persist_backtest_parity_row",
    "should_persist_parity_row",
    "ParityV2Fields",
]
