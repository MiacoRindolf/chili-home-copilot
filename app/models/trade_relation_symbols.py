"""Central relation symbols for trading compatibility surfaces."""

MANAGEMENT_ENVELOPES_RELATION = "trading_management_envelopes"
LEGACY_TRADES_COMPAT_RELATION = "trading_trades"
LEGACY_TRADE_ID_FK = f"{LEGACY_TRADES_COMPAT_RELATION}.id"
