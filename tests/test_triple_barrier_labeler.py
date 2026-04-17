"""DB integration tests for `triple_barrier_labeler` (Phase D)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models.trading import TripleBarrierLabelRow
from app.services.trading.triple_barrier import (
    OHLCVBar,
    TripleBarrierConfig,
)
from app.services.trading.triple_barrier_labeler import (
    label_single,
    label_summary,
    mode_is_active,
)


def _cfg() -> TripleBarrierConfig:
    return TripleBarrierConfig(tp_pct=0.02, sl_pct=0.01, max_bars=5, side="long")


def _tp_bars() -> list[OHLCVBar]:
    return [OHLCVBar(100, 103, 99.5, 102.5)]


def _sl_bars() -> list[OHLCVBar]:
    return [OHLCVBar(100, 100.5, 98.5, 99.0)]


class TestLabelSingle:
    def test_off_mode_does_not_insert(self, db, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.triple_barrier_labeler.settings.brain_triple_barrier_mode",
            "off",
            raising=False,
        )
        out = label_single(
            db,
            ticker="AAPL",
            label_date=date(2026, 1, 15),
            entry_close=100.0,
            future_bars=_tp_bars(),
            cfg=_cfg(),
        )
        assert out.inserted is False
        # label still computed so callers can inspect it
        assert out.label.label == 1
        assert out.label.barrier_hit == "tp"
        # DB row must not exist
        rows = db.query(TripleBarrierLabelRow).filter_by(ticker="AAPL").all()
        assert rows == []

    def test_shadow_mode_inserts(self, db):
        out = label_single(
            db,
            ticker="MSFT",
            label_date=date(2026, 1, 15),
            entry_close=100.0,
            future_bars=_tp_bars(),
            cfg=_cfg(),
            mode_override="shadow",
        )
        assert out.inserted is True
        row = db.query(TripleBarrierLabelRow).filter_by(ticker="MSFT").one()
        assert row.label == 1
        assert row.barrier_hit == "tp"
        assert row.mode == "shadow"
        assert row.side == "long"
        assert row.tp_pct == pytest.approx(0.02)
        assert row.sl_pct == pytest.approx(0.01)
        assert row.max_bars == 5
        assert row.realized_return_pct == pytest.approx(0.02)

    def test_idempotent_upsert(self, db):
        out1 = label_single(
            db,
            ticker="GOOG",
            label_date=date(2026, 1, 15),
            entry_close=100.0,
            future_bars=_tp_bars(),
            cfg=_cfg(),
            mode_override="shadow",
        )
        out2 = label_single(
            db,
            ticker="GOOG",
            label_date=date(2026, 1, 15),
            entry_close=100.0,
            future_bars=_tp_bars(),
            cfg=_cfg(),
            mode_override="shadow",
        )
        assert out1.inserted is True
        assert out2.inserted is False
        rows = db.query(TripleBarrierLabelRow).filter_by(ticker="GOOG").all()
        assert len(rows) == 1

    def test_different_barrier_configs_coexist(self, db):
        """Same (ticker, date) but different barriers → distinct rows."""
        tight = TripleBarrierConfig(tp_pct=0.01, sl_pct=0.005, max_bars=5, side="long")
        wide = TripleBarrierConfig(tp_pct=0.03, sl_pct=0.02, max_bars=5, side="long")
        label_single(
            db, ticker="NVDA", label_date=date(2026, 1, 15),
            entry_close=100.0, future_bars=_tp_bars(),
            cfg=tight, mode_override="shadow",
        )
        label_single(
            db, ticker="NVDA", label_date=date(2026, 1, 15),
            entry_close=100.0, future_bars=_tp_bars(),
            cfg=wide, mode_override="shadow",
        )
        rows = db.query(TripleBarrierLabelRow).filter_by(ticker="NVDA").all()
        assert len(rows) == 2

    def test_sl_outcome_written(self, db):
        out = label_single(
            db,
            ticker="AMD",
            label_date=date(2026, 1, 15),
            entry_close=100.0,
            future_bars=_sl_bars(),
            cfg=_cfg(),
            mode_override="shadow",
        )
        assert out.inserted is True
        row = db.query(TripleBarrierLabelRow).filter_by(ticker="AMD").one()
        assert row.label == -1
        assert row.barrier_hit == "sl"
        assert row.realized_return_pct == pytest.approx(-0.01)

    def test_missing_data_still_records(self, db):
        """Empty future_bars → label=0, barrier_hit=missing_data, row still inserted."""
        out = label_single(
            db,
            ticker="TSLA",
            label_date=date(2026, 1, 15),
            entry_close=100.0,
            future_bars=[],
            cfg=_cfg(),
            mode_override="shadow",
        )
        assert out.inserted is True
        row = db.query(TripleBarrierLabelRow).filter_by(ticker="TSLA").one()
        assert row.barrier_hit == "missing_data"
        assert row.label == 0

    def test_snapshot_id_persisted(self, db):
        out = label_single(
            db,
            ticker="SHOP",
            label_date=date(2026, 1, 15),
            entry_close=100.0,
            future_bars=_tp_bars(),
            cfg=_cfg(),
            snapshot_id=42_999,
            mode_override="shadow",
        )
        assert out.inserted is True
        row = db.query(TripleBarrierLabelRow).filter_by(ticker="SHOP").one()
        assert row.snapshot_id == 42_999


class TestModeGate:
    def test_mode_is_active_default_off(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.triple_barrier_labeler.settings.brain_triple_barrier_mode",
            "off",
            raising=False,
        )
        assert mode_is_active() is False

    def test_mode_is_active_shadow(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.triple_barrier_labeler.settings.brain_triple_barrier_mode",
            "shadow",
            raising=False,
        )
        assert mode_is_active() is True

    def test_mode_override_wins(self):
        assert mode_is_active(override="shadow") is True
        assert mode_is_active(override="off") is False

    def test_bogus_mode_treated_as_off(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.trading.triple_barrier_labeler.settings.brain_triple_barrier_mode",
            "wild_west",
            raising=False,
        )
        assert mode_is_active() is False


class TestLabelSummary:
    def test_empty_summary(self, db):
        # No rows in the fresh schema → all zeros but keys present
        out = label_summary(db, lookback_hours=24)
        assert out["labels_total"] == 0
        assert out["tickers_distinct"] == 0
        assert out["by_barrier"] == {"tp": 0, "sl": 0, "timeout": 0, "missing_data": 0}
        assert out["label_distribution"] == {"+1": 0, "-1": 0, "0": 0}
        assert out["last_label_at"] is None
        assert "tp_pct_cfg" in out and "sl_pct_cfg" in out

    def test_mixed_summary(self, db):
        label_single(
            db, ticker="AAA", label_date=date(2026, 1, 15),
            entry_close=100.0, future_bars=_tp_bars(),
            cfg=_cfg(), mode_override="shadow",
        )
        label_single(
            db, ticker="BBB", label_date=date(2026, 1, 15),
            entry_close=100.0, future_bars=_sl_bars(),
            cfg=_cfg(), mode_override="shadow",
        )
        label_single(
            db, ticker="CCC", label_date=date(2026, 1, 15),
            entry_close=100.0,
            future_bars=[OHLCVBar(100, 100.5, 99.5, 100.1)] * 5,  # timeout
            cfg=_cfg(), mode_override="shadow",
        )
        out = label_summary(db, lookback_hours=24)
        assert out["labels_total"] == 3
        assert out["tickers_distinct"] == 3
        assert out["by_barrier"]["tp"] == 1
        assert out["by_barrier"]["sl"] == 1
        assert out["by_barrier"]["timeout"] == 1
        assert out["label_distribution"] == {"+1": 1, "-1": 1, "0": 1}
