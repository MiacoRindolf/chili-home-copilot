"""Project Brain: autonomous, self-evolving AI agents for the full project lifecycle.

Modules:
- base        -- AgentBase class with shared capabilities
- registry    -- agent registry and inter-agent message bus
- web_research -- web research via DuckDuckGo + LLM summarization
- learning    -- orchestrator for running all agent learning cycles
- agents/     -- individual agent implementations (product_owner, etc.)
"""

from .registry import AGENT_REGISTRY, get_agent, list_agents
from .learning import (
    run_project_brain_cycle,
    get_project_brain_status,
    get_project_brain_metrics,
)

__all__ = [
    "AGENT_REGISTRY",
    "get_agent",
    "list_agents",
    "run_project_brain_cycle",
    "get_project_brain_status",
    "get_project_brain_metrics",
]
