"""Background runner UI copy: orchestration wording (not neural mesh sequencing)."""

from __future__ import annotations

from app.services.trading.brain_runner_display import (
    background_cycle_phase_line,
    runner_active_full_plain,
    runner_active_secondary_details,
    runner_idle_caption,
    runner_phase_denominator_suffix,
    runner_phase_primary,
)


def test_runner_phase_primary_no_slash_denominator() -> None:
    s = runner_phase_primary(4)
    assert s == "Runner phase 4"
    assert "/" not in s
    assert "24" not in s


def test_runner_phase_denominator_suffix_low_emphasis() -> None:
    assert runner_phase_denominator_suffix(24) == "of 24 worker steps"
    assert "/" not in runner_phase_denominator_suffix(24)


def test_runner_idle_caption() -> None:
    s = runner_idle_caption()
    assert "idle" in s.lower() or "waiting" in s.lower()
    assert "/" not in s


def test_runner_active_full_plain_truthful_no_x_slash_y() -> None:
    """Plain fallback keeps phase + total without ``4/24`` pipeline idiom."""
    s = runner_active_full_plain(4, 24)
    assert "Runner phase 4" in s
    assert "of 24 worker steps" in s
    assert "4/24" not in s


def test_runner_active_full_plain_with_extras() -> None:
    s = runner_active_full_plain(4, 24, tickers_processed=30, elapsed_s=12.4)
    assert "of 24 worker steps" in s
    assert "30 scored" in s
    assert "12s elapsed" in s
    assert "4/24" not in s


def test_runner_active_secondary_details_only_tail() -> None:
    assert runner_active_secondary_details() == ""
    assert "scored" in runner_active_secondary_details(tickers_processed=1)
    assert "s elapsed" in runner_active_secondary_details(elapsed_s=3.2)


def test_background_cycle_phase_line_alias_matches_full_plain() -> None:
    assert background_cycle_phase_line(4, 24) == runner_active_full_plain(4, 24)
    assert background_cycle_phase_line(4, 24, tickers_processed=5, elapsed_s=9.0) == runner_active_full_plain(
        4, 24, tickers_processed=5, elapsed_s=9.0
    )
