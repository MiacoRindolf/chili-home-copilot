"""Per-purpose routing policy.

Each LLM call site in CHILI passes a ``purpose=`` tag to the gateway.
The gateway looks up that purpose in ``llm_purpose_policy`` to decide:

  * Which routing strategy: passthrough | augmented | tree
  * Whether to decompose (tree-only)
  * Whether to cross-examine
  * Whether to use premium synthesis (vs return compiled-Ollama directly)
  * Which models to use at each tier (NULL = defaults from runtime_state)

Operators can flip behaviors by UPDATE on this table — no code change needed.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .tree_types import PurposePolicy

logger = logging.getLogger(__name__)


# Hard-coded fallback when no DB row is found. Conservative defaults =
# legacy passthrough so an unknown purpose can never accidentally enable
# the heavy tree pipeline.
_FALLBACK = PurposePolicy(
    purpose="<fallback>",
    routing_strategy="passthrough",
    decompose=False,
    cross_examine=False,
    use_premium_synthesis=True,
    high_stakes=False,
)

_PROJECT_AGENT_PURPOSES = frozenset({
    "project_ai_engineer",
    "project_architect",
    "project_backend_engineer",
    "project_devops_engineer",
    "project_frontend_engineer",
    "project_product_owner",
    "project_project_manager",
    "project_qa_engineer",
    "project_security_engineer",
    "project_ux_designer",
})

_REASONING_BACKGROUND_PURPOSES = frozenset({
    "reasoning_anticipate",
    "reasoning_evolve",
    "reasoning_user_model",
    "reasoning_web_research",
})


def _passthrough_policy(
    purpose: str,
    *,
    use_premium_synthesis: bool,
    high_stakes: bool,
) -> PurposePolicy:
    return PurposePolicy(
        purpose=purpose,
        routing_strategy="passthrough",
        decompose=False,
        cross_examine=False,
        use_premium_synthesis=use_premium_synthesis,
        high_stakes=high_stakes,
    )


def _offline_passthrough_policy(purpose: str) -> PurposePolicy:
    return _passthrough_policy(
        purpose,
        use_premium_synthesis=False,
        high_stakes=False,
    )


_PURPOSE_DEFAULTS: dict[str, PurposePolicy] = {
    "code_review": _offline_passthrough_policy("code_review"),
    "code_search": _offline_passthrough_policy("code_search"),
    "desktop_normalize_app": _offline_passthrough_policy("desktop_normalize_app"),
    "desktop_refine_speech": _offline_passthrough_policy("desktop_refine_speech"),
    "project_playwright": _offline_passthrough_policy("project_playwright"),
    "project_web_research": _offline_passthrough_policy("project_web_research"),
    "trading_reasoning": _offline_passthrough_policy("trading_reasoning"),
    "trading_pattern_mine": _offline_passthrough_policy("trading_pattern_mine"),
    "trading_reflect": _offline_passthrough_policy("trading_reflect"),
    "pattern_research_extract": _offline_passthrough_policy("pattern_research_extract"),
    "trading_analyze": _passthrough_policy(
        "trading_analyze",
        use_premium_synthesis=True,
        high_stakes=False,
    ),
    "trading_analyze_stream": _passthrough_policy(
        "trading_analyze_stream",
        use_premium_synthesis=True,
        high_stakes=False,
    ),
    "smart_pick_stream": _passthrough_policy(
        "smart_pick_stream",
        use_premium_synthesis=True,
        high_stakes=False,
    ),
    "trading_smart_pick": _passthrough_policy(
        "trading_smart_pick",
        use_premium_synthesis=True,
        high_stakes=False,
    ),
    "trading_brain_assistant": _passthrough_policy(
        "trading_brain_assistant",
        use_premium_synthesis=True,
        high_stakes=False,
    ),
    "autotrader_revalidation": _passthrough_policy(
        "autotrader_revalidation",
        use_premium_synthesis=False,
        high_stakes=True,
    ),
    "pattern_adjustment": _passthrough_policy(
        "pattern_adjustment",
        use_premium_synthesis=False,
        high_stakes=True,
    ),
    "trade_plan_extract": _passthrough_policy(
        "trade_plan_extract",
        use_premium_synthesis=False,
        high_stakes=True,
    ),
    "position_plan_generator": _passthrough_policy(
        "position_plan_generator",
        use_premium_synthesis=False,
        high_stakes=True,
    ),
    "pattern_suggest": _passthrough_policy(
        "pattern_suggest",
        use_premium_synthesis=False,
        high_stakes=True,
    ),
    **{
        purpose: _offline_passthrough_policy(purpose)
        for purpose in _PROJECT_AGENT_PURPOSES
    },
    **{
        purpose: _offline_passthrough_policy(purpose)
        for purpose in _REASONING_BACKGROUND_PURPOSES
    },
}


def _clone_policy(policy: PurposePolicy, purpose: str | None = None) -> PurposePolicy:
    return PurposePolicy(**{**policy.__dict__, "purpose": purpose or policy.purpose})


def get_policy(db: Session, purpose: str) -> PurposePolicy:
    purpose = (purpose or "").strip() or "llm_default"
    try:
        row = db.execute(text(
            "SELECT purpose, routing_strategy, decompose, cross_examine, "
            "       use_premium_synthesis, high_stakes, "
            "       primary_local_model, secondary_local_model, synthesizer_model, "
            "       max_chunks, chunk_timeout_sec, enabled "
            "FROM llm_purpose_policy WHERE purpose = :p LIMIT 1"
        ), {"p": purpose}).fetchone()
    except Exception as e:
        logger.debug("[context_brain.policy] lookup failed for %s: %s", purpose, e)
        row = None

    if row is None:
        purpose_default = _PURPOSE_DEFAULTS.get(purpose)
        if purpose_default is not None:
            return _clone_policy(purpose_default, purpose)

        # Fall back to llm_default if the specific purpose row is missing
        if purpose != "llm_default":
            try:
                row = db.execute(text(
                    "SELECT purpose, routing_strategy, decompose, cross_examine, "
                    "       use_premium_synthesis, high_stakes, "
                    "       primary_local_model, secondary_local_model, synthesizer_model, "
                    "       max_chunks, chunk_timeout_sec, enabled "
                    "FROM llm_purpose_policy WHERE purpose = 'llm_default' LIMIT 1"
                )).fetchone()
            except Exception:
                row = None

    if row is None:
        return _clone_policy(_FALLBACK, purpose)

    return PurposePolicy(
        purpose=str(row[0]),
        routing_strategy=str(row[1] or "passthrough"),
        decompose=bool(row[2]),
        cross_examine=bool(row[3]),
        use_premium_synthesis=bool(row[4]),
        high_stakes=bool(row[5]),
        primary_local_model=(row[6] if row[6] else None),
        secondary_local_model=(row[7] if row[7] else None),
        synthesizer_model=(row[8] if row[8] else None),
        max_chunks=int(row[9] or 8),
        chunk_timeout_sec=int(row[10] or 30),
        enabled=bool(row[11]),
    )


def list_policies(db: Session) -> list[PurposePolicy]:
    rows = db.execute(text(
        "SELECT purpose, routing_strategy, decompose, cross_examine, "
        "       use_premium_synthesis, high_stakes, "
        "       primary_local_model, secondary_local_model, synthesizer_model, "
        "       max_chunks, chunk_timeout_sec, enabled "
        "FROM llm_purpose_policy ORDER BY purpose"
    )).fetchall()
    out: list[PurposePolicy] = []
    for r in rows or []:
        out.append(PurposePolicy(
            purpose=str(r[0]),
            routing_strategy=str(r[1] or "passthrough"),
            decompose=bool(r[2]),
            cross_examine=bool(r[3]),
            use_premium_synthesis=bool(r[4]),
            high_stakes=bool(r[5]),
            primary_local_model=(r[6] if r[6] else None),
            secondary_local_model=(r[7] if r[7] else None),
            synthesizer_model=(r[8] if r[8] else None),
            max_chunks=int(r[9] or 8),
            chunk_timeout_sec=int(r[10] or 30),
            enabled=bool(r[11]),
        ))
    return out


# Default models — used when policy.primary_local_model is NULL.
def default_primary_local() -> str:
    return "qwen2.5-coder:3b-instruct-q8_0"


def default_secondary_local() -> str:
    """The cross-exam companion. We default to a small *different family*
    model so disagreement rate is meaningful. ``has_model`` in
    ollama_client lets the cross-examiner skip when this isn't pulled."""
    return "llama3.2:1b"


def default_synthesizer() -> str:
    """gpt-5.5 via the openai_client cascade."""
    return "gpt-5.5"
