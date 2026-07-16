from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.services.trading import governance
from app.services.trading.momentum_neural import risk_policy


def test_alpaca_daily_budget_scales_with_equity_and_ignores_fixed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(risk_policy.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        risk_policy.settings,
        "chili_momentum_risk_daily_loss_fraction_of_equity",
        0.025,
        raising=False,
    )
    equity = {"value": 40_000.0}
    monkeypatch.setattr(
        risk_policy,
        "_account_equity_usd",
        lambda *_args, **_kwargs: equity["value"],
    )

    assert risk_policy.equity_relative_daily_loss_cap(
        1.0, "alpaca_spot"
    ) == pytest.approx(1_000.0)
    equity["value"] = 100_000.0
    assert risk_policy.equity_relative_daily_loss_cap(
        1_000_000.0, "alpaca_spot"
    ) == pytest.approx(2_500.0)


def test_alpaca_daily_budget_fails_closed_when_equity_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(risk_policy.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        risk_policy.settings,
        "chili_momentum_risk_daily_loss_fraction_of_equity",
        0.05,
        raising=False,
    )
    monkeypatch.setattr(
        risk_policy,
        "_account_equity_usd",
        lambda *_args, **_kwargs: None,
    )

    assert risk_policy.equity_relative_daily_loss_cap(
        999_999.0, "alpaca_spot"
    ) == 0.0


def test_alpaca_governance_uses_momentum_equity_fraction_and_no_usd_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        governance.settings,
        "chili_momentum_risk_daily_loss_fraction_of_equity",
        0.03,
        raising=False,
    )
    monkeypatch.setattr(
        governance.settings,
        "chili_global_max_daily_loss_usd",
        1.0,
    )
    monkeypatch.setattr(
        risk_policy,
        "_account_equity_usd",
        lambda *_args, **_kwargs: 100_000.0,
    )

    cap, source, detail = governance._per_broker_daily_loss_cap_detail(
        "alpaca_spot"
    )

    assert cap == pytest.approx(3_000.0)
    assert source == "pct_cash_value"
    assert detail["daily_risk_fraction_of_equity"] == pytest.approx(0.03)
    assert detail["account_equity_usd"] == pytest.approx(100_000.0)
    assert "momentum_fixed_cap_usd" not in detail


def test_alpaca_governance_missing_equity_is_explicitly_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        governance.settings,
        "chili_momentum_risk_daily_loss_fraction_of_equity",
        0.03,
        raising=False,
    )
    monkeypatch.setattr(
        risk_policy,
        "_account_equity_usd",
        lambda *_args, **_kwargs: None,
    )

    cap, source, detail = governance._per_broker_daily_loss_cap_detail(
        "alpaca_spot"
    )

    assert cap == 0.0
    assert source == "adaptive_equity_fraction_unavailable"
    assert detail["selected_cap_usd"] == 0.0


def test_adaptive_paper_cap_functions_have_no_activation_only_dollar_literals() -> None:
    functions = (
        risk_policy.alpaca_paper_hard_loss_cap_usd,
        risk_policy.equity_relative_daily_loss_cap,
        governance._per_broker_daily_loss_cap_detail,
    )
    for function in functions:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        forbidden = {
            float(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
            and float(node.value) in {50.0, 250.0}
        }
        assert forbidden == set(), function.__name__
