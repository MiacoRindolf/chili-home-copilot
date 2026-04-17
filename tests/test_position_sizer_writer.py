"""Phase H - DB integration tests for ``position_sizer_writer``.

Exercises mode gating, row writes, divergence math, summary shape,
and off-mode short-circuit.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.services.trading.position_sizer_model import (
    CorrelationBudget,
    PortfolioBudget,
    PositionSizerInput,
    compute_proposal,
)
from app.services.trading.position_sizer_writer import (
    LegacySizing,
    mode_is_active,
    proposals_summary,
    write_proposal,
)


def _cleanup_log(db):
    db.execute(text("DELETE FROM trading_position_sizer_log WHERE source LIKE 'phh_test_%'"))
    db.commit()


def _default_inp(**over) -> PositionSizerInput:
    base = dict(
        ticker="PHH_AAA",
        direction="long",
        asset_class="equity",
        entry_price=100.0,
        stop_price=95.0,
        capital=100_000.0,
        calibrated_prob=0.52,
        payoff_fraction=0.05,
        loss_per_unit=0.05,
    )
    base.update(over)
    return PositionSizerInput(**base)


def _force_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(
        "app.services.trading.position_sizer_writer.settings.brain_position_sizer_mode",
        mode,
        raising=False,
    )


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------


class TestModeGate:
    def test_off_mode_is_noop_and_returns_none(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "off")
        assert mode_is_active() is False

        res = write_proposal(
            db,
            inp=_default_inp(),
            source="phh_test_off",
        )
        assert res is None
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_position_sizer_log "
            "WHERE source = 'phh_test_off'"
        )).scalar_one()
        assert count == 0

    def test_shadow_mode_writes_one_row(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "shadow")
        res = write_proposal(
            db,
            inp=_default_inp(),
            source="phh_test_shadow",
            legacy=LegacySizing(notional=1000.0, quantity=10.0, source="alerts"),
        )
        assert res is not None
        assert res.mode == "shadow"
        assert res.proposed_notional > 0
        assert res.divergence_bps is not None
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_position_sizer_log "
            "WHERE source = 'phh_test_shadow'"
        )).scalar_one()
        assert count == 1

    def test_unknown_mode_is_coerced_to_off(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "gibberish")
        res = write_proposal(
            db,
            inp=_default_inp(),
            source="phh_test_unknown",
        )
        assert res is None


# ---------------------------------------------------------------------------
# Row contents
# ---------------------------------------------------------------------------


class TestRowContents:
    def test_row_captures_ranker_inputs_and_caps(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "shadow")
        correlation = CorrelationBudget(
            bucket="equity:P",
            open_notional=14_800.0,
            max_bucket_notional=15_000.0,
        )
        portfolio = PortfolioBudget(
            total_capital=100_000.0,
            deployed_notional=14_800.0,
            max_total_notional=100_000.0,
            ticker_open_notional=0.0,
        )
        res = write_proposal(
            db,
            inp=_default_inp(),
            correlation=correlation,
            portfolio=portfolio,
            source="phh_test_contents",
            legacy=LegacySizing(notional=500.0, quantity=5.0, source="alerts"),
        )
        assert res is not None
        row = db.execute(text("""
            SELECT proposal_id, source, ticker, calibrated_prob, payoff_fraction,
                   expected_net_pnl, proposed_notional, correlation_cap_triggered,
                   correlation_bucket, max_bucket_notional, legacy_notional,
                   legacy_source, divergence_bps, mode
            FROM trading_position_sizer_log WHERE id = :id
        """), {"id": res.log_id}).fetchone()
        assert row is not None
        assert row[0] == res.proposal_id
        assert row[1] == "phh_test_contents"
        assert row[2] == "PHH_AAA"
        assert row[3] == pytest.approx(0.52)
        assert row[4] == pytest.approx(0.05)
        assert row[5] > 0
        # Bucket only has $200 headroom, so proposal must be clipped to <= 200.
        assert row[6] <= 200.0 + 1e-6
        assert row[7] is True
        assert row[8] == "equity:P"
        assert row[9] == pytest.approx(15_000.0)
        assert row[10] == pytest.approx(500.0)
        assert row[11] == "alerts"
        assert row[12] is not None
        assert row[13] == "shadow"

    def test_no_legacy_sets_divergence_null(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "shadow")
        res = write_proposal(
            db,
            inp=_default_inp(),
            source="phh_test_nolegacy",
        )
        assert res is not None
        assert res.divergence_bps is None
        row = db.execute(text(
            "SELECT legacy_notional, legacy_source, divergence_bps "
            "FROM trading_position_sizer_log WHERE id = :id"
        ), {"id": res.log_id}).fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None

    def test_legacy_zero_with_nonzero_proposal_reports_sentinel(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "shadow")
        res = write_proposal(
            db,
            inp=_default_inp(),
            source="phh_test_zerolegacy",
            legacy=LegacySizing(notional=0.0, quantity=0.0, source="alerts"),
        )
        assert res is not None
        assert res.divergence_bps == 1_000_000.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestRiskDialIntegration:
    def test_dial_recorded_when_active(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "shadow")
        # Activate the dial in shadow mode and seed a row.
        monkeypatch.setattr(
            "app.services.trading.risk_dial_service.settings.brain_risk_dial_mode",
            "shadow",
            raising=False,
        )
        from app.services.trading.risk_dial_service import resolve_dial
        resolve_dial(
            db, user_id=777, regime="risk_on", drawdown_pct=0.0,
            source="phi_dial_test",
        )
        res = write_proposal(
            db,
            inp=_default_inp(user_id=777),
            source="phh_test_dial",
        )
        assert res is not None
        row = db.execute(text(
            "SELECT risk_dial_multiplier FROM trading_position_sizer_log "
            "WHERE id = :id"
        ), {"id": res.log_id}).fetchone()
        assert row is not None
        assert row[0] is not None
        assert float(row[0]) == pytest.approx(1.0)

    def test_dial_is_null_when_mode_off(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "shadow")
        monkeypatch.setattr(
            "app.services.trading.risk_dial_service.settings.brain_risk_dial_mode",
            "off",
            raising=False,
        )
        res = write_proposal(
            db,
            inp=_default_inp(user_id=777),
            source="phh_test_dial_off",
        )
        assert res is not None
        row = db.execute(text(
            "SELECT risk_dial_multiplier FROM trading_position_sizer_log "
            "WHERE id = :id"
        ), {"id": res.log_id}).fetchone()
        assert row[0] is None


class TestDeterminism:
    def test_same_input_produces_same_proposal_id(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "shadow")
        inp = _default_inp()
        r1 = write_proposal(db, inp=inp, source="phh_test_det")
        r2 = write_proposal(db, inp=inp, source="phh_test_det")
        assert r1 is not None and r2 is not None
        assert r1.proposal_id == r2.proposal_id
        # Append-only: both rows exist.
        count = db.execute(text(
            "SELECT COUNT(*) FROM trading_position_sizer_log WHERE proposal_id = :pid"
        ), {"pid": r1.proposal_id}).scalar_one()
        assert count == 2


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class TestSummary:
    def test_summary_returns_frozen_shape(self, db, monkeypatch):
        _cleanup_log(db)
        _force_mode(monkeypatch, "shadow")
        write_proposal(
            db, inp=_default_inp(ticker="PHH_SUM1"),
            source="phh_test_sum",
            legacy=LegacySizing(notional=1000.0, quantity=10.0, source="alerts"),
        )
        write_proposal(
            db, inp=_default_inp(ticker="PHH_SUM2"),
            source="phh_test_sum",
            legacy=LegacySizing(notional=5000.0, quantity=50.0, source="paper"),
        )

        summary = proposals_summary(db, lookback_hours=24)
        assert set(summary.keys()) == {
            "mode", "lookback_hours", "proposals_total",
            "by_source", "by_divergence_bucket",
            "mean_divergence_bps", "p90_divergence_bps",
            "cap_trigger_counts", "by_dial_bucket", "latest_proposal",
        }
        assert summary["mode"] == "shadow"
        assert summary["lookback_hours"] == 24
        assert summary["proposals_total"] >= 2
        assert summary["by_source"].get("phh_test_sum", 0) >= 2
        # Four divergence buckets must be present, even if zero-valued.
        assert set(summary["by_divergence_bucket"].keys()) == {
            "under_100_bps", "100_500_bps", "500_2000_bps", "over_2000_bps",
        }
        # Cap trigger counts have exactly the two named keys.
        assert set(summary["cap_trigger_counts"].keys()) == {
            "correlation_cap", "notional_cap",
        }
        # Phase I: dial buckets must be present (may all be 'unknown' if
        # dial mode is off).
        assert set(summary["by_dial_bucket"].keys()) == {
            "unknown", "under_0_5", "0_5_to_0_8", "0_8_to_1_0",
            "1_0_to_1_2", "over_1_2",
        }
        assert summary["latest_proposal"] is not None
        assert set(summary["latest_proposal"].keys()) == {
            "proposal_id", "source", "ticker", "proposed_notional",
            "legacy_notional", "divergence_bps", "observed_at",
            "risk_dial_multiplier",
        }
