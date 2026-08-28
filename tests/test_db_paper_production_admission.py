"""DB-paper production admission ceremony — Codex item #4 integration tests.

ZERO fixture injection: walang db_paper_final_admission_provider with-block,
walang monkeypatched binding, walang hand-built material — ang production path
mismo. (Ang buong ceremony-to-fill ay sakop na rin ng
test_momentum_paper_runner.py::test_paper_runner_writes_runtime_snapshot_and_sim_fill
na dumadaan na ngayon sa production provider.)

Runnable: pytest tests/test_db_paper_production_admission.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.db_paper_identity import (
    resolve_db_paper_account_binding,
)


def _make_variant(db):
    from app.models.trading import MomentumStrategyVariant

    import uuid as _uuid

    v = MomentumStrategyVariant(
        family="momentum_pullback",
        variant_key=f"dbp-int-{_uuid.uuid4().hex[:8]}",
        params_json={},
        label="dbp-int-test",
        execution_family="alpaca_spot",
    )
    db.add(v)
    db.flush()
    return v


def test_created_paper_session_is_born_with_the_binding(db, paired_client):
    """Codex item #1: ang binding ay nasa PAREHONG transaction ng paglikha."""
    from app.models.core import User
    from app.services.trading.momentum_neural.operator_actions import (
        create_paper_draft_session,
    )

    user = db.query(User).first()
    variant = _make_variant(db)
    from app.models.trading import MomentumSymbolViability
    from datetime import datetime, timezone

    db.add(MomentumSymbolViability(
        symbol="DBPT",
        variant_id=int(variant.id),
        viability_score=0.9,
        paper_eligible=True,
        live_eligible=False,
        freshness_ts=datetime.now(timezone.utc).replace(tzinfo=None),
        regime_snapshot_json={},
        execution_readiness_json={"spread_bps": 10.0},
        explain_json={},
        evidence_window_json={},
        scope="symbol",
    ))
    db.flush()
    res = create_paper_draft_session(
        db, user_id=int(user.id), symbol="DBPT", variant_id=int(variant.id),
        execution_family="alpaca_spot",
    )
    assert res.get("ok"), res
    from app.models.trading import TradingAutomationSession

    sess = db.query(TradingAutomationSession).filter_by(
        id=int(res["session_id"])
    ).one()
    binding = (sess.risk_snapshot_json or {}).get("db_paper_account_binding")
    assert isinstance(binding, dict), "ipinanganak nang unbound ang session"
    assert binding == resolve_db_paper_account_binding(db)
    assert binding["account_scope"] == "db-paper:chili_test"


def test_legacy_flat_session_is_self_healed_on_tick(db, paired_client, monkeypatch):
    """Codex item #3 Layer 2: ang flat legacy session ay nabibigyan ng binding
    sa unang tick, sa ilalim ng row lock."""
    from app.models.core import User
    from app.services.trading.momentum_neural.operator_actions import (
        create_paper_draft_session,
    )
    from app.models.trading import (
        MomentumSymbolViability,
        TradingAutomationSession,
    )
    from app.services.trading.momentum_neural import paper_runner
    from datetime import datetime, timezone

    from app.config import settings as _settings

    monkeypatch.setattr(
        _settings, "chili_momentum_paper_runner_enabled", True, raising=False
    )
    user = db.query(User).first()
    variant = _make_variant(db)
    db.add(MomentumSymbolViability(
        symbol="DBPH",
        variant_id=int(variant.id),
        viability_score=0.9,
        paper_eligible=True,
        live_eligible=False,
        freshness_ts=datetime.now(timezone.utc).replace(tzinfo=None),
        regime_snapshot_json={},
        execution_readiness_json={"spread_bps": 10.0},
        explain_json={},
        evidence_window_json={},
        scope="symbol",
    ))
    db.flush()
    res = create_paper_draft_session(
        db, user_id=int(user.id), symbol="DBPH", variant_id=int(variant.id),
        execution_family="alpaca_spot",
    )
    assert res.get("ok"), res
    sess = db.query(TradingAutomationSession).filter_by(
        id=int(res["session_id"])
    ).one()
    # gayahin ang legacy: tanggalin ang binding
    snap = dict(sess.risk_snapshot_json or {})
    snap.pop("db_paper_account_binding", None)
    sess.risk_snapshot_json = snap
    db.flush()
    out = paper_runner.tick_paper_session(
        db, int(sess.id), quote_fn=lambda s: {"bid": 5.0, "ask": 5.02, "mid": 5.01}
    )
    assert out is not None
    db.refresh(sess)
    healed = (sess.risk_snapshot_json or {}).get("db_paper_account_binding")
    assert isinstance(healed, dict), f"hindi nag-self-heal: {out}"
    assert healed == resolve_db_paper_account_binding(db)


def test_existing_binding_is_never_rewritten(db, paired_client, monkeypatch):
    """Ang maling scope ay dapat MANATILING kita bilang mismatch veto —
    hindi tahimik na 'inaayos'."""
    from app.models.core import User
    from app.services.trading.momentum_neural.operator_actions import (
        create_paper_draft_session,
    )
    from app.models.trading import (
        MomentumSymbolViability,
        TradingAutomationSession,
    )
    from app.services.trading.momentum_neural import paper_runner
    from datetime import datetime, timezone

    from app.config import settings as _settings

    monkeypatch.setattr(
        _settings, "chili_momentum_paper_runner_enabled", True, raising=False
    )
    user = db.query(User).first()
    variant = _make_variant(db)
    db.add(MomentumSymbolViability(
        symbol="DBPW",
        variant_id=int(variant.id),
        viability_score=0.9,
        paper_eligible=True,
        live_eligible=False,
        freshness_ts=datetime.now(timezone.utc).replace(tzinfo=None),
        regime_snapshot_json={},
        execution_readiness_json={"spread_bps": 10.0},
        explain_json={},
        evidence_window_json={},
        scope="symbol",
    ))
    db.flush()
    res = create_paper_draft_session(
        db, user_id=int(user.id), symbol="DBPW", variant_id=int(variant.id),
        execution_family="alpaca_spot",
    )
    sess = db.query(TradingAutomationSession).filter_by(
        id=int(res["session_id"])
    ).one()
    snap = dict(sess.risk_snapshot_json or {})
    wrong = {
        "account_scope": "db-paper:wrong",
        "account_identity_sha256": "0" * 64,
    }
    snap["db_paper_account_binding"] = wrong
    sess.risk_snapshot_json = snap
    db.flush()
    paper_runner.tick_paper_session(
        db, int(sess.id), quote_fn=lambda s: {"bid": 5.0, "ask": 5.02, "mid": 5.01}
    )
    db.refresh(sess)
    kept = (sess.risk_snapshot_json or {}).get("db_paper_account_binding")
    assert kept["account_scope"] == "db-paper:wrong", "ni-rewrite ang binding!"
