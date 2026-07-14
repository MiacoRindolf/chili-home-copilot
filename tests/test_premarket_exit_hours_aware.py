"""Hours-aware equity exit (2026-06-16 — BEEM/AHMA stranded-long bug).

In premarket/after-hours Robinhood REJECTS a regular-hours order, so a premarket
equity entry whose stop breached could not be flattened — the sell never placed →
retry-cap → live_error → naked long with no working exit. The reactive exit
(`_submit_live_market_exit`) was hours-blind while the entry/scale-out already pass
the ext-hours overrides. These tests pin the fix AND the parity contract:

  * equity premarket   → marketable LIMIT carrying extended_hours=True +
                         market_hours_override='extended_hours' + extended_hours_override
                         (NEVER a bare market order, even urgent / attempt-3+).
  * equity regular hrs → BYTE-IDENTICAL to before (no ext kwargs on the call).
  * crypto (coinbase)  → BYTE-IDENTICAL (the RH-only overrides are never passed —
                         the coinbase adapter would not accept them).
"""

from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
)


_EXT_KEYS = ("market_hours_override", "extended_hours_override")


class _FakeAdapter:
    """Records the exact kwargs the runner passes to each order method."""

    def __init__(self):
        self.calls = []

    def get_position_quantity(self, product_id):
        return 1000.0  # >= requested qty → no broker-clamp

    def place_limit_order_gtc(self, **kwargs):
        self.calls.append(("limit", kwargs))
        return {"ok": True, "order_id": "lim_1", "client_order_id": kwargs.get("client_order_id")}

    def place_market_order(self, **kwargs):
        self.calls.append(("market", kwargs))
        return {"ok": True, "order_id": "mkt_1", "client_order_id": kwargs.get("client_order_id")}


def _run_exit(monkeypatch, *, family, symbol, session, reason="stop_hit", attempts=0, qty=100.0):
    """Drive _submit_live_market_exit with all DB-touching helpers stubbed out and the
    equity session forced to `session`. Returns (adapter, result, le)."""
    # Stub the DB-touching collaborators (pure unit test — no live DB / broker).
    monkeypatch.setattr(lr, "_commit_le", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        lr, "_cancel_scale_limit_and_clamp", lambda db, sess, adapter, *, le, requested_qty, reason: requested_qty
    )
    # Force the equity session classification (crypto always returns "regular").
    monkeypatch.setattr(lr_market_profile(), "market_session_now", lambda *a, **k: session)

    adapter = _FakeAdapter()
    sess = SimpleNamespace(
        id=1, symbol=symbol, execution_family=family, correlation_id="testcorr",
    )
    le = {"exit_submit_attempts": attempts}
    result = lr._submit_live_market_exit(
        None, sess, adapter,
        le=le, product_id=symbol, quantity=qty, client_order_id="cid_1",
        reason=reason, bid=5.0, ask=5.10, mid=5.05,
    )
    return adapter, result, le


def lr_market_profile():
    from app.services.trading.momentum_neural import market_profile
    return market_profile


# ---------------------------------------------------------------------------
# THE FIX: equity premarket must place an EXTENDED-HOURS LIMIT, never a bare market
# ---------------------------------------------------------------------------

def test_equity_premarket_exit_uses_extended_hours_limit(monkeypatch):
    adapter, result, le = _run_exit(
        monkeypatch, family=EXECUTION_FAMILY_ROBINHOOD_SPOT, symbol="BEEM", session="premarket",
    )
    assert result.get("ok")
    assert len(adapter.calls) == 1
    kind, kwargs = adapter.calls[0]
    assert kind == "limit", "premarket equity exit must be a LIMIT (RH rejects market in ext-hours)"
    assert kwargs.get("extended_hours") is True
    assert kwargs.get("market_hours_override") == "extended_hours"
    assert kwargs.get("extended_hours_override") is True
    assert le.get("exit_session_extended") is True


@pytest.mark.parametrize(
    ("family", "symbol", "session"),
    (
        (EXECUTION_FAMILY_ROBINHOOD_SPOT, "BEEM", "premarket"),
        (EXECUTION_FAMILY_COINBASE_SPOT, "TAO-USD", "regular"),
    ),
)
def test_non_alpaca_exit_never_reads_alpaca_deadman_owner_claim(
    monkeypatch,
    family,
    symbol,
    session,
):
    def _unexpected_alpaca_claim_read(*_args, **_kwargs):
        raise AssertionError("non-Alpaca exit must not read Alpaca owner claims")

    monkeypatch.setattr(
        lr,
        "_read_exact_alpaca_deadman_handoff",
        _unexpected_alpaca_claim_read,
    )

    adapter, result, _le = _run_exit(
        monkeypatch,
        family=family,
        symbol=symbol,
        session=session,
    )

    assert result.get("ok")
    assert len(adapter.calls) == 1


def test_equity_premarket_urgent_flatten_still_limit_not_market(monkeypatch):
    # Urgent flattens normally skip the limit ladder (want OUT now via market). In
    # extended hours a market is rejected, so the fix must STILL force a limit.
    adapter, result, le = _run_exit(
        monkeypatch, family=EXECUTION_FAMILY_ROBINHOOD_SPOT, symbol="AHMA",
        session="premarket", reason="kill_switch_flatten",
    )
    kind, kwargs = adapter.calls[0]
    assert kind == "limit"
    assert kwargs.get("extended_hours") is True
    assert all(kwargs.get(k) for k in _EXT_KEYS)


def test_equity_premarket_attempt3_forces_limit_not_market(monkeypatch):
    # attempts becomes 3 → the normal ladder would fall through to a MARKET order.
    # In extended hours the fix forces a hard-crossed limit instead.
    adapter, result, le = _run_exit(
        monkeypatch, family=EXECUTION_FAMILY_ROBINHOOD_SPOT, symbol="BEEM",
        session="premarket", attempts=2,
    )
    kind, kwargs = adapter.calls[0]
    assert kind == "limit", "attempt-3+ equity exit in ext-hours must NOT be a bare market"
    assert all(kwargs.get(k) for k in _EXT_KEYS)


def test_equity_afterhours_exit_uses_extended_hours_limit(monkeypatch):
    adapter, result, le = _run_exit(
        monkeypatch, family=EXECUTION_FAMILY_ROBINHOOD_SPOT, symbol="BEEM", session="afterhours",
    )
    kind, kwargs = adapter.calls[0]
    assert kind == "limit"
    assert kwargs.get("extended_hours") is True
    assert all(kwargs.get(k) for k in _EXT_KEYS)


# ---------------------------------------------------------------------------
# PARITY: regular-hours equity + crypto must be BYTE-IDENTICAL (no ext kwargs)
# ---------------------------------------------------------------------------

def test_equity_regular_hours_exit_has_no_ext_kwargs(monkeypatch):
    adapter, result, le = _run_exit(
        monkeypatch, family=EXECUTION_FAMILY_ROBINHOOD_SPOT, symbol="BEEM", session="regular",
    )
    kind, kwargs = adapter.calls[0]
    # Regular hours: unchanged ladder — attempt 1 is a limit at bid−guard, but with
    # NONE of the extended-hours kwargs (parity with pre-fix behavior).
    assert "extended_hours" not in kwargs
    assert "market_hours_override" not in kwargs
    assert "extended_hours_override" not in kwargs
    assert le.get("exit_session_extended") is None


def test_crypto_exit_has_no_ext_kwargs(monkeypatch):
    # Crypto (coinbase) must never receive the RH-only overrides — the adapter would
    # not accept them. _exit_extended is gated on robinhood_spot, so this is safe.
    adapter, result, le = _run_exit(
        monkeypatch, family=EXECUTION_FAMILY_COINBASE_SPOT, symbol="TAO-USD", session="regular",
    )
    kind, kwargs = adapter.calls[0]
    assert "extended_hours" not in kwargs
    assert "market_hours_override" not in kwargs
    assert "extended_hours_override" not in kwargs


def test_crypto_premarket_classification_irrelevant(monkeypatch):
    # Even if (hypothetically) the session classifier returned non-regular for a
    # crypto symbol, the family gate keeps the exit byte-identical.
    adapter, result, le = _run_exit(
        monkeypatch, family=EXECUTION_FAMILY_COINBASE_SPOT, symbol="TAO-USD", session="premarket",
    )
    kind, kwargs = adapter.calls[0]
    assert "market_hours_override" not in kwargs
    assert "extended_hours_override" not in kwargs
