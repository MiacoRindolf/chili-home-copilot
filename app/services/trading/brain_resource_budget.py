"""Per learning-cycle caps for OHLCV fetches, miner row volume, and pattern injects.

Additive: miners consult the budget so one cycle cannot exhaust providers or flood the queue.
Thread-safe for parallel ticker fetches inside a cycle.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BrainResourceBudget:
    ohlcv_cap: int
    miner_rows_cap: int
    pattern_inject_cap: int
    miner_error_trip: int = 5
    ohlcv_used: int = 0
    miner_rows_used: int = 0
    pattern_inject_used: int = 0
    miner_errors: dict[str, int] = field(default_factory=dict)
    circuit_open: set[str] = field(default_factory=set)
    exhausted_log: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_settings(cls) -> BrainResourceBudget:
        from ...config import settings

        return cls(
            ohlcv_cap=max(0, int(getattr(settings, "brain_budget_ohlcv_per_cycle", 200))),
            miner_rows_cap=max(0, int(getattr(settings, "brain_budget_miner_rows_per_cycle", 80000))),
            pattern_inject_cap=max(0, int(getattr(settings, "brain_budget_pattern_injects_per_cycle", 24))),
            miner_error_trip=max(1, int(getattr(settings, "brain_budget_miner_error_trip", 5))),
        )

    def try_ohlcv(self, miner: str, n: int = 1) -> bool:
        """Return True if *n* OHLCV fetches are allowed for this cycle."""
        m = (miner or "unknown").strip() or "unknown"
        with self._lock:
            if m in self.circuit_open:
                return False
            if self.ohlcv_cap <= 0:
                return True
            if self.ohlcv_used + n > self.ohlcv_cap:
                self.exhausted_log.setdefault(
                    "ohlcv",
                    f"cap={self.ohlcv_cap} used={self.ohlcv_used}",
                )
                logger.warning(
                    "[brain.budget] OHLCV cap reached (%s); skipping further fetches for %s",
                    self.exhausted_log["ohlcv"],
                    m,
                )
                return False
            self.ohlcv_used += n
            return True

    def add_miner_rows(self, n: int) -> int:
        """Record up to *n* mined rows; returns how many were accepted (for trimming)."""
        if n <= 0:
            return 0
        with self._lock:
            if self.miner_rows_cap <= 0:
                return n
            room = max(0, self.miner_rows_cap - self.miner_rows_used)
            take = min(n, room)
            self.miner_rows_used += take
            if take < n:
                self.exhausted_log.setdefault(
                    "miner_rows",
                    f"cap={self.miner_rows_cap}",
                )
                logger.warning(
                    "[brain.budget] miner_rows cap: accepted %s of %s rows",
                    take,
                    n,
                )
            return take

    def try_pattern_inject(self) -> bool:
        with self._lock:
            if self.pattern_inject_cap <= 0:
                return True
            if self.pattern_inject_used >= self.pattern_inject_cap:
                self.exhausted_log.setdefault(
                    "pattern_inject",
                    str(self.pattern_inject_cap),
                )
                logger.warning("[brain.budget] pattern inject cap reached")
                return False
            self.pattern_inject_used += 1
            return True

    def record_miner_error(self, miner: str) -> None:
        m = (miner or "unknown").strip() or "unknown"
        with self._lock:
            self.miner_errors[m] = self.miner_errors.get(m, 0) + 1
            if self.miner_errors[m] >= self.miner_error_trip:
                if m not in self.circuit_open:
                    logger.warning(
                        "[brain.budget] circuit open for miner=%s after %s errors",
                        m,
                        self.miner_errors[m],
                    )
                self.circuit_open.add(m)

    def to_report_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ohlcv_cap": self.ohlcv_cap,
                "ohlcv_used": self.ohlcv_used,
                "miner_rows_cap": self.miner_rows_cap,
                "miner_rows_used": self.miner_rows_used,
                "pattern_inject_cap": self.pattern_inject_cap,
                "pattern_inject_used": self.pattern_inject_used,
                "miner_errors": dict(self.miner_errors),
                "circuit_open": sorted(self.circuit_open),
                "exhausted": dict(self.exhausted_log),
            }
