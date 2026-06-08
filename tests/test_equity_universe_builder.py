"""Ross small-cap equity universe builder (per-setup universe tailoring).

The momentum lane must screen the full-market snapshot down to Ross's instrument
class — low-priced, in-play, liquid-enough small-caps — instead of riding the
static large-cap DEFAULT_SCAN_TICKERS (KLAC/MU/NVDA). Pure: synthetic snapshot
injected, no DB / no network.
"""

from app.services.trading.momentum_neural.universe import (
    EQUITY_ROSS_SMALLCAP,
    UniverseProfile,
    _pos_in_range,
    build_equity_universe,
)


def _row(ticker, chg, price, vol):
    return {"ticker": ticker, "todaysChangePerc": chg, "day": {"c": price, "v": vol}}


# A snapshot mixing Ross names with everything the profile must reject.
_SNAP = [
    _row("ROSSA", 50.0, 5.0, 2_000_000),    # $5 +50%  $10M turnover  -> KEEP
    _row("ROSSB", 120.0, 3.0, 5_000_000),   # $3 +120% $15M turnover  -> KEEP (ranks first)
    _row("MEGA", 3.0, 950.0, 1_000_000),    # MU-like mega-cap, price > max -> DROP
    _row("HIGH", 30.0, 150.0, 1_000_000),   # high-priced, price > max      -> DROP
    _row("PENNY", 200.0, 0.50, 10_000_000), # sub-$1 penny, price < min     -> DROP
    _row("DOWN", -10.0, 4.0, 2_000_000),    # down on the day (long bias)   -> DROP
    _row("ILLIQ", 40.0, 6.0, 50_000),       # $300k turnover < $1M floor    -> DROP
    _row("FLAT", 2.0, 8.0, 1_000_000),      # +2% below the in-play floor   -> DROP
]


def test_keeps_only_ross_smallcaps_sorted_by_move():
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=_SNAP)
    # Only the two qualifying small-cap movers survive, strongest move first.
    assert out == ["ROSSB", "ROSSA"]


def test_rejects_megacaps_pennies_downs_and_illiquids():
    out = set(build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=_SNAP))
    for rejected in ("MEGA", "HIGH", "PENNY", "DOWN", "ILLIQ", "FLAT"):
        assert rejected not in out


def test_max_universe_cap():
    prof = UniverseProfile(
        profile_id="t", asset_class="equity", label="t",
        price_min=1.0, price_max=20.0, min_dollar_volume=1_000_000.0,
        min_change_pct=5.0, max_universe=1,
    )
    out = build_equity_universe(prof, snapshot=_SNAP)
    assert out == ["ROSSB"]  # top mover only


def test_empty_snapshot_is_failsafe_empty():
    assert build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=[]) == []


def test_non_equity_profile_returns_empty():
    prof = UniverseProfile(profile_id="c", asset_class="crypto", label="c")
    assert build_equity_universe(prof, snapshot=_SNAP) == []


def test_price_band_is_inclusive_at_bounds():
    snap = [
        _row("ATMIN", 10.0, 1.0, 5_000_000),   # exactly price_min
        _row("ATMAX", 10.0, 20.0, 5_000_000),  # exactly price_max
        _row("OVER", 10.0, 20.01, 5_000_000),  # just over -> DROP
    ]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "ATMIN" in out and "ATMAX" in out
    assert "OVER" not in out


def test_last_trade_price_fallback_when_day_close_absent():
    # Premarket-shaped row: sparse day block, price only in lastTrade.
    snap = [{"ticker": "PREMKT", "todaysChangePerc": 25.0,
             "day": {"v": 3_000_000}, "lastTrade": {"p": 7.0}}]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert out == ["PREMKT"]


def test_missing_change_pct_dropped():
    snap = [{"ticker": "NOCHG", "day": {"c": 5.0, "v": 5_000_000}}]
    assert build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap) == []


def test_profile_documents_ross_class_defaults():
    # The ONE documented spec the operator tunes — Ross's instrument class.
    assert EQUITY_ROSS_SMALLCAP.asset_class == "equity"
    assert EQUITY_ROSS_SMALLCAP.price_min == 1.0
    assert EQUITY_ROSS_SMALLCAP.price_max == 20.0
    assert EQUITY_ROSS_SMALLCAP.min_dollar_volume == 1_000_000.0
    assert EQUITY_ROSS_SMALLCAP.min_change_pct == 5.0
    assert EQUITY_ROSS_SMALLCAP.low_float_bias is True


# ── (B) early-move / freshness ranking — enter fresh, not over-extended ───────

def test_fresh_near_high_ranks_above_faded_big_mover():
    snap = [
        # ran +200% but rolled to the BOTTOM of its day range (faded — too late)
        {"ticker": "FADED", "todaysChangePerc": 200.0,
         "day": {"c": 5.0, "h": 12.0, "l": 4.5, "v": 3_000_000}},
        # only +30% but pinned at the TOP of its range (fresh, still working)
        {"ticker": "FRESH", "todaysChangePerc": 30.0,
         "day": {"c": 7.9, "h": 8.0, "l": 6.0, "v": 3_000_000}},
    ]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    # Ross enters the fresh near-high name, not the faded monster.
    assert out.index("FRESH") < out.index("FADED")


def test_pos_in_range_helper():
    assert _pos_in_range({"day": {"h": 10.0, "l": 5.0}}, 10.0) == 1.0  # at the high (fresh)
    assert _pos_in_range({"day": {"h": 10.0, "l": 5.0}}, 5.0) == 0.0   # at the low (faded)
    assert _pos_in_range({"day": {"h": 10.0, "l": 5.0}}, 7.5) == 0.5   # mid-range
    assert _pos_in_range({"day": {}}, 7.5) == 0.5                       # no range -> neutral
    assert _pos_in_range({"day": {"h": 10.0, "l": 5.0}}, None) == 0.5   # no price -> neutral


def test_profile_has_snapshot_freshness_knob():
    # (A) the documented knob that forces a ~5-min snapshot pull for this profile.
    assert EQUITY_ROSS_SMALLCAP.snapshot_max_age_seconds == 300.0
