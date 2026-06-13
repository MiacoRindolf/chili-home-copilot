"""Crypto liquidity floor (2026-06-13 crypto-live plan, A1).

The Ross scorer ranked CHECK-USD (RVOL 34, $24k/24h) and T-USD (RVOL 5.2,
$43k/24h) alongside DOGE ($20.8M/24h). These pin the executability gate that
the scorer lacks — data-driven on turnover, no hardcoded ticker list.
"""

from types import SimpleNamespace

from app.services.trading.momentum_neural.crypto_liquidity import crypto_liquidity_ok


def _via(signals: dict):
    return SimpleNamespace(
        execution_readiness_json={"extra": {"ross_signals": signals}}
    )


def test_liquid_name_passes_with_a_size_cap():
    via = _via({"DOGE-USD": {"quote_volume_24h": 20_841_871.0}})
    ok, detail, cap = crypto_liquidity_ok("DOGE-USD", via)
    assert ok is True
    assert detail["liquidity_gate"] == "ok"
    # cap = 0.5 * (qv24h / 1440) = 0.5 * 14473.5 ≈ $7,236
    assert abs(cap - (0.5 * 20_841_871.0 / 1440.0)) < 1e-6


def test_toxic_thin_name_is_blocked():
    # CHECK-USD: RVOL 34 but only $24,551/24h — unexecutable.
    via = _via({"CHECK-USD": {"quote_volume_24h": 24_551.0, "rvol": 34.41}})
    ok, detail, cap = crypto_liquidity_ok("CHECK-USD", via)
    assert ok is False
    assert detail["reason"] == "quote_volume_below_floor"
    assert cap is None


def test_missing_turnover_fails_closed_with_distinct_reason():
    via = _via({"FOO-USD": {"rvol": 5.0}})  # no quote_volume_24h
    ok, detail, cap = crypto_liquidity_ok("FOO-USD", via)
    assert ok is False
    assert detail["reason"] == "liquidity_data_missing"  # data outage != thin name


def test_equity_symbol_is_not_gated():
    ok, detail, cap = crypto_liquidity_ok("AAPL", _via({}))
    assert ok is True
    assert detail["liquidity_gate"] == "n/a_equity"
    assert cap is None


def test_wide_spread_blocks_when_probe_enabled():
    via = _via({"WIDE-USD": {"quote_volume_24h": 5_000_000.0}})
    wide = SimpleNamespace(spread_bps=80.0)
    tight = SimpleNamespace(spread_bps=10.0)

    class _Adapter:
        def __init__(self, tick):
            self._tick = tick

        def get_best_bid_ask(self, product_id):
            return self._tick, None

    ok_wide, d_wide, _ = crypto_liquidity_ok("WIDE-USD", via, adapter=_Adapter(wide))
    ok_tight, d_tight, cap_tight = crypto_liquidity_ok("WIDE-USD", via, adapter=_Adapter(tight))
    assert ok_wide is False and d_wide["reason"] == "spread_above_floor"
    assert ok_tight is True and cap_tight > 0


def test_spread_probe_failure_falls_open_to_volume_gate():
    via = _via({"OK-USD": {"quote_volume_24h": 5_000_000.0}})

    class _Boom:
        def get_best_bid_ask(self, product_id):
            raise RuntimeError("venue down")

    ok, detail, cap = crypto_liquidity_ok("OK-USD", via, adapter=_Boom())
    assert ok is True  # the $-volume floor is the primary gate
    assert cap > 0
