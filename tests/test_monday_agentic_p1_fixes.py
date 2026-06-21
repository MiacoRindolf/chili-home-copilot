"""Monday agentic-rail readiness — the 5 P1 fixes from the pre-launch audit.

Pure-unit (no DB, no network). Each test pins one defect that, on the live agentic
equity path, would have wasted a Monday trade or frozen the lane:

  SR-1  daily-loss cap basis follows the ACTIVE execution rail (agentic $13,800 -> ~$690),
        not the drained legacy robinhood_spot account (~$950 -> ~$95 = freeze after 1 trade)
  FSM-1 the venue factory resolves an adapter for the agentic rail (orphan-safety net)
  AOP-1 get_order/cancel_order send NO schema-forbidden "id" key (only "order_id")
  AOP-2 a place response with no resolvable order_id FAILS the place (re-watch) vs orphan
  AOP-4 a get_product miss defaults equity to WHOLE shares (no fractional-limit reject)
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.venue.robinhood_mcp import RobinhoodAgenticMcpAdapter

_ACCT = "674153143"


class _Res:
    """Minimal McpToolResult stand-in: only .data() + .raw are read by _order_result."""

    def __init__(self, payload, raw=None):
        self._payload = payload
        self.raw = raw if raw is not None else {}

    def data(self):
        return self._payload


# ── SR-1: the freeze fix ──────────────────────────────────────────────────────


def test_lane_family_agentic_when_rail_agentic(monkeypatch):
    from app.services.trading.momentum_neural.auto_arm import _lane_execution_family
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", False)
    monkeypatch.setattr(settings, "chili_equity_execution_rail", "robinhood_agentic_mcp")
    assert _lane_execution_family() == "robinhood_agentic_mcp"


def test_lane_family_spot_when_rail_spot(monkeypatch):
    from app.services.trading.momentum_neural.auto_arm import _lane_execution_family
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", False)
    monkeypatch.setattr(settings, "chili_equity_execution_rail", "robinhood_spot")
    assert _lane_execution_family() == "robinhood_spot"


def test_lane_family_coinbase_when_crypto_only(monkeypatch):
    from app.services.trading.momentum_neural.auto_arm import _lane_execution_family
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", True)
    assert _lane_execution_family() == "coinbase_spot"


def test_daily_loss_cap_basis_follows_agentic_rail(monkeypatch):
    """THE freeze proof: with the agentic rail, the lane's daily-loss cap is computed off
    the $13,800 agentic account, not the ~$950 legacy account. The legacy basis capped the
    day at ~$95 — below a single ~$138 per-trade risk — so the lane froze after one loss."""
    import app.services.trading.momentum_neural.risk_policy as rp
    from app.services.trading.momentum_neural.auto_arm import _lane_execution_family

    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", False)
    monkeypatch.setattr(settings, "chili_momentum_risk_max_daily_loss_usd", 1000.0)
    eqmap = {"robinhood_spot": 950.0, "robinhood_agentic_mcp": 13800.0}
    monkeypatch.setattr(rp, "_account_equity_usd", lambda fam, **k: eqmap.get(fam, 0.0))

    monkeypatch.setattr(settings, "chili_equity_execution_rail", "robinhood_spot")
    cap_spot = rp.equity_relative_daily_loss_cap(1000.0, _lane_execution_family())
    monkeypatch.setattr(settings, "chili_equity_execution_rail", "robinhood_agentic_mcp")
    cap_agentic = rp.equity_relative_daily_loss_cap(1000.0, _lane_execution_family())

    assert cap_agentic > cap_spot                      # bigger basis -> bigger budget
    assert cap_agentic >= 300.0                         # comfortably above one ~$138 trade
    assert cap_agentic > 3.0 * cap_spot                 # ~$690 vs ~$95


# ── FSM-1: orphan-safety net can resolve the agentic adapter ───────────────────


def test_factory_registers_agentic_rail():
    from app.services.trading.venue.factory import (
        _BUILDERS, SUPPORTED_BROKER_SOURCES, is_supported,
    )
    assert "robinhood_agentic_mcp" in _BUILDERS
    assert is_supported("robinhood_agentic_mcp") is True
    assert "robinhood_agentic_mcp" in SUPPORTED_BROKER_SOURCES


def test_factory_builds_agentic_adapter():
    from app.services.trading.venue.factory import get_adapter
    ad = get_adapter("robinhood_agentic_mcp")
    assert ad is not None
    assert ad.__class__.__name__ == "RobinhoodAgenticMcpAdapter"


# ── AOP-1: no schema-forbidden "id" key in get_order / cancel_order ────────────


def _capturing_call(captured, payload):
    """Return a _call replacement that records the (capability -> args) it was given."""
    def _call(cap, args):
        captured[cap] = args
        return _Res(payload)
    return _call


def test_get_order_sends_no_id_key(monkeypatch):
    ad = RobinhoodAgenticMcpAdapter(market_data_adapter=object(), account_number=_ACCT)
    captured: dict = {}
    monkeypatch.setattr(ad, "_call", _capturing_call(captured, []))
    ad.get_order("ord-123")
    args = captured.get("get_order", {})
    assert args.get("order_id") == "ord-123"
    assert "id" not in args


def test_cancel_order_sends_no_id_key(monkeypatch):
    ad = RobinhoodAgenticMcpAdapter(market_data_adapter=object(), account_number=_ACCT)
    captured: dict = {}
    monkeypatch.setattr(ad, "_assert_account_is_agentic", lambda: None)
    monkeypatch.setattr(ad, "_call", _capturing_call(captured, {}))
    ad.cancel_order("ord-123")
    args = captured.get("cancel_order", {})
    assert args.get("order_id") == "ord-123"
    assert "id" not in args


# ── AOP-2: place with no resolvable order_id fails (re-watch), never orphans ───


def test_order_result_no_id_fails_the_place():
    ad = RobinhoodAgenticMcpAdapter(market_data_adapter=object(), account_number=_ACCT)
    out = ad._order_result(_Res({"state": "queued"}, raw={"weird": "no id"}), "cid-1")
    assert out["ok"] is False
    assert out["error"] == "no_order_id_in_place_response"


def test_order_result_flat_id_resolves():
    ad = RobinhoodAgenticMcpAdapter(market_data_adapter=object(), account_number=_ACCT)
    out = ad._order_result(_Res({"id": "ord-flat", "state": "filled"}), "cid-1")
    assert out["ok"] is True
    assert out["order_id"] == "ord-flat"


def test_order_result_nested_order_resolves():
    ad = RobinhoodAgenticMcpAdapter(market_data_adapter=object(), account_number=_ACCT)
    out = ad._order_result(_Res({"order": {"id": "ord-xyz", "state": "queued"}}), "cid-1")
    assert out["ok"] is True
    assert out["order_id"] == "ord-xyz"


# ── AOP-4: get_product miss -> equity whole-share rounding (no fractional reject) ─


def test_round_base_size_whole_shares_when_increment_is_one():
    from app.services.trading.momentum_neural.live_runner import _round_base_size
    assert float(_round_base_size(10.7, 1.0, 1.0)).is_integer()
    assert float(_round_base_size(3.2, 1.0, 1.0)).is_integer()
    # crypto path (fine increment) stays fractional — the equity default must not affect it
    assert not float(_round_base_size(0.12345, 0.00001, 0.0001)).is_integer()
