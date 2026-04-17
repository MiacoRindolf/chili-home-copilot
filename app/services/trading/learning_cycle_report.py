"""Reconcile-pass digest: bounded cycle metrics + template report + optional LLM polish + DB persist."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import LearningCycleAiReport
from ..llm_caller import call_llm

logger = logging.getLogger(__name__)

_cycle_report_invocation_lock = threading.Lock()
_cycle_report_invocation_count = 0

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
        "high_vol_discoveries",
        "brain_resource_budget",
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
        f"# Reconcile pass digest — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
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


def _int_metric(m: dict[str, Any], key: str, default: int = 0) -> int:
    v = m.get(key)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    return default


def build_template_cycle_report_markdown(metrics: dict[str, Any]) -> str:
    """Deterministic reconcile digest from whitelisted metrics (no LLM)."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        f"# Reconcile pass digest — {ts} UTC",
        "",
        "_Template summary from cycle counters (auditable, reproducible)._",
        "",
        "## Executive summary",
        "",
    ]

    prescreen = _int_metric(metrics, "prescreen_candidates")
    scanned = _int_metric(metrics, "tickers_scanned")
    scored = _int_metric(metrics, "tickers_scored")
    snaps = _int_metric(metrics, "snapshots_taken")
    patterns_new = _int_metric(metrics, "patterns_discovered")
    patterns_boost = _int_metric(metrics, "patterns_boosted")
    patterns_engine = _int_metric(metrics, "patterns_discovered_engine")
    bt_run = _int_metric(metrics, "backtests_run")
    q_bt = _int_metric(metrics, "queue_backtests_run")
    q_pending = _int_metric(metrics, "queue_pending")
    hypo_t = _int_metric(metrics, "hypotheses_tested")
    ml_tr = _int_metric(metrics, "ml_trained")
    elapsed = metrics.get("elapsed_s")
    err = metrics.get("error")

    lines.append(f"- Prescreen candidates: **{prescreen}**; tickers scanned **{scanned}**, scored **{scored}**; snapshots **{snaps}**.")
    if patterns_new or patterns_boost or patterns_engine:
        lines.append(
            f"- Patterns: **{patterns_new}** discovered, **{patterns_boost}** boosted, **{patterns_engine}** from pattern engine."
        )
    else:
        lines.append("- Patterns: no new discoveries this pass (counters at zero).")

    # Backtest throughput (heuristic thresholds; no historical median in single payload)
    if bt_run >= 8 or q_bt >= 5:
        bt_note = "Active backtest throughput."
    elif bt_run <= 1 and q_pending > 20:
        bt_note = "Backtest pipeline may be backing up (low runs, elevated queue pending)."
    elif bt_run <= 2:
        bt_note = "Backtest throughput light this pass."
    else:
        bt_note = "Steady backtest activity."
    lines.append(f"- Backtests: **{bt_run}** run, queue processed **{q_bt}**, **{q_pending}** pending — {bt_note}")

    if hypo_t:
        lines.append(f"- Hypotheses tested: **{hypo_t}**.")
    if ml_tr:
        lines.append(f"- ML training step ran (**{ml_tr}**).")
    if isinstance(elapsed, (int, float)):
        lines.append(f"- Wall time ~**{elapsed:.1f}s**.")
    if err:
        lines.append(f"- **Cycle reported an error:** `{err}`.")

    lines.extend(["", "## What changed", ""])

    ch: list[str] = []
    if _int_metric(metrics, "insights_decayed") or _int_metric(metrics, "insights_pruned"):
        ch.append(
            f"Insights maintenance: decayed **{_int_metric(metrics, 'insights_decayed')}**, pruned **{_int_metric(metrics, 'insights_pruned')}**."
        )
    if _int_metric(metrics, "weights_evolved"):
        ch.append(f"Adaptive weights evolved (**{_int_metric(metrics, 'weights_evolved')}** nudges).")
    if _int_metric(metrics, "proposals_generated"):
        ch.append(f"Strategy proposals generated: **{_int_metric(metrics, 'proposals_generated')}**.")
    if _int_metric(metrics, "intraday_discoveries") or _int_metric(metrics, "high_vol_discoveries"):
        ch.append(
            f"Secondary miners: intraday **{_int_metric(metrics, 'intraday_discoveries')}**, high-vol **{_int_metric(metrics, 'high_vol_discoveries')}**."
        )
    if _int_metric(metrics, "synergies_found"):
        ch.append(f"Synergies found: **{_int_metric(metrics, 'synergies_found')}**.")
    evo = metrics.get("evolution")
    if isinstance(evo, dict) and evo:
        ch.append(f"Evolution snapshot keys: {', '.join(list(evo.keys())[:12])}{'…' if len(evo) > 12 else ''}.")
    if not ch:
        ch.append("No major counter deltas beyond routine scanning/mining (see metrics block below).")
    lines.extend(ch)

    lines.extend(["", "## Risks and caveats", ""])
    risks: list[str] = []
    if q_pending > 80:
        risks.append("Large backtest queue pending — promotion latency may increase.")
    if bt_run == 0 and q_pending > 0:
        risks.append("Zero backtests completed this pass while work remains queued — check worker health or caps.")
    if err:
        risks.append("This pass ended with a recorded error; downstream steps may be partial.")
    acc = metrics.get("ml_accuracy")
    if isinstance(acc, (int, float)) and acc < 0.52 and ml_tr:
        risks.append("ML accuracy reported below coin-flip; treat meta-learner signals cautiously.")
    if not risks:
        risks.append("No anomalies flagged from counters alone; validate live book and data freshness separately.")
    lines.extend(f"- {r}" for r in risks)

    lines.extend(["", "## Suggested focus next cycle", ""])
    focus: list[str] = []
    if q_pending > 40:
        focus.append("Drain or inspect the backtest queue (budget, locks, executor).")
    if patterns_new == 0 and scanned > 100:
        focus.append("Prescreen volume is high but pattern discovery is flat — review mining thresholds or universe overlap.")
    if _int_metric(metrics, "returns_backfilled") or _int_metric(metrics, "scores_backfilled"):
        focus.append("Continue backfilling returns/scores where gaps remain.")
    if not focus:
        focus.append("Maintain current reconcile cadence; watch queue_pending and patterns_discovered trends.")
    lines.extend(f"- {f}" for f in focus)

    lines.extend(
        [
            "",
            "## Cycle metrics (raw)",
            "",
            "```json",
            json.dumps(metrics, indent=2, default=str)[:12000],
            "```",
        ]
    )
    return "\n".join(lines)


def generate_and_store_cycle_report(db: Session, user_id: int | None, report: dict[str, Any]) -> int | None:
    """Build payload, template report by default; optional LLM polish every Nth invocation."""
    global _cycle_report_invocation_count
    metrics = build_report_payload(report)
    metrics_json_str = json.dumps(metrics, default=str)
    if len(metrics_json_str) > 24000:
        metrics_json_str = metrics_json_str[:24000] + "…"

    content = build_template_cycle_report_markdown(metrics)

    every_n = max(0, int(getattr(settings, "learning_cycle_report_llm_every_n", 0) or 0))
    use_llm = False
    if every_n > 0:
        with _cycle_report_invocation_lock:
            _cycle_report_invocation_count += 1
            use_llm = _cycle_report_invocation_count % every_n == 0

    if use_llm:
        system = (
            "You are CHILI's trading brain analyst. Write a clear markdown report for the user "
            "based only on the reconcile-pass metrics provided. No disclaimers about not being "
            "financial advice unless one short line at the end."
        )
        user_prompt = (
            "Here is JSON from one automated reconcile pass (scans, patterns, backtests, "
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
            cacheable=True,
        )
        polished = (reply or "").strip()
        if polished:
            content = polished
        else:
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
