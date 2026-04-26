"""F.5 — Outcome tracker for the universal LLM gateway.

Every gateway call gets a row in ``llm_gateway_log``; this module is the
counterpart that records what happened **after** the call so the F.4
distiller has training signal to chew on.

The signal sources we capture, in order of confidence:

  1. Explicit user thumbs (chat UI ``POST /api/brain/context/gateway/thumbs``).
  2. Code dispatch outcome — did the patch apply, did pytest pass.
  3. Trade outcome — did the closed trade win (linked via gateway_log_id on
     the trade row when the gateway produced the entry rationale).
  4. Chat followup heuristic — short positive followup (~"thanks", "perfect")
     vs. clarification request (~"no I meant", "you misunderstood that").

Every recorded outcome lands in ``context_brain_outcome`` with a normalized
``quality_signal`` in ``[0, 1]`` so the distiller can join + average without
caring about source-specific encoding. Source is preserved in
``outcome_source`` for transparency and per-source debugging.

Failure mode: every public function is a soft no-op on exception. We never
want the outcome side path to break a chat reply or a dispatched code task.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Heuristic patterns for chat followup classification.
# ---------------------------------------------------------------------------

_POSITIVE_FOLLOWUP = re.compile(
    r"\b(thanks|thank you|perfect|great|got it|nice|exactly|awesome|sweet|"
    r"makes sense|that helps|appreciate|love it|nailed it|spot on)\b",
    re.IGNORECASE,
)

_NEGATIVE_FOLLOWUP = re.compile(
    r"\b(no(?:[, ]+i)? meant|that's wrong|that is wrong|wrong|incorrect|"
    r"misunderstood|doesn['']?t answer|didn['']?t answer|not what i asked|"
    r"try again|that's not|that is not|you're (mis|wrong)|stop doing|"
    r"don['']?t do that|hallucinat)\b",
    re.IGNORECASE,
)

_REGENERATE_HINT = re.compile(
    r"\b(regenerate|do over|redo|try again|rephrase|simpler|shorter|longer)\b",
    re.IGNORECASE,
)

# Short bare followups still count as positive — "thanks" alone is +1.
_SHORT_POSITIVE = {"thanks", "thx", "ty", "perfect", "great", "nice", "got it"}


def _classify_followup(text_msg: str) -> tuple[Optional[float], dict]:
    """Return (quality_signal, signal_breakdown) for a followup user message.

    ``quality_signal`` is None when the message is too ambiguous to score —
    in that case we still record presence-of-followup but no rating.
    """
    if not text_msg or not text_msg.strip():
        return None, {"reason": "empty"}

    msg = text_msg.strip()
    if msg.lower() in _SHORT_POSITIVE:
        return 0.9, {"hint": "short_positive_word"}

    pos_hits = len(_POSITIVE_FOLLOWUP.findall(msg))
    neg_hits = len(_NEGATIVE_FOLLOWUP.findall(msg))
    regen_hit = bool(_REGENERATE_HINT.search(msg))

    if neg_hits == 0 and pos_hits == 0 and not regen_hit:
        return None, {"reason": "no_signal"}

    # Explicit negative wins — even mixed messages with both pos+neg likely
    # are correcting something specific.
    if neg_hits >= 1:
        return 0.15, {"pos": pos_hits, "neg": neg_hits, "regen": regen_hit}

    if regen_hit and pos_hits == 0:
        return 0.35, {"pos": pos_hits, "neg": neg_hits, "regen": regen_hit}

    if pos_hits >= 1:
        # Pure positive (after eliminating negs/regens above).
        return 0.85, {"pos": pos_hits, "neg": neg_hits, "regen": regen_hit}

    return None, {"reason": "ambiguous"}


# ---------------------------------------------------------------------------
# Public recording helpers. Each one is best-effort: any exception is logged
# but never re-raised, so the side path can't break a hot user-facing call.
# ---------------------------------------------------------------------------

def _last_gateway_log_id(
    db: Session,
    *,
    user_id: Optional[int],
    purpose: Optional[str],
    minutes: int = 5,
) -> Optional[int]:
    """Look up the most-recent gateway_log row for a user/purpose, recent enough
    to be plausibly the call this followup is reacting to.
    """
    try:
        clauses = ["started_at >= NOW() - (:m * INTERVAL '1 minute')"]
        params: dict = {"m": minutes}
        if user_id is not None:
            clauses.append("user_id = :uid")
            params["uid"] = user_id
        if purpose:
            clauses.append("purpose = :p")
            params["p"] = purpose
        sql = (
            "SELECT id FROM llm_gateway_log WHERE "
            + " AND ".join(clauses)
            + " ORDER BY started_at DESC LIMIT 1"
        )
        row = db.execute(text(sql), params).fetchone()
        return int(row[0]) if row else None
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("[outcome_tracker] last_gateway_log_id failed: %s", e)
        return None


def _insert_outcome(
    db: Session,
    *,
    gateway_log_id: Optional[int],
    purpose: Optional[str],
    user_id: Optional[int],
    quality_signal: Optional[float],
    outcome_source: str,
    chat_message_id: Optional[int] = None,
    user_followed_up: Optional[bool] = None,
    user_regenerated: Optional[bool] = None,
    user_edited: Optional[bool] = None,
    user_dismissed: Optional[bool] = None,
    thumbs_vote: Optional[int] = None,
    raw: Optional[dict] = None,
    tree_id: Optional[int] = None,
) -> Optional[int]:
    """Write an outcome row. Returns the new id, or None on failure."""
    try:
        params = {
            "gw": gateway_log_id,
            "tree": tree_id,
            "chat": chat_message_id,
            "fu": user_followed_up,
            "regen": user_regenerated,
            "edited": user_edited,
            "dismissed": user_dismissed,
            "q": quality_signal,
            "thumbs": thumbs_vote,
            "src": outcome_source,
            "raw": json.dumps(raw) if raw else None,
            "p": purpose,
            "uid": user_id,
        }
        row = db.execute(
            text(
                """
                INSERT INTO context_brain_outcome (
                    gateway_log_id, tree_id, chat_message_id,
                    user_followed_up, user_regenerated, user_edited, user_dismissed,
                    quality_signal, thumbs_vote, outcome_source,
                    raw_signal_json, purpose, user_id
                ) VALUES (
                    :gw, :tree, :chat,
                    :fu, :regen, :edited, :dismissed,
                    :q, :thumbs, :src,
                    :raw, :p, :uid
                )
                RETURNING id
                """
            ),
            params,
        ).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:  # pragma: no cover
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[outcome_tracker] insert failed: %s", e)
        return None


def record_chat_followup(
    db: Session,
    *,
    user_id: Optional[int],
    user_message: str,
    chat_message_id: Optional[int] = None,
    purpose: str = "chat_user",
    lookback_minutes: int = 5,
) -> Optional[int]:
    """Heuristically score a user followup and write to context_brain_outcome.

    Called from the chat path right after a new user turn arrives, before the
    LLM is invoked for the new turn — so the signal is attributed to the
    *previous* gateway call.
    """
    try:
        quality, breakdown = _classify_followup(user_message)
        if quality is None and not breakdown:
            return None

        gw_id = _last_gateway_log_id(
            db, user_id=user_id, purpose=purpose, minutes=lookback_minutes
        )
        if gw_id is None:
            return None

        return _insert_outcome(
            db,
            gateway_log_id=gw_id,
            purpose=purpose,
            user_id=user_id,
            quality_signal=quality,
            outcome_source="chat_followup_heuristic",
            chat_message_id=chat_message_id,
            user_followed_up=True,
            user_regenerated=bool(breakdown.get("regen")),
            raw={"heuristic": breakdown, "snippet": user_message[:240]},
        )
    except Exception as e:  # pragma: no cover
        logger.warning("[outcome_tracker] record_chat_followup failed: %s", e)
        return None


def record_thumbs(
    db: Session,
    *,
    gateway_log_id: int,
    vote: int,
    user_id: Optional[int] = None,
    purpose: Optional[str] = None,
    note: Optional[str] = None,
) -> Optional[int]:
    """Record an explicit user thumbs (-1, 0, +1).

    Maps to ``quality_signal`` linearly: -1 → 0.0, 0 → 0.5, +1 → 1.0.
    """
    try:
        v = max(-1, min(1, int(vote)))
        quality = (v + 1) / 2.0
        return _insert_outcome(
            db,
            gateway_log_id=gateway_log_id,
            purpose=purpose,
            user_id=user_id,
            quality_signal=quality,
            outcome_source="user_thumbs",
            thumbs_vote=v,
            raw={"note": note} if note else None,
        )
    except Exception as e:  # pragma: no cover
        logger.warning("[outcome_tracker] record_thumbs failed: %s", e)
        return None


def record_dispatch_outcome(
    db: Session,
    *,
    gateway_log_id: int,
    success: bool,
    purpose: Optional[str] = None,
    detail: Optional[dict] = None,
) -> Optional[int]:
    """Record a code-dispatch step outcome (patch applied + tests passed = 1.0)."""
    try:
        return _insert_outcome(
            db,
            gateway_log_id=gateway_log_id,
            purpose=purpose,
            user_id=None,
            quality_signal=1.0 if success else 0.0,
            outcome_source="code_dispatch",
            raw=detail,
        )
    except Exception as e:  # pragma: no cover
        logger.warning("[outcome_tracker] record_dispatch_outcome failed: %s", e)
        return None


def record_trade_outcome(
    db: Session,
    *,
    gateway_log_id: int,
    pnl: float,
    purpose: Optional[str] = None,
    detail: Optional[dict] = None,
) -> Optional[int]:
    """Record a closed-trade outcome.

    Maps PnL to quality_signal via a soft sigmoid-ish bucket:
      pnl <= -1%  → 0.1     (clear loss)
      pnl <  0    → 0.35    (minor loss)
      pnl == 0    → 0.5     (neutral)
      pnl <  1%   → 0.65    (minor win)
      pnl >= 1%   → 0.9     (clear win)
    """
    try:
        if pnl <= -0.01:
            q = 0.1
        elif pnl < 0:
            q = 0.35
        elif pnl == 0:
            q = 0.5
        elif pnl < 0.01:
            q = 0.65
        else:
            q = 0.9
        raw = {"pnl": pnl, **(detail or {})}
        return _insert_outcome(
            db,
            gateway_log_id=gateway_log_id,
            purpose=purpose,
            user_id=None,
            quality_signal=q,
            outcome_source="trade_close",
            raw=raw,
        )
    except Exception as e:  # pragma: no cover
        logger.warning("[outcome_tracker] record_trade_outcome failed: %s", e)
        return None
