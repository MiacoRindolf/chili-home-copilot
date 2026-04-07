"""Selective normalization: canonical params + ``trading_backtest_param_sets``."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.models.trading import BacktestParamSet, BacktestResult, ScanPattern, TradingInsight
from app.services.trading.backtest_param_sets import (
    canonical_params_dict,
    get_or_create_backtest_param_set,
    materialize_backtest_params,
    param_hash_sha256,
)


def test_canonicalize_key_order_hash_stable() -> None:
    a = {"z": 1, "a": {"m": 2, "b": 3}}
    b = {"a": {"b": 3, "m": 2}, "z": 1}
    ca, cb = canonical_params_dict(a), canonical_params_dict(b)
    assert param_hash_sha256(ca) == param_hash_sha256(cb)


def test_distinct_params_distinct_hash() -> None:
    x = canonical_params_dict({"period": "1y"})
    y = canonical_params_dict({"period": "2y"})
    assert param_hash_sha256(x) != param_hash_sha256(y)


@pytest.mark.usefixtures("db")
def test_get_or_create_reuses_same_param_set(db: Session) -> None:
    p = {"period": "6mo", "interval": "1d", "ohlc_bars": 120}
    id1 = get_or_create_backtest_param_set(db, p)
    id2 = get_or_create_backtest_param_set(db, {"interval": "1d", "ohlc_bars": 120, "period": "6mo"})
    db.commit()
    assert id1 is not None and id2 is not None
    assert id1 == id2
    assert db.query(BacktestParamSet).count() == 1


@pytest.mark.usefixtures("db")
def test_materialize_prefers_params_then_param_set(db: Session) -> None:
    user = User(name=f"paramset_t_{uuid.uuid4().hex[:10]}")
    db.add(user)
    db.commit()
    db.refresh(user)
    sp = ScanPattern(
        name="NormT",
        description="d",
        rules_json="{}",
        origin="user",
    )
    db.add(sp)
    db.commit()
    db.refresh(sp)
    ins = TradingInsight(
        user_id=user.id,
        scan_pattern_id=sp.id,
        pattern_description="x",
        confidence=0.5,
        evidence_count=1,
    )
    db.add(ins)
    db.commit()
    db.refresh(ins)

    canon = canonical_params_dict({"period": "3mo", "interval": "1d"})
    h = param_hash_sha256(canon)
    ps = BacktestParamSet(param_hash=h, params_json=canon)
    db.add(ps)
    db.commit()
    db.refresh(ps)

    bt = BacktestResult(
        user_id=user.id,
        ticker="AAA",
        strategy_name="NormT",
        params=None,
        param_set_id=ps.id,
        return_pct=0.0,
        win_rate=0.5,
        trade_count=0,
        max_drawdown=0.0,
        related_insight_id=ins.id,
        scan_pattern_id=sp.id,
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)

    out = materialize_backtest_params(db, bt)
    assert out.get("period") == "3mo"
    assert out.get("interval") == "1d"


@pytest.mark.usefixtures("db")
def test_save_backtest_sets_param_set_id(db: Session) -> None:
    from app.services.backtest_service import save_backtest

    user = User(name=f"paramset_save_{uuid.uuid4().hex[:10]}")
    db.add(user)
    db.commit()
    db.refresh(user)
    sp = ScanPattern(
        name="SaveNorm",
        description="d",
        rules_json="{}",
        origin="user",
    )
    db.add(sp)
    db.commit()
    db.refresh(sp)
    ins = TradingInsight(
        user_id=user.id,
        scan_pattern_id=sp.id,
        pattern_description="x",
        confidence=0.5,
        evidence_count=1,
    )
    db.add(ins)
    db.commit()
    db.refresh(ins)

    result = {
        "ok": True,
        "ticker": "NVDA",
        "strategy": "SaveNorm",
        "return_pct": 1.0,
        "win_rate": 0.5,
        "trade_count": 2,
        "equity_curve": [],
        "period": "6mo",
        "interval": "1d",
    }
    rec = save_backtest(db, user.id, result, insight_id=ins.id, scan_pattern_id=sp.id)
    db.refresh(rec)
    assert rec.param_set_id is not None
    n_ps = db.query(BacktestParamSet).filter(BacktestParamSet.id == rec.param_set_id).count()
    assert n_ps == 1


@pytest.mark.usefixtures("db")
def test_materialize_prefers_denormalized_params_over_param_set(db: Session) -> None:
    canon = canonical_params_dict({"period": "1y"})
    h = param_hash_sha256(canon)
    ps = BacktestParamSet(param_hash=h, params_json=canon)
    db.add(ps)
    db.commit()
    db.refresh(ps)
    bt = BacktestResult(
        user_id=None,
        ticker="ZZZ",
        strategy_name="S",
        params={"period": "9mo", "interval": "1d"},
        param_set_id=ps.id,
        return_pct=0.0,
        win_rate=0.5,
        trade_count=0,
        max_drawdown=0.0,
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)
    assert materialize_backtest_params(db, bt).get("period") == "9mo"
