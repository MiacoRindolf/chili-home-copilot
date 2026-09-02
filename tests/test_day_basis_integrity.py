"""DEFECT 2 — move_pct reports fiction, because nothing validates the basis.

move_pct / gap_pct / todays_change_perc are an unguarded division by a vendor
field the code never checked:

    move_pct = (price - prev_close) / prev_close * 100

THE PROOF THAT IT IS THE BASIS AND NOT THE PRICE (lane log, 2026-09-02):

    06:39:23  [momentum_ws_ignition] symbol=HOS move_pct=3462.37 scored_ok=True
    06:39:37  [momentum_ws_ignition] symbol=HOS move_pct=3447.36 scored_ok=True

14s apart, 15.01 percentage points of "change" — which is a 0.43% price move.
The deltas are honest; only the level is invented. 10.687 / 35.6237 = 0.3000,
and the persisted row names it: momentum_symbol_viability HOS, updated_at
2026-09-02 22:56:34, ross_signals = {"price": 10.33, "gap_pct": 3345.63,
"prev_close": 0.3, "signal_type": "premarket_gap", ...}. Two independent tapes
say $0.30 never happened (iqfeed_trade_ticks 14:00-14:30Z: 1,687 prints,
10.37-10.6099; momentum_nbbo_spread_tape 14:00-14:20Z: 36 rows, mid
10.52-10.595), and HOS is absent from the independent full-market mover set.

The harm ran both ways. HOS took the #1 ross_score of the whole 32-name board
(0.5081 vs 0.3464 for the best real name) plus a top_market_gainers slot and the
+0.03 viability tilt, for a stock that moved 4.75% all day. In the other
direction ross_universe_change_below_profile (a +5.0% floor) fired 1,909 times
across 17 symbol-days whose true day change was +11.8% to +946.4% — SGLY x324
(+182.9%), HUIZ x323 (+186.3%), JZ x318 (+221.2%) on 2026-08-20 alone.

Every test below fails on origin/main. Pure unit tests, no DB, no network.
"""

from __future__ import annotations

import logging

import pytest

from app.services.trading.day_basis_guard import (
    DAY_BASIS_IMPLAUSIBLE_VS_SESSION,
    DAY_BASIS_MISSING,
    DAY_BASIS_OK,
    DAY_BASIS_OUTSIDE_PREV_RANGE,
    DAY_BASIS_REJECTED,
    basis_continuity_broken,
    classify_day_basis,
)
from app.services.trading.momentum_neural import ignition_loop as IL


# ── the guard itself ─────────────────────────────────────────────────────────


def test_hos_shape_is_rejected():
    """price 10.33, prevDay.c 0.30 → 34.4x. The exact 2026-09-02 row."""
    basis, verdict = classify_day_basis(0.30, open_price=10.30, price=10.33)
    assert basis is None
    assert verdict == DAY_BASIS_IMPLAUSIBLE_VS_SESSION


def test_hos_shape_is_rejected_premarket_too():
    """Premarket has no day.o, so the live price is the reference."""
    basis, verdict = classify_day_basis(0.30, open_price=None, price=10.33)
    assert basis is None
    assert verdict == DAY_BASIS_IMPLAUSIBLE_VS_SESSION


def test_sgld_a_real_525_percent_gapper_is_NOT_rejected():
    """SGLD 2026-09-02: prev close 5.08, low 16.76, high 31.78 (+525.6% day).

    A guard that rejects this is worse than the defect. 31.78/5.08 = 6.3x.
    """
    basis, verdict = classify_day_basis(5.08, open_price=31.78, price=28.0)
    assert verdict == DAY_BASIS_OK
    assert basis == 5.08


def test_the_largest_real_mover_in_the_window_is_NOT_rejected():
    """WVVIP 2026-08-25, +946.4% on the day — 10.5x, inside the 20x bound."""
    basis, verdict = classify_day_basis(1.00, open_price=10.46, price=10.46)
    assert verdict == DAY_BASIS_OK
    assert basis == 1.00


def test_close_outside_its_own_prior_bar_is_rejected():
    """A close outside the range it supposedly closed in is a wrong row."""
    basis, verdict = classify_day_basis(
        3.70, open_price=2.20, price=2.60, prev_high=1.80, prev_low=1.55
    )
    assert basis is None
    assert verdict == DAY_BASIS_OUTSIDE_PREV_RANGE


def test_close_inside_its_own_prior_bar_is_accepted():
    basis, verdict = classify_day_basis(
        1.70, open_price=2.20, price=2.60, prev_high=1.80, prev_low=1.55
    )
    assert verdict == DAY_BASIS_OK
    assert basis == 1.70


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan")])
def test_unparseable_basis_is_missing_not_zero(bad):
    basis, verdict = classify_day_basis(bad)
    assert basis is None
    assert verdict == DAY_BASIS_MISSING
    assert verdict not in DAY_BASIS_REJECTED  # caller's own fallback still applies


def test_continuity_detects_a_mid_session_rebase():
    assert basis_continuity_broken(1.70, 3.70) is True
    assert basis_continuity_broken(1.70, 1.72) is False
    assert basis_continuity_broken(None, 3.70) is False


# ── the tracker: a corrupt basis must not become a stamped change ────────────


def _row(ticker, *, prev_c, day_o, price, prev_h=None, prev_l=None):
    return {
        "ticker": ticker,
        "lastTrade": {"p": price},
        "day": {"v": 5_000_000, "h": price, "l": price * 0.95, "o": day_o, "c": price},
        "prevDay": {
            "c": prev_c,
            "v": 4_000_000,
            "h": prev_h if prev_h is not None else prev_c * 1.05,
            "l": prev_l if prev_l is not None else prev_c * 0.95,
        },
    }


@pytest.fixture
def tracker(monkeypatch):
    state: dict = {"snapshot": [], "universe": []}

    import app.services.massive_client as MC

    monkeypatch.setattr(
        MC, "get_full_market_snapshot", lambda *a, **k: state["snapshot"], raising=False
    )
    monkeypatch.setattr(
        IL, "build_equity_universe", lambda _p, *, snapshot=None: list(state["universe"])
    )
    return IL._UniverseTracker(), state


def test_corrupt_basis_yields_no_baseline_so_no_change_is_stamped(tracker, caplog):
    """The HOS row, end to end through the tracker."""
    trk, state = tracker
    state["snapshot"] = [_row("HOS", prev_c=0.30, day_o=10.30, price=10.33)]
    state["universe"] = ["HOS"]

    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        assert trk.refresh() == {"HOS"}

    assert trk.baseline_for("HOS") is None, "the fictional basis was cached"
    assert any("basis REJECTED" in r.message for r in caplog.records)

    quote = type("Q", (), {"last": 10.33, "mid": None, "price": None, "bid": None})()
    loop = object.__new__(IL.IgnitionScoringLoop)
    loop._tracker = trk
    assert loop._move_pct("HOS", quote) is None, "3462% was still computed"


def test_a_healthy_name_still_gets_its_baseline_and_change(tracker):
    trk, state = tracker
    state["snapshot"] = [_row("SGLD", prev_c=5.08, day_o=31.78, price=28.0)]
    state["universe"] = ["SGLD"]
    trk.refresh()

    assert trk.baseline_for("SGLD") == 5.08
    quote = type("Q", (), {"last": 28.0, "mid": None, "price": None, "bid": None})()
    loop = object.__new__(IL.IgnitionScoringLoop)
    loop._tracker = trk
    assert loop._move_pct("SGLD", quote) == pytest.approx(451.18, abs=0.01)


def test_the_alarm_fires_once_not_once_per_refresh(tracker, caplog):
    """HOS produced 798 observations and 9,290 viability writes in one session."""
    trk, state = tracker
    state["snapshot"] = [_row("HOS", prev_c=0.30, day_o=10.30, price=10.33)]
    state["universe"] = ["HOS"]
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        for _ in range(5):
            trk.refresh()
    assert len([r for r in caplog.records if "basis REJECTED" in r.message]) == 1


def test_mid_session_rebase_is_frozen_and_reported(tracker, caplog):
    """A prev close is fixed for the session. Movement is the corruption
    signature (KXIN/CDTG/CAST all show the implied basis moving intraday)."""
    trk, state = tracker
    state["snapshot"] = [_row("JZ", prev_c=1.70, day_o=2.20, price=2.60)]
    state["universe"] = ["JZ"]
    trk.refresh()
    assert trk.baseline_for("JZ") == 1.70

    # Next 20s refresh: the vendor hands back a different close for the same day.
    state["snapshot"] = [
        _row("JZ", prev_c=3.70, day_o=2.20, price=2.60, prev_h=3.9, prev_l=3.5)
    ]
    with caplog.at_level(logging.WARNING, logger=IL.__name__):
        trk.refresh()

    assert trk.baseline_for("JZ") == 1.70, "the lane silently re-based mid-session"
    assert any("basis DRIFTED" in r.message for r in caplog.records)


def test_basis_memory_resets_on_the_et_date_rollover(tracker):
    """A prev close legitimately changes BETWEEN sessions."""
    trk, state = tracker
    state["snapshot"] = [_row("JZ", prev_c=1.70, day_o=2.20, price=2.60)]
    state["universe"] = ["JZ"]
    trk.refresh()

    trk._basis_session_date = "1999-01-01"  # force a rollover
    state["snapshot"] = [
        _row("JZ", prev_c=2.60, day_o=3.00, price=3.20, prev_h=2.7, prev_l=2.5)
    ]
    trk.refresh()
    assert trk.baseline_for("JZ") == 2.60


# ── the second producer: scan_premarket_gaps sorts, then truncates ───────────


def test_premarket_gap_scan_drops_the_fictional_row(monkeypatch, caplog):
    """The persisted HOS row came through THIS path, and because the list is
    sorted by |gap_pct| then truncated, one fiction evicts real gappers."""
    from app.services.trading import intraday_signals as IS

    quotes = {
        "HOS": {"price": 10.33, "previous_close": 0.30},
        "REAL1": {"price": 7.00, "previous_close": 5.00},
        "REAL2": {"price": 6.00, "previous_close": 5.00},
    }
    monkeypatch.setattr(
        IS, "fetch_quote", lambda t: quotes.get(t), raising=False
    )
    import app.services.trading.market_data as MD

    monkeypatch.setattr(MD, "fetch_quote", lambda t: quotes.get(t), raising=False)

    with caplog.at_level(logging.WARNING, logger=IS.__name__):
        out = IS.scan_premarket_gaps(
            tickers=["HOS", "REAL1", "REAL2"], min_gap_pct=5.0, max_signals=2
        )

    tickers = [r["ticker"] for r in out]
    assert "HOS" not in tickers, "a 3,345% fiction took rank #1"
    assert tickers == ["REAL1", "REAL2"]
    assert any("basis REJECTED" in r.message for r in caplog.records)
