"""v3 ask_side_pressure_lock — Ross's ask-side L2 read, mechanized (2026-08-17).

Mula sa live-stream study: "when I'm in a position I am focused almost exclusively
with my eye on the level two and specifically the ASK price." Ang helper ay pure
(no I/O) at ratchet-only (Invariant A): kayang MAG-TIGHTEN lang ng stop sa isang
winner kapag ang ask wall ay lumalapal (ask_build), hindi na nare-refill ang bid,
at bid-favored na ang book/presyo — kailanman hindi nagluluwag, hindi nagbebenta.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.trading.momentum_neural.paper_execution import ask_side_pressure_lock


@dataclass(frozen=True)
class _Ladder:
    depth_imbal: float | None = -0.2
    depth_imbal_pctile: float | None = 0.5
    ofi: float | None = -0.3
    micro_edge: float | None = -1.0
    bid_refill: float | None = -0.10
    ask_build: float | None = 0.40
    spread_bps: float | None = 50.0
    snapshot_age_s: float | None = 4.0
    n_snaps: int = 5


def _base_kwargs(**over):
    kw = dict(
        high_water_mark=12.0,
        entry_price=10.0,
        bid=11.6,
        atr_pct=0.05,
        stop_atr_mult=1.5,
        reward_risk=2.0,
        current_stop=9.25,
        breakeven_floor=10.0,
        current_band_bps=600.0,
        ladder=_Ladder(),
        side_long=True,
    )
    kw.update(over)
    return kw


def test_fires_on_wall_plus_stall_plus_roll_and_ratchets_up():
    """Ang buong confluence (winner + wall + stall + roll) → fired, stop tumaas."""
    out = ask_side_pressure_lock(**_base_kwargs())
    assert out["armed"] is True
    assert out["fired"] is True
    assert out["trigger"] == "ask_wall_stall"
    assert out["new_stop_floor"] > 9.25
    # Invariant A: hindi kailanman mas mababa sa alinman sa current/breakeven floor
    assert out["new_stop_floor"] >= 10.0


def test_never_loosens_when_current_stop_already_tight():
    """Kung ang kasalukuyang stop ay mas mataas pa sa candidate, walang pagbabago."""
    out = ask_side_pressure_lock(**_base_kwargs(current_stop=11.99))
    assert out["new_stop_floor"] >= 11.99


def test_holds_below_profit_arm():
    """Hindi pa winner (peak_r < arm) → armed=False, no-op."""
    out = ask_side_pressure_lock(**_base_kwargs(high_water_mark=10.05))
    assert out["armed"] is False
    assert out["fired"] is False
    assert out["reason"] == "below_arm"
    assert out["new_stop_floor"] == 9.25


def test_holds_when_no_wall():
    """Walang ask build (o negatibo) → no_confluence, no-op."""
    out = ask_side_pressure_lock(**_base_kwargs(ladder=_Ladder(ask_build=0.05)))
    assert out["fired"] is False
    assert out["reason"] == "no_confluence"
    assert out["new_stop_floor"] == 9.25


def test_holds_when_bids_still_refilling():
    """Ross's continuation read: bumibili pa rin sa laki → HOLD (huwag i-tighten)."""
    out = ask_side_pressure_lock(**_base_kwargs(ladder=_Ladder(bid_refill=0.25)))
    assert out["fired"] is False
    assert out["reason"] == "no_confluence"


def test_holds_when_book_and_price_still_bid_favored():
    """Walang roll (micro at depth parehong positibo) → HOLD."""
    out = ask_side_pressure_lock(
        **_base_kwargs(ladder=_Ladder(micro_edge=2.0, depth_imbal=0.3))
    )
    assert out["fired"] is False
    assert out["reason"] == "no_confluence"


def test_holds_on_stale_or_thin_book():
    """Data-quality HOLD: lumang snapshot o kulang na rows → never decides."""
    stale = ask_side_pressure_lock(**_base_kwargs(ladder=_Ladder(snapshot_age_s=120.0)))
    thin = ask_side_pressure_lock(**_base_kwargs(ladder=_Ladder(n_snaps=2)))
    assert stale["fired"] is False and stale["reason"] == "stale_or_thin"
    assert thin["fired"] is False and thin["reason"] == "stale_or_thin"


def test_holds_on_missing_ladder_fields():
    """None sa mga required field → fail-safe no-op (never sells on bad data)."""
    out = ask_side_pressure_lock(
        **_base_kwargs(ladder=_Ladder(ask_build=None, bid_refill=None))
    )
    assert out["fired"] is False
    assert out["new_stop_floor"] == 9.25


def test_no_ladder_is_noop():
    out = ask_side_pressure_lock(**_base_kwargs(ladder=None))
    assert out["fired"] is False
    assert out["reason"] == "no_ladder"
    assert out["new_stop_floor"] == 9.25


def test_short_side_is_noop():
    out = ask_side_pressure_lock(**_base_kwargs(side_long=False))
    assert out["fired"] is False
    assert out["reason"] == "not_long"


def test_lock_never_looser_than_band():
    """Ang lock ay naka-clamp sa cushion band ngayong tick (hindi lalapad)."""
    tight_band = ask_side_pressure_lock(**_base_kwargs(current_band_bps=30.0))
    assert tight_band["fired"] is True
    # band 30bps → candidate = hwm*(1-0.003) = 11.964; floor >= iyon
    assert tight_band["new_stop_floor"] >= 12.0 * (1.0 - 30.0 / 10_000.0) - 1e-9


def test_counterfactual_matches_band_only_baseline():
    out = ask_side_pressure_lock(**_base_kwargs())
    assert out["counterfactual_band_stop"] == 9.25
