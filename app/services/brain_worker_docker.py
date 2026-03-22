"""Control the Compose ``brain-worker`` service via the Docker API (no subprocess worker).

The Brain UI starts/stops the **brain-worker** container. Requires ``docker`` (PyPI) and
``/var/run/docker.sock`` mounted into the ``chili`` container (see ``docker-compose.yml``).

If the container does not exist yet, create it once from the repo: ``docker compose up -d brain-worker``.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import settings

_LOG = logging.getLogger(__name__)


def _docker_sdk_client():
    import docker

    return docker.from_env()


def find_brain_worker_container():
    """Return the docker.models.containers.Container or None."""
    try:
        client = _docker_sdk_client()
    except Exception as e:
        _LOG.debug("Docker SDK unavailable: %s", e)
        return None

    service = (getattr(settings, "brain_worker_compose_service", None) or "brain-worker").strip()
    project = (getattr(settings, "brain_worker_compose_project", None) or "").strip() or None

    try:
        for c in client.containers.list(all=True):
            labels = c.labels or {}
            if labels.get("com.docker.compose.service") != service:
                continue
            if project and labels.get("com.docker.compose.project") != project:
                continue
            return c
    except Exception as e:
        _LOG.warning("list containers: %s", e)
        return None
    return None


def brain_worker_container_running() -> bool:
    c = find_brain_worker_container()
    if c is None:
        return False
    try:
        c.reload()
        return c.status == "running"
    except Exception:
        return False


def brain_worker_start_docker() -> dict[str, Any]:
    """Start the brain-worker container (``docker start``)."""
    try:
        _docker_sdk_client()
    except Exception as e:
        return {
            "ok": False,
            "error": f"Docker API unavailable: {e}",
            "hint": "Mount /var/run/docker.sock into the chili container and rebuild. "
            "For local uvicorn without Docker, run: docker compose up -d brain-worker",
        }

    c = find_brain_worker_container()
    if c is None:
        return {
            "ok": False,
            "error": "brain-worker container not found.",
            "hint": "From the repo root run once: docker compose up -d brain-worker",
        }

    try:
        c.reload()
        if c.status == "running":
            return {"ok": True, "mode": "docker", "already_running": True, "id": c.id[:12]}
        c.start()
        return {"ok": True, "mode": "docker", "started": True, "id": c.id[:12]}
    except Exception as e:
        _LOG.exception("brain_worker start")
        return {"ok": False, "error": str(e)}


def brain_worker_stop_docker(timeout_s: int = 90) -> dict[str, Any]:
    """Stop the brain-worker container."""
    try:
        _docker_sdk_client()
    except Exception as e:
        return {"ok": False, "error": f"Docker API unavailable: {e}"}

    c = find_brain_worker_container()
    if c is None:
        return {"ok": True, "mode": "docker", "already_stopped": True, "note": "container not found"}

    try:
        c.reload()
        if c.status != "running":
            return {"ok": True, "mode": "docker", "already_stopped": True}
        c.stop(timeout=timeout_s)
        return {"ok": True, "mode": "docker", "stopped": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def brain_worker_liveness_for_ui() -> str:
    """alive | dead | unknown — use container state, not PID across namespaces."""
    if brain_worker_container_running():
        return "alive"
    c = find_brain_worker_container()
    if c is None:
        return "dead"
    try:
        c.reload()
        if c.status in ("exited", "dead", "created"):
            return "dead"
        if c.status == "running":
            return "alive"
    except Exception:
        pass
    return "unknown"
