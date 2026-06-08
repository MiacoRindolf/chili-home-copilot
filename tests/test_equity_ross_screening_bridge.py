"""Equity Ross-screening bridge selection (momentum lane equities go Ross-style).

The momentum lane scored CRYPTO with differentiated Ross momentum quality
(ross_scores from RVOL/gap/float) but left EQUITIES at a flat default viability
with NO ross_score — because the per-symbol Ross signals were only ever produced
for the crypto scanner, never bridged from the equity intraday-signal sweep. So
the lane could not SELECT explosive equity movers; it would arm equities
arbitrarily once crypto_only was lifted.

`_equity_movers_for_ross_bridge` selects the LONG equity movers from a sweep that
carry a readable Ross pillar, so they can be routed through the same Ross-momentum
viability pipeline as crypto.
"""

from __future__ import annotations

from app.services.trading_scheduler import (
    _equity_movers_for_ross_bridge,
    _intraday_sweep_interval_seconds,
)
from app.config import settings


def test_sweep_cadence_stays_within_viability_freshness_gate():
    # The equity bridge runs inside the intraday sweep, the ONLY refresher for equity
    # momentum viability. If the sweep is slower than the live-entry freshness gate,
    # equities go stale between sweeps and every equity entry is blocked. The cadence
    # must therefore stay strictly UNDER the gate (with margin), derived from it.
    gate = float(
        getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0
    )
    interval = _intraday_sweep_interval_seconds()
    assert interval < gate, f"sweep {interval}s must be < freshness gate {gate}s"
    assert interval == int(max(120.0, gate / 2.0))  # half the gate, floored at 120s
    assert interval >= 120


def _sweep(premarket=None, orb=None, momentum=None):
    return {
        "premarket_gaps": premarket or [],
        "orb_signals": orb or [],
        "momentum_signals": momentum or [],
    }


def test_long_gapper_with_gap_pct_is_selected():
    out = _sweep(premarket=[{"ticker": "MRVL", "gap_pct": 7.63, "direction": "up"}])
    sel = _equity_movers_for_ross_bridge(out)
    assert [s["ticker"] for s in sel] == ["MRVL"]


def test_momentum_signal_with_rvol_is_selected():
    out = _sweep(momentum=[{"ticker": "TYRA", "rvol": 5.2, "direction": "up"}])
    assert [s["ticker"] for s in _equity_movers_for_ross_bridge(out)] == ["TYRA"]


def test_short_orb_breakdown_is_dropped():
    # The lane is LONG-only — a short ORB breakdown must not become an armable
    # equity candidate even though it carries a pillar.
    out = _sweep(orb=[{"ticker": "INTC", "gap_pct": -5.0, "direction": "short"}])
    assert _equity_movers_for_ross_bridge(out) == []


def test_signal_without_any_ross_pillar_is_dropped():
    # ORB-up carrying only breakout_pct (which score_universe can't read) would land
    # at a flat 0 score — exactly the flat-equity problem this bridge fixes — so it
    # is excluded rather than re-introducing undifferentiated equities.
    out = _sweep(orb=[{"ticker": "AMD", "breakout_pct": 9.1, "direction": "up"}])
    assert _equity_movers_for_ross_bridge(out) == []


def test_missing_direction_defaults_to_long():
    out = _sweep(premarket=[{"ticker": "MU", "gap_pct": 4.85}])
    assert [s["ticker"] for s in _equity_movers_for_ross_bridge(out)] == ["MU"]


def test_down_gap_is_dropped():
    out = _sweep(premarket=[{"ticker": "XYZ", "gap_pct": -6.0, "direction": "down"}])
    assert _equity_movers_for_ross_bridge(out) == []


def test_non_dict_and_missing_ticker_are_dropped():
    out = _sweep(
        premarket=["not-a-dict", {"gap_pct": 5.0, "direction": "up"}],  # no ticker
        momentum=[{"symbol": "OK", "rvol": 3.0}],  # symbol key accepted
    )
    assert [s.get("symbol") for s in _equity_movers_for_ross_bridge(out)] == ["OK"]


def test_empty_or_non_dict_sweep_returns_empty():
    assert _equity_movers_for_ross_bridge({}) == []
    assert _equity_movers_for_ross_bridge(None) == []
    assert _equity_movers_for_ross_bridge("nope") == []


def test_mixed_sweep_keeps_only_long_pillar_carrying():
    out = _sweep(
        premarket=[
            {"ticker": "MRVL", "gap_pct": 7.6, "direction": "up"},       # keep
            {"ticker": "DOWN", "gap_pct": -3.0, "direction": "down"},    # drop (short)
        ],
        orb=[
            {"ticker": "INTC", "breakout_pct": 10.0, "direction": "short"},  # drop
            {"ticker": "ORBU", "rvol": 4.0, "direction": "up"},             # keep
        ],
        momentum=[{"ticker": "TYRA", "rvol": 5.2, "change_pct": 8.0, "direction": "up"}],  # keep
    )
    assert sorted(s["ticker"] for s in _equity_movers_for_ross_bridge(out)) == [
        "MRVL",
        "ORBU",
        "TYRA",
    ]
