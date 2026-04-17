"""P4 — Live readiness gates.

* Kill switch short-circuits ``run_auto_trader_tick`` and ``tick_auto_trader_monitor``
  even when the desk is unpaused and live_orders is on.
* Desk ``paused=false`` + live_orders override end-to-end: orchestrator reads
  desk state via ``effective_autotrader_runtime``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.services.trading.auto_trader import run_auto_trader_tick
from app.services.trading.auto_trader_monitor import tick_auto_trader_monitor
from app.services.trading.autotrader_desk import (
    effective_autotrader_runtime,
    set_desk_live_orders,
    set_desk_paused,
)
from app.services.trading.governance import (
    activate_kill_switch,
    deactivate_kill_switch,
    is_kill_switch_active,
)


@pytest.fixture(autouse=True)
def _ensure_kill_switch_reset():
    if is_kill_switch_active():
        deactivate_kill_switch()
    yield
    if is_kill_switch_active():
        deactivate_kill_switch()


def test_kill_switch_blocks_orchestrator_entry(
    paired_client, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _c, _user = paired_client
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_live_enabled", True)
    set_desk_paused(db, False)
    set_desk_live_orders(db, True)

    activate_kill_switch("test_kill")
    try:
        res = run_auto_trader_tick(db)
    finally:
        deactivate_kill_switch()

    assert res.get("skipped") is True
    assert res.get("reason") == "kill_switch"


def test_kill_switch_blocks_monitor(
    paired_client, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _c, _user = paired_client
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_rth_only", False)
    monkeypatch.setattr(_s, "chili_autotrader_live_enabled", True)
    set_desk_paused(db, False)
    set_desk_live_orders(db, True)

    adapter = MagicMock()
    adapter.is_enabled.return_value = True

    activate_kill_switch("test_kill_monitor")
    try:
        with patch(
            "app.services.trading.venue.robinhood_spot.RobinhoodSpotAdapter",
            return_value=adapter,
        ):
            res = tick_auto_trader_monitor(db)
    finally:
        deactivate_kill_switch()

    assert res.get("skipped") == "kill_switch"
    adapter.place_market_order.assert_not_called()


def test_desk_paused_blocks_orchestrator_even_with_env_on(
    paired_client, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _c, _user = paired_client
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_live_enabled", True)
    set_desk_paused(db, True)
    set_desk_live_orders(db, True)

    res = run_auto_trader_tick(db)
    assert res.get("skipped") is True
    assert res.get("reason") == "paused_or_disabled"
    assert res.get("runtime", {}).get("paused") is True


def test_desk_live_override_reflects_in_runtime(
    paired_client, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _c, _user = paired_client
    from app.config import settings as _s

    monkeypatch.setattr(_s, "chili_autotrader_enabled", True)
    monkeypatch.setattr(_s, "chili_autotrader_live_enabled", False)

    set_desk_paused(db, False)
    set_desk_live_orders(db, True)
    rt = effective_autotrader_runtime(db)
    assert rt["paused"] is False
    assert rt["live_orders_effective"] is True
    assert rt["live_orders_env"] is False
    assert rt["desk_live_override"] is True
    assert rt["tick_allowed"] is True

    set_desk_live_orders(db, None)
    rt2 = effective_autotrader_runtime(db)
    assert rt2["desk_live_override"] is False
    assert rt2["live_orders_effective"] is False


def test_desk_patch_end_to_end(paired_client, db: Session) -> None:
    client, _user = paired_client

    r = client.patch(
        "/api/trading/autotrader/desk",
        json={"paused": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["runtime"]["paused"] is True

    r = client.patch(
        "/api/trading/autotrader/desk",
        json={"paused": False, "live_orders": True},
    )
    assert r.status_code == 200, r.text
    rt = r.json()["runtime"]
    assert rt["paused"] is False
    assert rt["live_orders_effective"] is True
    assert rt["desk_live_override"] is True

    r = client.patch(
        "/api/trading/autotrader/desk",
        json={"live_orders": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["runtime"]["desk_live_override"] is False
