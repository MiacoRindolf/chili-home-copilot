from __future__ import annotations

import json
from types import SimpleNamespace

from app.routers.trading_sub import operator as operator_router
from app.services.trading import public_api


def _response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _budget(**overrides):
    base = {
        "can_open_new": False,
        "rejection_reason": "invalid_capital",
        "open_positions": 0,
        "stock_positions": 0,
        "crypto_positions": 0,
        "option_positions": 0,
        "total_heat_pct": 0.0,
        "available_heat_pct": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_route_deps(monkeypatch, *, capital, budget):
    seen: list[float | None] = []

    monkeypatch.setattr(
        operator_router,
        "get_identity_ctx",
        lambda _request, _db: {
            "user_id": None,
            "is_guest": False,
            "capital": capital,
        },
    )
    monkeypatch.setattr(public_api, "get_risk_limits", lambda: object())
    monkeypatch.setattr(
        public_api,
        "get_breaker_status",
        lambda: {"tripped": False, "reason": None},
    )

    def _risk_snapshot(_db, _user_id, capital_arg, _limits):
        seen.append(capital_arg)
        return budget

    monkeypatch.setattr(public_api, "get_portfolio_risk_snapshot", _risk_snapshot)
    return seen


def test_operator_risk_budget_blocks_when_identity_capital_missing(monkeypatch) -> None:
    seen = _patch_route_deps(monkeypatch, capital=None, budget=_budget())

    response = operator_router.operator_risk_budget(
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
        db=object(),
    )

    data = _response_json(response)
    assert data["ok"] is True
    assert seen == [None]
    assert data["capital"] is None
    assert data["capital_available"] is False
    assert data["capital_source"] == "unavailable"
    assert data["can_open_new"] is False
    assert data["rejection_reason"] == "invalid_capital"


def test_operator_risk_budget_uses_explicit_identity_capital(
    monkeypatch,
) -> None:
    seen = _patch_route_deps(
        monkeypatch,
        capital=12_345.67,
        budget=_budget(can_open_new=True, rejection_reason=None),
    )

    response = operator_router.operator_risk_budget(
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
        db=object(),
    )

    data = _response_json(response)
    assert data["ok"] is True
    assert seen == [12_345.67]
    assert data["capital"] == 12_345.67
    assert data["capital_available"] is True
    assert data["capital_source"] == "identity_ctx"


def test_operator_risk_budget_rejects_truthy_noncapital_values(monkeypatch) -> None:
    seen = _patch_route_deps(monkeypatch, capital=True, budget=_budget())

    response = operator_router.operator_risk_budget(
        SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
        db=object(),
    )

    data = _response_json(response)
    assert seen == [None]
    assert data["capital"] is None
    assert data["capital_available"] is False
    assert data["capital_source"] == "unavailable"
    assert data["can_open_new"] is False
