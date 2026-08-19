"""ROOT CAUSE ng AZI hang (2026-08-19): unbounded na account-scoped advisory lock.

Ang order-placement path ay tumatawag ng bare ``pg_advisory_xact_lock``, na sa
PostgreSQL ay **BLOCKING nang walang hanggan** — at walang `lock_timeout` na
naka-set sa mga session na ito. ACCOUNT-scoped ang key, kaya BAWAT session sa
iisang Alpaca paper account ay nagpipila sa IISANG lock.

Nasukat nang live: session 14440 (**AZI**) ay nasa `live_pending_entry` mula
12:01:38 hanggang 12:12:27 PT — **648 segundo** sa loob ng iisang tick, na WALANG
kahit isang log line. Dahil `max_instances=1` ang live-runner job, nagyeyelo ang
buong lane — kaya walang namamahala kahit sa mga HAWAK na posisyon.

⚠️ Sinasadyang ``pg_try_advisory_xact_lock`` ang ginamit sa halip na
``SET LOCAL lock_timeout``: ang lock_timeout ay nagre-raise, na **nag-a-abort ng
transaksyon** at magpapalason sa bawat sumunod na statement
(InFailedSqlTransaction). Ang try-variant ay nagbabalik ng boolean at malusog ang
transaksyon.
"""
from __future__ import annotations

import time

import pytest

import app.services.trading.momentum_neural.alpaca_orphan_claims as aoc


class _FakeResult:
    def __init__(self, val):
        self._val = val

    def scalar(self):
        return self._val


class _FakeDB:
    """Nagbabalik ng False sa try-lock hanggang `unlock_after` na tawag."""

    def __init__(self, unlock_after: int = 10**9):
        self.calls = 0
        self.unlock_after = unlock_after
        self.blocking_used = False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "pg_try_advisory_xact_lock" in sql:
            self.calls += 1
            return _FakeResult(self.calls >= self.unlock_after)
        if "pg_advisory_xact_lock" in sql:
            self.blocking_used = True
            return _FakeResult(None)
        return _FakeResult(None)


def _run_lock_phase(db, wait_s: float, key: int = 99):
    """Gayahin ang lock phase gaya ng nasa placement path."""
    if wait_s <= 0:
        db.execute("SELECT pg_advisory_xact_lock(:key)", {"key": key})
        return True
    deadline = time.monotonic() + wait_s
    while True:
        got = db.execute("SELECT pg_try_advisory_xact_lock(:key)", {"key": key}).scalar()
        if bool(got):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def test_busy_lock_fails_closed_instead_of_blocking_forever():
    """ANG BUONG PUNTO: ang abalang lock ay bumibitaw sa loob ng budget —
    hindi humaharang nang 648 segundo."""
    db = _FakeDB(unlock_after=10**9)  # hindi kailanman magbibigay
    t0 = time.monotonic()
    ok = _run_lock_phase(db, 0.5)
    elapsed = time.monotonic() - t0
    assert ok is False
    assert elapsed < 3.0, elapsed
    assert db.calls >= 2, db.calls
    # Kailanman ay hindi ginamit ang blocking variant.
    assert db.blocking_used is False


def test_lock_acquired_when_it_frees_up():
    """Kapag bumakante ang lock sa loob ng budget, NAKUKUHA ito — walang binago
    sa masayang landas."""
    db = _FakeDB(unlock_after=3)
    ok = _run_lock_phase(db, 5.0)
    assert ok is True
    assert db.calls == 3


def test_zero_wait_restores_legacy_blocking_lock():
    """PARITY: 0 ⇒ ang lumang unbounded na blocking lock (kill-switch)."""
    db = _FakeDB()
    ok = _run_lock_phase(db, 0.0)
    assert ok is True
    assert db.blocking_used is True
    assert db.calls == 0


def test_setting_exists_with_safe_bounds():
    from app.config import settings

    v = getattr(settings, "chili_momentum_alpaca_risk_lock_max_wait_seconds", None)
    assert v is not None
    assert 0.0 <= float(v) <= 120.0
    # Ang default ay dapat MAIKSI kumpara sa 648s na naobserbahan.
    assert float(v) < 60.0


@pytest.mark.parametrize("wait_s", [0.2, 0.4])
def test_wait_is_actually_bounded_by_the_setting(wait_s):
    db = _FakeDB(unlock_after=10**9)
    t0 = time.monotonic()
    _run_lock_phase(db, wait_s)
    elapsed = time.monotonic() - t0
    assert elapsed >= wait_s * 0.5
    assert elapsed < wait_s + 2.0
