"""Unit tests for AI Analyze chart_levels extraction (trading router helpers)."""

from app.routers.trading import (
    _extract_chart_levels,
    _normalize_chart_levels,
    _strip_chart_levels_block,
)


def test_extract_and_strip_chart_levels():
    text = """Some verdict text.

```json:chart_levels
{
  "entry": 185.5,
  "stop": 180.0,
  "targets": [190, 195.5],
  "support": [182],
  "resistance": [188, 200],
  "sma_20": 184,
  "vwap": 183.25
}
```
"""
    ann = _extract_chart_levels(text)
    assert ann is not None
    assert ann["entry"] == 185.5
    assert ann["stop"] == 180.0
    assert ann["targets"] == [190.0, 195.5]
    assert ann["support"] == [182.0]
    assert ann["resistance"] == [188.0, 200.0]
    assert ann["sma_20"] == 184.0
    assert ann["vwap"] == 183.25

    stripped = _strip_chart_levels_block(text)
    assert "json:chart_levels" not in stripped
    assert "Some verdict" in stripped


def test_normalize_drops_unknown_and_strings():
    raw = {
        "entry": 10,
        "bogus": "x",
        "targets": [1, "bad", 2],
        "support": [],
    }
    out = _normalize_chart_levels(raw)
    assert out == {"entry": 10.0, "targets": [1.0, 2.0]}


def test_extract_invalid_json_returns_none():
    assert _extract_chart_levels("```json:chart_levels\n{not json}\n```") is None
