"""Whether a stored backtest ``strategy_name`` matches the linked ``ScanPattern.name`` (truncation-aware)."""
from __future__ import annotations

_STRATEGY_COL_LEN = 100


def strategy_label_aligns_scan_pattern_name(
    strategy_name: str | None,
    pattern_name: str | None,
) -> bool:
    """Align if equal or one side is a 100-char prefix of the other (DB column limit)."""
    spn = (pattern_name or "").strip()
    rsn = (strategy_name or "").strip()
    if not spn or not rsn:
        return False
    if rsn == spn:
        return True
    if len(spn) > _STRATEGY_COL_LEN and rsn == spn[:_STRATEGY_COL_LEN]:
        return True
    if len(rsn) > _STRATEGY_COL_LEN and spn == rsn[:_STRATEGY_COL_LEN]:
        return True
    return False
