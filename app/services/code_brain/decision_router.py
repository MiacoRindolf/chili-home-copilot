"""Code Brain decision router — deterministic-first routing gate.

This is the brain's spine. Every event the bus claims is funneled here.
The router decides one of FIVE outcomes per task, in order:

  1. ``template``     — a confident pattern matched. Apply the template
                        deterministically. **NO LLM CALL.** Free, fast,
                        100% reproducible.
  2. ``local_model``  — a distilled local model has been promoted and
                        the task is mid-stakes. Call Ollama. **No paid
                        token cost.**
  3. ``premium``      — task is novel or high-stakes AND budget remains.
                        Call gpt-5.1 via the cascade. Costs money.
  4. ``escalate``     — task is novel + high-stakes but budget exhausted,
                        OR the task is genuinely ambiguous. Notify
                        operator via the Brain UI; do not act.
  5. ``skip``         — frozen scope hit, kill switch active, dedupe,
                        or rule gate veto. Audit and stop.

Mirrors the trading brain's auto_trader rule cascade: rule gates first,
LLM revalidation only when the deterministic system can't decide.

Every decision writes one row to ``code_decision_router_log`` so we can
later answer "why did the brain spend $X on task Y" or "why did the
brain refuse to act on task Z" — the exact analog of trading's
execution_audit.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import runtime_state

logger = logging.getLogger(__name__)


class Decision(str, Enum):
    TEMPLATE = "template"
    LOCAL_MODEL = "local_model"
    PREMIUM = "premium"
    ESCALATE = "escalate"
    SKIP = "skip"


@dataclass
class TaskContext:
    """Everything the router needs to make a decision.

    Built once per event and passed in. Routers stay pure — they read
    state from the context and the runtime_state row, never call out
    to mutable systems mid-decision.
    """
    task_id: int
    title: str
    brief_body: str
    sub_path: str
    repo_id: Optional[int]
    repo_name: str
    intended_files: list[str]
    estimated_diff_loc: int
    prior_failure_count: int
    is_high_stakes: bool  # touches frozen_scope_paths or production-critical code


@dataclass
class RoutingDecision:
    decision: Decision
    reason: str
    matched_pattern_id: Optional[int]
    matched_pattern_name: Optional[str]
    pattern_confidence: Optional[Decimal]
    novelty_score: Decimal
    rule_snapshot: dict[str, Any]
    log_id: int  # row id in code_decision_router_log

    def __str__(self) -> str:
        return f"<RoutingDecision {self.decision.value}: {self.reason}>"


# ---------------------------------------------------------------------------
# Q1 — does a confident pattern match?
# ---------------------------------------------------------------------------

@dataclass
class PatternMatch:
    pattern_id: int
    pattern_name: str
    confidence: Decimal
    archetype: str


def _glob_to_regex(glob: str) -> re.Pattern:
    """Tiny glob → regex helper. Supports ``*`` and ``**``. No brace expansion."""
    pat = re.escape(glob)
    pat = pat.replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.compile("^" + pat + "$")


def _match_pattern(db: Session, ctx: TaskContext) -> Optional[PatternMatch]:
    """Find the highest-confidence pattern whose signature matches ``ctx``.

    Match criteria (all must hold):
      * pattern.confidence >= runtime_state.template_min_confidence
      * pattern.brief_keywords ⊆ task.brief_body (case-insensitive)
      * pattern.file_glob_pattern matches at least one of intended_files
        (or matches sub_path if intended_files is empty).

    Returns ``None`` if nothing qualifies.
    """
    state = runtime_state.get_state(db)
    min_conf = state.template_min_confidence

    rows = db.execute(
        text(
            "SELECT id, name, brief_keywords, file_glob_pattern, "
            "       diff_archetype, confidence "
            "FROM code_patterns "
            "WHERE confidence >= :mc "
            "ORDER BY confidence DESC, success_count DESC "
            "LIMIT 50"
        ),
        {"mc": min_conf},
    ).fetchall()

    if not rows:
        return None

    brief_lc = (ctx.title + "\n" + ctx.brief_body).lower()
    targets = list(ctx.intended_files) or ([ctx.sub_path] if ctx.sub_path else [])

    for row in rows:
        keywords_raw = row[2]
        keywords: list[str]
        if isinstance(keywords_raw, list):
            keywords = [str(k).lower() for k in keywords_raw]
        elif isinstance(keywords_raw, str):
            try:
                parsed = json.loads(keywords_raw)
                keywords = [str(k).lower() for k in parsed] if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                keywords = []
        else:
            keywords = []

        if keywords and not all(k in brief_lc for k in keywords):
            continue

        glob = (row[3] or "").strip()
        if glob:
            rx = _glob_to_regex(glob)
            if not any(rx.match(t) for t in targets):
                continue

        return PatternMatch(
            pattern_id=int(row[0]),
            pattern_name=str(row[1]),
            confidence=Decimal(row[5] or 0),
            archetype=str(row[4] or ""),
        )

    return None


# ---------------------------------------------------------------------------
# Q2 — novelty / stakes
# ---------------------------------------------------------------------------

def _novelty_score(db: Session, ctx: TaskContext) -> Decimal:
    """A 0..1 score: 0 = looks just like things we've seen succeed before,
    1 = nothing in our history looks like this task.

    Heuristic v1: ratio of brief tokens NOT seen in recent successful
    coding_agent_suggestion responses. Cheap, deterministic, no LLM.
    Refined later when pattern_miner has more signal.
    """
    tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", ctx.brief_body or ""))
    if not tokens:
        return Decimal("0.5")

    seen_rows = db.execute(
        text(
            "SELECT response_text FROM coding_agent_suggestion "
            "WHERE created_at > NOW() - INTERVAL '14 days' "
            "  AND COALESCE(response_text, '') <> '' "
            "  AND model <> 'error' "
            "ORDER BY id DESC LIMIT 200"
        )
    ).fetchall()
    if not seen_rows:
        return Decimal("1.0")

    seen_text = " ".join((r[0] or "") for r in seen_rows).lower()
    if not seen_text.strip():
        return Decimal("1.0")

    unseen = sum(1 for t in tokens if t.lower() not in seen_text)
    score = unseen / max(1, len(tokens))
    return Decimal(str(round(score, 4)))


# ---------------------------------------------------------------------------
# Q-skip rules
# ---------------------------------------------------------------------------

def _kill_switch_active(db: Session) -> Optional[str]:
    row = db.execute(
        text("SELECT active, reason FROM code_kill_switch_state WHERE id = 1")
    ).fetchone()
    if row and row[0]:
        return str(row[1] or "kill switch active")
    return None


def _frozen_scope_violation(db: Session, ctx: TaskContext) -> Optional[str]:
    """If any intended_file or sub_path matches a ``severity='block'`` glob,
    return the human-readable reason. Mirrors trading's kill_switch + frozen
    scope guard pattern.
    """
    if not ctx.intended_files and not ctx.sub_path:
        return None
    targets = list(ctx.intended_files) or ([ctx.sub_path] if ctx.sub_path else [])

    rows = db.execute(
        text(
            "SELECT glob, reason FROM frozen_scope_paths WHERE severity = 'block'"
        )
    ).fetchall()
    for glob, reason in rows or []:
        rx = _glob_to_regex(str(glob))
        if any(rx.match(t) for t in targets):
            return f"frozen_scope:{glob} → {reason}"
    return None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _log_decision(
    db: Session,
    *,
    event_id: Optional[int],
    ctx: TaskContext,
    decision: Decision,
    matched_pattern_id: Optional[int],
    pattern_confidence: Optional[Decimal],
    novelty: Decimal,
    rule_snapshot: dict[str, Any],
) -> int:
    row = db.execute(
        text(
            "INSERT INTO code_decision_router_log "
            "(event_id, task_id, decision, matched_pattern_id, "
            " pattern_confidence, novelty_score, rule_snapshot) "
            "VALUES (:e, :t, :d, :mp, :pc, :ns, CAST(:rs AS jsonb)) "
            "RETURNING id"
        ),
        {
            "e": event_id,
            "t": ctx.task_id,
            "d": decision.value,
            "mp": matched_pattern_id,
            "pc": pattern_confidence,
            "ns": novelty,
            "rs": json.dumps(rule_snapshot, default=str),
        },
    ).fetchone()
    db.commit()
    return int(row[0])


def record_outcome(
    db: Session,
    *,
    log_id: int,
    outcome: str,
    cost_usd: float = 0.0,
    llm_tokens_used: int = 0,
) -> None:
    """Fill in the outcome columns once the routed action completes.

    ``outcome`` ∈ {'applied', 'failed', 'escalated', 'skipped', 'merged'}.
    ``cost_usd`` is added to the running daily premium spend (only for
    decisions that actually paid OpenAI).
    """
    db.execute(
        text(
            "UPDATE code_decision_router_log SET "
            "  completed_at = NOW(), "
            "  outcome = :o, "
            "  cost_usd = :c, "
            "  llm_tokens_used = :tk "
            "WHERE id = :id"
        ),
        {
            "o": outcome,
            "c": Decimal(str(cost_usd or 0)),
            "tk": int(llm_tokens_used or 0),
            "id": int(log_id),
        },
    )
    db.commit()
    if cost_usd and cost_usd > 0:
        runtime_state.record_premium_spend(db, cost_usd)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def route(
    db: Session,
    ctx: TaskContext,
    *,
    event_id: Optional[int] = None,
) -> RoutingDecision:
    """The brain's central decision function.

    Order of checks (bail at the first applicable):

      Q-skip:  kill switch / frozen scope / strikeout
      Q1:      template match (no LLM)
      Q2:      mid-stakes + local model promoted → local
      Q3:      novel/high-stakes + budget remains → premium
      Q4:      novel/high-stakes + no budget OR strikeout → escalate

    Always writes one ``code_decision_router_log`` row. Caller is expected
    to invoke :func:`record_outcome` once the routed action completes so
    cost / tokens / final outcome are captured.
    """
    snapshot: dict[str, Any] = {
        "task_id": ctx.task_id,
        "intended_files_count": len(ctx.intended_files),
        "estimated_diff_loc": ctx.estimated_diff_loc,
        "prior_failure_count": ctx.prior_failure_count,
        "is_high_stakes": ctx.is_high_stakes,
    }

    # ---- Q-skip ------------------------------------------------------------
    ks = _kill_switch_active(db)
    if ks:
        snapshot["skip_reason"] = ks
        log_id = _log_decision(
            db,
            event_id=event_id,
            ctx=ctx,
            decision=Decision.SKIP,
            matched_pattern_id=None,
            pattern_confidence=None,
            novelty=Decimal("0"),
            rule_snapshot=snapshot,
        )
        return RoutingDecision(
            decision=Decision.SKIP,
            reason=f"kill_switch_active: {ks}",
            matched_pattern_id=None,
            matched_pattern_name=None,
            pattern_confidence=None,
            novelty_score=Decimal("0"),
            rule_snapshot=snapshot,
            log_id=log_id,
        )

    fs = _frozen_scope_violation(db, ctx)
    if fs:
        snapshot["skip_reason"] = fs
        log_id = _log_decision(
            db,
            event_id=event_id,
            ctx=ctx,
            decision=Decision.SKIP,
            matched_pattern_id=None,
            pattern_confidence=None,
            novelty=Decimal("0"),
            rule_snapshot=snapshot,
        )
        return RoutingDecision(
            decision=Decision.SKIP,
            reason=fs,
            matched_pattern_id=None,
            matched_pattern_name=None,
            pattern_confidence=None,
            novelty_score=Decimal("0"),
            rule_snapshot=snapshot,
            log_id=log_id,
        )

    if ctx.prior_failure_count >= 3:
        snapshot["skip_reason"] = "task_failure_strikeout"
        log_id = _log_decision(
            db,
            event_id=event_id,
            ctx=ctx,
            decision=Decision.ESCALATE,
            matched_pattern_id=None,
            pattern_confidence=None,
            novelty=Decimal("0"),
            rule_snapshot=snapshot,
        )
        return RoutingDecision(
            decision=Decision.ESCALATE,
            reason="prior_failure_count>=3 — operator review required",
            matched_pattern_id=None,
            matched_pattern_name=None,
            pattern_confidence=None,
            novelty_score=Decimal("0"),
            rule_snapshot=snapshot,
            log_id=log_id,
        )

    # ---- Q1: pattern match -------------------------------------------------
    pm = _match_pattern(db, ctx)
    if pm is not None:
        snapshot["matched_pattern"] = pm.pattern_name
        log_id = _log_decision(
            db,
            event_id=event_id,
            ctx=ctx,
            decision=Decision.TEMPLATE,
            matched_pattern_id=pm.pattern_id,
            pattern_confidence=pm.confidence,
            novelty=Decimal("0.0"),
            rule_snapshot=snapshot,
        )
        return RoutingDecision(
            decision=Decision.TEMPLATE,
            reason=f"pattern_match:{pm.pattern_name}@{pm.confidence}",
            matched_pattern_id=pm.pattern_id,
            matched_pattern_name=pm.pattern_name,
            pattern_confidence=pm.confidence,
            novelty_score=Decimal("0.0"),
            rule_snapshot=snapshot,
            log_id=log_id,
        )

    # ---- Compute novelty for Q2/Q3 -----------------------------------------
    novelty = _novelty_score(db, ctx)
    snapshot["novelty"] = float(novelty)

    state = runtime_state.get_state(db)
    runtime_state.reset_daily_spend_if_new_day(db)
    state = runtime_state.get_state(db)
    remaining = state.daily_premium_usd_cap - state.spent_today_usd
    snapshot["budget_remaining_usd"] = float(remaining)

    # ---- Q2: local model for mid-stakes ------------------------------------
    if state.local_model_promoted and not ctx.is_high_stakes:
        log_id = _log_decision(
            db,
            event_id=event_id,
            ctx=ctx,
            decision=Decision.LOCAL_MODEL,
            matched_pattern_id=None,
            pattern_confidence=None,
            novelty=novelty,
            rule_snapshot=snapshot,
        )
        return RoutingDecision(
            decision=Decision.LOCAL_MODEL,
            reason=f"local_model_promoted={state.local_model_tag} novelty={novelty}",
            matched_pattern_id=None,
            matched_pattern_name=None,
            pattern_confidence=None,
            novelty_score=novelty,
            rule_snapshot=snapshot,
            log_id=log_id,
        )

    # ---- Q3: premium when novel/high-stakes + budget -----------------------
    needs_premium = ctx.is_high_stakes or novelty >= state.novelty_premium_threshold
    if needs_premium and remaining > Decimal("0.10"):
        log_id = _log_decision(
            db,
            event_id=event_id,
            ctx=ctx,
            decision=Decision.PREMIUM,
            matched_pattern_id=None,
            pattern_confidence=None,
            novelty=novelty,
            rule_snapshot=snapshot,
        )
        return RoutingDecision(
            decision=Decision.PREMIUM,
            reason=(
                f"novelty={novelty} high_stakes={ctx.is_high_stakes} "
                f"budget=${remaining}"
            ),
            matched_pattern_id=None,
            matched_pattern_name=None,
            pattern_confidence=None,
            novelty_score=novelty,
            rule_snapshot=snapshot,
            log_id=log_id,
        )

    # ---- Q4: low-novelty + no local model + no premium budget → escalate -
    snapshot["skip_reason"] = (
        f"no_pattern + no_local_model + premium_remaining=${remaining}"
    )
    log_id = _log_decision(
        db,
        event_id=event_id,
        ctx=ctx,
        decision=Decision.ESCALATE,
        matched_pattern_id=None,
        pattern_confidence=None,
        novelty=novelty,
        rule_snapshot=snapshot,
    )
    return RoutingDecision(
        decision=Decision.ESCALATE,
        reason=(
            "no template, local model not promoted, "
            f"premium budget=${remaining} — operator review required"
        ),
        matched_pattern_id=None,
        matched_pattern_name=None,
        pattern_confidence=None,
        novelty_score=novelty,
        rule_snapshot=snapshot,
        log_id=log_id,
    )
