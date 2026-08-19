"""Wall-clock budget ng live-runner batch (2026-08-19).

Sinukat sa buhay na window: batch wall p50 10.2s / p90 14.8s — pero **MAX 648s**,
kung saan ang IISANG session (sid=14440/AZI) ay na-block nang 10.8 minuto na
WALANG kahit isang log line: network read na walang deadline, hindi mabagal na
loop. Dahil `max_instances=1` ang job, nagyeyelo ang BUONG runner — walang ibang
session ang natitikwas sa loob ng labing-isang minuto, kasama ang mga HAWAK na
posisyon na ang stop/trail/scale-out ay namamahala LAMANG sa loob ng
``tick_live_session``.
"""
from __future__ import annotations

import time

from app.config import settings
from app.services.trading_scheduler import _dispatch_live_runner_ticks


def test_budget_returns_without_waiting_for_a_hung_session(monkeypatch):
    """Ang na-hang na session ay HINDI dapat pumigil sa pagbabalik ng batch."""
    monkeypatch.setattr(
        settings, "chili_momentum_live_runner_batch_budget_seconds", 1.0,
        raising=False,
    )

    def _tick(sid: int):
        if sid == 999:
            time.sleep(30)  # ang na-hang (walang deadline sa totoong buhay)
            return True, 30000
        return True, 5

    t0 = time.monotonic()
    ticked, timings = _dispatch_live_runner_ticks(
        [1, 2, 3, 999], workers=4, tick_one=_tick
    )
    elapsed = time.monotonic() - t0

    # Bumalik sa loob ng budget, hindi naghintay ng 30s.
    assert elapsed < 10.0, elapsed
    # Ang malulusog na session ay natikwas pa rin.
    assert ticked >= 3, (ticked, timings)
    for sid in (1, 2, 3):
        assert sid in timings
    # Ang na-hang ay wala sa resulta — hindi ito ibinilang na tapos.
    assert 999 not in timings


def test_zero_budget_restores_unbounded_wait(monkeypatch):
    """PARITY: budget=0 ⇒ hinihintay ang lahat (legacy na ugali)."""
    monkeypatch.setattr(
        settings, "chili_momentum_live_runner_batch_budget_seconds", 0.0,
        raising=False,
    )

    def _tick(sid: int):
        time.sleep(0.2)
        return True, 200

    ticked, timings = _dispatch_live_runner_ticks(
        [1, 2, 3], workers=3, tick_one=_tick
    )
    assert ticked == 3
    assert set(timings) == {1, 2, 3}


def test_serial_path_unchanged(monkeypatch):
    """workers<=1 ⇒ seryeng loop, byte-identical (naka-pin ng umiiral na parity)."""
    monkeypatch.setattr(
        settings, "chili_momentum_live_runner_batch_budget_seconds", 1.0,
        raising=False,
    )
    seen = []

    def _tick(sid: int):
        seen.append(sid)
        return True, 1

    ticked, timings = _dispatch_live_runner_ticks(
        [7, 8], workers=1, tick_one=_tick
    )
    assert seen == [7, 8]
    assert ticked == 2


def test_healthy_batch_is_not_truncated(monkeypatch):
    """Ang normal na batch (lahat mas mabilis kaysa budget) ay buo pa rin."""
    monkeypatch.setattr(
        settings, "chili_momentum_live_runner_batch_budget_seconds", 5.0,
        raising=False,
    )

    def _tick(sid: int):
        time.sleep(0.1)
        return True, 100

    ticked, timings = _dispatch_live_runner_ticks(
        list(range(6)), workers=3, tick_one=_tick
    )
    assert ticked == 6
    assert len(timings) == 6
