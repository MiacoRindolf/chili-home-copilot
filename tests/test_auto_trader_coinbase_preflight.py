from __future__ import annotations

from types import SimpleNamespace


def _alert(*, ticker: str = "ACX-USD") -> SimpleNamespace:
    return SimpleNamespace(
        id=101,
        ticker=ticker,
        asset_type="crypto",
        entry_price=0.0426,
    )


def test_coinbase_preflight_blocks_new_entry_when_broker_already_holds_ticker(
    monkeypatch,
):
    from app.services import coinbase_service
    from app.services.trading import auto_trader

    alert = _alert()
    sync_calls: list[int | None] = []
    block_calls: list[dict] = []
    monkeypatch.setattr(
        coinbase_service,
        "get_fresh_positions",
        lambda: [
            {
                "ticker": "ACX-USD",
                "quantity": 3822.0,
                "average_buy_price": 0.0428708,
                "current_price": 0.0426,
                "equity": 162.8172,
            }
        ],
    )
    monkeypatch.setattr(
        coinbase_service,
        "sync_positions_to_db",
        lambda _db, user_id: sync_calls.append(user_id) or {"updated": 1},
    )
    monkeypatch.setattr(
        auto_trader,
        "_block_live_order",
        lambda _db, **kwargs: block_calls.append(kwargs)
        or kwargs["out"].__setitem__("skipped", kwargs["out"].get("skipped", 0) + 1),
    )

    snap: dict = {}
    out = {"skipped": 0}
    blocked = auto_trader._coinbase_broker_position_preflight_blocks_entry(
        object(),
        uid=7,
        alert=alert,
        snap=snap,
        llm_snap=None,
        out=out,
    )

    assert blocked is True
    assert out["skipped"] == 1
    assert sync_calls == [7]
    assert snap["broker_truth_status"] == "position_already_open"
    assert snap["broker_truth_reason"] == "coinbase_existing_position_preflight"
    assert snap["broker_truth_existing_quantity"] == 3822.0
    assert snap["broker_truth_existing_avg_entry"] == 0.0428708
    assert snap["broker_truth_existing_current_price"] == 0.0426
    assert snap["broker_truth_existing_equity"] == 162.8172
    assert block_calls[0]["reason"] == "broker_position_already_open"


def test_coinbase_preflight_allows_new_entry_when_broker_has_no_matching_position(
    monkeypatch,
):
    from app.services import coinbase_service
    from app.services.trading import auto_trader

    alert = _alert()
    monkeypatch.setattr(
        coinbase_service,
        "get_fresh_positions",
        lambda: [{"ticker": "QNT-USD", "quantity": 1.0}],
    )

    snap: dict = {}
    out = {"skipped": 0}
    blocked = auto_trader._coinbase_broker_position_preflight_blocks_entry(
        object(),
        uid=7,
        alert=alert,
        snap=snap,
        llm_snap=None,
        out=out,
    )

    assert blocked is False
    assert out["skipped"] == 0
    assert snap["broker_truth_status"] == "no_existing_coinbase_position"
