"""Shared dataclasses for the Context Brain pipeline.

These types are passed between the stages: retrievers produce
``ContextCandidate``s; the scorer attaches relevance scores; the
budget enforcer marks ``selected``; the composer assembles the
final ``AssembledContext``.

Keeping these in one module so each stage can import without
circular dependency risk.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# Canonical source IDs. Add a new one only when wiring a new retriever.
SOURCE_RAG = "rag"
SOURCE_PROJECT_FILES = "project_files"
SOURCE_PERSONALITY = "personality"
SOURCE_MEMORY = "memory"
SOURCE_CODE_BRAIN = "code_brain"
SOURCE_REASONING = "reasoning"
SOURCE_PROJECT_BRAIN = "project_brain"
SOURCE_PLANNER = "planner"
SOURCE_CHAT_HISTORY = "chat_history"

KNOWN_SOURCES = frozenset({
    SOURCE_RAG, SOURCE_PROJECT_FILES, SOURCE_PERSONALITY, SOURCE_MEMORY,
    SOURCE_CODE_BRAIN, SOURCE_REASONING, SOURCE_PROJECT_BRAIN,
    SOURCE_PLANNER, SOURCE_CHAT_HISTORY,
})


# Canonical intents. Heuristic classifier maps user input to one of these.
INTENT_CODE = "code"          # writing/reviewing/explaining code
INTENT_TRADING = "trading"    # markets, positions, signals, broker
INTENT_PLANNING = "planning"  # tasks, projects, plans, todos
INTENT_KNOWLEDGE = "knowledge"  # questions about the project itself
INTENT_CASUAL = "casual"      # chitchat, off-topic
INTENT_META = "meta"          # questions about chili itself, settings, etc.

KNOWN_INTENTS = frozenset({
    INTENT_CODE, INTENT_TRADING, INTENT_PLANNING,
    INTENT_KNOWLEDGE, INTENT_CASUAL, INTENT_META,
})


@dataclass
class ContextCandidate:
    """One unit of retrievable context produced by a retriever.

    The scorer takes a list of these, attaches ``relevance_score``, and
    the budget enforcer flips ``selected`` on those that fit.
    """
    source_id: str
    content: str
    raw_score: float = 0.0           # retriever's own confidence (0..1 ish)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Filled in by scorer
    relevance_score: float = 0.0
    final_weight: float = 1.0

    # Filled in by budget
    tokens_estimated: int = 0
    selected: bool = False

    # Filled in by distiller (when applicable)
    distilled: bool = False

    # Cached so we don't re-hash during composition
    _content_hash: Optional[str] = None

    @property
    def content_hash(self) -> str:
        if self._content_hash is None:
            self._content_hash = hashlib.sha256(
                f"{self.source_id}|{self.content}".encode("utf-8")
            ).hexdigest()
        return self._content_hash

    @property
    def preview(self) -> str:
        """Short human-readable preview used in logs / Brain UI."""
        text = self.content.strip().replace("\n", " ")
        return text[:200]


@dataclass
class IntentClassification:
    intent: str
    confidence: float            # 0..1
    signals: list[str] = field(default_factory=list)  # which heuristics fired


@dataclass
class AssembledContext:
    """Final output of the Context Brain pipeline.

    Goes back to the chat hot path which then passes ``prompt_text`` to
    ``openai_client.chat()``. ``assembly_id`` lets the feedback layer
    link the LLM response back to the assembly record once the cascade
    returns.
    """
    prompt_text: str
    intent: IntentClassification
    candidates: list[ContextCandidate]
    total_tokens: int
    budget_token_cap: int
    distilled: bool = False
    distillation_tokens_saved: int = 0
    strategy_version: int = 1
    assembly_id: Optional[int] = None
    elapsed_ms: int = 0

    @property
    def selected_candidates(self) -> list[ContextCandidate]:
        return [c for c in self.candidates if c.selected]

    @property
    def sources_used_summary(self) -> dict[str, int]:
        """``{"rag": 3, "memory": 2, ...}`` — handy for the assembly log."""
        out: dict[str, int] = {}
        for c in self.candidates:
            if c.selected:
                out[c.source_id] = out.get(c.source_id, 0) + 1
        return out


# Per-source token caps as fractions of the total budget. The composer
# clamps each source to at most this fraction. Tunable via runtime_state
# in a future phase.
DEFAULT_SOURCE_CAPS: dict[str, float] = {
    SOURCE_RAG: 0.30,
    SOURCE_PROJECT_FILES: 0.25,
    SOURCE_CODE_BRAIN: 0.25,
    SOURCE_PROJECT_BRAIN: 0.20,
    SOURCE_PLANNER: 0.15,
    SOURCE_REASONING: 0.10,
    SOURCE_PERSONALITY: 0.08,
    SOURCE_MEMORY: 0.10,
    SOURCE_CHAT_HISTORY: 0.20,
}


# Estimate tokens cheaply without calling tiktoken (which would add a
# dependency). Heuristic: ~4 chars per token in English. Good enough for
# budgeting; the LLM still does the real count downstream.
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)
