"""Experiment tracking and data lineage for the trading brain.

Tags every backtest/learning cycle with:
- Data snapshot info (ticker set + date range + data hash)
- Model version from the registry
- Code version (git SHA)
- Structured lab-notebook logging per learning cycle
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "experiment_log"
_LOG_FILE = _DATA_DIR / "experiment_log.jsonl"


def get_git_sha() -> str | None:
    """Get current git commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).resolve().parents[3]),
        )
        if result.returncode == 0:
            return result.stdout.strip()[:12]
    except Exception:
        pass
    return None


def compute_data_hash(tickers: list[str], date_range: tuple[str, str]) -> str:
    """Compute a reproducibility hash from the ticker set + date range."""
    payload = json.dumps({"tickers": sorted(tickers), "range": list(date_range)}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def tag_backtest_result(
    result: dict[str, Any],
    *,
    tickers: list[str] | None = None,
    date_range: tuple[str, str] | None = None,
    scan_pattern_id: int | None = None,
) -> dict[str, Any]:
    """Add lineage metadata to a backtest result dict."""
    lineage: dict[str, Any] = {
        "tagged_at": datetime.utcnow().isoformat() + "Z",
        "git_sha": get_git_sha(),
    }

    if tickers and date_range:
        lineage["data_hash"] = compute_data_hash(tickers, date_range)
        lineage["ticker_count"] = len(tickers)
        lineage["date_range"] = list(date_range)

    try:
        from .model_registry import get_registry
        reg = get_registry()
        active = reg.get_active("pattern_meta_learner")
        if active:
            lineage["model_version"] = active.version_id
    except Exception:
        pass

    if scan_pattern_id:
        lineage["scan_pattern_id"] = scan_pattern_id

    result["lineage"] = lineage
    return result


def log_learning_cycle(
    cycle_id: str | int,
    *,
    input_params: dict[str, Any] | None = None,
    patterns_discovered: int = 0,
    patterns_promoted: int = 0,
    patterns_demoted: int = 0,
    backtests_run: int = 0,
    tickers_scanned: int = 0,
    ml_trained: bool = False,
    ml_metrics: dict[str, Any] | None = None,
    metric_deltas: dict[str, float] | None = None,
    duration_seconds: float = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a structured experiment log entry for a learning cycle."""
    entry = {
        "type": "learning_cycle",
        "cycle_id": str(cycle_id),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "git_sha": get_git_sha(),
        "input_params": input_params or {},
        "results": {
            "patterns_discovered": patterns_discovered,
            "patterns_promoted": patterns_promoted,
            "patterns_demoted": patterns_demoted,
            "backtests_run": backtests_run,
            "tickers_scanned": tickers_scanned,
            "ml_trained": ml_trained,
            "ml_metrics": ml_metrics or {},
            "metric_deltas": metric_deltas or {},
        },
        "duration_seconds": round(duration_seconds, 1),
        "extra": extra or {},
    }

    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug("[experiment_tracker] Failed to write log: %s", e)

    return entry


def query_experiments(
    *,
    cycle_type: str = "learning_cycle",
    limit: int = 50,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Read experiment log entries (most recent first)."""
    if not _LOG_FILE.exists():
        return []

    entries = []
    try:
        for line in _LOG_FILE.read_text().strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == cycle_type:
                    if since and entry.get("timestamp", "") < since:
                        continue
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    except Exception:
        return []

    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries[:limit]


def get_experiment_trends(limit: int = 20) -> dict[str, Any]:
    """Compute trends across recent learning cycles for tracking regression."""
    cycles = query_experiments(limit=limit)
    if not cycles:
        return {"ok": True, "cycles": 0, "trends": {}}

    metrics_over_time: dict[str, list[float]] = {
        "patterns_discovered": [],
        "patterns_promoted": [],
        "backtests_run": [],
        "duration_seconds": [],
    }

    for c in reversed(cycles):
        results = c.get("results", {})
        for key in metrics_over_time:
            if key == "duration_seconds":
                val = c.get(key, 0)
            else:
                val = results.get(key, 0)
            metrics_over_time[key].append(float(val))

    trends = {}
    for key, vals in metrics_over_time.items():
        if len(vals) >= 2:
            recent = sum(vals[-5:]) / min(len(vals), 5)
            older = sum(vals[:5]) / min(len(vals), 5)
            trends[key] = {
                "recent_avg": round(recent, 2),
                "older_avg": round(older, 2),
                "direction": "up" if recent > older else ("down" if recent < older else "flat"),
            }

    return {
        "ok": True,
        "cycles": len(cycles),
        "trends": trends,
    }
