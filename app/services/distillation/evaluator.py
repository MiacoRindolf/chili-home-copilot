"""Replay a held-out task set against a candidate Ollama model and score it.

Each task in the eval set has a frozen prompt and a frozen "validation
oracle" — typically a pytest invocation. The candidate produces a
completion, the completion is applied as a diff in a worktree, and the
oracle decides pass/fail. Pass rate is the headline metric; p50 latency is
the tie-breaker.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GOLDEN_PATH = Path(__file__).parent / "golden_tasks.json"


@dataclass
class EvalResult:
    pass_rate: float
    p50_latency_ms: int
    eval_rows: int
    per_task: list[dict]


def run_eval(candidate_model: str, *, eval_rows: int = 100) -> EvalResult:
    """Run the candidate against the eval set. Returns aggregate scores."""
    if not GOLDEN_PATH.exists():
        logger.warning("[distillation.evaluator] no golden_tasks.json — eval skipped")
        return EvalResult(pass_rate=0.0, p50_latency_ms=0, eval_rows=0, per_task=[])

    tasks = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))[:eval_rows]
    per_task: list[dict] = []
    latencies: list[int] = []
    passes = 0
    for t in tasks:
        t0 = time.monotonic()
        ok = _run_one_task(candidate_model, t)
        lat = int((time.monotonic() - t0) * 1000)
        latencies.append(lat)
        if ok:
            passes += 1
        per_task.append({"id": t.get("id"), "passed": ok, "latency_ms": lat})

    pr = passes / len(tasks) if tasks else 0.0
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    return EvalResult(pass_rate=pr, p50_latency_ms=p50, eval_rows=len(tasks), per_task=per_task)


def _run_one_task(model: str, task: dict) -> bool:
    """Replay one task. Stub for now — Phase D.5 wires in the real oracle."""
    # Placeholder so the skeleton compiles. Real implementation:
    #   1. route_chat(starting_tier=1, max_tier=1, model=model, ...)
    #   2. apply diff in /tmp/eval-{id}
    #   3. run task['oracle']['command']  (e.g. "pytest tests/test_x.py -k y")
    #   4. return exit_code == 0
    return False
