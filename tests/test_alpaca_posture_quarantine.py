"""The recertification lane is Alpaca paper, equity, and long-only."""

from __future__ import annotations

import time

from app.services.trading.momentum_neural import (
    alpaca_reconcile,
    alpaca_orphan_claims,
    operator_actions,
    risk_policy,
)
from app.services.trading.venue import alpaca_spot as alpaca_spot_mod


def test_alpaca_real_endpoint_is_quarantined(monkeypatch):
    monkeypatch.setattr(operator_actions.settings, "chili_alpaca_paper", False)

    assert (
        operator_actions._alpaca_execution_quarantine_reason(
            "alpaca_spot", "VEEE"
        )
        == "alpaca_live_posture_not_certified"
    )


def test_alpaca_crypto_and_short_are_quarantined(monkeypatch):
    monkeypatch.setattr(operator_actions.settings, "chili_alpaca_paper", True)

    assert (
        operator_actions._alpaca_execution_quarantine_reason(
            "alpaca_spot", "BTC-USD"
        )
        == "alpaca_crypto_execution_not_certified"
    )
    assert (
        operator_actions._alpaca_execution_quarantine_reason(
            "alpaca_spot", "BTC/USD"
        )
        == "alpaca_crypto_execution_not_certified"
    )
    assert (
        operator_actions._alpaca_execution_quarantine_reason(
            "alpaca_spot", "ACTU", asset_class="crypto"
        )
        == "alpaca_crypto_execution_not_certified"
    )
    assert (
        operator_actions._alpaca_execution_quarantine_reason(
            "alpaca_short", "VEEE"
        )
        == "alpaca_short_execution_not_certified"
    )
    assert (
        operator_actions._alpaca_execution_quarantine_reason(
            "alpaca_spot", "VEEE"
        )
        is None
    )


def test_reconcile_quarantines_explicit_crypto_asset_class(monkeypatch):
    monkeypatch.setattr(operator_actions.settings, "chili_alpaca_paper", True)

    reason = alpaca_reconcile._alpaca_reconcile_shape_quarantine_reason(
        symbol="ACTU",
        metadata={"order_request": {"asset_class": "crypto"}},
    )

    assert reason == "alpaca_crypto_execution_not_certified"


def test_paper_account_cache_is_never_reused_after_live_posture_flip(
    monkeypatch,
) -> None:
    monkeypatch.setattr(risk_policy.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        risk_policy.settings,
        "chili_alpaca_expected_account_id",
        "acct-cache-a",
        raising=False,
    )
    risk_policy._ALPACA_ACCT_CACHE.update({
        "scope": "alpaca:paper",
        "expected_account_id": "acct-cache-a",
        "observed_account_id": "acct-cache-a",
        "equity": 100_000.0,
        "bp": 400_000.0,
        "ts": time.monotonic(),
    })
    assert risk_policy._alpaca_account_cached() == (100_000.0, 400_000.0)

    monkeypatch.setattr(risk_policy.settings, "chili_alpaca_paper", False)
    assert risk_policy._alpaca_account_cached() == (None, None)
    assert risk_policy._ALPACA_ACCT_CACHE["scope"] is None
    assert risk_policy._ALPACA_ACCT_CACHE["equity"] == 0.0

    risk_policy._ALPACA_ACCT_CACHE.update({
        "scope": "alpaca:paper",
        "expected_account_id": "acct-cache-a",
        "observed_account_id": "acct-cache-a",
        "equity": 100_000.0,
        "bp": 400_000.0,
        "ts": time.monotonic(),
    })
    risk_policy._ACCOUNT_EQUITY_LAST_GOOD.update({
        "alpaca_spot": {"value": 100_000.0, "ts": time.monotonic()},
        "alpaca_short": {"value": 100_000.0, "ts": time.monotonic()},
    })
    assert (
        risk_policy._account_equity_usd("alpaca_spot", prefer_equity=True)
        is None
    )
    assert risk_policy._account_equity_usd("alpaca_spot") is None
    assert "alpaca_spot" not in risk_policy._ACCOUNT_EQUITY_LAST_GOOD
    assert "alpaca_short" not in risk_policy._ACCOUNT_EQUITY_LAST_GOOD


def test_paper_account_cache_and_last_good_rotate_with_exact_account_uuid(
    monkeypatch,
) -> None:
    account_a = "acct-generation-a"
    account_b = "acct-generation-b"
    snapshots = iter([
        {
            "ok": True,
            "paper": True,
            "account_id": account_a,
            "equity": 100_000.0,
            "buying_power": 400_000.0,
        },
        {
            "ok": True,
            "paper": True,
            "account_id": account_b,
            "equity": 5_000.0,
            "buying_power": 20_000.0,
        },
        {"ok": False, "error": "transient account read"},
    ])

    class _Adapter:
        def get_account_snapshot(self):
            return next(snapshots)

    monkeypatch.setattr(risk_policy.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        risk_policy.settings,
        "chili_alpaca_expected_account_id",
        account_a,
        raising=False,
    )
    monkeypatch.setattr(alpaca_spot_mod, "AlpacaSpotAdapter", _Adapter)
    risk_policy._clear_alpaca_account_caches()
    try:
        assert risk_policy._alpaca_account_cached() == (100_000.0, 400_000.0)
        assert risk_policy._account_equity_usd(
            "alpaca_spot", prefer_equity=True
        ) == 100_000.0
        assert f"alpaca_spot|{account_a}" in risk_policy._ACCOUNT_EQUITY_LAST_GOOD

        # Same paper posture, different exact UUID.  The new $5k read is a real
        # account generation, not a <10% transient flake from account A.
        monkeypatch.setattr(
            risk_policy.settings,
            "chili_alpaca_expected_account_id",
            account_b,
            raising=False,
        )
        assert risk_policy._alpaca_account_cached() == (5_000.0, 20_000.0)
        assert f"alpaca_spot|{account_a}" not in risk_policy._ACCOUNT_EQUITY_LAST_GOOD
        assert risk_policy._account_equity_usd(
            "alpaca_spot", prefer_equity=True
        ) == 5_000.0

        # A transient miss may use only B's own short-grace capital basis.
        risk_policy._ALPACA_ACCT_CACHE["ts"] = (
            time.monotonic() - risk_policy._AGENTIC_BP_TTL_SEC - 1.0
        )
        assert risk_policy._alpaca_account_cached() == (5_000.0, 20_000.0)
        assert risk_policy._account_equity_usd(
            "alpaca_spot", prefer_equity=True
        ) == 5_000.0
        assert f"alpaca_spot|{account_b}" in risk_policy._ACCOUNT_EQUITY_LAST_GOOD
    finally:
        risk_policy._clear_alpaca_account_caches()


def test_wrong_paper_account_snapshot_cannot_reuse_last_good(monkeypatch) -> None:
    expected = "acct-expected"

    class _WrongAdapter:
        def get_account_snapshot(self):
            return {
                "ok": True,
                "paper": True,
                "account_id": "acct-other",
                "equity": 999_999.0,
                "buying_power": 3_999_996.0,
            }

    monkeypatch.setattr(risk_policy.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        risk_policy.settings,
        "chili_alpaca_expected_account_id",
        expected,
        raising=False,
    )
    monkeypatch.setattr(alpaca_spot_mod, "AlpacaSpotAdapter", _WrongAdapter)
    risk_policy._clear_alpaca_account_caches()
    risk_policy._ACCOUNT_EQUITY_LAST_GOOD[f"alpaca_spot|{expected}"] = {
        "value": 10_000.0,
        "ts": time.monotonic(),
    }
    try:
        assert risk_policy._alpaca_account_cached() == (None, None)
        assert risk_policy._account_equity_usd(
            "alpaca_spot", prefer_equity=True
        ) is None
        assert not any(
            key.startswith("alpaca_")
            for key in risk_policy._ACCOUNT_EQUITY_LAST_GOOD
        )
    finally:
        risk_policy._clear_alpaca_account_caches()


def test_alpaca_daily_loss_cap_never_exceeds_fixed_250_defense(monkeypatch) -> None:
    monkeypatch.setattr(risk_policy.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        risk_policy,
        "_equity_relative_cap",
        lambda *args, **kwargs: 1_400.0,
    )

    assert risk_policy.equity_relative_daily_loss_cap(
        250.0,
        "alpaca_spot",
    ) == 250.0
    assert risk_policy.equity_relative_daily_loss_cap(
        100.0,
        "alpaca_spot",
    ) == 100.0


def test_live_posture_or_scope_cannot_open_risk_reservation_transaction(
    monkeypatch,
) -> None:
    opened = []
    monkeypatch.setattr(
        alpaca_orphan_claims,
        "_with_short_session",
        lambda fn: opened.append(True),
    )
    request = {
        "product_id": "ACTU",
        "side": "buy",
        "base_size": "10",
        "limit_price": "10.00",
        "client_order_id": "cid-paper-only",
        "position_intent": "buy_to_open",
        "order_type": "limit",
        "time_in_force": "day",
        "extended_hours": False,
    }
    common = dict(
        symbol="ACTU",
        claim_token="entry-1",
        owner_session_id=1,
        client_order_id="cid-paper-only",
        order_request=request,
        order_role="primary",
        reserved_risk_usd=10.0,
        account_equity_usd=100_000.0,
    )

    monkeypatch.setattr(alpaca_orphan_claims.settings, "chili_alpaca_paper", True)
    out = alpaca_orphan_claims.reserve_alpaca_entry_risk_committed(
        **common,
        account_scope="alpaca:live",
    )
    assert out["reason"] == "alpaca_account_scope_not_certified"

    monkeypatch.setattr(alpaca_orphan_claims.settings, "chili_alpaca_paper", False)
    out = alpaca_orphan_claims.reserve_alpaca_entry_risk_committed(
        **common,
        account_scope="alpaca:paper",
    )
    assert out["reason"] == "alpaca_live_posture_not_certified"

    monkeypatch.setattr(alpaca_orphan_claims.settings, "chili_alpaca_paper", True)
    short_request = {
        **request,
        "side": "sell",
        "position_intent": "sell_to_open",
    }
    out = alpaca_orphan_claims.reserve_alpaca_entry_risk_committed(
        **{**common, "order_request": short_request},
        account_scope="alpaca:paper",
    )
    assert out["reason"] == "alpaca_equity_long_only"

    gtc_request = {**request, "time_in_force": "gtc"}
    out = alpaca_orphan_claims.reserve_alpaca_entry_risk_committed(
        **{**common, "order_request": gtc_request},
        account_scope="alpaca:paper",
    )
    assert out["reason"] == "alpaca_equity_long_only"

    slash_request = {**request, "product_id": "BTC/USD"}
    out = alpaca_orphan_claims.reserve_alpaca_entry_risk_committed(
        **{**common, "symbol": "BTC/USD", "order_request": slash_request},
        account_scope="alpaca:paper",
    )
    assert out["reason"] == "alpaca_equity_long_only"

    crypto_class_request = {**request, "asset_class": "crypto"}
    out = alpaca_orphan_claims.reserve_alpaca_entry_risk_committed(
        **{**common, "order_request": crypto_class_request},
        account_scope="alpaca:paper",
    )
    assert out["reason"] == "alpaca_equity_long_only"
    assert opened == []
