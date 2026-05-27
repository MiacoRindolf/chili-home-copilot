"""Control the Compose ``brain-worker`` service via the Docker API (no subprocess worker).

The Brain UI starts/stops the **brain-worker** container. Requires ``docker`` (PyPI) and
``/var/run/docker.sock`` mounted into the ``chili`` container (see ``docker-compose.yml``).

If the container does not exist yet, create it once from the repo: ``docker compose up -d brain-worker``.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..config import settings

_LOG = logging.getLogger(__name__)
_LIVENESS_CACHE_TTL_SECONDS = 2.0
_liveness_cache: dict[str, Any] = {"expires_at": 0.0, "value": "unknown"}


def _docker_sdk_client():
    import docker

    return docker.from_env()


def _close_docker_sdk_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception as exc:  # pragma: no cover - defensive logging only
        _LOG.debug("Docker SDK close failed: %s", exc)


def _clear_liveness_cache() -> None:
    _liveness_cache["expires_at"] = 0.0
    _liveness_cache["value"] = "unknown"


def _find_brain_worker_container(client: Any):
    service = (getattr(settings, "brain_worker_compose_service", None) or "brain-worker").strip()
    project = (getattr(settings, "brain_worker_compose_project", None) or "").strip() or None

    for c in client.containers.list(all=True):
        labels = c.labels or {}
        if labels.get("com.docker.compose.service") != service:
            continue
        if project and labels.get("com.docker.compose.project") != project:
            continue
        return c
    return None


def find_brain_worker_container():
    """Return the docker.models.containers.Container or None.

    Prefer the liveness/start/stop helpers in this module for normal use; they
    close Docker SDK clients after each short operation.
    """
    client = None
    try:
        client = _docker_sdk_client()
    except Exception as e:
        _LOG.debug("Docker SDK unavailable: %s", e)
        return None

    try:
        c = _find_brain_worker_container(client)
        if c is None:
            _close_docker_sdk_client(client)
        return c
    except Exception as e:
        _LOG.warning("list containers: %s", e)
        _close_docker_sdk_client(client)
        return None


def _brain_worker_container_state() -> str:
    try:
        client = _docker_sdk_client()
    except Exception as e:
        _LOG.debug("Docker SDK unavailable: %s", e)
        return "missing"

    try:
        c = _find_brain_worker_container(client)
        if c is None:
            return "missing"
        c.reload()
        return str(c.status or "unknown").lower()
    except Exception as e:
        _LOG.warning("brain-worker container state: %s", e)
        return "unknown"
    finally:
        _close_docker_sdk_client(client)


def brain_worker_container_running() -> bool:
    return _brain_worker_container_state() == "running"


def brain_worker_start_docker() -> dict[str, Any]:
    """Start the brain-worker container (``docker start``)."""
    client = None
    try:
        client = _docker_sdk_client()
    except Exception as e:
        return {
            "ok": False,
            "error": f"Docker API unavailable: {e}",
            "hint": "Mount /var/run/docker.sock into the chili container and rebuild. "
            "For local uvicorn without Docker, run: docker compose up -d brain-worker",
        }

    try:
        c = _find_brain_worker_container(client)
        if c is None:
            return {
                "ok": False,
                "error": "brain-worker container not found.",
                "hint": "From the repo root run once: docker compose up -d brain-worker",
            }

        c.reload()
        if c.status == "running":
            return {"ok": True, "mode": "docker", "already_running": True, "id": c.id[:12]}
        c.start()
        return {"ok": True, "mode": "docker", "started": True, "id": c.id[:12]}
    except Exception as e:
        _LOG.exception("brain_worker start")
        return {"ok": False, "error": str(e)}
    finally:
        _clear_liveness_cache()
        _close_docker_sdk_client(client)


def brain_worker_stop_docker(timeout_s: int = 90) -> dict[str, Any]:
    """Stop the brain-worker container."""
    client = None
    try:
        client = _docker_sdk_client()
    except Exception as e:
        return {"ok": False, "error": f"Docker API unavailable: {e}"}

    try:
        c = _find_brain_worker_container(client)
        if c is None:
            return {"ok": True, "mode": "docker", "already_stopped": True, "note": "container not found"}

        c.reload()
        if c.status != "running":
            return {"ok": True, "mode": "docker", "already_stopped": True}
        c.stop(timeout=timeout_s)
        return {"ok": True, "mode": "docker", "stopped": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        _clear_liveness_cache()
        _close_docker_sdk_client(client)


def brain_worker_liveness_for_ui() -> str:
    """alive | dead | unknown | missing - use container state across namespaces."""
    now = time.monotonic()
    if now < float(_liveness_cache.get("expires_at") or 0.0):
        return str(_liveness_cache.get("value") or "unknown")

    state = _brain_worker_container_state()
    if state == "running":
        result = "alive"
    elif state in ("exited", "dead", "created", "missing"):
        result = "missing" if state == "missing" else "dead"
    else:
        result = "unknown"

    _liveness_cache["value"] = result
    _liveness_cache["expires_at"] = now + _LIVENESS_CACHE_TTL_SECONDS
    return result
