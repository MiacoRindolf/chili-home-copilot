"""B2: lightweight in-process event bus for pane coordination.

Before this module, the PO agent mutated planner tasks directly from the
Agents pane, and the Feed + Handoff panes found out only on the next poll.
Now ``push_requirement_to_planner`` publishes a ``PlannerRequirementPushed``
event; any component (feed aggregator, handoff cache invalidator, audit log)
subscribes via ``subscribe``.

Kept intentionally dependency-free — no asyncio queue, no external broker.
Handlers run inline in the same request/session. Failures are caught so one
bad handler cannot blow up an agent cycle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List

_log = logging.getLogger("chili.project_brain.events")


@dataclass
class PlannerRequirementPushed:
    """Fired when the PO agent pushes a requirement into a planner project
    as a planner task. Consumers: feed aggregator (Feed pane), handoff cache
    invalidator (Handoff pane), audit/metrics.
    """

    user_id: int
    requirement_id: int
    planner_project_id: int
    planner_task_id: int
    title: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    extra: Dict[str, Any] = field(default_factory=dict)


# Handler signature: ``fn(event) -> None``. Handlers must be fast and
# exception-safe; the bus swallows exceptions to protect the producer.
Handler = Callable[[PlannerRequirementPushed], None]

_handlers: Dict[str, List[Handler]] = {}


def subscribe(event_name: str, handler: Handler) -> None:
    """Register a handler for an event name. Idempotent — re-subscribing
    the same function is a no-op.
    """
    existing = _handlers.setdefault(event_name, [])
    if handler not in existing:
        existing.append(handler)


def unsubscribe(event_name: str, handler: Handler) -> None:
    """Remove a handler. Safe to call with a handler that was never
    subscribed — useful in tests that set up and tear down.
    """
    if event_name in _handlers:
        try:
            _handlers[event_name].remove(handler)
        except ValueError:
            pass


def publish(event: PlannerRequirementPushed) -> None:
    """Dispatch ``event`` to all subscribed handlers in registration order.
    Exceptions from one handler do not prevent later handlers from running.
    """
    for handler in list(_handlers.get(type(event).__name__, [])):
        try:
            handler(event)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning(
                "event handler %s for %s raised: %s",
                getattr(handler, "__name__", repr(handler)),
                type(event).__name__,
                exc,
            )


def clear_handlers_for_tests() -> None:
    """Test-only helper: wipe all handlers. Do not call from production code."""
    _handlers.clear()
