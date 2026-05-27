from __future__ import annotations

from app.services.trading import intraday_signals


class _DummyDb:
    pass


def test_auto_paper_candidates_rank_confident_crypto_when_stocks_closed():
    signals = [
        {
            "ticker": "AAPL",
            "price": 100.0,
            "confidence": 0.90,
            "signal_type": "orb_breakout",
            "breakout_pct": 2.0,
        },
        {
            "ticker": "BTC-USD",
            "price": 100.0,
            "confidence": 0.70,
            "signal_type": "momentum_continuation",
            "momentum_pct": 1.5,
        },
        {
            "ticker": "ETH-USD",
            "price": 100.0,
            "confidence": 0.50,
            "signal_type": "momentum_continuation",
            "momentum_pct": 2.0,
        },
    ]

    selected, diag = intraday_signals._auto_paper_candidates(
        signals,
        stock_session_open=False,
    )

    assert [sig["ticker"] for sig in selected] == ["BTC-USD"]
    assert diag["auto_paper_candidates_considered"] == 3
    assert diag["auto_paper_candidates_eligible"] == 1
    assert diag["auto_paper_candidates_selected"] == 1
    assert diag["auto_paper_skip_reasons"] == {
        "stock_session_closed": 1,
        "below_confidence_floor": 1,
    }


def test_auto_paper_candidates_rank_by_confidence_priority_and_strength():
    signals = [
        {
            "ticker": "AAPL",
            "price": 100.0,
            "confidence": 0.70,
            "signal_type": "premarket_gap",
            "gap_pct": 8.0,
        },
        {
            "ticker": "MSFT",
            "price": 100.0,
            "confidence": 0.70,
            "signal_type": "momentum_continuation",
            "momentum_pct": 1.1,
        },
        {
            "ticker": "NVDA",
            "price": 100.0,
            "confidence": 0.80,
            "signal_type": "orb_breakout",
            "breakout_pct": 1.0,
        },
    ]

    selected, diag = intraday_signals._auto_paper_candidates(
        signals,
        stock_session_open=True,
        max_candidates=2,
    )

    assert [sig["ticker"] for sig in selected] == ["NVDA", "MSFT"]
    assert diag["auto_paper_candidates_eligible"] == 3
    assert diag["auto_paper_candidates_selected"] == 2


def test_run_intraday_sweep_reports_auto_paper_diagnostics(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        intraday_signals,
        "scan_premarket_gaps",
        lambda: [
            {
                "ticker": "AAPL",
                "price": 100.0,
                "confidence": 0.90,
                "signal_type": "premarket_gap",
                "gap_pct": 4.0,
            }
        ],
    )
    monkeypatch.setattr(
        intraday_signals,
        "scan_opening_range_breakout",
        lambda: [],
    )
    monkeypatch.setattr(
        intraday_signals,
        "scan_momentum_continuation",
        lambda db=None: [
            {
                "ticker": "BTC-USD",
                "price": 100.0,
                "confidence": 0.75,
                "signal_type": "momentum_continuation",
                "momentum_pct": 1.2,
            }
        ],
    )
    monkeypatch.setattr(
        intraday_signals,
        "_stock_auto_paper_session_open",
        lambda: False,
    )

    def _fake_auto_enter(db, user_id, signals):
        captured["signals"] = signals
        return len(signals)

    from app.services.trading import paper_trading

    monkeypatch.setattr(paper_trading, "auto_enter_from_signals", _fake_auto_enter)

    out = intraday_signals.run_intraday_signal_sweep(
        _DummyDb(),
        user_id=1,
        auto_paper=True,
    )

    assert out["paper_entered"] == 1
    assert out["auto_paper_candidates_considered"] == 2
    assert out["auto_paper_candidates_eligible"] == 1
    assert out["auto_paper_skip_reasons"] == {"stock_session_closed": 1}
    assert [sig["ticker"] for sig in captured["signals"]] == ["BTC-USD"]


def test_scanner_confidence_scores_cross_paper_floor_for_strong_setups():
    gap_conf = intraday_signals._score_premarket_gap_confidence(
        gap_pct=4.0,
        min_gap_pct=intraday_signals.DEFAULT_PREMARKET_MIN_GAP_PCT,
    )
    orb_conf = intraday_signals._score_orb_confidence(breakout_pct=1.0)
    momentum_conf = intraday_signals._score_momentum_confidence(
        momentum_pct=1.5,
        rvol=1.2,
        rvol_min=0.8,
        pullback_pct=0.2,
    )

    assert gap_conf >= intraday_signals.INTRADAY_AUTO_PAPER_MIN_CONFIDENCE
    assert orb_conf >= intraday_signals.INTRADAY_AUTO_PAPER_MIN_CONFIDENCE
    assert momentum_conf >= intraday_signals.INTRADAY_AUTO_PAPER_MIN_CONFIDENCE
