from __future__ import annotations

from app.services.trading.reproducibility import _extract_sharpe


def test_extract_sharpe_keeps_zero_return_pct_over_legacy_pnl_pct() -> None:
    result = _extract_sharpe(
        {
            "ok": True,
            "trades": [
                {"return_pct": 0.0, "pnl_pct": 99.0},
                {"return_pct": 1.0, "pnl_pct": 1.0},
                {"return_pct": -1.0, "pnl_pct": -1.0},
            ],
        }
    )

    assert result == 0.0


def test_extract_sharpe_falls_back_to_pnl_pct_when_return_missing() -> None:
    result = _extract_sharpe(
        {
            "ok": True,
            "trades": [
                {"return_pct": None, "pnl_pct": 1.0},
                {"pnl_pct": -1.0},
            ],
        }
    )

    assert result == 0.0
