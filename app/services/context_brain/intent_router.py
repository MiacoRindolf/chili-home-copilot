"""Heuristic intent classifier — no LLM call.

Classifies an incoming chat message into one of the canonical intents
defined in ``types``. Decides which retrievers should fire (skip
irrelevant ones to save tokens + latency).

Approach: keyword-bag scoring + recency signals from chat_logs.
This is intentionally cheap and deterministic — the learning loop
later refines weights for retriever selection per intent, not the
intent classifier itself. (We can swap in a tiny learned classifier
in a future phase, but heuristic v1 buys us 80% of the value with
zero cost and full debuggability.)
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .types import (
    INTENT_CASUAL,
    INTENT_CODE,
    INTENT_KNOWLEDGE,
    INTENT_META,
    INTENT_PLANNING,
    INTENT_TRADING,
    IntentClassification,
)

logger = logging.getLogger(__name__)


# Keyword tables. Word-boundary regex so e.g. "code" doesn't match "decode".
# Each entry: (regex, weight). Higher-weight = stronger signal.
_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    INTENT_CODE: [
        (r"\b(code|function|class|method|variable|import|module|package)\b", 1.0),
        (r"\b(bug|fix|refactor|implement|debug|exception|error|stacktrace|traceback)\b", 1.2),
        (r"\b(python|javascript|typescript|sql|postgres|sqlite|fastapi|flask|django|react|flutter|dart)\b", 1.0),
        (r"\b(commit|branch|pull request|pr|merge|rebase|diff)\b", 0.9),
        (r"\b(test|pytest|unittest|coverage)\b", 0.9),
        (r"```", 1.5),  # code fence in user message = strong code intent
    ],
    INTENT_TRADING: [
        (r"\b(trade|trading|trader|broker|robinhood|coinbase)\b", 1.5),
        (r"\b(buy|sell|short|long|position|stop|target|stop[- ]loss|take[- ]profit)\b", 1.0),
        (r"\b(stock|equity|crypto|btc|eth|ticker|symbol)\b", 0.9),
        (r"\b(market|premarket|aftermarket|RTH|extended hours)\b", 0.8),
        (r"\b(portfolio|allocation|risk|drawdown|kelly|sharpe)\b", 1.0),
        (r"\b(autotrader|backtest|forward[- ]?test|cpcv|pattern|signal)\b", 1.1),
    ],
    INTENT_PLANNING: [
        (r"\b(plan|task|todo|to[- ]do|backlog|sprint|epic|milestone)\b", 1.2),
        (r"\b(deadline|due|schedule|calendar|appointment|meeting)\b", 1.0),
        (r"\b(project|initiative|goal|objective|kpi|okr)\b", 0.7),
        (r"\b(prioritize|estimate|effort|story[- ]?points?)\b", 1.0),
    ],
    INTENT_META: [
        (r"\b(chili|cowork|claude|kill[- ]switch|brain|setting|config)\b", 1.0),
        (r"\b(how do (you|I|we)|what (can|does|is) (you|chili))\b", 0.8),
        (r"\b(api[- ]key|token|credential|env|environment variable|\.env)\b", 1.0),
        (r"\b(restart|reload|restart container|recreate|deploy)\b", 0.9),
    ],
    INTENT_KNOWLEDGE: [
        (r"\b(what|why|how|when|where|who|explain|describe|tell me about)\b", 0.6),
        (r"\b(documentation|docs|readme|guide|tutorial)\b", 0.9),
        (r"\b(history|previous|earlier|last (week|month|year)|recent)\b", 0.7),
    ],
}


def _score_keywords(message: str) -> dict[str, tuple[float, list[str]]]:
    """For each intent, return (score, list_of_matched_patterns)."""
    out: dict[str, tuple[float, list[str]]] = {}
    msg_lc = message.lower()
    for intent, patterns in _KEYWORDS.items():
        score = 0.0
        signals: list[str] = []
        for pat, w in patterns:
            if re.search(pat, msg_lc, re.IGNORECASE):
                score += w
                signals.append(f"kw:{pat}")
        if score > 0:
            out[intent] = (score, signals)
    return out


def _recency_signal(db: Session, user_id: Optional[int]) -> dict[str, float]:
    """Boost intents the user has been engaged with recently.

    Reads action_type distribution from chat_logs for this user in the
    last 24h. Maps a few common action_types to intents and adds a
    small (0.3) bonus per signal so a clear pattern shifts the score
    but doesn't overpower a strong keyword match.
    """
    if user_id is None:
        return {}

    try:
        rows = db.execute(
            text(
                "SELECT action_type, COUNT(*) AS n "
                "FROM chat_logs "
                "WHERE user_id = :uid "
                "  AND created_at > NOW() - INTERVAL '24 hours' "
                "  AND action_type IS NOT NULL "
                "GROUP BY action_type "
                "ORDER BY n DESC LIMIT 5"
            ),
            {"uid": int(user_id)},
        ).fetchall()
    except Exception:
        # chat_logs schema varies per branch; fail silent and let keyword
        # scoring carry the day.
        return {}

    signals: dict[str, float] = {}
    for action_type, _n in rows or []:
        at = (action_type or "").lower()
        # Action-type → intent mapping (best-effort)
        if any(k in at for k in ("code", "git", "commit", "diff", "patch", "lint", "test")):
            signals[INTENT_CODE] = signals.get(INTENT_CODE, 0) + 0.3
        elif any(k in at for k in ("trade", "broker", "position", "ticker", "buy", "sell")):
            signals[INTENT_TRADING] = signals.get(INTENT_TRADING, 0) + 0.3
        elif any(k in at for k in ("plan", "task", "schedule", "calendar")):
            signals[INTENT_PLANNING] = signals.get(INTENT_PLANNING, 0) + 0.3
        elif any(k in at for k in ("config", "setting", "env", "kill")):
            signals[INTENT_META] = signals.get(INTENT_META, 0) + 0.3
    return signals


def classify_intent(
    message: str,
    *,
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> IntentClassification:
    """Public entry. Returns the most likely intent + a [0,1] confidence.

    Confidence is computed as ``top_score / sum_of_all_scores`` so a clear
    winner gets close to 1 and an ambiguous tie gets ~0.5.
    """
    msg = (message or "").strip()
    if not msg:
        return IntentClassification(intent=INTENT_CASUAL, confidence=1.0, signals=["empty"])

    kw_scores = _score_keywords(msg)
    rec_scores = _recency_signal(db, user_id) if db is not None else {}

    combined: dict[str, float] = {}
    signals_per_intent: dict[str, list[str]] = {}
    for intent, (score, sigs) in kw_scores.items():
        combined[intent] = combined.get(intent, 0) + score
        signals_per_intent.setdefault(intent, []).extend(sigs)
    for intent, score in rec_scores.items():
        combined[intent] = combined.get(intent, 0) + score
        signals_per_intent.setdefault(intent, []).append(f"recency:+{score}")

    if not combined:
        # No keyword/recency signal at all → casual chitchat
        return IntentClassification(intent=INTENT_CASUAL, confidence=0.5, signals=["no_signal"])

    total = sum(combined.values()) or 1.0
    intent, top = max(combined.items(), key=lambda kv: kv[1])
    confidence = round(top / total, 4)

    return IntentClassification(
        intent=intent,
        confidence=confidence,
        signals=signals_per_intent.get(intent, []),
    )


# Which retrievers to fire per intent. ``"*"`` means all. Using sets so
# the assembly orchestrator can do fast membership checks.
RETRIEVER_PLAN: dict[str, set[str]] = {
    INTENT_CODE: {
        "code_brain", "rag", "memory", "personality", "chat_history",
        "project_brain", "project_files", "planner",
    },
    INTENT_TRADING: {
        "rag", "memory", "personality", "chat_history",
        "project_brain", "reasoning",
    },
    INTENT_PLANNING: {
        "planner", "rag", "memory", "personality", "chat_history",
        "project_brain", "reasoning",
    },
    INTENT_KNOWLEDGE: {
        "rag", "memory", "chat_history", "project_files",
        "code_brain", "personality",
    },
    INTENT_META: {
        "rag", "personality", "chat_history",
    },
    INTENT_CASUAL: {
        "personality", "memory", "chat_history",
    },
}


def retrievers_for(intent: str) -> set[str]:
    """Return the set of retriever IDs we should fire for this intent."""
    return RETRIEVER_PLAN.get(intent, RETRIEVER_PLAN[INTENT_CASUAL])
