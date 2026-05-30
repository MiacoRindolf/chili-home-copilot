"""f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing (2026-05-09).

Pin the cost-aware gate + per-venue cap + buying-power resolver:

  Gate cases (≥6 per brief):
    1. RH equity → fee=0, allowed (RH-fee-free no-op).
    2. RH crypto (whitelisted) → fee=0, allowed.
    3. Coinbase crypto with high edge (12% >> 150bps) → allowed.
    4. Coinbase crypto right at threshold (1.5%) → allowed.
    5. Coinbase crypto below threshold (1.0%) → BLOCKED.
    6. Coinbase crypto with no projected_profit_pct (None) → BLOCKED
       (treated as 0bps).
    7. Empty ticker → no-venue.

  Cap cases (≥2 per brief):
    1. Cap not exceeded → allowed.
    2. Cap exceeded by notional → BLOCKED.
    3. Cap exceeded by positions → BLOCKED.

Helper-level (no DB except cap test).
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from app.services.trading import broker_selector as bs
from app.services.trading import cost_aware_gate as cag
from app.services.trading.cost_aware_gate import (
    REASON_CAP_NOTIONAL,
    REASON_CAP_OK,
    REASON_CAP_POSITIONS,
    REASON_GATE_COINBASE_BLOCKED,
    REASON_GATE_COINBASE_PASSED,
    REASON_GATE_NO_VENUE,
    REASON_GATE_RH_FEE_FREE,
    REASON_GATE_TCA_INVALID,
    REASON_GATE_TCA_UNPROVEN,
    cost_aware_min_edge_gate,
    per_venue_cap_check,
    resolve_coinbase_buying_power,
)


def _settings_stub(
    *,
    fee_bps: int = 120, buffer_bps: int = 30,
    max_notional: float = 50.0, max_positions: int = 3,
    include_tca: bool = False, min_tca_samples: int = 5,
):
    return SimpleNamespace(
        chili_coinbase_taker_fee_bps_round_trip=fee_bps,
        chili_min_edge_safety_buffer_bps=buffer_bps,
        chili_coinbase_max_notional_usd=max_notional,
        chili_coinbase_max_concurrent_positions=max_positions,
        chili_coinbase_cost_gate_include_tca_estimates=include_tca,
        chili_coinbase_cost_gate_min_tca_samples=min_tca_samples,
        chili_broker_selector_rh_crypto_degraded_fallback_enabled=False,
    )


def test_tca_backing_sample_reader_uses_management_envelopes_contract() -> None:
    source = inspect.getsource(cag._coinbase_tca_backing_usable_samples)

    assert "MANAGEMENT_ENVELOPES_RELATION" in source
    assert "FROM trading_trades" not in source


class _FakeCapRows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeCapDb:
    def __init__(self, rows=None, *, raise_on_execute: bool = False):
        self.rows = rows or []
        self.raise_on_execute = raise_on_execute
        self.sql = ""

    def execute(self, stmt):
        self.sql = str(stmt)
        if self.raise_on_execute:
            raise RuntimeError("boom")
        return _FakeCapRows(self.rows)


class _FakeMappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return _FakeMappings(self._row)


class _FakeScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _FakeCostDb:
    def __init__(
        self,
        row,
        *,
        usable_samples=9,
        raise_on_estimate_query=False,
        raise_on_usable_count=False,
    ):
        self.row = row
        self.usable_samples = usable_samples
        self.raise_on_estimate_query = raise_on_estimate_query
        self.raise_on_usable_count = raise_on_usable_count
        self.sqls = []
        self.params = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sqls.append(sql)
        self.params.append(params or {})
        if "COUNT(*)" in sql:
            if self.raise_on_usable_count:
                raise RuntimeError("usable sample check unavailable")
            return _FakeScalarResult(self.usable_samples)
        if self.raise_on_estimate_query:
            raise RuntimeError("estimate query unavailable")
        return _FakeResult(self.row)


# ── Gate cases ──────────────────────────────────────────────────────


def test_gate_rh_equity_fee_zero_no_block():
    s = _settings_stub()
    res = cost_aware_min_edge_gate(
        ticker="AAPL", projected_profit_pct=12.0, settings_=s,
    )
    assert res.allowed is True
    assert res.reason == REASON_GATE_RH_FEE_FREE
    assert res.fee_bps == 0


def test_gate_rh_whitelisted_crypto_fee_zero():
    s = _settings_stub()
    for ticker in ("BTC-USD", "ETH-USD", "ADA-USD", "DOGE-USD"):
        res = cost_aware_min_edge_gate(
            ticker=ticker, projected_profit_pct=10.0, settings_=s,
        )
        assert res.allowed is True
        assert res.reason == REASON_GATE_RH_FEE_FREE
        assert res.fee_bps == 0


def test_gate_rh_crypto_degraded_fallback_uses_coinbase_fee_floor(monkeypatch):
    s = _settings_stub()
    s.chili_broker_selector_rh_crypto_degraded_fallback_enabled = True
    min_failures = 2
    lookback_minutes = 60

    monkeypatch.setattr(
        bs,
        "rh_crypto_degradation_state",
        lambda *_args, **_kwargs: bs.RhCryptoDegradationState(
            degraded=True,
            failures=min_failures,
            min_failures=min_failures,
            lookback_minutes=lookback_minutes,
            reason=bs.RH_CRYPTO_DEGRADED_REASON_FAILURE_THRESHOLD,
        ),
    )

    res = cost_aware_min_edge_gate(
        ticker="BTC-USD",
        projected_profit_pct=1.0,
        settings_=s,
    )

    assert res.allowed is False
    assert res.reason == REASON_GATE_COINBASE_BLOCKED
    assert res.fee_bps == 120
    assert res.threshold_bps == 150


def test_gate_rh_crypto_degraded_fallback_passes_high_edge(monkeypatch):
    s = _settings_stub()
    s.chili_broker_selector_rh_crypto_degraded_fallback_enabled = True
    min_failures = 2
    lookback_minutes = 60

    monkeypatch.setattr(
        bs,
        "rh_crypto_degradation_state",
        lambda *_args, **_kwargs: bs.RhCryptoDegradationState(
            degraded=True,
            failures=min_failures,
            min_failures=min_failures,
            lookback_minutes=lookback_minutes,
            reason=bs.RH_CRYPTO_DEGRADED_REASON_FAILURE_THRESHOLD,
        ),
    )

    res = cost_aware_min_edge_gate(
        ticker="BTC-USD",
        projected_profit_pct=2.0,
        settings_=s,
    )

    assert res.allowed is True
    assert res.reason == REASON_GATE_COINBASE_PASSED
    assert res.fee_bps == 120
    assert res.threshold_bps == 150


def test_gate_coinbase_high_edge_passes():
    s = _settings_stub()
    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=12.0, settings_=s,
    )
    assert res.allowed is True
    assert res.reason == REASON_GATE_COINBASE_PASSED
    assert res.fee_bps == 120
    assert res.threshold_bps == 150
    assert res.edge_bps == 1200


def test_gate_coinbase_right_at_threshold_passes():
    """Edge == threshold (150bps) → allowed (>= comparison)."""
    s = _settings_stub()
    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=1.5, settings_=s,
    )
    assert res.allowed is True
    assert res.reason == REASON_GATE_COINBASE_PASSED
    assert res.edge_bps == 150


def test_gate_coinbase_below_threshold_blocks():
    s = _settings_stub()
    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=1.0, settings_=s,
    )
    assert res.allowed is False
    assert res.reason == REASON_GATE_COINBASE_BLOCKED
    assert res.edge_bps == 100
    assert res.fee_bps == 120
    assert res.threshold_bps == 150


def test_gate_coinbase_none_edge_treated_as_zero_blocks():
    s = _settings_stub()
    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=None, settings_=s,
    )
    assert res.allowed is False
    assert res.reason == REASON_GATE_COINBASE_BLOCKED
    assert res.edge_bps == 0


@pytest.mark.parametrize("projected_edge", [float("nan"), float("inf"), True, "bad"])
def test_gate_coinbase_nonfinite_projected_edge_blocks(projected_edge):
    s = _settings_stub()

    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=projected_edge, settings_=s,
    )

    assert res.allowed is False
    assert res.reason == REASON_GATE_COINBASE_BLOCKED
    assert res.edge_bps == 0
    assert res.threshold_bps == 150


def test_gate_empty_ticker_returns_no_venue():
    s = _settings_stub()
    res = cost_aware_min_edge_gate(
        ticker="", projected_profit_pct=10.0, settings_=s,
    )
    assert res.allowed is False
    assert res.reason == REASON_GATE_NO_VENUE


def test_gate_buffer_setting_changes_threshold():
    """Operator overrides buffer to 100bps → threshold = 220bps,
    so 1.5% (150bps) now BLOCKS."""
    s = _settings_stub(buffer_bps=100)
    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=1.5, settings_=s,
    )
    assert res.allowed is False
    assert res.threshold_bps == 220


def test_gate_higher_tier_fee_lowers_floor():
    """Operator on Tier 4 (15bps taker -> 30bps round-trip) →
    threshold = 60bps, so 1% passes."""
    s = _settings_stub(fee_bps=30)
    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=1.0, settings_=s,
    )
    assert res.allowed is True
    assert res.fee_bps == 30
    assert res.threshold_bps == 60


def test_gate_coinbase_tca_estimate_raises_threshold():
    """When enabled, p90 spread+slippage must come out of gross edge."""
    s = _settings_stub(include_tca=True)
    db = _FakeCostDb({
        "sample_trades": 9,
        "window_days": 30,
        "p90_spread_bps": 12.0,
        "p90_slippage_bps": 88.0,
        "median_spread_bps": 3.0,
        "median_slippage_bps": 40.0,
        "last_updated_at": None,
    })

    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=2.0, settings_=s, db=db,
    )

    assert res.allowed is False
    assert res.threshold_bps == 250
    assert res.tca_cost_bps == 100
    assert res.tca_snapshot is not None
    assert res.tca_snapshot["used"] is True
    assert res.tca_snapshot["sample_basis"] == "usable_finite_tca_trades"
    assert res.tca_snapshot["usable_samples"] == 9
    assert db.params[0]["side"] == "long"
    assert db.params[1]["side"] == "long"


def test_gate_coinbase_tca_estimate_requires_usable_sample_count():
    s = _settings_stub(include_tca=True, min_tca_samples=5)
    db = _FakeCostDb({
        "sample_trades": 4,
        "window_days": 30,
        "p90_spread_bps": 12.0,
        "p90_slippage_bps": 88.0,
        "median_spread_bps": 3.0,
        "median_slippage_bps": 40.0,
        "last_updated_at": None,
    })

    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=2.0, settings_=s, db=db,
    )

    assert res.allowed is False
    assert res.reason == REASON_GATE_TCA_UNPROVEN
    assert res.tca_cost_bps == 0
    assert res.tca_snapshot is not None
    assert res.tca_snapshot["used"] is False
    assert res.tca_snapshot["reason"] == "insufficient_samples"


def test_gate_coinbase_tca_estimate_rechecks_usable_backing_samples():
    s = _settings_stub(include_tca=True, min_tca_samples=5)
    db = _FakeCostDb({
        "sample_trades": 9,
        "window_days": 30,
        "p90_spread_bps": 12.0,
        "p90_slippage_bps": 88.0,
        "median_spread_bps": 3.0,
        "median_slippage_bps": 40.0,
        "last_updated_at": None,
    }, usable_samples=4)

    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=12.0, settings_=s, db=db,
    )

    assert res.allowed is False
    assert res.reason == REASON_GATE_TCA_UNPROVEN
    assert res.tca_cost_bps == 0
    assert res.tca_snapshot is not None
    assert res.tca_snapshot["used"] is False
    assert res.tca_snapshot["reason"] == "insufficient_usable_samples"
    assert res.tca_snapshot["usable_samples"] == 4


def test_gate_coinbase_tca_estimate_blocks_when_usable_check_fails():
    s = _settings_stub(include_tca=True, min_tca_samples=5)
    db = _FakeCostDb({
        "sample_trades": 9,
        "window_days": 30,
        "p90_spread_bps": 12.0,
        "p90_slippage_bps": 88.0,
        "median_spread_bps": 3.0,
        "median_slippage_bps": 40.0,
        "last_updated_at": None,
    }, raise_on_usable_count=True)

    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=12.0, settings_=s, db=db,
    )

    assert res.allowed is False
    assert res.reason == REASON_GATE_TCA_INVALID
    assert res.tca_cost_bps == 0
    assert res.tca_snapshot is not None
    assert res.tca_snapshot["used"] is False
    assert res.tca_snapshot["reason"] == "usable_sample_check_failed"


def test_gate_coinbase_tca_estimate_query_failure_blocks():
    s = _settings_stub(include_tca=True, min_tca_samples=5)
    db = _FakeCostDb(None, raise_on_estimate_query=True)

    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=12.0, settings_=s, db=db,
    )

    assert res.allowed is False
    assert res.reason == REASON_GATE_TCA_INVALID
    assert res.tca_cost_bps == 0
    assert res.tca_snapshot is not None
    assert res.tca_snapshot["used"] is False
    assert res.tca_snapshot["reason"] == "tca_estimate_query_failed"


def test_gate_coinbase_tca_estimate_blocks_nonfinite_cost_row():
    s = _settings_stub(include_tca=True, min_tca_samples=5)
    db = _FakeCostDb({
        "sample_trades": 9,
        "window_days": 30,
        "p90_spread_bps": float("inf"),
        "p90_slippage_bps": 88.0,
        "median_spread_bps": 3.0,
        "median_slippage_bps": float("nan"),
        "last_updated_at": None,
    })

    res = cost_aware_min_edge_gate(
        ticker="AKT-USD", projected_profit_pct=12.0, settings_=s, db=db,
    )

    assert res.allowed is False
    assert res.reason == REASON_GATE_TCA_INVALID
    assert res.tca_cost_bps == 0
    assert res.tca_snapshot is not None
    assert res.tca_snapshot["used"] is False
    assert res.tca_snapshot["reason"] == "invalid_tca_estimate"
    assert res.tca_snapshot["invalid_fields"] == [
        "median_slippage_bps",
        "p90_spread_bps",
    ]


# ── Per-venue cap cases ─────────────────────────────────────────────


def _seed_open_coinbase_trade(
    db,
    *,
    trade_id,
    ticker,
    qty,
    entry_price,
    auto_trader: bool = True,
):
    """Seed an open Coinbase trade so the cap query sees it."""
    from app.models.trading import Trade
    from app.services.trading.management_scope import MANAGEMENT_SCOPE_AUTO_TRADER_V1

    if db.query(Trade).filter(Trade.id == trade_id).first() is not None:
        return
    tr = Trade(
        id=trade_id, ticker=ticker, status="open",
        broker_source="coinbase", direction="long",
        quantity=qty, entry_price=entry_price,
        auto_trader_version="v1" if auto_trader else None,
        management_scope=MANAGEMENT_SCOPE_AUTO_TRADER_V1 if auto_trader else "broker_sync",
        tags="autotrader_v1" if auto_trader else "coinbase-sync",
    )
    db.add(tr)
    db.commit()


def test_cap_no_open_positions_allows(db):
    s = _settings_stub()
    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=10.0,
        db=db, settings_=s,
    )
    assert res.allowed is True
    assert res.reason == REASON_CAP_OK
    assert res.current_positions == 0


def test_cap_notional_exceeded_blocks(db):
    """1 open Coinbase trade @ $45 + 10 proposed = 55 > 50 cap."""
    _seed_open_coinbase_trade(
        db, trade_id=8001, ticker="AKT-USD", qty=100.0, entry_price=0.45,
    )
    s = _settings_stub(max_notional=50.0)
    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=10.0,
        db=db, settings_=s,
    )
    assert res.allowed is False
    assert res.reason == REASON_CAP_NOTIONAL


def test_cap_position_count_exceeded_blocks(db):
    """3 open Coinbase trades + new one would be 4th >= cap of 3."""
    for i, t in enumerate(("AKT-USD", "RENDER-USD", "ARB-USD"), start=8010):
        _seed_open_coinbase_trade(
            db, trade_id=i, ticker=t, qty=1.0, entry_price=1.0,
        )
    s = _settings_stub(max_positions=3, max_notional=10000.0)
    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=1.0,
        db=db, settings_=s,
    )
    assert res.allowed is False
    assert res.reason == REASON_CAP_POSITIONS


def test_cap_zero_static_limits_allows_managed_positions(db):
    """0 disables the static Coinbase cap; other risk gates still apply."""
    _seed_open_coinbase_trade(
        db, trade_id=8018, ticker="AKT-USD", qty=100.0, entry_price=0.45,
    )
    s = _settings_stub(max_positions=0, max_notional=0.0)
    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=10_000.0,
        db=db, settings_=s,
    )
    assert res.allowed is True
    assert res.reason == REASON_CAP_OK
    assert res.current_positions == 1


def test_cap_ignores_passive_coinbase_sync_rows(db):
    """A broker-sync Coinbase holding should not consume the autotrader lane."""
    _seed_open_coinbase_trade(
        db,
        trade_id=8020,
        ticker="THQ-USD",
        qty=30_000.0,
        entry_price=0.02,
        auto_trader=False,
    )
    s = _settings_stub(max_positions=1, max_notional=400.0)
    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=300.0,
        db=db, settings_=s,
    )
    assert res.allowed is True
    assert res.reason == REASON_CAP_OK
    assert res.current_positions == 0


def test_cap_robinhood_venue_no_op(db):
    """Phase 5 cost gate doesn't enforce caps for RH (RH has its
    own size/heat gates upstream). Returns allowed=True regardless."""
    s = _settings_stub()
    res = per_venue_cap_check(
        venue="robinhood", proposed_notional_usd=999999.0,
        db=db, settings_=s,
    )
    assert res.allowed is True


def test_cap_phase5k_flag_defaults_to_compatibility_view():
    fake_db = _FakeCapDb([SimpleNamespace(notional=5.0)])
    s = _settings_stub(max_positions=3, max_notional=100.0)

    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=10.0,
        db=fake_db, settings_=s,
    )

    assert res.allowed is True
    assert "FROM trading_trades" in fake_db.sql
    assert "trading_management_envelopes" not in fake_db.sql


def test_cap_phase5k_flag_is_typed_default_false_in_settings(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv(cag.PHASE5K_COINBASE_CAP_ENV, raising=False)

    s = Settings(_env_file=None)

    assert s.chili_phase5k_coinbase_cap_use_envelopes is False


def test_cap_phase5k_env_alias_flows_through_typed_settings(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv(cag.PHASE5K_COINBASE_CAP_ENV, "true")

    s = Settings(_env_file=None)

    assert s.chili_phase5k_coinbase_cap_use_envelopes is True


def test_cap_phase5k_env_does_not_override_explicit_settings_object(monkeypatch):
    monkeypatch.setenv(cag.PHASE5K_COINBASE_CAP_ENV, "true")
    fake_db = _FakeCapDb([SimpleNamespace(notional=5.0)])
    s = _settings_stub(max_positions=3, max_notional=100.0)

    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=10.0,
        db=fake_db, settings_=s,
    )

    assert res.allowed is True
    assert "FROM trading_trades" in fake_db.sql
    assert "trading_management_envelopes" not in fake_db.sql


def test_cap_phase5k_flag_can_use_management_envelopes():
    fake_db = _FakeCapDb([SimpleNamespace(notional=5.0)])
    s = _settings_stub(max_positions=3, max_notional=100.0)
    s.chili_phase5k_coinbase_cap_use_envelopes = True

    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=10.0,
        db=fake_db, settings_=s,
    )

    assert res.allowed is True
    assert "FROM trading_management_envelopes" in fake_db.sql
    assert "FROM trading_trades" not in fake_db.sql


def test_cap_phase5k_query_failure_stays_conservative():
    fake_db = _FakeCapDb(raise_on_execute=True)
    s = _settings_stub(max_positions=3, max_notional=100.0)
    s.chili_phase5k_coinbase_cap_use_envelopes = True

    res = per_venue_cap_check(
        venue="coinbase", proposed_notional_usd=10.0,
        db=fake_db, settings_=s,
    )

    assert res.allowed is False
    assert res.reason == REASON_CAP_NOTIONAL
    assert res.current_positions == 999


# ── Buying-power resolver ───────────────────────────────────────────


def test_buying_power_aggregates_usd_and_usdc():
    """Per Phase 2 G1: cash field is USD wallet (often $0 with
    USDC-only funding); total buying power = cash + USDC quantity."""
    cag._BUYING_POWER_CACHE["value"] = None
    cag._BUYING_POWER_CACHE["ts"] = 0.0

    res = resolve_coinbase_buying_power(
        portfolio_fn=lambda: {"cash": 100.0, "equity": 2300.0},
        positions_fn=lambda: [
            {"ticker": "USDC-USD", "quantity": 2200.0},
            {"ticker": "BTC-USD", "quantity": 0.001},
        ],
    )
    assert res["usd"] == 100.0
    assert res["usdc"] == 2200.0
    assert res["total"] == 2300.0


def test_buying_power_usdc_only_funding():
    """The Phase 2 G1 fingerprint: cash=$0, USDC=$2200 → total=$2200."""
    cag._BUYING_POWER_CACHE["value"] = None
    cag._BUYING_POWER_CACHE["ts"] = 0.0

    res = resolve_coinbase_buying_power(
        portfolio_fn=lambda: {"cash": 0.0},
        positions_fn=lambda: [{"ticker": "USDC-USD", "quantity": 2200.015}],
    )
    assert res["usd"] == 0.0
    assert res["usdc"] == pytest.approx(2200.015)
    assert res["total"] == pytest.approx(2200.015)


def test_buying_power_resilient_to_fetch_failure():
    cag._BUYING_POWER_CACHE["value"] = None
    cag._BUYING_POWER_CACHE["ts"] = 0.0

    def _boom():
        raise RuntimeError("api unavailable")

    res = resolve_coinbase_buying_power(
        portfolio_fn=_boom,
        positions_fn=lambda: [],
    )
    # Total resolves to 0 (conservative); upstream caller can decide
    # to skip Coinbase routing on zero buying power.
    assert res["usd"] == 0.0
    assert res["total"] == 0.0


# ── Reason constants pinned ─────────────────────────────────────────


def test_reason_constants_pinned():
    assert REASON_GATE_RH_FEE_FREE == "rh_fee_free"
    assert REASON_GATE_COINBASE_PASSED == "coinbase_clears_fee_threshold"
    assert REASON_GATE_COINBASE_BLOCKED == "coinbase_below_fee_threshold"
    assert REASON_GATE_NO_VENUE == "no_venue_supports"
    assert REASON_CAP_OK == "within_cap"
    assert REASON_CAP_NOTIONAL == "venue_notional_cap_exceeded"
    assert REASON_CAP_POSITIONS == "venue_concurrent_positions_cap_exceeded"
