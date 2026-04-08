"""Pure strings for Trading Brain background runner UI (kept in sync with desk JS).

The worker uses a fixed step count; wording avoids ``x/y`` slash patterns that read
like neural mesh serialization.
"""

from __future__ import annotations


def runner_idle_caption() -> str:
    """Shown when no learning cycle is actively running."""
    return "Runner idle · waiting for the next scheduled cycle"


def runner_phase_primary(steps_completed: int) -> str:
    """High-emphasis segment (no denominator)."""
    return f"Runner phase {int(steps_completed)}"


def runner_phase_denominator_suffix(total_steps: int) -> str:
    """Low-emphasis denominator — worker schedule, not neural graph depth."""
    return f"of {int(total_steps)} worker steps"


def runner_active_secondary_details(
    *,
    tickers_processed: int = 0,
    elapsed_s: float | None = None,
) -> str:
    """Trailing details after the phase line (plain text, joined with middle dot)."""
    parts: list[str] = []
    if tickers_processed > 0:
        parts.append(f"{int(tickers_processed)} scored")
    if elapsed_s is not None and elapsed_s >= 0:
        parts.append(f"{int(round(float(elapsed_s)))}s elapsed")
    return " · ".join(parts)


def runner_active_full_plain(
    steps_completed: int,
    total_steps: int,
    *,
    tickers_processed: int = 0,
    elapsed_s: float | None = None,
) -> str:
    """Single-line plain fallback / screen readers (no HTML)."""
    core = f"{runner_phase_primary(steps_completed)} ({runner_phase_denominator_suffix(total_steps)})"
    tail = runner_active_secondary_details(tickers_processed=tickers_processed, elapsed_s=elapsed_s)
    return core + (" · " + tail if tail else "")


# Back-compat for older tests / imports
def background_cycle_phase_line(
    steps_completed: int,
    total_steps: int,
    *,
    tickers_processed: int = 0,
    elapsed_s: float | None = None,
) -> str:
    """Deprecated name; prefer ``runner_active_full_plain``."""
    return runner_active_full_plain(
        steps_completed,
        total_steps,
        tickers_processed=tickers_processed,
        elapsed_s=elapsed_s,
    )
