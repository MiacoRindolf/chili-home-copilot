"""Deep-study AI report for one learning cycle: bounded metrics + LLM + DB persist."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import LearningCycleAiReport
from ..llm_caller import call_llm

logger = logging.getLogger(__name__)

# Top-level keys safe to copy into metrics_json (no huge nested blobs).
_WHITELIST_KEYS: frozenset[str] = frozenset(
    {
        "prescreen_candidates",
        "prescreen_sources",
        "tickers_scanned",
        "tickers_scored",
        "snapshots_taken",
        "returns_backfilled",
        "scores_backfilled",
        "insights_decayed",
        "insights_pruned",
        "patterns_discovered",
        "patterns_boosted",
        "backtests_run",
        "queue_backtests_run",
        "queue_patterns_processed",
        "queue_pending",
        "queue_empty",
        "hypotheses_tested",
        "hypotheses_challenged",
        "real_trade_adjustments",
        "weights_evolved",
        "hypothesis_patterns_spawned",
        "breakout_patterns_learned",
        "intraday_discoveries",
        "patterns_refined",
        "exit_adjustments",
        "fakeout_patterns",
        "sizing_adjustments",
        "inter_alert_insights",
        "timeframe_insights",
        "synergies_found",
        "journal_written",
        "signal_events",
        "ml_trained",
        "ml_accuracy",
        "ml_feedback_boosted",
        "ml_feedback_penalised",
        "proposals_generated",
        "patterns_discovered_engine",
        "patterns_tested",
        "patterns_evolved",
        "data_provider",
        "elapsed_s",
        "elapsed_s_pre_report",
        "interrupted",
        "error",
        "cycle_ai_report_id",
    }
)


def _sanitize_evolution(evo: Any) -> dict[str, Any]:
    if not isinstance(evo, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in list(evo.items())[:40]:
        if isinstance(v, (int, float, bool)) or v is None:
            out[str(k)[:80]] = v
        elif isinstance(v, str) and len(v) <= 300:
            out[str(k)[:80]] = v
        elif isinstance(v, dict):
            inner: dict[str, Any] = {}
            for k2, v2 in list(v.items())[:25]:
                if isinstance(v2, (int, float, bool)) or v2 is None:
                    inner[str(k2)[:60]] = v2
                elif isinstance(v2, str) and len(v2) <= 200:
                    inner[str(k2)[:60]] = v2
            if inner:
                out[str(k)[:80]] = inner
    return out


def _sanitize_step_timings(st: Any) -> dict[str, Any]:
    if not isinstance(st, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in list(st.items())[:50]:
        key = str(k)[:80]
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[key] = v
        elif isinstance(v, dict):
            sub = {str(sk)[:40]: sv for sk, sv in list(v.items())[:10] if isinstance(sv, (int, float, str))}
            if sub:
                out[key] = sub
    return out


def build_report_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Bounded JSON-safe snapshot for prompts and metrics_json storage."""
    payload: dict[str, Any] = {}
    for key in _WHITELIST_KEYS:
        if key not in report:
            continue
        val = report[key]
        if key == "prescreen_sources" and isinstance(val, dict):
            payload[key] = {str(k)[:40]: int(v) if isinstance(v, (int, float)) else v for k, v in list(val.items())[:30]}
        else:
            payload[key] = val

    if "evolution" in report:
        payload["evolution"] = _sanitize_evolution(report.get("evolution"))

    if "step_timings" in report:
        payload["step_timings"] = _sanitize_step_timings(report.get("step_timings"))

    return payload


def _fallback_markdown(metrics: dict[str, Any]) -> str:
    lines = [
        f"# Learning cycle report — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        "_LLM unavailable or call failed; metrics-only summary._",
        "",
        "## Cycle metrics",
        "",
        "```json",
        json.dumps(metrics, indent=2, default=str)[:12000],
        "```",
    ]
    if metrics.get("error"):
        lines.insert(3, f"**Cycle error:** {metrics['error']}")
    return "\n".join(lines)


def generate_and_store_cycle_report(db: Session, user_id: int | None, report: dict[str, Any]) -> int | None:
    """Build payload, call LLM, insert row (always), return new id."""
    metrics = build_report_payload(report)
    metrics_json_str = json.dumps(metrics, default=str)
    if len(metrics_json_str) > 24000:
        metrics_json_str = metrics_json_str[:24000] + "…"

    system = (
        "You are CHILI's trading brain analyst. Write a clear markdown report for the user "
        "based only on the learning-cycle metrics provided. No disclaimers about not being "
        "financial advice unless one short line at the end."
    )
    user_prompt = (
        "Here is JSON from one automated learning cycle (scans, patterns, backtests, "
        "hypotheses, ML, proposals, timings). Produce a structured markdown report with:\n\n"
        "## Executive summary\n"
        "3-5 bullets on what this cycle accomplished.\n\n"
        "## What changed\n"
        "Patterns, weights, hypotheses, ML, signals — only what the data supports.\n\n"
        "## Risks and caveats\n"
        "Data limits, low sample sizes, or anomalies if implied by the numbers.\n\n"
        "## Suggested focus next cycle\n"
        "2-4 concrete priorities.\n\n"
        f"CYCLE_METRICS_JSON:\n{metrics_json_str}"
    )

    reply = call_llm(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1800,
        trace_id="learning-cycle-report",
    )
    content = (reply or "").strip()
    if not content:
        content = _fallback_markdown(metrics)

    row = LearningCycleAiReport(
        user_id=user_id,
        content=content,
        metrics_json=metrics,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("[learning_cycle_report] Stored cycle AI report id=%s user_id=%s", row.id, user_id)
    return row.id
