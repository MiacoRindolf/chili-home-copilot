"""Phase 11: execution_family registry and seams (no arbitrage behavior)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.trading.execution_family_registry import (
    DOCUMENTED_EXECUTION_FAMILIES,
    EXECUTION_FAMILY_BASIS_TRADE,
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE,
    ExecutionFamilyNotImplementedError,
    execution_family_capabilities,
    is_documented_execution_family,
    is_momentum_automation_implemented,
    momentum_execution_seam_meta,
    normalize_execution_family,
    resolve_live_spot_adapter_factory,
)


def test_normalize_execution_family_defaults() -> None:
    assert normalize_execution_family(None) == EXECUTION_FAMILY_COINBASE_SPOT
    assert normalize_execution_family("") == EXECUTION_FAMILY_COINBASE_SPOT
    assert normalize_execution_family("  Coinbase_Spot ") == EXECUTION_FAMILY_COINBASE_SPOT


def test_documented_vs_implemented() -> None:
    assert is_documented_execution_family(EXECUTION_FAMILY_COINBASE_SPOT)
    assert is_momentum_automation_implemented(EXECUTION_FAMILY_COINBASE_SPOT)
    assert is_documented_execution_family(EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE)
    assert not is_momentum_automation_implemented(EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE)
    assert not is_documented_execution_family("unknown_family_xyz")


def test_resolve_live_spot_adapter_factory_coinbase_only() -> None:
    factory = resolve_live_spot_adapter_factory(EXECUTION_FAMILY_COINBASE_SPOT)
    assert callable(factory)
    inst = factory()
    assert inst is not None
    with pytest.raises(ExecutionFamilyNotImplementedError):
        resolve_live_spot_adapter_factory(EXECUTION_FAMILY_BASIS_TRADE)


def test_capabilities_payload_shape() -> None:
    caps = execution_family_capabilities()
    assert len(caps) == len(DOCUMENTED_EXECUTION_FAMILIES)
    by_id = {c["id"]: c for c in caps}
    assert by_id[EXECUTION_FAMILY_COINBASE_SPOT]["status"] == "implemented"
    assert by_id[EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE]["status"] == "planned"


def test_momentum_execution_seam_meta() -> None:
    m = momentum_execution_seam_meta()
    assert "strategy_vs_execution" in m
    assert EXECUTION_FAMILY_COINBASE_SPOT in m["implemented_automation_families"]


def test_registry_module_avoids_learning_import() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    p = root / "app" / "services" / "trading" / "execution_family_registry.py"
    text = p.read_text(encoding="utf-8")
    assert "learning.py" not in text
    assert "from app.services.trading.learning" not in text


def _risk_eval_db_mock() -> MagicMock:
    """Query chains return empty (one_or_none None, count 0)."""
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value = q
    q.one_or_none.return_value = None
    q.count.return_value = 0
    q.all.return_value = []
    db.query.return_value = q
    return db


def test_risk_evaluator_blocks_unknown_execution_family() -> None:
    """No DB: unknown execution_family is blocked before DB-heavy checks."""
    from app.services.trading.momentum_neural.risk_evaluator import evaluate_proposed_momentum_automation

    db = _risk_eval_db_mock()
    out = evaluate_proposed_momentum_automation(
        db,
        user_id=1,
        symbol="BTC-USD",
        variant_id=1,
        mode="paper",
        execution_family="definitely_not_a_registered_family",
    )
    assert out.get("allowed") is False
    errs = " ".join(str(e).lower() for e in (out.get("errors") or []))
    assert "unknown" in errs or "documented" in errs


def test_risk_evaluator_blocks_documented_but_unimplemented_family() -> None:
    from app.services.trading.momentum_neural.risk_evaluator import evaluate_proposed_momentum_automation

    db = _risk_eval_db_mock()
    out = evaluate_proposed_momentum_automation(
        db,
        user_id=1,
        symbol="BTC-USD",
        variant_id=1,
        mode="paper",
        execution_family=EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE,
    )
    assert out.get("allowed") is False
    assert any("not implemented" in str(e).lower() for e in (out.get("errors") or []))


def test_enqueue_refresh_rejects_unimplemented_family() -> None:
    from app.services.trading.momentum_neural.operator_actions import enqueue_symbol_refresh

    out = enqueue_symbol_refresh(MagicMock(), symbol="BTC-USD", execution_family=EXECUTION_FAMILY_BASIS_TRADE)
    assert out.get("ok") is False
    assert out.get("reason") == "execution_family_not_implemented"
