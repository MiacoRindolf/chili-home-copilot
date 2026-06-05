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
    miner_rows_rejected: int = 0
    pattern_inject_used: int = 0
    miner_errors: dict[str, int] = field(default_factory=dict)
    circuit_open: set[str] = field(default_factory=set)
    exhausted_log: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_settings(cls) -> BrainResourceBudget:
        from ...config import settings

        # Universe basis: how many tickers a cycle is configured to mine. The caps
        # used to be FIXED at slow-serial-fetch sizes (ohlcv=280, which was 280 x
        # ~8s/fetch = ~38 min/cycle), throttling mining to <30% of the universe.
        # With provider-aware concurrency the full universe fetches fast AND
        # rate-safe (each provider's rate governor bounds load), so the caps now
        # SCALE to cover it. An explicit setting still pins them (operator override).
        _max_tickers = max(1, int(getattr(settings, "brain_mine_patterns_max_tickers", 1000) or 1000))
        _intraday = [
            iv for iv in str(getattr(settings, "brain_intraday_intervals", "") or "").split(",")
            if iv.strip() and iv.strip() != "1d"
        ]
        _interval_sweeps = 1 + len(_intraday)  # 1d + each intraday interval

        # OHLCV fetch count is cheap (rate-governed); cover a full sweep of every
        # interval + headroom for other miners that may share a cycle's budget.
        _ohlcv_override = getattr(settings, "brain_budget_ohlcv_per_cycle", None)
        ohlcv_cap = (
            max(0, int(_ohlcv_override)) if _ohlcv_override is not None
            else _max_tickers * _interval_sweeps + _max_tickers // 2
        )

        # Miner rows are held in RAM (a memory guard, not a rate limit): ~1yr of
        # daily bars (~256/ticker) + intraday headroom, scaled to the universe.
        _rows_override = getattr(settings, "brain_budget_miner_rows_per_cycle", None)
        miner_rows_cap = (
            max(0, int(_rows_override)) if _rows_override is not None
            else _max_tickers * 400
        )

        return cls(
            ohlcv_cap=ohlcv_cap,
            miner_rows_cap=miner_rows_cap,
            pattern_inject_cap=max(0, int(getattr(settings, "brain_budget_pattern_injects_per_cycle", 32) or 32)),
            miner_error_trip=max(1, int(getattr(settings, "brain_budget_miner_error_trip", 5) or 5)),
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

    def remaining_ohlcv(self) -> int | None:
        """Return remaining OHLCV slots, or None when the cap is unlimited."""
        with self._lock:
            if self.ohlcv_cap <= 0:
                return None
            return max(0, self.ohlcv_cap - self.ohlcv_used)

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
                rejected = n - take
                self.miner_rows_rejected += rejected
                if "miner_rows" not in self.exhausted_log:
                    self.exhausted_log["miner_rows"] = (
                        f"cap={self.miner_rows_cap}"
                    )
                    logger.warning(
                        "[brain.budget] miner_rows cap: accepted %s of %s rows",
                        take,
                        n,
                    )
            return take

    def remaining_miner_rows(self) -> int | None:
        """Return remaining mined-row slots, or None when the cap is unlimited."""
        with self._lock:
            if self.miner_rows_cap <= 0:
                return None
            return max(0, self.miner_rows_cap - self.miner_rows_used)

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
                "miner_rows_rejected": self.miner_rows_rejected,
                "miner_rows_remaining": (
                    None
                    if self.miner_rows_cap <= 0
                    else max(0, self.miner_rows_cap - self.miner_rows_used)
                ),
                "pattern_inject_cap": self.pattern_inject_cap,
                "pattern_inject_used": self.pattern_inject_used,
                "miner_errors": dict(self.miner_errors),
                "circuit_open": sorted(self.circuit_open),
                "exhausted": dict(self.exhausted_log),
            }
