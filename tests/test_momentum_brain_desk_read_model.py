"""Momentum brain desk read-model: fast-path stats + degrade-instead-of-hang.

The desk/summary endpoints hung >45s on prod because ``_viability_durable_stats``
seq-scanned ``momentum_symbol_viability`` per request (top-5 score sort alone
measured 48.8s). These tests pin:

1. correctness of the rewritten single-pass stats + payload shape, and
2. the degrade path: a failing viability statement (real poisoned Postgres
   transaction) must not 500 the payload and must not poison the session for
   the later outcome-window sections.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
from app.services.trading.momentum_neural import brain_desk_summary as bds
from app.services.trading.momentum_neural.persistence import (
    ensure_momentum_strategy_variants,
)

pytestmark = pytest.mark.usefixtures("_asgi_test_client")


def _variant(db: Session) -> MomentumStrategyVariant:
    ensure_momentum_strategy_variants(db)
    db.commit()
    return (
        db.query(MomentumStrategyVariant)
        .order_by(MomentumStrategyVariant.id.asc())
        .first()
    )


def _seed_viability(
    db: Session,
    *,
    symbol: str,
    variant_id: int,
    viability_score: float,
    paper_eligible: bool,
    live_eligible: bool,
) -> None:
    db.add(
        MomentumSymbolViability(
            symbol=symbol,
            scope="symbol",
            variant_id=variant_id,
            viability_score=viability_score,
            paper_eligible=paper_eligible,
            live_eligible=live_eligible,
            freshness_ts=datetime.utcnow(),
            regime_snapshot_json={},
            execution_readiness_json={},
            explain_json={},
            evidence_window_json={},
            source_node_id="test_seed",
            correlation_id="test-desk",
        )
    )


def _seed_three(db: Session) -> None:
    v = _variant(db)
    _seed_viability(
        db, symbol="TOPL-USD", variant_id=v.id, viability_score=0.91,
        paper_eligible=True, live_eligible=True,
    )
    _seed_viability(
        db, symbol="MIDP-USD", variant_id=v.id, viability_score=0.55,
        paper_eligible=True, live_eligible=False,
    )
    _seed_viability(
        db, symbol="LOWN-USD", variant_id=v.id, viability_score=0.72,
        paper_eligible=False, live_eligible=False,
    )
    db.commit()


def test_viability_durable_stats_single_pass_counts(db: Session) -> None:
    _seed_three(db)
    stats = bds._viability_durable_stats(db)
    assert stats.get("error") is None
    assert stats["row_count"] == 3
    assert stats["live_eligible_count"] == 1
    assert stats["paper_only_count"] == 1
    assert stats["fresh_last_24h_count"] == 3
    # Highest score first; scalar-column path keeps the exact preview format.
    assert stats["top_lines"][0] == "TOPL-USD · 0.91 · live"
    assert stats["top_lines"][1] == "LOWN-USD · 0.72 · paper-only"
    assert len(stats["top_lines"]) == 3


def test_desk_payload_shape(db: Session) -> None:
    _seed_three(db)
    payload = bds.get_momentum_brain_desk_payload(db)
    assert payload["ok"] is True
    pool_card = payload["nodes"][bds.VIABILITY_NODE_ID]
    assert pool_card["durable_row_count"] == 3
    assert pool_card["live_eligible_count"] == 1
    assert payload["outcomes_window"]["table_present"] is True
    assert "badges" in payload and "momentum_panel" in payload


def test_desk_payload_degrades_and_session_survives_poisoned_txn(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing viability statement aborts the Postgres transaction; the payload
    must degrade that section only — later sections (outcome windows) and the
    caller's session must keep working."""
    _seed_three(db)

    def _poison(session: Session):
        session.execute(text("SELECT 1 / 0"))

    monkeypatch.setattr(bds, "_viability_counts", _poison)
    payload = bds.get_momentum_brain_desk_payload(db)
    assert payload["ok"] is True
    pool_card = payload["nodes"][bds.VIABILITY_NODE_ID]
    assert pool_card["durable_row_count"] == 0
    # The session was recovered after the aborted txn: the outcome-window section
    # (queries AFTER the failure) still produced a real answer, not a cascade.
    assert payload["outcomes_window"]["table_present"] is True
    assert "error" not in payload["outcomes_window"]
    # And the caller can keep using the session afterwards.
    assert db.execute(text("SELECT 1")).scalar() == 1


def test_statement_timeout_is_armed_locally(db: Session) -> None:
    """build context arms a txn-scoped statement_timeout and leaves the session
    usable; the setting must not leak past a rollback."""
    _seed_three(db)
    bds.build_momentum_neural_graph_context(db)
    # pg_settings.setting reports raw milliseconds (SHOW would normalize to '5s').
    raw = text("SELECT setting FROM pg_settings WHERE name = 'statement_timeout'")
    val = db.execute(raw).scalar()
    assert val == str(bds._READ_MODEL_STATEMENT_TIMEOUT_MS)
    db.rollback()
    val_after = db.execute(raw).scalar()
    assert val_after != str(bds._READ_MODEL_STATEMENT_TIMEOUT_MS)
