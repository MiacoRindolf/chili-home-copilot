"""Agent registry and inter-agent message bus.

All Project Brain agents register here. The registry provides lookup,
listing, and message routing between agents.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .base import AgentBase
from ...models.project_brain import AgentMessage

logger = logging.getLogger(__name__)

AGENT_REGISTRY: Dict[str, AgentBase] = {}


def register_agent(agent: AgentBase) -> None:
    AGENT_REGISTRY[agent.name] = agent
    logger.info("[project_brain] Registered agent: %s", agent.name)


def get_agent(name: str) -> Optional[AgentBase]:
    return AGENT_REGISTRY.get(name)


def list_agents() -> List[Dict[str, Any]]:
    return [
        {
            "name": a.name,
            "label": a.label,
            "icon": a.icon,
            "active": a.active,
            "role": a.role_prompt[:100],
        }
        for a in AGENT_REGISTRY.values()
    ]


def get_message_feed(db: Session, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent inter-agent messages for the dashboard."""
    msgs = (
        db.query(AgentMessage)
        .filter(AgentMessage.user_id == user_id)
        .order_by(AgentMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": m.id,
            "from": m.from_agent,
            "to": m.to_agent,
            "type": m.message_type,
            "content": m.content_json,
            "acknowledged": m.acknowledged,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in msgs
    ]


def _register_all_agents() -> None:
    """Import and register all available agents."""
    from .agents.product_owner import ProductOwnerAgent
    from .agents.project_manager import ProjectManagerAgent
    from .agents.architect import ArchitectAgent
    register_agent(ProductOwnerAgent())
    register_agent(ProjectManagerAgent())
    register_agent(ArchitectAgent())


_register_all_agents()
