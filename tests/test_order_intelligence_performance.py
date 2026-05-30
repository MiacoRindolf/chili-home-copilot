from __future__ import annotations

import io
import json

from app.services.trading import order_intelligence


class _StreamingFillLog:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.open_calls = 0

    def exists(self) -> bool:
        return True

    def open(self, *args: object, **kwargs: object) -> io.StringIO:
        self.open_calls += 1
        return io.StringIO("".join(self.lines))

    def read_text(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("execution quality report should stream the fill log")


def _line(ticker: str, slippage_bps: float, order_type: str = "limit", hour: int = 9) -> str:
    return json.dumps(
        {
            "ticker": ticker,
            "slippage_bps": slippage_bps,
            "order_type": order_type,
            "hour_of_day": hour,
        }
    ) + "\n"


def test_execution_quality_report_streams_and_bounds_matching_fills(monkeypatch) -> None:
    log = _StreamingFillLog(
        [
            _line("ABC", 1.0),
            _line("XYZ", 99.0),
            "not-json\n",
            _line("ABC", 2.0),
            _line("ABC", 3.0, order_type="market", hour=10),
        ]
    )
    monkeypatch.setattr(order_intelligence, "_FILL_LOG", log)

    report = order_intelligence.get_execution_quality_report(ticker="ABC", limit=2)

    assert log.open_calls == 1
    assert report["fills"] == 2
    assert report["avg_slippage_bps"] == 2.5
    assert report["p90_slippage_bps"] == 3.0
    assert report["by_order_type"] == {
        "limit": {"avg_bps": 2.0, "n": 1},
        "market": {"avg_bps": 3.0, "n": 1},
    }
    assert report["by_hour"] == {
        9: {"avg_bps": 2.0, "n": 1},
        10: {"avg_bps": 3.0, "n": 1},
    }
