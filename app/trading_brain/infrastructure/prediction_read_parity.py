"""Frozen parity: legacy list[dict] vs mirror-hydrated list[dict] (Phase 5)."""

from __future__ import annotations

import json
import math
from typing import Any


def _float_match(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return math.isclose(fa, fb, rel_tol=1e-9, abs_tol=1e-6)


def _norm_signals(x: Any) -> list[str]:
    if not isinstance(x, list):
        return []
    return [str(s).strip() for s in x]


def _patterns_sig_set(x: Any) -> set[str]:
    if not isinstance(x, list):
        return set()
    out: set[str] = set()
    for m in x:
        if isinstance(m, dict):
            out.add(json.dumps(m, sort_keys=True, default=str))
    return out


def _row_parity(legacy: dict, mirror: dict) -> tuple[bool, str]:
    """Return (ok, reason)."""
    tu = (legacy.get("ticker") or "").strip().upper()
    mu = (mirror.get("ticker") or "").strip().upper()
    if tu != mu:
        return False, f"ticker {tu!r}!={mu!r}"

    lc, mc = legacy.get("confidence"), mirror.get("confidence")
    if lc != mc:
        return False, f"confidence {lc!r}!={mc!r}"

    for k in ("direction", "vix_regime"):
        ls = (legacy.get(k) or None)
        ms = (mirror.get(k) or None)
        if ls is not None and isinstance(ls, str):
            ls = ls.strip() or None
        if ms is not None and isinstance(ms, str):
            ms = ms.strip() or None
        if ls != ms:
            return False, f"{k} mismatch"

    for fk in ("score", "price", "meta_ml_probability", "suggested_stop", "suggested_target", "risk_reward", "position_size_pct"):
        if not _float_match(legacy.get(fk), mirror.get(fk)):
            return False, f"{fk} mismatch"

    if _norm_signals(legacy.get("signals")) != _norm_signals(mirror.get("signals")):
        return False, "signals mismatch"

    if _patterns_sig_set(legacy.get("matched_patterns")) != _patterns_sig_set(mirror.get("matched_patterns")):
        return False, "matched_patterns mismatch"

    return True, ""


def legacy_mirror_list_parity_ok(legacy: list[dict], mirror: list[dict]) -> tuple[bool, str]:
    """Zip by index (same sort_rank / post-sort order)."""
    if len(legacy) != len(mirror):
        return False, f"len_legacy={len(legacy)} len_mirror={len(mirror)}"
    for i, (a, b) in enumerate(zip(legacy, mirror)):
        ok, reason = _row_parity(a, b)
        if not ok:
            return False, f"idx={i} {reason}"
    return True, ""
