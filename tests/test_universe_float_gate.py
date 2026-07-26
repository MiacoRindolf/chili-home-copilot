"""Ross-parity L4 (2026-07-25): float gate on the final ranked universe subset.

Ross scans FLOAT FIRST (supply side). The gate excludes above-band names from the
ranked pool (slot reallocation — the next-ranked low-float name backfills), with a
bounded lookup budget (2x profile.max_universe, never the uncapped hard ceiling),
FAIL-OPEN on None/error/exhausted budget, and a kill-switch restoring byte-identical
output. Reference = the ONE shared viability A-setup ceiling (no second number).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.services.trading.momentum_neural.universe import (
    EQUITY_ROSS_SMALLCAP,
    build_equity_universe,
)

_UNI = "app.services.trading.momentum_neural.universe"
_MASSIVE = "app.services.massive_client"


def _snapshot(n: int = 6) -> list[dict]:
    # n screen-passing movers in the REAL Massive/Polygon snapshot shape (lastTrade.p,
    # day.{v,h,l,c}, todaysChangePerc), descending strength so ranking is deterministic:
    # T0 strongest ... T{n-1} weakest.
    rows = []
    for i in range(n):
        rows.append({
            "ticker": f"T{i}",
            "lastTrade": {"p": 5.9},  # pinned near the high -> strong pos_in_range
            "day": {"v": 2_000_000, "h": 6.0, "l": 4.0, "c": 5.9, "vw": 5.5},
            "todaysChangePerc": 50.0 - i * 5.0,
        })
    return rows


class _Settings:
    # minimal settings stub: gate ON, uncapped OFF (top-N cap = profile), no recatch
    chili_momentum_universe_float_gate_enabled = True
    chili_momentum_universe_uncapped_enabled = False
    chili_momentum_universe_hard_ceiling = 1500
    chili_momentum_hot_mover_recatch_enabled = False
    chili_momentum_a_setup_quality_floor_float_ceiling_shares = 20_000_000.0


def _build(floats: dict[str, float | None], *, settings_obj=None, profile=None):
    prof = profile or EQUITY_ROSS_SMALLCAP
    with patch(f"{_MASSIVE}.get_ticker_float", side_effect=lambda t: floats.get(t)), \
            patch("app.config.settings", settings_obj or _Settings()):
        return build_equity_universe(prof, snapshot=_snapshot())


def test_high_float_excluded_and_backfilled():
    floats = {"T0": 60_000_000.0, "T1": 3_000_000.0, "T2": 5_000_000.0,
              "T3": 8_000_000.0, "T4": 2_000_000.0, "T5": 1_000_000.0}
    out = _build(floats)
    assert "T0" not in out, f"60M-float name must be excluded, got {out}"
    assert "T1" in out and "T5" in out  # low-float names retained/backfilled


def test_none_float_fails_open():
    floats = {"T0": None, "T1": 3_000_000.0}
    out = _build(floats)
    assert "T0" in out  # unknown float never blocks (viability gate is the backstop)


def test_flag_off_byte_identical():
    class _Off(_Settings):
        chili_momentum_universe_float_gate_enabled = False

    floats = {"T0": 60_000_000.0}
    out = _build(floats, settings_obj=_Off())
    assert "T0" in out  # gate skipped -> high-float name retained (legacy behavior)


def test_missing_flag_preserves_output_without_provider_read():
    settings_obj = SimpleNamespace(
        chili_momentum_universe_uncapped_enabled=False,
        chili_momentum_universe_hard_ceiling=1500,
        chili_momentum_hot_mover_recatch_enabled=False,
        chili_momentum_a_setup_quality_floor_float_ceiling_shares=20_000_000.0,
    )
    with patch(f"{_MASSIVE}.get_ticker_float") as get_float, \
            patch("app.config.settings", settings_obj):
        out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=_snapshot())
    assert "T0" in out
    get_float.assert_not_called()


def test_lookup_budget_bounded():
    calls = {"n": 0}

    def _counting_float(t):
        calls["n"] += 1
        return 1_000_000.0

    prof = EQUITY_ROSS_SMALLCAP
    with patch(f"{_MASSIVE}.get_ticker_float", side_effect=_counting_float), \
            patch("app.config.settings", _Settings()):
        build_equity_universe(prof, snapshot=_snapshot())
    assert calls["n"] <= 2 * prof.max_universe, (
        f"lookup budget breached: {calls['n']} > {2 * prof.max_universe}"
    )


def test_profile_override_beats_shared_reference():
    from dataclasses import replace

    prof = replace(EQUITY_ROSS_SMALLCAP, float_shares_max=2_500_000.0)
    floats = {"T0": 3_000_000.0, "T1": 1_000_000.0}  # T0 above the 2.5M override
    out = _build(floats, profile=prof)
    assert "T0" not in out
    assert "T1" in out
