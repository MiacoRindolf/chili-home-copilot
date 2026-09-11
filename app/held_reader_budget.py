"""Pure resource accounting for ordinary PAPER held-market readers.

No imports of settings, DB engines or trading packages: app.db calls this before
constructing its caller pool. These are existing operational capacities, never
market thresholds or permission to change the configured execution owner.
"""
from dataclasses import asdict, dataclass
from typing import Any


# The event loop already used max_workers=3. Keep its consumer and accounting
# on one definition; this does not change event concurrency or tick spacing.
LIVE_LOOP_TICK_WORKERS = 3
MOMENTUM_EXEC_ROLES = frozenset({"all", "web", "worker", "cron_only", "momentum_exec_only"})


@dataclass(frozen=True)
class HeldReaderBudget:
    retained: int
    original_overflow: int
    caller_overflow: int
    reader_capacity: int
    driver_capacity: int
    resident_caller_slots: int
    reason: str
    process_role: str

    def receipt(self) -> dict[str, Any]:
        finite_budget = _integer(self.retained, minimum=1) and _integer(self.original_overflow, minimum=0)
        values = asdict(self)
        if not finite_budget:
            # Invalid raw configuration is not a JSON nonfinite value or a
            # fabricated usable budget. The caller still receives it unchanged.
            for name in ("retained", "original_overflow", "caller_overflow"):
                values[name] = None
        return {
            **values,
            "budget_scope": "partitioned_sqlalchemy_peak_excludes_direct_libpq_sockets",
            "required_usable_caller_slots": 2,
            "original_peak": self.retained + self.original_overflow if finite_budget else None,
            "caller_peak": self.retained + self.caller_overflow if finite_budget else None,
            "derivation": "min(driver_capacity,overflow,max(0,peak-2-resident_caller_slots))",
            "capacity_is_not_a_guarantee_of_free_slots_or_all_driver_concurrency": True,
        }


def _integer(value: Any, *, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def resolve_held_reader_budget(
    settings: Any, *, pool_size: int, max_overflow: int,
    pytest_process: bool = False, mp_child: bool = False,
) -> HeldReaderBudget:
    """Reserve only finite overflow, preserving known resident and fence needs.

    The caller supplies values AFTER its existing service/pytest caps. Unknown
    or unbounded pools cannot prove a partition. Low budgets retain the original
    caller capacity and name their limitation instead of consuming its last slot.
    """
    role = str(getattr(settings, "chili_scheduler_role", "none") or "none").strip().lower()
    width, resident = 0, 0

    def result(reason: str, capacity: int = 0) -> HeldReaderBudget:
        return HeldReaderBudget(pool_size, max_overflow, max_overflow - capacity if capacity else max_overflow,
                                capacity, width, resident, reason, role)

    if not _integer(pool_size, minimum=1) or not _integer(max_overflow, minimum=0):
        return result("reader_pool_budget_invalid_or_unbounded")
    if pytest_process:
        return result("reader_pytest_default_not_reserved")
    if mp_child:
        return result("reader_multiprocess_child_not_reserved")
    if role not in MOMENTUM_EXEC_ROLES:
        return result("reader_process_role_not_ordinary_driver")
    if (getattr(settings, "chili_alpaca_paper", None) is not True
            or getattr(settings, "chili_momentum_legacy_alpaca_dispatch_enabled", None) is not True):
        return result("reader_ordinary_paper_posture_not_active")
    if getattr(settings, "chili_momentum_live_runner_enabled", None) is not True:
        return result("reader_driver_not_enabled")
    event = getattr(settings, "chili_momentum_live_runner_loop_enabled", None) is True
    batch = getattr(settings, "chili_momentum_live_runner_scheduler_enabled", None) is True
    if event:
        # live_runner_loop's process-generation advisory fence retains ONE
        # caller-pool connection, even while its transaction is closed.
        resident = 1
        width = LIVE_LOOP_TICK_WORKERS
    if batch:
        override = getattr(settings, "chili_momentum_live_runner_batch_workers", 0)
        concurrency = getattr(settings, "chili_momentum_risk_max_concurrent_live_sessions", 5)
        if not _integer(override, minimum=0) or not _integer(concurrency, minimum=1):
            return result("reader_driver_capacity_invalid")
        width = max(width, override or concurrency)
    if width == 0:
        return result("reader_driver_not_enabled")
    peak = pool_size + max_overflow
    if peak < 2 + resident:
        return result("reader_existing_caller_headroom_insufficient")
    capacity = min(width, max_overflow, max(0, peak - 2 - resident))
    return result("reader_budget_partitioned" if capacity else "reader_budget_unavailable", capacity)
