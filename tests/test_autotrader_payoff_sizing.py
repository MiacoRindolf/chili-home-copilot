"""f-stop-engine-payoff-ratio-gate (2026-05-19) — pin the autotrader's
payoff-ratio-aware sizing scaler.

The Tier A demote-gate protection (shipped 2026-05-18, commit 23bde18)
prevents skew-driven patterns like pid 585 (4.97:1 payoff over 86
trades) from being mis-demoted on win-rate alone. This brief extends
the same payoff_ratio signal to the AUTOTRADER ENTRY path: scale
position notional based on the pattern's demonstrated payoff quality.

The 2026-05-18 TCA finding showed avg +102 bps entry slippage on
crypto, consuming ~60% of pattern 585's gross edge. Sizing UP on
high-payoff patterns + sizing DOWN on sub-1:1 ones is one of three
levers (alongside maker-only routing and tighter price gating) that
preserve realized edge.

Pinned invariants:

1. Settings flag defaults to False (no behavior change on deploy).
2. Settings min_n defaults to 5 (matches mig 246 + Tier A demote gate).
3. auto_trader.py contains the scaler code path (static-grep).
4. Scaler is wrapped in try/except (NEVER crash entry).
5. Scaler reads payoff_ratio + payoff_ratio_n from scan_patterns.

Code-shape tests rather than full-broker integration — same pattern
as test_coinbase_maker_only_routing.py and the bracket-stop tests.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


# ── #1 — settings flag default ────────────────────────────────────────


def test_payoff_sizing_flag_defaults_to_false():
    """No behavior change on deploy until operator opts in."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.chili_autotrader_payoff_sizing_enabled is False


def test_payoff_sizing_min_n_defaults_to_5():
    """Matches mig 246's payoff_ratio_n + the Tier A demote gate floor."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.chili_autotrader_payoff_min_n == 5


def test_payoff_sizing_posterior_defaults_are_neutral():
    """Thin samples shrink toward neutral instead of threshold cliffs."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.chili_autotrader_payoff_prior_ratio == 1.0
    assert s.chili_autotrader_payoff_prior_n == 20
    assert s.chili_autotrader_payoff_min_multiplier == 0.5
    assert s.chili_autotrader_payoff_max_multiplier == 1.5


# ── #2 — auto_trader.py has the scaler code ───────────────────────────


def test_auto_trader_has_payoff_sizing_block():
    """The auto_trader.py entry-sizing chain must contain the new
    payoff_ratio-aware block (composes after HRP / survival / pilot)."""
    text = _read("app/services/trading/auto_trader.py")

    # Marker that identifies the new code block (the brief identifier
    # appears in the inline comment).
    assert "f-stop-engine-payoff-ratio-gate" in text, (
        "auto_trader.py is missing the f-stop-engine-payoff-ratio-gate "
        "marker. The payoff-ratio sizing scaler must be present."
    )

    # Reads the Tier A column from scan_patterns
    assert "payoff_ratio" in text, (
        "auto_trader.py is missing payoff_ratio reference. The scaler "
        "must read scan_patterns.payoff_ratio."
    )

    # Honors the settings flag
    assert "chili_autotrader_payoff_sizing_enabled" in text, (
        "auto_trader.py is missing the flag check. Default-OFF behavior "
        "must be preserved."
    )


def test_auto_trader_payoff_scaler_writes_snapshot_fields():
    """The scaler must write observability fields to snap{} so the
    autotrader_runs audit log can show which tier fired."""
    text = (
        _read("app/services/trading/auto_trader.py")
        + _read("app/services/trading/payoff_sizing.py")
    )
    for field in [
        '"payoff_sizing_tier"',
        '"payoff_sizing_multiplier"',
        '"payoff_ratio_observed"',
        '"payoff_ratio_n_observed"',
    ]:
        assert field in text, (
            f"auto_trader.py is missing snap[{field}]. Required for "
            f"audit observability."
        )


def test_payoff_scaler_tier_labels_and_bounds():
    """The helper preserves tier labels but computes a smoothed multiplier."""
    text = _read("app/services/trading/payoff_sizing.py")
    for tier in ["very_high", "high", "moderate", "low", "insufficient_n"]:
        assert tier in text, (
            f"payoff_sizing.py is missing the '{tier}' tier label. "
            f"Each tier label is part of the audit contract."
        )

    for mult in ["1.5", "0.5"]:
        assert mult in text, (
            f"payoff_sizing.py is missing multiplier bound {mult}. "
            f"Multiplier bounds are part of the sizing contract."
        )


def test_payoff_scaler_smooths_pattern_585_near_threshold():
    from app.services.trading.payoff_sizing import compute_payoff_sizing

    decision = compute_payoff_sizing(payoff_ratio=4.9656999, payoff_ratio_n=86)

    assert decision.tier == "high"
    assert 1.25 < decision.multiplier < 1.5
    assert decision.adjusted_ratio is not None
    assert decision.adjusted_ratio < 4.9656999


def test_payoff_scaler_deflates_thin_extreme_payoff():
    from app.services.trading.payoff_sizing import compute_payoff_sizing

    decision = compute_payoff_sizing(payoff_ratio=29.57, payoff_ratio_n=7)

    assert decision.tier == "very_high"
    assert 1.0 < decision.multiplier < 1.25
    assert decision.confidence_weight < 0.5


# ── #3 — try/except wrapper (sizing never crashes) ────────────────────


def test_auto_trader_payoff_scaler_wrapped_in_try():
    """A payoff-sizing failure (DB hiccup, NULL row, etc.) must NEVER
    crash an entry attempt. The block must be wrapped in try/except."""
    text = _read("app/services/trading/auto_trader.py")
    lines = text.splitlines()
    # Find the brief marker, walk down to find a `try:` then a writer
    # call, then an `except Exception:` within reasonable window.
    marker_idx = None
    for i, ln in enumerate(lines):
        if "f-stop-engine-payoff-ratio-gate" in ln:
            marker_idx = i
            break
    assert marker_idx is not None, "marker not found"

    try_found = False
    except_found = False
    for k in range(marker_idx, min(len(lines), marker_idx + 80)):
        if lines[k].strip().startswith("try:"):
            try_found = True
        if try_found and re.match(r"\s*except\b", lines[k]):
            except_found = True
            break
    assert try_found and except_found, (
        "auto_trader.py payoff-sizing block is not wrapped in "
        "try/except. Sizing must NEVER crash entry."
    )


# ── #4 — composition order (after pilot_promoted, before qty) ─────────


def test_auto_trader_payoff_composes_after_pilot_before_qty():
    """The scaler must compose AFTER the pilot_promoted_risk_multiplier
    block (so pilot down-scaling is preserved) and BEFORE the
    `qty = int(notional / px)` final share computation."""
    text = _read("app/services/trading/auto_trader.py")

    # Get positions of the three anchors
    pilot_pos = text.find("pilot_promoted_risk_multiplier")
    marker_pos = text.find("f-stop-engine-payoff-ratio-gate")
    qty_pos = text.find("qty_raw = notional / px", marker_pos)

    assert pilot_pos > 0, "pilot_promoted_risk_multiplier anchor not found"
    assert marker_pos > 0, "payoff marker not found"
    assert qty_pos > 0, "qty_raw = notional/px anchor not found"

    assert pilot_pos < marker_pos < qty_pos, (
        f"Composition order wrong. Expected: pilot < payoff < qty. "
        f"Got: pilot={pilot_pos} payoff={marker_pos} qty={qty_pos}"
    )
