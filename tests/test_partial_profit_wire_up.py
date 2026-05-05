"""Tests for f-partial-profit-wire-up.

Covers the 10 cases from the brief:
  1-4. ``compute_live_exit_levels`` action emission semantics around
       ``partial_at_1r`` + ``partial_taken`` + terminal-priority.
  5.   ``run_exit_engine`` separates ``partial_actions`` from terminal
       ``actions`` in the return dict.
  6-8. ``place_partial_close`` paper-mode happy path + already-partialed
       refusal + invalid-fraction refusal.
  9.   End-to-end auto-trader-shaped integration: a synthetic 1R-hit
       paper position flows from ``run_exit_engine`` through
       ``place_partial_close`` and ends up with ``partial_taken=True``,
       reduced quantity, and bookkeeping populated.
  10.  Sanity: ``partial_profit_eligible`` is no longer set anywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.trading import PaperTrade
from app.services.trading import live_exit_engine as lee
from app.services.trading import paper_trading as pt_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_paper_trade(
    db,
    *,
    entry_price: float = 100.0,
    stop_price: float = 95.0,
    quantity: float = 10.0,
    direction: str = "long",
    partial_taken: bool = False,
    entry_offset_days: int = 1,
) -> PaperTrade:
    pt = PaperTrade(
        ticker="TEST",
        direction=direction,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=entry_price * 1.10,
        quantity=quantity,
        status="open",
        entry_date=datetime.utcnow() - timedelta(days=entry_offset_days),
        partial_taken=partial_taken,
    )
    db.add(pt)
    db.commit()
    db.refresh(pt)
    return pt


def _stub_external_market_data(monkeypatch, atr_value: float = 1.0) -> None:
    """Replace fetch_ohlcv_df + compute_atr so compute_live_exit_levels can run offline.

    Returns a synthetic OHLCV frame long enough for ATR (>=14 bars) and
    swing-low (>=5 bars) computation, plus a constant ATR series so the
    trail / BOS branches don't crash on missing data.
    """
    import pandas as pd

    def _fake_fetch_ohlcv_df(ticker, period=None, interval=None, start=None, end=None):
        idx = pd.date_range("2026-01-01", periods=30, freq="D")
        return pd.DataFrame(
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
             "Volume": 1_000_000},
            index=idx,
        )

    def _fake_compute_atr(highs, lows, closes, period=14):
        import numpy as np
        return np.array([atr_value] * len(closes))

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_ohlcv_df",
        _fake_fetch_ohlcv_df,
    )
    monkeypatch.setattr(
        "app.services.trading.indicator_core.compute_atr",
        _fake_compute_atr,
    )


# ---------------------------------------------------------------------------
# 1. action="partial" emitted on 1R hit when no terminal would fire
# ---------------------------------------------------------------------------

def test_compute_live_exit_levels_emits_partial_at_1r(db, monkeypatch):
    _stub_external_market_data(monkeypatch)
    pt = _seed_paper_trade(
        db, entry_price=100.0, stop_price=95.0, quantity=10.0,
    )
    # exit_config with partial_at_1r=True passed in via _load_exit_config
    # (no scan_pattern_id needed; defaults dict is augmented via monkeypatch).
    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["partial_at_1r"] = True
        cfg["partial_close_fraction"] = 0.5
        # Disable max_bars + BOS so terminal exits don't preempt the partial.
        cfg["max_bars"] = 9999
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)

    # Risk = 100 - 95 = 5; price 105 = exactly 1R.
    result = lee.compute_live_exit_levels(db, pt, current_price=105.0)
    assert result["action"] == "partial", result
    assert result.get("r_multiple") == 1.0
    assert result.get("partial_close_fraction") == 0.5
    assert result.get("exit_price") == 105.0


# ---------------------------------------------------------------------------
# 2. No partial when partial_at_1r=False
# ---------------------------------------------------------------------------

def test_compute_live_exit_levels_no_partial_when_disabled(db, monkeypatch):
    _stub_external_market_data(monkeypatch)
    pt = _seed_paper_trade(db, entry_price=100.0, stop_price=95.0)

    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["partial_at_1r"] = False  # explicit disable
        cfg["max_bars"] = 9999
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)

    # 1R hit but partial gate is off -> hold.
    result = lee.compute_live_exit_levels(db, pt, current_price=105.0)
    assert result["action"] == "hold"
    assert "partial_close_fraction" not in result


# ---------------------------------------------------------------------------
# 3. Terminal exit takes precedence over partial on same bar
# ---------------------------------------------------------------------------

def test_compute_live_exit_levels_terminal_preempts_partial(db, monkeypatch):
    _stub_external_market_data(monkeypatch)
    # entry 100, stop 95, target 110. Price 110 = 2R at target -> exit_target.
    pt = _seed_paper_trade(
        db, entry_price=100.0, stop_price=95.0, quantity=10.0,
    )
    pt.target_price = 110.0
    db.commit()

    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["partial_at_1r"] = True
        cfg["partial_close_fraction"] = 0.5
        cfg["max_bars"] = 9999
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)

    result = lee.compute_live_exit_levels(db, pt, current_price=110.0)
    assert result["action"] == "exit_target", result


# ---------------------------------------------------------------------------
# 4. No re-fire when partial_taken=True
# ---------------------------------------------------------------------------

def test_compute_live_exit_levels_no_refire_when_partial_taken(db, monkeypatch):
    _stub_external_market_data(monkeypatch)
    pt = _seed_paper_trade(
        db, entry_price=100.0, stop_price=95.0, partial_taken=True,
    )

    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["partial_at_1r"] = True
        cfg["max_bars"] = 9999
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)

    # Even at 2R, already-partialed trade should hold.
    result = lee.compute_live_exit_levels(db, pt, current_price=109.0)
    assert result["action"] == "hold"


# ---------------------------------------------------------------------------
# 5. run_exit_engine separates partial_actions from terminal actions
# ---------------------------------------------------------------------------

def test_run_exit_engine_separates_partial_from_terminal(db, monkeypatch):
    _stub_external_market_data(monkeypatch)
    # Two paper trades:
    #   pt_partial: at 1R, partial_at_1r=True -> partial bucket
    #   pt_terminal: blown stop -> terminal bucket
    pt_partial = _seed_paper_trade(
        db, entry_price=100.0, stop_price=95.0, quantity=10.0,
    )
    pt_partial.ticker = "PARTIAL"
    pt_terminal = _seed_paper_trade(
        db, entry_price=100.0, stop_price=95.0, quantity=10.0,
    )
    pt_terminal.ticker = "TERMINAL"
    db.commit()

    def _quote_router(ticker):
        if ticker == "PARTIAL":
            return {"price": 105.0}  # 1R hit
        if ticker == "TERMINAL":
            return {"price": 94.0}   # below stop
        return None

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote", _quote_router,
    )

    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["partial_at_1r"] = True
        cfg["partial_close_fraction"] = 0.5
        cfg["max_bars"] = 9999
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)

    out = lee.run_exit_engine(db)
    assert out["ok"] is True
    terminal_tickers = {a["ticker"] for a in out["actions"]}
    partial_tickers = {a["ticker"] for a in out["partial_actions"]}
    assert "TERMINAL" in terminal_tickers
    assert "PARTIAL" in partial_tickers
    assert "PARTIAL" not in terminal_tickers
    assert "TERMINAL" not in partial_tickers


# ---------------------------------------------------------------------------
# 6. place_partial_close happy path
# ---------------------------------------------------------------------------

def test_place_partial_close_happy_path(db):
    pt = _seed_paper_trade(db, quantity=10.0)
    out = pt_mod.place_partial_close(db, pt, fraction=0.5, current_price=105.0)
    assert out["ok"] is True
    assert out["quantity"] == pytest.approx(5.0)
    db.refresh(pt)
    assert pt.partial_taken is True
    assert pt.partial_taken_qty == pytest.approx(5.0)
    assert pt.partial_taken_price is not None
    assert pt.partial_taken_at is not None
    assert pt.quantity == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# 7. place_partial_close refuses already-partialed
# ---------------------------------------------------------------------------

def test_place_partial_close_refuses_already_partialed(db):
    pt = _seed_paper_trade(db, quantity=10.0, partial_taken=True)
    out = pt_mod.place_partial_close(db, pt, fraction=0.5, current_price=105.0)
    assert out["ok"] is False
    assert out["error"] == "already_partialed"


# ---------------------------------------------------------------------------
# 8. place_partial_close refuses invalid fraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_place_partial_close_refuses_invalid_fraction(db, bad):
    pt = _seed_paper_trade(db, quantity=10.0)
    out = pt_mod.place_partial_close(db, pt, fraction=bad, current_price=105.0)
    assert out["ok"] is False
    assert out["error"].startswith("invalid_fraction:")


# ---------------------------------------------------------------------------
# 9. End-to-end: 1R-hit paper trade flows through run_exit_engine + place_partial_close
# ---------------------------------------------------------------------------

def test_end_to_end_partial_flow(db, monkeypatch):
    _stub_external_market_data(monkeypatch)
    pt = _seed_paper_trade(db, entry_price=100.0, stop_price=95.0, quantity=10.0)

    monkeypatch.setattr(
        "app.services.trading.market_data.fetch_quote",
        lambda ticker: {"price": 105.0},
    )

    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["partial_at_1r"] = True
        cfg["partial_close_fraction"] = 0.5
        cfg["max_bars"] = 9999
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)

    out = lee.run_exit_engine(db)
    assert len(out["partial_actions"]) == 1
    rec = out["partial_actions"][0]
    assert rec["position_id"] == pt.id
    assert rec["partial_close_fraction"] == 0.5

    outcome = pt_mod.place_partial_close(
        db, pt, fraction=rec["partial_close_fraction"],
        current_price=rec["current_price"],
    )
    assert outcome["ok"] is True

    db.refresh(pt)
    assert pt.partial_taken is True
    assert pt.partial_taken_qty == pytest.approx(5.0)
    assert pt.quantity == pytest.approx(5.0)
    assert pt.status == "open"  # remaining position keeps running

    # Re-running run_exit_engine on the same trade must NOT re-fire.
    out2 = lee.run_exit_engine(db)
    assert all(
        a["position_id"] != pt.id for a in out2.get("partial_actions", [])
    )


# ---------------------------------------------------------------------------
# 10. partial_profit_eligible flag is no longer set
# ---------------------------------------------------------------------------

def test_partial_profit_eligible_flag_removed(db, monkeypatch):
    """The legacy informational flag was dead (zero readers); ensure it
    no longer appears in the result dict so any future grep doesn't
    rediscover it as ambient noise.
    """
    _stub_external_market_data(monkeypatch)
    pt = _seed_paper_trade(db, entry_price=100.0, stop_price=95.0)

    orig_load = lee._load_exit_config

    def _patched_load(db_, sp_id):
        cfg = orig_load(db_, sp_id)
        cfg["partial_at_1r"] = True
        cfg["max_bars"] = 9999
        cfg["use_bos"] = False
        return cfg

    monkeypatch.setattr(lee, "_load_exit_config", _patched_load)

    result = lee.compute_live_exit_levels(db, pt, current_price=105.0)
    assert "partial_profit_eligible" not in result
