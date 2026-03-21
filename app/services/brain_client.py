"""HTTP client for the optional Brain service (`chili-brain/`)."""
from __future__ import annotations

import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


def run_learning_cycle_via_brain_service(timeout_s: float = 7200.0) -> dict[str, Any]:
    """POST /v1/run-learning-cycle on Brain service. Raises on HTTP error or missing URL.

    While waiting, cooperates with ``shutdown_requested()`` (DB/file stop in worker):
    closing the httpx client can interrupt a blocking read so the worker can exit.
    """
    from .trading.learning import shutdown_requested

    base = (settings.brain_service_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("brain_service_url is not configured")

    url = f"{base}/v1/run-learning-cycle"
    headers: dict[str, str] = {}
    secret = (settings.brain_internal_secret or "").strip()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    logger.info("[brain_client] POST %s", url)

    client = httpx.Client(timeout=timeout_s)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: client.post(url, headers=headers))
            while True:
                done, _ = wait([future], timeout=1.0, return_when=FIRST_COMPLETED)
                if future in done:
                    break
                if shutdown_requested():
                    try:
                        client.close()
                    except Exception:
                        pass
            try:
                r = future.result()
            except Exception as e:
                if shutdown_requested():
                    logger.info("[brain_client] Request ended after cooperative stop: %s", e)
                    return {"ok": False, "reason": "stopped", "stopped": True}
                raise
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return {"ok": False, "error": "Brain service returned non-object JSON"}
        return data
    finally:
        try:
            client.close()
        except Exception:
            pass
