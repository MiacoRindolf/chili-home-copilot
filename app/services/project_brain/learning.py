"""Learning cycle orchestrator for Project Brain agents.

Runs each active agent's learning cycle in sequence, tracks status
and progress similar to the existing Reasoning Brain pattern.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from ...config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_status: Dict[str, Any] = {
    "running": False,
    "last_run": None,
    "last_agent": None,
    "step": None,
    "progress": 0.0,
    "error": None,
}


def get_project_brain_status() -> Dict[str, Any]:
    return dict(_status)


def get_project_brain_metrics(db: Session, user_id: int) -> Dict[str, Any]:
    """Aggregate metrics across all agents."""
    from .registry import AGENT_REGISTRY
    agents = {}
    for name, agent in AGENT_REGISTRY.items():
        agents[name] = agent.get_metrics(db, user_id)
    return {
        "status": get_project_brain_status(),
        "agents": agents,
    }


def run_project_brain_cycle(db: Session, user_id: int, agent_name: Optional[str] = None) -> Dict[str, Any]:
    """Run the learning cycle for one or all active agents.

    If agent_name is given, run only that agent. Otherwise run all active agents.
    """
    from .registry import AGENT_REGISTRY

    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "Cycle already running"}

    try:
        _status.update(running=True, error=None, progress=0.0, step="starting")

        agents_to_run = []
        if agent_name:
            agent = AGENT_REGISTRY.get(agent_name)
            if not agent:
                return {"ok": False, "error": f"Agent {agent_name!r} not found"}
            agents_to_run = [(agent_name, agent)]
        else:
            agents_to_run = [(n, a) for n, a in AGENT_REGISTRY.items() if a.active]

        if not agents_to_run:
            _status.update(running=False, step="idle", progress=1.0)
            return {"ok": True, "agents_run": 0}

        results = {}
        total = len(agents_to_run)
        for i, (name, agent) in enumerate(agents_to_run):
            _status.update(
                last_agent=name,
                step=f"running {agent.label}",
                progress=(i / total),
            )
            try:
                result = agent.run_cycle(db, user_id)
                results[name] = result
                logger.info("[project_brain] %s cycle completed: %s", name, result)
            except Exception as e:
                logger.exception("[project_brain] %s cycle failed", name)
                results[name] = {"error": str(e)}

        _status.update(
            running=False,
            last_run=datetime.utcnow().isoformat(),
            step="idle",
            progress=1.0,
        )
        return {"ok": True, "agents_run": len(agents_to_run), "results": results}
    finally:
        _lock.release()


def run_project_brain_cycle_background(db_factory, user_id: int, agent_name: Optional[str] = None) -> None:
    """Kick off a cycle in a background thread. db_factory returns a new Session."""
    def _run():
        db = db_factory()
        try:
            run_project_brain_cycle(db, user_id, agent_name)
        finally:
            db.close()

    t = threading.Thread(target=_run, daemon=True, name="project-brain-cycle")
    t.start()
