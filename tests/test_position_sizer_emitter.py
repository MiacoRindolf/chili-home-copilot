"""DB integration tests for :mod:`app.services.trading.position_sizer_emitter`.

The emitter is the thin shim the four legacy sizer call-sites use to
record a Phase H shadow proposal. These tests exercise:

* Off-mode short-circuit.
* Shadow-mode write path end-to-end (without NetEdgeRanker) - verifies
  the fallback geometric inputs produce a sane row.
* Divergence captured against the caller's legacy notional.
* Defensive swallow: invalid prices return ``None`` without raising.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.services.trading.position_sizer_emitter import (
    EmitterSignal,
    emit_shadow_proposal,
)
from app.services.trading.position_sizer_writer import LegacySizing


@pytest.fixture(autouse=True)
def _clear_log(db):
    """Ensure the sizer log is empty for each test."""
    db.execute(text("DELETE FROM trading_position_sizer_log"))
    db.commit()
    yield
    db.execute(text("DELETE FROM trading_position_sizer_log"))
    db.commit()


def _signal(**over) -> EmitterSignal:
    defaults = dict(
        source="test.emitter",
        ticker="PHHE_EMIT",
        direction="long",
        entry_price=100.0,
        stop_price=98.0,
        capital=50_000.0,
        target_price=103.0,
        asset_class="equity",
        user_id=None,
        pattern_id=None,
        regime="neutral",
        confidence=0.6,
    )
    defaults.update(over)
    return EmitterSignal(**defaults)


# ---------------------------------------------------------------------------
# Off-mode
# ---------------------------------------------------------------------------


class TestOffMode:
    def test_off_mode_returns_none_and_writes_nothing(self, db):
        with patch(
            "app.services.trading.position_sizer_writer.settings.brain_position_sizer_mode",
            "off",
        ):
            result = emit_shadow_proposal(
                db,
                signal=_signal(),
                legacy=LegacySizing(notional=10_000.0, quantity=100.0, source="legacy"),
            )
        assert result is None
        count = db.execute(
            text("SELECT COUNT(*) FROM trading_position_sizer_log"),
        ).scalar_one()
        assert count == 0


# ---------------------------------------------------------------------------
# Shadow-mode end-to-end
# ---------------------------------------------------------------------------


class TestShadowMode:
    def test_shadow_writes_row_with_legacy_divergence(self, db):
        # Patch NetEdgeRanker off so the emitter uses fallback inputs
        # (keeps this test isolated from Phase E behavior).
        with patch(
            "app.services.trading.position_sizer_writer.settings.brain_position_sizer_mode",
            "shadow",
        ), patch(
            "app.services.trading.net_edge_ranker.mode_is_active",
            return_value=False,
        ):
            result = emit_shadow_proposal(
                db,
                signal=_signal(),
                legacy=LegacySizing(
                    notional=5_000.0,
                    quantity=50.0,
                    source="alerts.legacy",
                ),
            )

        assert result is not None
        assert result.mode == "shadow"
        row = db.execute(
            text("""
                SELECT source, ticker, direction, mode, legacy_notional,
                       legacy_quantity, legacy_source, divergence_bps,
                       proposed_notional, proposed_quantity,
                       calibrated_prob, payoff_fraction, cost_fraction
                FROM trading_position_sizer_log
                ORDER BY id DESC
                LIMIT 1
            """),
        ).mappings().fetchone()
        assert row is not None
        assert row["source"] == "test.emitter"
        assert row["ticker"] == "PHHE_EMIT"
        assert row["direction"] == "long"
        assert row["mode"] == "shadow"
        assert row["legacy_notional"] == pytest.approx(5_000.0)
        assert row["legacy_quantity"] == pytest.approx(50.0)
        assert row["legacy_source"] == "alerts.legacy"
        assert row["divergence_bps"] is not None
        assert float(row["divergence_bps"]) >= 0.0
        assert float(row["calibrated_prob"]) == pytest.approx(0.6, abs=1e-6)
        assert float(row["payoff_fraction"]) >= 0.0
        # Fallback path (ranker off) yields zero costs.
        assert float(row["cost_fraction"]) == pytest.approx(0.0, abs=1e-9)

    def test_shadow_with_no_legacy_leaves_divergence_null(self, db):
        with patch(
            "app.services.trading.position_sizer_writer.settings.brain_position_sizer_mode",
            "shadow",
        ), patch(
            "app.services.trading.net_edge_ranker.mode_is_active",
            return_value=False,
        ):
            result = emit_shadow_proposal(
                db,
                signal=_signal(ticker="PHHE_NOLEG"),
                legacy=LegacySizing(notional=None, quantity=None, source="noleg"),
            )
        assert result is not None
        row = db.execute(
            text("""
                SELECT divergence_bps, legacy_notional
                FROM trading_position_sizer_log
                WHERE ticker = 'PHHE_NOLEG'
                ORDER BY id DESC
                LIMIT 1
            """),
        ).mappings().fetchone()
        assert row is not None
        assert row["legacy_notional"] is None
        assert row["divergence_bps"] is None


# ---------------------------------------------------------------------------
# Defensive behavior
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_invalid_prices_return_none_without_writing(self, db):
        with patch(
            "app.services.trading.position_sizer_writer.settings.brain_position_sizer_mode",
            "shadow",
        ):
            result = emit_shadow_proposal(
                db,
                signal=_signal(entry_price=0.0),
                legacy=LegacySizing(notional=1.0, quantity=1.0, source="bad"),
            )
        assert result is None
        count = db.execute(
            text("SELECT COUNT(*) FROM trading_position_sizer_log"),
        ).scalar_one()
        assert count == 0

    def test_zero_capital_returns_none(self, db):
        with patch(
            "app.services.trading.position_sizer_writer.settings.brain_position_sizer_mode",
            "shadow",
        ):
            result = emit_shadow_proposal(
                db,
                signal=_signal(capital=0.0),
                legacy=LegacySizing(notional=1.0, quantity=1.0, source="bad"),
            )
        assert result is None

    def test_crypto_ticker_routes_as_crypto_asset_class(self, db):
        with patch(
            "app.services.trading.position_sizer_writer.settings.brain_position_sizer_mode",
            "shadow",
        ), patch(
            "app.services.trading.net_edge_ranker.mode_is_active",
            return_value=False,
        ):
            sig = EmitterSignal(
                source="test.emitter",
                ticker="BTC-USD",
                direction="long",
                entry_price=50_000.0,
                stop_price=48_000.0,
                capital=20_000.0,
                target_price=55_000.0,
                asset_class=None,  # let the emitter infer
                confidence=0.6,
            )
            result = emit_shadow_proposal(
                db,
                signal=sig,
                legacy=LegacySizing(notional=1_000.0, quantity=0.02, source="paper"),
            )
        assert result is not None
        row = db.execute(
            text("""
                SELECT asset_class, correlation_bucket
                FROM trading_position_sizer_log
                WHERE ticker = 'BTC-USD'
                ORDER BY id DESC
                LIMIT 1
            """),
        ).mappings().fetchone()
        assert row is not None
        assert row["asset_class"] == "crypto"
