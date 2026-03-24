"""Phase 4: prediction mirror dual-write (Postgres). Parity field set is frozen; see app/trading_brain/README.md."""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from app.models.trading_brain_phase1 import BrainPredictionLine, BrainPredictionSnapshot
from app.trading_brain.infrastructure.prediction_line_mapper import (
    legacy_prediction_rows_to_dtos,
    prediction_universe_fingerprint,
)
from app.trading_brain.infrastructure.prediction_mirror_session import (
    brain_prediction_mirror_write_dedicated,
)
from app.trading_brain.infrastructure.repositories.prediction_snapshot_sqlalchemy import (
    SqlAlchemyBrainPredictionSnapshotRepository,
)
from app.trading_brain.schemas.prediction_snapshot import PredictionSnapshotSealDTO


def _norm_json(x: object) -> str:
    return json.dumps(x, sort_keys=True, default=str)


def _assert_line_parity(legacy: dict, row: BrainPredictionLine) -> None:
    """Frozen parity: ticker, sort_rank, score, confidence, direction, price, meta_ml, regime, signals, patterns, risk fields."""
    assert row.ticker == legacy["ticker"]
    assert row.score == pytest.approx(float(legacy["score"]))
    assert row.confidence == legacy.get("confidence")
    assert row.direction == legacy.get("direction")
    if legacy.get("price") is None:
        assert row.price is None
    else:
        assert row.price == pytest.approx(float(legacy["price"]))
    if legacy.get("meta_ml_probability") is None:
        assert row.meta_ml_probability is None
    else:
        assert row.meta_ml_probability == pytest.approx(float(legacy["meta_ml_probability"]))
    assert row.vix_regime == legacy.get("vix_regime")
    assert row.signals_json == list(legacy.get("signals") or [])
    assert _norm_json(row.matched_patterns_json) == _norm_json(legacy.get("matched_patterns") or [])
    if legacy.get("suggested_stop") is None:
        assert row.suggested_stop is None
    else:
        assert row.suggested_stop == pytest.approx(float(legacy["suggested_stop"]))
    if legacy.get("suggested_target") is None:
        assert row.suggested_target is None
    else:
        assert row.suggested_target == pytest.approx(float(legacy["suggested_target"]))
    if legacy.get("risk_reward") is None:
        assert row.risk_reward is None
    else:
        assert row.risk_reward == pytest.approx(float(legacy["risk_reward"]))
    if legacy.get("position_size_pct") is None:
        assert row.position_size_pct is None
    else:
        assert row.position_size_pct == pytest.approx(float(legacy["position_size_pct"]))


def test_prediction_universe_fingerprint_order_invariant() -> None:
    a = prediction_universe_fingerprint(["ZZ", "aa", "aa"])
    b = prediction_universe_fingerprint(["AA", "zz"])
    assert a == b


def test_legacy_rows_to_dtos_sort_rank_and_parity_fields() -> None:
    legacy = [
        {
            "ticker": "FOO",
            "score": -2.5,
            "confidence": 40,
            "direction": "short",
            "price": 100.0,
            "meta_ml_probability": 0.42,
            "signals": ["Pattern: x"],
            "matched_patterns": [{"name": "p1", "win_rate": 55}],
            "vix_regime": "elevated",
            "suggested_stop": 101.0,
            "suggested_target": 98.0,
            "risk_reward": 2.0,
            "position_size_pct": 1.5,
        },
        {
            "ticker": "BAR",
            "score": 3.0,
            "confidence": 80,
            "direction": "long",
            "price": 50.0,
            "meta_ml_probability": None,
            "signals": [],
            "matched_patterns": [],
            "vix_regime": "normal",
            "suggested_stop": None,
            "suggested_target": None,
            "risk_reward": None,
            "position_size_pct": None,
        },
    ]
    dtos = legacy_prediction_rows_to_dtos(legacy)
    assert len(dtos) == 2
    assert dtos[0].sort_rank == 0 and dtos[0].ticker == "FOO"
    assert dtos[1].sort_rank == 1 and dtos[1].ticker == "BAR"


def test_seal_snapshot_append_only_and_parity(db: Session) -> None:
    repo = SqlAlchemyBrainPredictionSnapshotRepository()
    legacy_a = [
        {
            "ticker": "X",
            "score": 5.0,
            "confidence": 70,
            "direction": "long",
            "price": 10.0,
            "meta_ml_probability": 0.6,
            "signals": ["a"],
            "matched_patterns": [{"name": "m"}],
            "vix_regime": "normal",
            "suggested_stop": 9.0,
            "suggested_target": 12.0,
            "risk_reward": 2.0,
            "position_size_pct": 2.0,
        }
    ]
    h1 = PredictionSnapshotSealDTO(
        universe_fingerprint=prediction_universe_fingerprint(["x"]),
        ticker_count=1,
    )
    sid1 = repo.seal_snapshot(db, header=h1, lines=legacy_prediction_rows_to_dtos(legacy_a))
    db.commit()

    legacy_b = [
        {
            "ticker": "Y",
            "score": 1.0,
            "confidence": 50,
            "direction": "neutral",
            "price": 20.0,
            "meta_ml_probability": None,
            "signals": [],
            "matched_patterns": [],
            "vix_regime": "extreme",
            "suggested_stop": None,
            "suggested_target": None,
            "risk_reward": None,
            "position_size_pct": None,
        }
    ]
    h2 = PredictionSnapshotSealDTO(
        universe_fingerprint=prediction_universe_fingerprint(["y"]),
        ticker_count=1,
    )
    sid2 = repo.seal_snapshot(db, header=h2, lines=legacy_prediction_rows_to_dtos(legacy_b))
    db.commit()

    assert sid2 != sid1
    n_snap = db.query(BrainPredictionSnapshot).count()
    assert n_snap >= 2

    row = db.query(BrainPredictionLine).filter(BrainPredictionLine.snapshot_id == sid1).one()
    assert row.sort_rank == 0
    _assert_line_parity(legacy_a[0], row)


def test_mirror_write_skips_empty_and_respects_flag(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "brain_prediction_dual_write_enabled", True, raising=False)
    before = db.query(BrainPredictionSnapshot).count()
    assert brain_prediction_mirror_write_dedicated(legacy_rows=[], universe_fingerprint="x", ticker_count=0) is None
    db.commit()
    assert db.query(BrainPredictionSnapshot).count() == before

    monkeypatch.setattr(settings, "brain_prediction_dual_write_enabled", False, raising=False)
    legacy = [{"ticker": "Z", "score": 1.0, "confidence": 1, "direction": "long", "vix_regime": "n"}]
    assert (
        brain_prediction_mirror_write_dedicated(
            legacy_rows=legacy,
            universe_fingerprint="ab" * 32,
            ticker_count=1,
        )
        is None
    )


def test_mirror_write_dedicated_happy_path(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "brain_prediction_dual_write_enabled", True, raising=False)
    legacy = [
        {
            "ticker": "Z",
            "score": 2.0,
            "confidence": 55,
            "direction": "long",
            "price": 5.0,
            "meta_ml_probability": 0.51,
            "signals": ["s"],
            "matched_patterns": [{"name": "p"}],
            "vix_regime": "normal",
            "suggested_stop": 4.0,
            "suggested_target": 7.0,
            "risk_reward": 1.5,
            "position_size_pct": 3.0,
        }
    ]
    fp = prediction_universe_fingerprint(["z"])
    sid = brain_prediction_mirror_write_dedicated(legacy_rows=legacy, universe_fingerprint=fp, ticker_count=1)
    assert sid is not None
    db.expire_all()
    row = db.query(BrainPredictionLine).filter(BrainPredictionLine.snapshot_id == sid).one()
    _assert_line_parity(legacy[0], row)
