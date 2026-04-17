"""Phase I: pure unit tests for :mod:`app.services.trading.capital_reweight_model`.

No database; fast (<1s total).
"""
from __future__ import annotations

import pytest

from app.services.trading.capital_reweight_model import (
    BucketContext,
    CapitalReweightConfig,
    CapitalReweightInput,
    compute_reweight,
)


def _default_cfg(**overrides) -> CapitalReweightConfig:
    base = dict(
        max_single_bucket_pct=35.0,
        min_weight_pct=0.0,
        regime_tilt_enabled=True,
    )
    base.update(overrides)
    return CapitalReweightConfig(**base)


class TestEmptyAndDegenerate:
    def test_empty_buckets_returns_empty_allocations(self):
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="cautious",
                dial_value=1.0,
                buckets=tuple(),
            ),
            config=_default_cfg(),
        )
        assert out.allocations == tuple()
        assert out.mean_drift_bps == 0.0
        assert out.p90_drift_bps == 0.0

    def test_zero_capital_produces_zero_target(self):
        buckets = (
            BucketContext(name="equity:A", current_notional=0.0, volatility=1.0),
            BucketContext(name="equity:B", current_notional=0.0, volatility=2.0),
        )
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=0.0,
                regime="cautious",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=_default_cfg(),
        )
        for a in out.allocations:
            assert a.target_notional == pytest.approx(0.0)


class TestInverseVolDefault:
    def test_equal_vol_splits_equally(self):
        buckets = (
            BucketContext(name="a", current_notional=0.0, volatility=1.0),
            BucketContext(name="a2", current_notional=0.0, volatility=1.0),
        )
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_on",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=_default_cfg(max_single_bucket_pct=100.0),
        )
        targets = {a.bucket: a.target_notional for a in out.allocations}
        assert targets["a"] == pytest.approx(5_000.0)
        assert targets["a2"] == pytest.approx(5_000.0)

    def test_higher_vol_gets_less_weight(self):
        buckets = (
            BucketContext(name="low", current_notional=0.0, volatility=1.0),
            BucketContext(name="high", current_notional=0.0, volatility=3.0),
        )
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_on",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=_default_cfg(max_single_bucket_pct=100.0),
        )
        targets = {a.bucket: a.target_notional for a in out.allocations}
        assert targets["low"] > targets["high"]
        assert targets["low"] == pytest.approx(7_500.0)
        assert targets["high"] == pytest.approx(2_500.0)


class TestRegimeTilt:
    def test_dial_scales_total_deployed_when_enabled(self):
        buckets = (
            BucketContext(name="a", current_notional=0.0, volatility=1.0),
        )
        cfg = _default_cfg(regime_tilt_enabled=True, max_single_bucket_pct=100.0)
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_off",
                dial_value=0.3,
                buckets=buckets,
            ),
            config=cfg,
        )
        assert out.allocations[0].target_notional == pytest.approx(3_000.0)

    def test_dial_ignored_when_tilt_disabled(self):
        buckets = (
            BucketContext(name="a", current_notional=0.0, volatility=1.0),
        )
        cfg = _default_cfg(regime_tilt_enabled=False, max_single_bucket_pct=100.0)
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_off",
                dial_value=0.3,
                buckets=buckets,
            ),
            config=cfg,
        )
        assert out.allocations[0].target_notional == pytest.approx(10_000.0)


class TestSingleBucketCap:
    def test_cap_fires_and_redistributes(self):
        buckets = (
            BucketContext(name="hot", current_notional=0.0, volatility=1.0),
            BucketContext(name="cool1", current_notional=0.0, volatility=3.0),
            BucketContext(name="cool2", current_notional=0.0, volatility=3.0),
        )
        cfg = _default_cfg(max_single_bucket_pct=35.0)
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_on",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=cfg,
        )
        targets = {a.bucket: a.target_notional for a in out.allocations}
        assert targets["hot"] == pytest.approx(3_500.0)  # capped
        assert targets["cool1"] + targets["cool2"] == pytest.approx(6_500.0, rel=1e-4)
        hot_alloc = next(a for a in out.allocations if a.bucket == "hot")
        assert hot_alloc.cap_triggered is True

    def test_cap_does_not_fire_when_under(self):
        buckets = (
            BucketContext(name="a", current_notional=0.0, volatility=1.0),
            BucketContext(name="b", current_notional=0.0, volatility=1.0),
            BucketContext(name="c", current_notional=0.0, volatility=1.0),
            BucketContext(name="d", current_notional=0.0, volatility=1.0),
        )
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_on",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=_default_cfg(),
        )
        assert all(a.cap_triggered is False for a in out.allocations)
        for a in out.allocations:
            assert a.target_notional == pytest.approx(2_500.0)


class TestDriftComputation:
    def test_drift_bps_is_absolute_and_scaled(self):
        buckets = (
            BucketContext(name="a", current_notional=2_000.0, volatility=1.0),
        )
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_on",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=_default_cfg(max_single_bucket_pct=100.0),
        )
        assert out.allocations[0].drift_bps == pytest.approx(
            abs(10_000.0 - 2_000.0) / 10_000.0 * 1e4,
        )

    def test_zero_drift_when_current_matches_target(self):
        buckets = (
            BucketContext(name="a", current_notional=5_000.0, volatility=1.0),
            BucketContext(name="b", current_notional=5_000.0, volatility=1.0),
        )
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_on",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=_default_cfg(max_single_bucket_pct=100.0),
        )
        for a in out.allocations:
            assert a.drift_bps == pytest.approx(0.0)


class TestIdempotency:
    def test_reweight_id_same_for_same_user_date(self):
        inp = CapitalReweightInput(
            user_id=42,
            as_of_date="2026-04-16",
            total_capital=1_000.0,
            regime="cautious",
            dial_value=0.7,
            buckets=(BucketContext(name="a", current_notional=0.0, volatility=1.0),),
        )
        a = compute_reweight(inp, config=_default_cfg())
        b = compute_reweight(inp, config=_default_cfg())
        assert a.reweight_id == b.reweight_id
        assert len(a.reweight_id) == 32

    def test_reweight_id_differs_by_date(self):
        base = dict(
            user_id=42,
            total_capital=1_000.0,
            regime="cautious",
            dial_value=0.7,
            buckets=(BucketContext(name="a", current_notional=0.0, volatility=1.0),),
        )
        a = compute_reweight(
            CapitalReweightInput(as_of_date="2026-04-16", **base),
            config=_default_cfg(),
        )
        b = compute_reweight(
            CapitalReweightInput(as_of_date="2026-04-17", **base),
            config=_default_cfg(),
        )
        assert a.reweight_id != b.reweight_id

    def test_reweight_id_global_vs_user(self):
        base = dict(
            as_of_date="2026-04-16",
            total_capital=1_000.0,
            regime="cautious",
            dial_value=0.7,
            buckets=(BucketContext(name="a", current_notional=0.0, volatility=1.0),),
        )
        a = compute_reweight(CapitalReweightInput(user_id=None, **base), config=_default_cfg())
        b = compute_reweight(CapitalReweightInput(user_id=1, **base), config=_default_cfg())
        assert a.reweight_id != b.reweight_id


class TestCapTriggerAccounting:
    def test_cap_trigger_counts_reported(self):
        buckets = (
            BucketContext(name="hot", current_notional=0.0, volatility=1.0),
            BucketContext(name="cool", current_notional=0.0, volatility=100.0),
        )
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_on",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=_default_cfg(max_single_bucket_pct=35.0),
        )
        assert out.cap_triggers["single_bucket"] >= 1

    def test_concentration_flag_fires_on_big_current_bucket(self):
        buckets = (
            BucketContext(
                name="dominant",
                current_notional=8_000.0,
                volatility=1.0,
            ),
            BucketContext(name="tiny", current_notional=2_000.0, volatility=1.0),
        )
        out = compute_reweight(
            CapitalReweightInput(
                user_id=None,
                as_of_date="2026-04-16",
                total_capital=10_000.0,
                regime="risk_on",
                dial_value=1.0,
                buckets=buckets,
            ),
            config=_default_cfg(max_single_bucket_pct=35.0),
        )
        assert out.cap_triggers["concentration"] == 1
