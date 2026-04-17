"""Phase L.18 pure unit tests - breadth + cross-sectional RS classifier."""
from __future__ import annotations

from datetime import date

import pytest

from app.services.trading.breadth_relstr_model import (
    ALL_SYMBOLS,
    BREADTH_MIXED,
    BREADTH_RISK_OFF,
    BREADTH_RISK_ON,
    BreadthRelstrConfig,
    BreadthRelstrInput,
    BreadthRelstrOutput,
    SECTOR_SYMBOLS,
    SYMBOL_IWM,
    SYMBOL_QQQ,
    SYMBOL_SPY,
    UniverseMember,
    classify_direction,
    classify_trend,
    compute_breadth_relstr,
    compute_snapshot_id,
)
from app.services.trading.macro_regime_model import (
    TREND_DOWN,
    TREND_FLAT,
    TREND_MISSING,
    TREND_UP,
)


# ---------------------------------------------------------------------------
# Deterministic IDs and typed guards
# ---------------------------------------------------------------------------


def test_compute_snapshot_id_is_deterministic():
    a = compute_snapshot_id(date(2026, 4, 16))
    b = compute_snapshot_id(date(2026, 4, 16))
    c = compute_snapshot_id(date(2026, 4, 17))
    assert a == b
    assert a != c
    assert isinstance(a, str)
    assert len(a) == 16


def test_compute_snapshot_id_rejects_non_date():
    with pytest.raises(TypeError):
        compute_snapshot_id("2026-04-16")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# UniverseMember invariants
# ---------------------------------------------------------------------------


def test_universe_member_rejects_invalid_trend():
    with pytest.raises(ValueError):
        UniverseMember(symbol="XLK", trend="banana", direction=TREND_UP)


def test_universe_member_rejects_invalid_direction():
    with pytest.raises(ValueError):
        UniverseMember(symbol="XLK", trend=TREND_UP, direction="moon")


def test_universe_member_defaults_are_missing():
    m = UniverseMember(symbol="XLK")
    assert m.missing is False
    assert m.trend == TREND_MISSING
    assert m.direction == TREND_MISSING
    assert m.last_close is None
    assert m.prev_close is None


# ---------------------------------------------------------------------------
# classify_trend + classify_direction
# ---------------------------------------------------------------------------


def test_classify_trend_matches_macro_regime_thresholds():
    cfg = BreadthRelstrConfig(trend_up_threshold=0.01)
    assert classify_trend(0.02, cfg=cfg) == TREND_UP
    assert classify_trend(-0.02, cfg=cfg) == TREND_DOWN
    assert classify_trend(0.005, cfg=cfg) == TREND_FLAT
    assert classify_trend(-0.005, cfg=cfg) == TREND_FLAT
    assert classify_trend(None, cfg=cfg) == TREND_MISSING


def test_classify_direction_basic():
    assert classify_direction(101.0, 100.0) == TREND_UP
    assert classify_direction(99.0, 100.0) == TREND_DOWN
    assert classify_direction(100.0, 100.0) == TREND_FLAT
    assert classify_direction(None, 100.0) == TREND_MISSING
    assert classify_direction(100.0, None) == TREND_MISSING
    assert classify_direction(100.0, 0.0) == TREND_MISSING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _member_up(sym: str, mom20: float = 0.04) -> UniverseMember:
    """Convenience: member with close above prior close and positive 20d mom."""
    return UniverseMember(
        symbol=sym,
        last_close=101.0,
        prev_close=100.0,
        momentum_20d=mom20,
        trend=TREND_UP,
        direction=TREND_UP,
    )


def _member_down(sym: str, mom20: float = -0.04) -> UniverseMember:
    return UniverseMember(
        symbol=sym,
        last_close=99.0,
        prev_close=100.0,
        momentum_20d=mom20,
        trend=TREND_DOWN,
        direction=TREND_DOWN,
    )


def _member_flat(sym: str) -> UniverseMember:
    return UniverseMember(
        symbol=sym,
        last_close=100.0,
        prev_close=100.0,
        momentum_20d=0.0,
        trend=TREND_FLAT,
        direction=TREND_FLAT,
    )


def _all_basket_up() -> list[UniverseMember]:
    members: list[UniverseMember] = []
    for i, sym in enumerate(SECTOR_SYMBOLS):
        # give each sector a slightly different momentum so leader is stable
        members.append(_member_up(sym, mom20=0.04 + 0.001 * i))
    for sym in (SYMBOL_SPY, SYMBOL_QQQ, SYMBOL_IWM):
        members.append(_member_up(sym, mom20=0.03))
    return members


def _all_basket_down() -> list[UniverseMember]:
    members: list[UniverseMember] = []
    for i, sym in enumerate(SECTOR_SYMBOLS):
        members.append(_member_down(sym, mom20=-0.04 - 0.001 * i))
    for sym in (SYMBOL_SPY, SYMBOL_QQQ, SYMBOL_IWM):
        members.append(_member_down(sym, mom20=-0.03))
    return members


# ---------------------------------------------------------------------------
# Core compute behaviour
# ---------------------------------------------------------------------------


def test_compute_risk_on_canonical():
    out = compute_breadth_relstr(
        BreadthRelstrInput(
            as_of_date=date(2026, 4, 16),
            members=_all_basket_up(),
        )
    )
    assert isinstance(out, BreadthRelstrOutput)
    assert out.breadth_label == BREADTH_RISK_ON
    assert out.breadth_numeric == 1
    assert out.members_sampled == len(ALL_SYMBOLS)
    assert out.members_advancing == len(ALL_SYMBOLS)
    assert out.members_declining == 0
    assert out.advance_ratio == 1.0
    assert out.coverage_score == 1.0
    assert out.leader_sector in SECTOR_SYMBOLS
    assert out.laggard_sector in SECTOR_SYMBOLS
    assert out.leader_sector != out.laggard_sector
    # Size/style tilts are 0 when sector baseline momentum equals spy=0.03.
    assert out.size_tilt == pytest.approx(0.0, abs=1e-9)
    assert out.style_tilt == pytest.approx(0.0, abs=1e-9)


def test_compute_risk_off_canonical():
    out = compute_breadth_relstr(
        BreadthRelstrInput(
            as_of_date=date(2026, 4, 16),
            members=_all_basket_down(),
        )
    )
    assert out.breadth_label == BREADTH_RISK_OFF
    assert out.breadth_numeric == -1
    assert out.members_advancing == 0
    assert out.members_declining == len(ALL_SYMBOLS)
    assert out.advance_ratio == 0.0


def test_compute_mixed_on_neutral_ad():
    members: list[UniverseMember] = []
    # Half up, half down sectors + flat benchmarks -> advance_ratio around 0.5.
    for i, sym in enumerate(SECTOR_SYMBOLS):
        if i % 2 == 0:
            members.append(_member_up(sym, mom20=0.02))
        else:
            members.append(_member_down(sym, mom20=-0.02))
    members.append(_member_flat(SYMBOL_SPY))
    members.append(_member_flat(SYMBOL_QQQ))
    members.append(_member_flat(SYMBOL_IWM))
    out = compute_breadth_relstr(
        BreadthRelstrInput(
            as_of_date=date(2026, 4, 16),
            members=members,
        )
    )
    assert out.breadth_label == BREADTH_MIXED
    assert out.breadth_numeric == 0
    assert 0.3 < out.advance_ratio < 0.7


def test_partial_coverage_reduces_coverage_score():
    members = _all_basket_up()[:4]  # only 4 of 14 symbols present
    out = compute_breadth_relstr(
        BreadthRelstrInput(
            as_of_date=date(2026, 4, 16),
            members=members,
        )
    )
    assert out.coverage_score == round(4 / float(len(ALL_SYMBOLS)), 6)
    assert out.symbols_sampled == 4
    assert out.symbols_missing == len(ALL_SYMBOLS) - 4


def test_all_missing_returns_mixed_label():
    out = compute_breadth_relstr(
        BreadthRelstrInput(
            as_of_date=date(2026, 4, 16),
            members=[],
        )
    )
    assert out.breadth_label == BREADTH_MIXED
    assert out.breadth_numeric == 0
    assert out.members_sampled == 0
    assert out.members_advancing == 0
    assert out.members_declining == 0
    assert out.coverage_score == 0.0
    assert out.leader_sector is None
    assert out.laggard_sector is None
    assert out.size_tilt is None
    assert out.style_tilt is None


def test_leader_laggard_tie_break_is_deterministic():
    # Two sectors with identical RS; alphabetical symbol breaks the tie.
    members = [
        UniverseMember(
            symbol="XLK",
            last_close=101.0, prev_close=100.0, momentum_20d=0.05,
            trend=TREND_UP, direction=TREND_UP,
        ),
        UniverseMember(
            symbol="XLB",
            last_close=101.0, prev_close=100.0, momentum_20d=0.05,
            trend=TREND_UP, direction=TREND_UP,
        ),
        UniverseMember(
            symbol="SPY",
            last_close=101.0, prev_close=100.0, momentum_20d=0.01,
            trend=TREND_UP, direction=TREND_UP,
        ),
    ]
    out = compute_breadth_relstr(
        BreadthRelstrInput(as_of_date=date(2026, 4, 16), members=members)
    )
    # XLK > XLB alphabetically, so XLK wins leader on tie; XLB takes laggard.
    assert out.leader_sector == "XLK"
    assert out.laggard_sector == "XLB"


def test_rs_vs_spy_sign_is_correct():
    members = [
        UniverseMember(
            symbol="XLK", last_close=101.0, prev_close=100.0,
            momentum_20d=0.08, trend=TREND_UP, direction=TREND_UP,
        ),
        UniverseMember(
            symbol="SPY", last_close=101.0, prev_close=100.0,
            momentum_20d=0.03, trend=TREND_UP, direction=TREND_UP,
        ),
    ]
    out = compute_breadth_relstr(
        BreadthRelstrInput(as_of_date=date(2026, 4, 16), members=members)
    )
    assert out.sector_map["XLK"]["rs_vs_spy_20d"] == pytest.approx(0.05, abs=1e-9)


def test_size_and_style_tilts_reflect_iwm_qqq_vs_spy():
    members = [
        UniverseMember(
            symbol=SYMBOL_SPY, last_close=101.0, prev_close=100.0,
            momentum_20d=0.03, trend=TREND_UP, direction=TREND_UP,
        ),
        UniverseMember(
            symbol=SYMBOL_QQQ, last_close=101.0, prev_close=100.0,
            momentum_20d=0.06, trend=TREND_UP, direction=TREND_UP,
        ),
        UniverseMember(
            symbol=SYMBOL_IWM, last_close=101.0, prev_close=100.0,
            momentum_20d=-0.02, trend=TREND_DOWN, direction=TREND_DOWN,
        ),
    ]
    out = compute_breadth_relstr(
        BreadthRelstrInput(as_of_date=date(2026, 4, 16), members=members)
    )
    assert out.style_tilt == pytest.approx(0.03, abs=1e-9)   # QQQ - SPY
    assert out.size_tilt == pytest.approx(-0.05, abs=1e-9)   # IWM - SPY


def test_payload_has_stable_top_level_keys():
    out = compute_breadth_relstr(
        BreadthRelstrInput(
            as_of_date=date(2026, 4, 16),
            members=_all_basket_up(),
        )
    )
    assert set(out.payload.keys()) == {"readings", "config"}
    assert set(out.payload["config"].keys()) == {
        "trend_up_threshold",
        "strong_trend_threshold",
        "tilt_threshold",
        "min_coverage_score",
        "risk_on_ratio",
        "risk_off_ratio",
    }
    # readings carry the full basket even when some entries are missing.
    assert set(out.payload["readings"].keys()) == set(ALL_SYMBOLS)


def test_duplicate_and_extra_symbols_are_silently_ignored():
    members = _all_basket_up()
    # Append a duplicate (overrides indexed entry) and a foreign symbol.
    members.append(_member_down("XLK", mom20=-0.10))
    members.append(UniverseMember(
        symbol="ZZZ_NOT_IN_BASKET",
        last_close=10.0, prev_close=9.0, momentum_20d=0.5,
        trend=TREND_UP, direction=TREND_UP,
    ))
    out = compute_breadth_relstr(
        BreadthRelstrInput(as_of_date=date(2026, 4, 16), members=members)
    )
    # Duplicate wins last-write, so XLK should now register as declining.
    assert out.sector_map["XLK"]["trend"] == TREND_DOWN
    # Foreign symbol must not inflate the basket size.
    assert out.members_sampled == len(ALL_SYMBOLS)


def test_coverage_gate_threshold_is_separate_from_classification():
    # Even at zero coverage the pure model never raises and produces the
    # frozen output shape. Coverage gating is a service-layer concern.
    out = compute_breadth_relstr(
        BreadthRelstrInput(as_of_date=date(2026, 4, 16), members=[])
    )
    assert out.coverage_score == 0.0
    assert out.breadth_label == BREADTH_MIXED


def test_output_is_frozen_dataclass():
    out = compute_breadth_relstr(
        BreadthRelstrInput(
            as_of_date=date(2026, 4, 16),
            members=_all_basket_up(),
        )
    )
    with pytest.raises((AttributeError, Exception)):
        out.breadth_label = "broad_risk_off"  # type: ignore[misc]


def test_new_highs_lows_count_only_sampled_members():
    members = [
        UniverseMember(
            symbol="XLK", last_close=101.0, prev_close=100.0,
            momentum_20d=0.08, trend=TREND_UP, direction=TREND_UP,
            new_high_20d=True,
        ),
        UniverseMember(
            symbol="XLE", last_close=99.0, prev_close=100.0,
            momentum_20d=-0.06, trend=TREND_DOWN, direction=TREND_DOWN,
            new_low_20d=True,
        ),
        # Missing entry claiming a new-high should not be counted.
        UniverseMember(
            symbol="XLV", missing=True,
            new_high_20d=True,
        ),
    ]
    out = compute_breadth_relstr(
        BreadthRelstrInput(as_of_date=date(2026, 4, 16), members=members)
    )
    assert out.new_highs_count == 1
    assert out.new_lows_count == 1
