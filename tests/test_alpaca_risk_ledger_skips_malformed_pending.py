"""Risk-ledger scan: isang sirang pending row ay HINDI dapat bumulag sa buong account.

2026-09-03 (Alpaca PAPER, RTH): session 19534 MIMI ay ``live_pending_entry`` na
may ``entry_client_order_id`` pero NULL ``entry_order_request`` (kinansela ng
broker ang order nang walang fill; hindi na-terminalize ang session). Ang legacy
pending loop sa ``_reserve_alpaca_entry_risk`` ay nag-raise ng
``ValueError("legacy_reservation_invalid")`` sa row na iyon, at ang except ay
nagbalik ng ``{"ok": False, "reason": "risk_ledger_unreadable"}`` — para sa
BAWAT simbolong humihingi ng reservation. Nasukat: 240 na ``legacy_reservation_
invalid`` sa log, 80 na ``live_entry_deferred_final_bbo / risk_ledger_unreadable``
sa RTH, 40 sa CHPT lamang — ang top gainer ng araw — pagkatapos ng OK na quote.

Ang tamang gawi: laktawan ang sirang row nang MAY telemetry (ang pangalan ng
row sa log at sa resulta), at ituloy ang scan para sa malulusog na row.
"""
from __future__ import annotations

import math

import pytest

from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    _legacy_pending_reservation_usd,
)


def _certified_request(symbol: str, cid: str) -> dict:
    """Isang frozen entry request na tatanggapin ng ``_certified_frozen_entry_request``."""
    return {
        "symbol": symbol,
        "side": "buy",
        "client_order_id": cid,
        "order_type": "limit",
        "qty": "10",
        "limit_price": "1.00",
        "time_in_force": "day",
    }


def test_malformed_pending_row_returns_none_not_raise():
    live = {
        "entry_client_order_id": "chili_ml_e_19534_38836920_3a70b2dda8",
        "entry_inflight_risk_usd": 12.74,
        "entry_order_request": None,  # ← ang eksaktong hugis ng MIMI 19534
        "entry_submitted": True,
    }
    out = _legacy_pending_reservation_usd(
        live, symbol="MIMI", client_order_id=live["entry_client_order_id"]
    )
    assert out is None


def test_non_finite_or_nonpositive_risk_returns_none():
    for bad in (float("nan"), float("inf"), 0.0, -3.0, None, "x"):
        live = {
            "entry_client_order_id": "cid-1",
            "entry_inflight_risk_usd": bad,
            "entry_order_request": _certified_request("ABC", "cid-1"),
        }
        assert _legacy_pending_reservation_usd(live, symbol="ABC", client_order_id="cid-1") is None


def test_healthy_pending_row_returns_its_risk(monkeypatch):
    # Ang certification ng frozen request ay may sariling contract; para sa
    # helper na ito ang mahalaga ay ang HUGIS ng resulta: float kapag buo.
    import app.services.trading.momentum_neural.alpaca_orphan_claims as m

    monkeypatch.setattr(m, "_certified_frozen_entry_request", lambda *a, **k: True)
    live = {
        "entry_client_order_id": "cid-2",
        "entry_inflight_risk_usd": 40.5,
        "entry_order_request": _certified_request("XYZ", "cid-2"),
    }
    out = _legacy_pending_reservation_usd(live, symbol="XYZ", client_order_id="cid-2")
    assert isinstance(out, float) and math.isclose(out, 40.5)


def test_uncertified_request_returns_none(monkeypatch):
    import app.services.trading.momentum_neural.alpaca_orphan_claims as m

    monkeypatch.setattr(m, "_certified_frozen_entry_request", lambda *a, **k: False)
    live = {
        "entry_client_order_id": "cid-3",
        "entry_inflight_risk_usd": 5.0,
        "entry_order_request": _certified_request("QQQ", "cid-3"),
    }
    assert _legacy_pending_reservation_usd(live, symbol="QQQ", client_order_id="cid-3") is None


@pytest.mark.parametrize("cid", ["", None])
def test_missing_cid_is_still_evaluated_not_raised(cid):
    live = {
        "entry_client_order_id": cid,
        "entry_inflight_risk_usd": 1.0,
        "entry_order_request": None,
    }
    assert _legacy_pending_reservation_usd(live, symbol="NOCID", client_order_id=cid or "") is None
