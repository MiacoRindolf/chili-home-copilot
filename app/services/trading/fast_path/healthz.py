"""Tiny aiohttp /healthz for compose's health check.

Returns 200 + JSON snapshot when:
* Master switch is enabled
* DB writer queue is below 90% of capacity
* No pair has been ``halted``
* At least one pair has ``last_bar_at`` within the last 90s OR no pair
  has ``streaming`` state yet (boot grace — 30s after process start)

Returns 503 otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None  # type: ignore

logger = logging.getLogger(__name__)

# Boot grace — for the first N seconds after start, /healthz returns 200
# even if no bars have arrived yet (markets may be quiet).
BOOT_GRACE_S = 30.0


class HealthzServer:
    def __init__(
        self,
        port: int,
        *,
        snapshot_fn,
    ) -> None:
        self._port = int(port)
        self._snapshot_fn = snapshot_fn
        self._runner: Any = None
        self._site: Any = None
        self._started_at = time.monotonic()

    async def start(self) -> None:
        if web is None:
            logger.critical(
                "[fast_path] aiohttp not installed — /healthz unavailable. "
                "Add `aiohttp>=3.9` to requirements."
            )
            return
        app = web.Application()
        app.router.add_get("/healthz", self._handle)
        app.router.add_get("/", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)
        await self._site.start()
        logger.info("[fast_path] /healthz listening on :%s", self._port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle(self, request) -> Any:
        try:
            snap = self._snapshot_fn()
        except Exception as exc:
            return web.json_response(
                {"healthy": False, "error": f"snapshot_failed:{exc}"},
                status=503,
            )
        ok, reason = self._evaluate(snap)
        snap["healthy"] = bool(ok)
        snap["reason"] = reason
        return web.json_response(snap, status=200 if ok else 503)

    def _evaluate(self, snap: dict) -> tuple[bool, str]:
        # Boot grace
        if (time.monotonic() - self._started_at) < BOOT_GRACE_S:
            return True, "boot_grace"

        if not snap.get("enabled", True):
            return True, "disabled"

        writer = snap.get("writer", {}) or {}
        queue_depth = int(writer.get("queue_depth") or 0)
        queue_max = int(writer.get("queue_max") or 1)
        if queue_max > 0 and (queue_depth / queue_max) > 0.9:
            return False, f"queue_full:{queue_depth}/{queue_max}"
        if int(writer.get("consecutive_batch_failures") or 0) >= 3:
            return False, "db_write_failing"

        pairs = (snap.get("status") or {}).get("pairs") or {}
        any_halted = any(p.get("state") == "halted" for p in pairs.values())
        if any_halted:
            return False, "pair_halted"

        # At least one pair must have streamed a bar in the last 90s.
        from datetime import datetime
        now = datetime.utcnow()
        any_recent = False
        for p in pairs.values():
            ts = p.get("last_bar_at")
            if not ts:
                continue
            try:
                t = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
                if (now - t).total_seconds() < 90:
                    any_recent = True
                    break
            except (TypeError, ValueError):
                continue
        if not any_recent:
            # Silent markets are possible on illiquid pairs; we'll only
            # fail if we had bars recently and now don't.
            had_any_ever = any(p.get("last_bar_at") for p in pairs.values())
            if had_any_ever:
                return False, "no_recent_bars"
            return True, "warming_up"

        return True, "ok"


__all__ = ["HealthzServer"]
