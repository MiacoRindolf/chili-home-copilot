"""Crypto $-volume liquidity re-rank (selection fillability).

Mirrors the equity _liquidity_rerank for the crypto lane: among names that clear
the binary liquidity floor, arm the deepest 24h-turnover (most fillable) first by
blending ross/viability rank with dollar-volume rank. Pure (stub rows, no DB) —
exercises the rank-blend, fail-open, mixed-lane safety, and the bias toggle.
"""

from __future__ import annotations

from app.config import settings
from app.services.trading.momentum_neural.auto_arm import _crypto_liquidity_rerank


class _Row:
    """Minimal stand-in for a MomentumSymbolViability row (only the fields the
    re-rank reads: symbol + the ross_signals turnover datum)."""

    def __init__(self, symbol: str, qv: float | None, viability: float = 0.5):
        self.symbol = symbol
        self.viability_score = viability
        extra_sig = {} if qv is None else {symbol.upper(): {"quote_volume_24h": qv}}
        self.execution_readiness_json = {"extra": {"ross_signals": extra_sig}}


def _syms(rows):
    return [r.symbol for r in rows]


def test_rerank_prefers_deepest_turnover():
    # ross order A,B,C; B+C far deeper than the thin ross-top A -> a deep name arms first
    a = _Row("AAA-USD", 1_000_000)     # ross #0 but thin ($1M)
    b = _Row("BBB-USD", 100_000_000)   # ross #1, deep ($100M)
    c = _Row("CCC-USD", 50_000_000)    # ross #2, mid ($50M)
    out = _crypto_liquidity_rerank([a, b, c])
    # blend: A=0+2=2, B=1+0=1, C=2+1=3 -> B first (deep), then A, then C
    assert out[0] is b
    assert _syms(out) == ["BBB-USD", "AAA-USD", "CCC-USD"]


def test_rerank_keeps_order_when_turnover_tracks_ross():
    # ross-top is also the deepest -> no change
    a = _Row("AAA-USD", 100_000_000)
    b = _Row("BBB-USD", 10_000_000)
    c = _Row("CCC-USD", 1_000_000)
    out = _crypto_liquidity_rerank([a, b, c])
    assert out[0] is a


def test_fail_open_when_no_turnover_data():
    a = _Row("AAA-USD", None)
    b = _Row("BBB-USD", None)
    out = _crypto_liquidity_rerank([a, b])
    assert out == [a, b]  # missing data -> unchanged (fail-open)


def test_noop_when_fewer_than_two_crypto():
    a = _Row("AAA-USD", 5_000_000)
    out = _crypto_liquidity_rerank([a])
    assert out == [a]


def test_non_crypto_rows_untouched_in_mixed():
    # mixed list: equity row among crypto -> only crypto slots re-ranked, equity
    # keeps its position. (3 crypto so the rank-blend actually reorders — with 2
    # the vrank+drank blend always ties.)
    a = _Row("AAA-USD", 1_000_000)     # crypto ross#0, thin
    eq = _Row("TSLA", 999_999_999)     # equity (no -USD) -> must NOT move
    b = _Row("BBB-USD", 100_000_000)   # crypto ross#1, deep
    c = _Row("CCC-USD", 50_000_000)    # crypto ross#2, mid
    out = _crypto_liquidity_rerank([a, eq, b, c])
    assert out[1] is eq                                  # equity slot untouched
    crypto_out = [out[0], out[2], out[3]]                # the crypto slots
    assert crypto_out[0] is b                            # deepest blend armed first
    assert {r.symbol for r in crypto_out} == {"AAA-USD", "BBB-USD", "CCC-USD"}


def test_disabled_when_liquidity_bias_off(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_liquidity_bias", False)
    a = _Row("AAA-USD", 1_000_000)
    b = _Row("BBB-USD", 100_000_000)
    out = _crypto_liquidity_rerank([a, b])
    assert out == [a, b]  # bias off -> unchanged
