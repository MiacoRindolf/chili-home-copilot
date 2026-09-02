"""Pure cost model ng L1 tape drain: bakit may CEILING ang frontier sa open.

Ang writer ng ``iqfeed_trade_bridge.py`` ay nag-i-issue ng isang bounded batch
(``max_events`` retained events) kada loop, at ang loop ay serial: hindi
nagsisimula ang susunod na drain hangga't hindi tapos ang insert + release ng
nauna. Kaya ang throughput ay ``max_events / batch_seconds``; kapag ang arrival
rate ng tape ay lampas doon, LUMALAKI ang lag nang deterministiko, gaano man
katagal maghintay. Ang mga helper dito ay DB-free at ginagamit ng benchmark at
ng unit tests para mailagay sa numero ang sinukat na 4-6 s / 3,600 events.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DrainCostModel:
    """``batch_seconds = fixed_s + per_event_s * events`` (linear fit sa profile)."""

    fixed_s: float
    per_event_s: float

    def batch_seconds(self, events: int) -> float:
        if events <= 0:
            return 0.0
        return self.fixed_s + self.per_event_s * events

    def steady_state_capacity(self, max_events: int) -> float:
        """Retained events kada segundo na kayang i-commit sa isang serial loop."""
        seconds = self.batch_seconds(max_events)
        if max_events <= 0 or seconds <= 0:
            return 0.0
        return max_events / seconds

    def lag_growth_per_wall_second(self, arrival_events_per_s: float, max_events: int) -> float:
        """Segundo ng tape na nadadagdag sa lag kada segundo ng wall clock (>0 = lumalayo).

        Kapag ``capacity >= arrival`` ay 0 (nakakahabol); kung hindi, ang
        frontier ay umuusad lang nang ``capacity/arrival`` real time.
        """
        capacity = self.steady_state_capacity(max_events)
        if arrival_events_per_s <= 0:
            return 0.0
        if capacity >= arrival_events_per_s:
            return 0.0
        return 1.0 - capacity / arrival_events_per_s


def fit_per_event_cost(samples: list[tuple[int, float]]) -> DrainCostModel:
    """Least-squares fit ng ``(events, seconds)`` samples sa ``fixed + per_event*events``."""
    if not samples:
        raise ValueError("at least one (events, seconds) sample is required")
    if len(samples) == 1:
        events, seconds = samples[0]
        if events <= 0:
            raise ValueError("events must be positive")
        return DrainCostModel(fixed_s=0.0, per_event_s=seconds / events)
    n = float(len(samples))
    mean_x = sum(e for e, _ in samples) / n
    mean_y = sum(s for _, s in samples) / n
    sxx = sum((e - mean_x) ** 2 for e, _ in samples)
    if sxx == 0.0:
        return DrainCostModel(fixed_s=0.0, per_event_s=mean_y / mean_x if mean_x else 0.0)
    sxy = sum((e - mean_x) * (s - mean_y) for e, s in samples)
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    return DrainCostModel(fixed_s=max(0.0, intercept), per_event_s=max(0.0, slope))


def prints_per_batch_under_backlog(max_events: int, trade_frames: int) -> int:
    """Ilang EXACT PRINT ang kasya sa isang retained-event budget kapag bawat
    trade frame ay may MANDATORY na kapares na quote (provenance pairing):
    dalawang retained event kada print, kaya kalahati lang ng budget ang prints."""
    if max_events <= 0 or trade_frames <= 0:
        return 0
    return min(trade_frames, max_events // 2)
