from __future__ import annotations

from app.models.trading import Trade
from app.services.trading.management_envelopes import (
    LEGACY_TRADES_COMPAT_RELATION,
    MANAGEMENT_ENVELOPES_RELATION,
)


def test_trade_orm_mapper_remains_legacy_compatibility_contract() -> None:
    assert MANAGEMENT_ENVELOPES_RELATION == "trading_management_envelopes"
    assert LEGACY_TRADES_COMPAT_RELATION == "trading_trades"
    assert Trade.__tablename__ == LEGACY_TRADES_COMPAT_RELATION
    assert Trade.__table__.name == LEGACY_TRADES_COMPAT_RELATION


def test_trade_id_public_identity_remains_envelope_id() -> None:
    assert Trade.id.property.columns[0].name == "id"
    assert Trade.position_id.property.columns[0].name == "position_id"
    assert Trade.decision_id.property.columns[0].name == "decision_id"
