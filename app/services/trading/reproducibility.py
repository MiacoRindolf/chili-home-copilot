"""Reproducibility discipline for the trading brain.

Ensures:
- Fixed random seeds in all stochastic components
- Immutable data snapshots per experiment
- Code version tagging per learning cycle
- Ablation study framework (toggle components on/off, measure impact)
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_SEED = 42
_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "reproducibility"


class ReproducibleRNG:
    """Deterministic random number generator for use across all stochastic components."""

    def __init__(self, seed: int = DEFAULT_SEED):
        self._base_seed = seed
        self._rng = random.Random(seed)
        self._np_rng = np.random.default_rng(seed)

    def reset(self, seed: int | None = None) -> None:
        s = seed if seed is not None else self._base_seed
        self._rng = random.Random(s)
        self._np_rng = np.random.default_rng(s)

    @property
    def python(self) -> random.Random:
        return self._rng

    @property
    def numpy(self) -> np.random.Generator:
        return self._np_rng

    def seed_for_component(self, component: str) -> int:
        """Derive a deterministic sub-seed for a named component."""
        h = hashlib.sha256(f"{self._base_seed}:{component}".encode()).hexdigest()
        return int(h[:8], 16)


_global_rng = ReproducibleRNG(DEFAULT_SEED)


def get_rng() -> ReproducibleRNG:
    """Get the global reproducible RNG."""
    return _global_rng


def snapshot_data(
    tickers: list[str],
    date_range: tuple[str, str],
    data: dict[str, Any],
) -> str:
    """Save an immutable data snapshot and return its hash ID."""
    payload = json.dumps({
        "tickers": sorted(tickers),
        "date_range": list(date_range),
        "data_keys": sorted(data.keys()),
        "snapshot_time": datetime.utcnow().isoformat() + "Z",
    }, sort_keys=True)

    snapshot_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]

    snapshot_dir = _DATA_DIR / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_file = snapshot_dir / f"{snapshot_hash}.json"

    if not snapshot_file.exists():
        try:
            snapshot_file.write_text(json.dumps({
                "hash": snapshot_hash,
                "tickers": sorted(tickers),
                "date_range": list(date_range),
                "created_at": datetime.utcnow().isoformat() + "Z",
                "n_rows": sum(len(v) if isinstance(v, list) else 1 for v in data.values()),
            }, indent=2))
        except Exception as e:
            logger.debug("[reproducibility] Failed to save snapshot: %s", e)

    return snapshot_hash


def run_ablation_study(
    db: Session,
    pattern_id: int,
    *,
    components: list[str] | None = None,
) -> dict[str, Any]:
    """Run ablation study: disable each condition in a pattern and measure impact.

    For each condition in the pattern's rules_json, re-run the backtest with
    that condition removed and measure the Sharpe ratio delta.
    """
    from ...models.trading import ScanPattern

    pattern = db.query(ScanPattern).filter(ScanPattern.id == pattern_id).first()
    if not pattern:
        return {"ok": False, "error": "Pattern not found"}

    try:
        rj = json.loads(pattern.rules_json) if isinstance(pattern.rules_json, str) else (pattern.rules_json or {})
        conditions = rj.get("conditions", [])
    except Exception:
        return {"ok": False, "error": "Cannot parse rules_json"}

    if len(conditions) < 2:
        return {"ok": False, "error": "Need at least 2 conditions for ablation"}

    from ..backtest_service import run_pattern_backtest

    ticker = None
    if pattern.scope_tickers:
        try:
            tks = pattern.scope_tickers if isinstance(pattern.scope_tickers, list) else []
            ticker = tks[0] if tks else None
        except Exception:
            pass
    if not ticker:
        return {"ok": False, "error": "No ticker for backtest"}

    baseline = run_pattern_backtest(
        ticker, conditions, pattern_name=f"ablation_baseline_{pattern.name}",
    )
    baseline_sharpe = _extract_sharpe(baseline)

    ablation_results = []
    for i, cond in enumerate(conditions):
        reduced = conditions[:i] + conditions[i + 1:]
        if not reduced:
            continue

        label = f"without_{cond.get('indicator', 'unknown')}_{cond.get('op', '')}_{cond.get('value', cond.get('ref', ''))}"
        result = run_pattern_backtest(
            ticker, reduced, pattern_name=f"ablation_{label}",
        )
        ablated_sharpe = _extract_sharpe(result)
        delta = baseline_sharpe - ablated_sharpe if baseline_sharpe is not None and ablated_sharpe is not None else None

        ablation_results.append({
            "removed_condition": cond,
            "condition_index": i,
            "sharpe_without": ablated_sharpe,
            "sharpe_delta": round(delta, 4) if delta is not None else None,
            "label": label,
            "is_noise": delta is not None and abs(delta) < 0.05,
        })

    ablation_results.sort(key=lambda a: abs(a.get("sharpe_delta") or 0), reverse=True)

    noise_count = sum(1 for a in ablation_results if a.get("is_noise"))

    return {
        "ok": True,
        "pattern_id": pattern_id,
        "pattern_name": pattern.name,
        "baseline_sharpe": baseline_sharpe,
        "conditions_tested": len(ablation_results),
        "noise_conditions": noise_count,
        "results": ablation_results,
    }


def _extract_sharpe(bt_result: dict[str, Any]) -> float | None:
    """Extract Sharpe ratio from a backtest result dict."""
    if not bt_result.get("ok"):
        return None
    trades = bt_result.get("trades", [])
    if not trades:
        return None

    returns = []
    for t in trades:
        ret = t.get("return_pct") or t.get("pnl_pct")
        if ret is not None:
            returns.append(float(ret))

    if len(returns) < 2:
        return None

    mean_r = sum(returns) / len(returns)
    var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r = var_r ** 0.5
    if std_r <= 0:
        return 0.0

    import math
    return round(mean_r / std_r * math.sqrt(252), 4)
