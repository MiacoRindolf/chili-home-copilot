"""Dataclasses passed between the F.10 pipeline stages.

Kept separate from ``types.py`` (the F.1-F.3 retrieval-stage types) so
import graphs stay clean.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# Chunk kinds — used by chunk_executor to choose retrieval and prompt style
CHUNK_KIND_FACT = "fact"
CHUNK_KIND_REASONING = "reasoning"
CHUNK_KIND_CODE = "code"
CHUNK_KIND_GENERAL = "general"


@dataclass
class PurposePolicy:
    """Loaded once per gateway call from ``llm_purpose_policy``."""
    purpose: str
    routing_strategy: str   # 'passthrough' | 'augmented' | 'tree'
    decompose: bool
    cross_examine: bool
    use_premium_synthesis: bool
    high_stakes: bool
    primary_local_model: Optional[str] = None
    secondary_local_model: Optional[str] = None
    synthesizer_model: Optional[str] = None
    max_chunks: int = 8
    chunk_timeout_sec: int = 30
    enabled: bool = True


@dataclass
class ChunkPlan:
    """One sub-question produced by the decomposer."""
    index: int
    query: str
    kind: str = CHUNK_KIND_GENERAL


@dataclass
class DecompositionPlan:
    chunks: list[ChunkPlan] = field(default_factory=list)
    strategy: str = "heuristic_passthrough"  # how it was produced
    decomposer_model: Optional[str] = None
    decompose_latency_ms: int = 0


@dataclass
class ChunkResponse:
    """One chunk, fully resolved (primary + optional secondary)."""
    plan: ChunkPlan
    primary_model: Optional[str] = None
    primary_response: str = ""
    primary_tokens_out: int = 0
    primary_latency_ms: int = 0
    secondary_model: Optional[str] = None
    secondary_response: Optional[str] = None
    secondary_tokens_out: int = 0
    secondary_latency_ms: int = 0
    similarity_score: Optional[float] = None
    selected_response: str = ""
    selection_reason: str = "primary_only"
    is_high_stakes: bool = False
    success: bool = False
    error: Optional[str] = None


@dataclass
class TreeOutcome:
    """Final output of the tree pipeline."""
    final_text: str
    chunks: list[ChunkResponse] = field(default_factory=list)
    compiled_context: str = ""
    decomposition_strategy: str = ""
    used_synthesis: bool = False
    synthesizer_model: Optional[str] = None
    # Cost / latency
    decompose_latency_ms: int = 0
    chunk_latency_ms: int = 0
    compile_latency_ms: int = 0
    synthesize_latency_ms: int = 0
    total_latency_ms: int = 0
    ollama_calls_count: int = 0
    premium_calls_count: int = 0
    ollama_total_tokens: int = 0
    premium_total_tokens: int = 0
    premium_cost_usd: float = 0.0
    # Identifiers populated when DB rows are inserted
    gateway_log_id: Optional[int] = None
    tree_id: Optional[int] = None
    success: bool = True
    error: Optional[str] = None


@dataclass
class GatewayCallResult:
    """What ``gateway_chat()`` returns to its caller. Same SHAPE as
    openai_client.chat() so existing code keeps working.

    The two-key invariant ``{"reply": str, "model": str}`` is preserved.
    Extra metadata available for callers that want it.
    """
    reply: str
    model: str
    tokens_used: int = 0
    routing_strategy: str = "passthrough"
    gateway_log_id: Optional[int] = None
    tree_id: Optional[int] = None
    raw: dict = field(default_factory=dict)

    def as_legacy_dict(self) -> dict:
        """Return the {reply, tokens_used, model} dict that
        openai_client.chat() callers expect."""
        return {
            "reply": self.reply,
            "tokens_used": self.tokens_used,
            "model": self.model,
        }
