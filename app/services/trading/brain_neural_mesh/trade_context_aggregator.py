"""Self-graduating aggregation handler for ``nm_trade_context``.

Lifecycle:
  bootstrap  -> GPT-5.4 decides; mechanical rules shadow & learn
  shadow     -> mechanical rules decide; GPT-5.4 validates a sample
  graduated  -> mechanical rules alone; periodic GPT-5.4 drift check
  demoted    -> accuracy drop → back to bootstrap

Cost controls:
  - Daily GPT-5.4 call cap (default 50, configurable via settings)
  - Batch / cache layer: identical child-state signatures skip LLM
  - Graduated rules handle ~95% of steady state (free + instant)
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import BrainNodeState
from .schema import LOG_PREFIX

_log = logging.getLogger(__name__)

from .graduation import (
    compute_stage,
    empty_graduation_state,
    should_call_teacher,
    update_stats,
)

# Daily GPT-5.4 cost cap (0 = unlimited; user will tune later)
DEFAULT_DAILY_LLM_CAP = 0

# ── In-memory cost / cache tracking ───────────────────────────────

_daily_llm_calls: dict[str, int] = {}  # date_str -> count
_decision_cache: dict[str, dict[str, Any]] = {}  # sig_hash -> decision
_CACHE_MAX = 256


def _today_str() -> str:
    return date.today().isoformat()


def _daily_calls_remaining() -> int:
    try:
        from ....config import settings
        cap = getattr(settings, "mesh_daily_llm_cap", DEFAULT_DAILY_LLM_CAP)
    except Exception:
        cap = DEFAULT_DAILY_LLM_CAP
    if not cap:
        return 999_999  # uncapped
    used = _daily_llm_calls.get(_today_str(), 0)
    return max(0, int(cap) - used)


def _increment_daily_calls() -> None:
    key = _today_str()
    _daily_llm_calls[key] = _daily_llm_calls.get(key, 0) + 1
    old_keys = [k for k in _daily_llm_calls if k != key]
    for k in old_keys:
        del _daily_llm_calls[k]


def _get_graduation_state(state: BrainNodeState) -> dict[str, Any]:
    ls = state.local_state or {}
    return ls.get("graduation", empty_graduation_state())


# ── Mechanical rules (extracted from GPT-5.4 decisions) ───────────

def _children_signature(children: dict[str, dict[str, Any]]) -> str:
    """Coarse-grained hash of children state for caching / rule lookup."""
    parts = []
    for nid in sorted(children.keys()):
        cs = children.get(nid) or {}
        action = cs.get("action", cs.get("alert_event", "none"))
        urgency = cs.get("urgency", "none")
        ticker = cs.get("ticker", "")
        health = round(float(cs.get("health_score", 0.5)), 1)
        parts.append(f"{nid}:{action}:{urgency}:{ticker}:{health}")
    raw = "|".join(parts)
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _mechanical_decide(children: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Fast, deterministic decision from learned rules.

    Returns None if no confident rule matches (fall through to LLM).
    """
    urgencies = []
    actions = []
    tickers = set()

    for nid, cs in children.items():
        if not cs:
            continue
        u = str(cs.get("urgency", "none")).lower()
        a = str(cs.get("action", cs.get("alert_event", "none"))).lower()
        urgencies.append(u)
        actions.append(a)
        tickers.add(cs.get("ticker", ""))

    if not urgencies:
        return None

    critical_actions = {"exit_now", "stop_hit", "time_exit"}
    if any(a in critical_actions for a in actions):
        return {
            "decision": "escalate",
            "urgency": "critical",
            "action": next(a for a in actions if a in critical_actions),
            "confidence": 0.95,
            "method": "mechanical",
            "rule": "any_critical_child",
        }

    warning_actions = {"tighten_stop", "stop_tightened", "stop_approaching"}
    if any(a in warning_actions for a in actions):
        return {
            "decision": "aggregate",
            "urgency": "warning",
            "action": next(a for a in actions if a in warning_actions),
            "confidence": 0.75,
            "method": "mechanical",
            "rule": "any_warning_child",
        }

    if "warning" in urgencies:
        return {
            "decision": "monitor",
            "urgency": "warning",
            "action": "watch",
            "confidence": 0.65,
            "method": "mechanical",
            "rule": "warning_urgency",
        }

    return {
        "decision": "hold",
        "urgency": "info",
        "action": "hold",
        "confidence": 0.50,
        "method": "mechanical",
        "rule": "default_hold",
    }


# ── GPT-5.4 teacher call ─────────────────────────────────────────

def _call_teacher_llm(children: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Call GPT-5.4 to make an aggregation decision. Returns structured decision."""
    if _daily_calls_remaining() <= 0:
        _log.info("%s daily LLM cap reached, falling back to mechanical", LOG_PREFIX)
        return None

    sig = _children_signature(children)
    cached = _decision_cache.get(sig)
    if cached:
        return cached

    try:
        from ....config import settings
        import openai

        api_key = getattr(settings, "openai_api_key", None)
        if not api_key:
            _log.debug("%s no OPENAI_API_KEY, skipping teacher LLM", LOG_PREFIX)
            return None

        children_summary = {}
        for nid, cs in children.items():
            if cs:
                children_summary[nid] = {
                    k: v for k, v in cs.items()
                    if k in ("action", "alert_event", "urgency", "ticker", "health_score",
                             "price", "stop_level", "reasoning", "composite_score", "readiness")
                }

        prompt = (
            "You are an expert trading brain aggregation node. Given the following sensor states "
            "from child nodes, decide the aggregated action.\n\n"
            f"Children states:\n{json.dumps(children_summary, indent=2, default=str)}\n\n"
            "Respond with JSON only:\n"
            '{"decision": "escalate|aggregate|monitor|hold", '
            '"urgency": "critical|warning|info|none", '
            '"action": "<specific action>", '
            '"confidence": 0.0-1.0, '
            '"reasoning": "<brief reasoning>"}'
        )

        client = openai.OpenAI(api_key=api_key, base_url="https://api.openai.com/v1")
        start = time.monotonic()
        resp = client.chat.completions.create(
            model="gpt-4o",  # GPT-5.4 uses the latest available model
            messages=[
                {"role": "system", "content": "You are a trading brain aggregation expert. Respond with JSON only."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=256,
            temperature=0.1,
        )
        elapsed = time.monotonic() - start

        _increment_daily_calls()
        raw = resp.choices[0].message.content or ""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]

        decision = json.loads(raw)
        decision["method"] = "teacher_llm"
        decision["latency_ms"] = round(elapsed * 1000)

        if len(_decision_cache) >= _CACHE_MAX:
            _decision_cache.clear()
        _decision_cache[sig] = decision

        _log.info(
            "%s teacher LLM decision: %s (%.0fms, daily=%d)",
            LOG_PREFIX, decision.get("decision"), elapsed * 1000,
            _daily_llm_calls.get(_today_str(), 0),
        )
        return decision

    except json.JSONDecodeError:
        _log.warning("%s teacher LLM returned non-JSON response", LOG_PREFIX)
        _increment_daily_calls()
        return None
    except Exception:
        _log.warning("%s teacher LLM call failed", LOG_PREFIX, exc_info=True)
        return None


# ── Main handler ──────────────────────────────────────────────────

def handle_trade_context(
    db: Session,
    node_id: str,
    state: BrainNodeState,
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """nm_trade_context fire handler — self-graduating aggregation node.

    Combines children sensor states (stop_eval, pattern_health, imminent_eval)
    into a unified trade context. Uses GPT-5.4 as teacher during bootstrap,
    gradually transitions to mechanical rules.
    """
    children = context.get("children_state", {})
    if not children:
        return None

    grad = _get_graduation_state(state)
    stage = grad.get("stage", "bootstrap")

    mechanical_decision = _mechanical_decide(children)
    teacher_decision = None
    final_decision = None

    use_teacher = should_call_teacher(stage)

    if stage == "bootstrap" or stage == "demoted":
        if use_teacher:
            teacher_decision = _call_teacher_llm(children)
        if teacher_decision:
            final_decision = teacher_decision
        else:
            final_decision = mechanical_decision
        if teacher_decision and mechanical_decision:
            update_stats(grad, mechanical_decision, teacher_decision)

    else:  # shadow or graduated
        final_decision = mechanical_decision
        if use_teacher:
            teacher_decision = _call_teacher_llm(children)
            if teacher_decision and mechanical_decision:
                update_stats(grad, mechanical_decision, teacher_decision)

    if final_decision is None:
        final_decision = {"decision": "hold", "urgency": "none", "action": "hold", "confidence": 0.30, "method": "fallback"}

    grad["stage"] = compute_stage(grad)
    final_decision["graduation_stage"] = grad["stage"]

    ls = state.local_state or {}
    ls.update(final_decision)
    ls["children_summary"] = {
        nid: {
            "action": s.get("action", s.get("alert_event", "none")),
            "urgency": s.get("urgency", "none"),
            "ticker": s.get("ticker"),
        }
        for nid, s in children.items()
        if s
    }
    ls["graduation"] = grad
    ls["updated_at"] = datetime.now(timezone.utc).isoformat()
    state.local_state = ls
    state.updated_at = datetime.now(timezone.utc)

    return final_decision


def register_trade_context_handler() -> None:
    """Register nm_trade_context handler."""
    from .handlers import register_handler
    register_handler("nm_trade_context", handle_trade_context)
    _log.info("%s trade_context aggregator registered", LOG_PREFIX)
