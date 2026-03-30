"""Regime-gated motif checks for pattern mining (equity regime from SPY path)."""
from __future__ import annotations

from typing import Any, Callable

# (filter callable on all_rows, label suffix)
_REGIME_MOTIFS: list[tuple[str, Callable[[dict[str, Any]], bool], str]] = [
    (
        "risk_on",
        lambda r: r["macd"] > r["macd_sig"] and r["macd_hist"] > 0 and 40 <= r["rsi"] <= 65,
        "MACD+ hist>0 + RSI 40–65 (trend continuation)",
    ),
    (
        "risk_on",
        lambda r: r.get("ema_stack") and r["adx"] > 22,
        "EMA stack + ADX>22",
    ),
    (
        "risk_off",
        lambda r: r["rsi"] < 38 and r["bb_pct"] < 0.2 and r["macd_hist"] > 0,
        "Oversold + lower BB + MACD hist positive (bounce)",
    ),
    (
        "risk_off",
        lambda r: r["adx"] > 28 and r["stoch_k"] < 22 and r.get("ema_stack"),
        "Strong trend pullback: ADX>28 + Stoch<22 + EMA stack",
    ),
]


def run_regime_gated_mining_checks(
    all_rows: list[dict[str, Any]],
    check_fn: Callable[[list[dict[str, Any]], str], None],
    *,
    min_regime_rows: int = 12,
) -> None:
    """Invoke ``check_fn(filtered, full_label)`` for each regime × motif."""
    for regime, pred, label in _REGIME_MOTIFS:
        sub = [r for r in all_rows if r.get("regime") == regime]
        if len(sub) < min_regime_rows:
            continue
        filtered = [r for r in sub if pred(r)]
        full_label = f"Regime {regime}: {label}"
        check_fn(filtered, full_label)
