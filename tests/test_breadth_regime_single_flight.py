"""BREADTH-REGIME STAMPEDE — ang bridge starvation ng 2026-08-20 umaga.

Ang uncached compute ay umabot ng 100-180s sa ilalim ng market-hours load — mas
mahaba kaysa sa 60s TTL — kaya bawat caller pagkatapos mag-expire ay nagsimula ng
SARILING 45-araw na scan ng 31M-row/11GB `momentum_viability_history`, lahat sa
ilalim ng process-wide ignition→arm bridge lock: **6 arms vs 107 kahapon.**

Tatlong depekto, tatlong lunas:
  1. stampede      -> single-flight: isang thread lang ang nagre-recompute
  2. TTL < runtime -> ang matatalo ay nagsisilbi ng STALE (per-hour lang magbago)
  3. floor 45d > laman 43d -> 30d (sagana sa 20 trailing session)
"""
from __future__ import annotations

import threading
import time

from app.services.trading.momentum_neural import breadth_regime as br


class _SlowDB:
    """Session stub na nagbibilang ng compute at nagpapabagal para mag-overlap."""

    def __init__(self, delay=0.3):
        self.calls = 0
        self.delay = delay

    def execute(self, *_a, **_k):
        self.calls += 1
        time.sleep(self.delay)

        class _R:
            def fetchall(self):
                return []

        return _R()


def _fresh_memo(monkeypatch):
    monkeypatch.setattr(br, "_REGIME_MEMO", {"key": None, "value": None})
    monkeypatch.setattr(br, "_REGIME_RECOMPUTE_LOCK", threading.Lock())


def test_losers_serve_neutral_instead_of_stampeding(monkeypatch):
    """Habang may recompute na tumatakbo, ang ibang caller ay HINDI nagsisimula
    ng sarili nilang scan — _NEUTRAL agad (walang unang value pa)."""
    monkeypatch.delenv("CHILI_PYTEST", raising=False)
    monkeypatch.delenv("GOLDEN", raising=False)
    _fresh_memo(monkeypatch)
    db = _SlowDB(delay=0.5)
    results = []

    def _call():
        results.append(br.compute_breadth_regime(db))

    threads = [threading.Thread(target=_call) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # ISANG compute lang ang tumakbo (unang execute call ay sa breadth query);
    # kung nag-stampede, >= 2 thread ang tatawag sa execute.
    assert db.calls <= 2, f"stampede: {db.calls} DB calls"
    assert len(results) == 6
    # Ang mga natalo ay nakatanggap ng fail-closed _NEUTRAL, hindi exception.
    assert all(r is not None for r in results)


def test_stale_value_is_served_while_recomputing(monkeypatch):
    """Kapag MAY lumang value, iyon ang inihahain sa matatalo — hindi _NEUTRAL,
    hindi bagong scan."""
    monkeypatch.delenv("CHILI_PYTEST", raising=False)
    monkeypatch.delenv("GOLDEN", raising=False)
    _fresh_memo(monkeypatch)
    stale = br.BreadthRegime(True, "YJ", 3, 1.0, 2.0, False, "wildcard")
    br._REGIME_MEMO["value"] = stale
    br._REGIME_MEMO["computed_at_monotonic"] = time.monotonic() - 120.0  # expired
    db = _SlowDB(delay=0.5)
    got = []

    holder = threading.Thread(target=lambda: got.append(br.compute_breadth_regime(db)))
    holder.start()
    time.sleep(0.1)  # tiyaking hawak na ng holder ang lock
    loser = br.compute_breadth_regime(db)
    holder.join()
    assert loser is stale, "ang natalo ay dapat maghain ng stale, hindi mag-scan"


def test_fresh_memo_still_serves_without_lock(monkeypatch):
    """Ang sariwang memo (< 60s) ay hinahain nang WALANG lock o DB call."""
    monkeypatch.delenv("CHILI_PYTEST", raising=False)
    monkeypatch.delenv("GOLDEN", raising=False)
    _fresh_memo(monkeypatch)
    fresh = br.BreadthRegime(False, None, 9, 0.1, 0.0, False, "broad")
    br._REGIME_MEMO["value"] = fresh
    br._REGIME_MEMO["computed_at_monotonic"] = time.monotonic()
    db = _SlowDB()
    assert br.compute_breadth_regime(db) is fresh
    assert db.calls == 0


def test_hist_floor_is_bounded_to_thirty_days():
    """Ang 45-araw na sahig ay nagbabasa ng halos buong 11GB table (43 araw ang
    laman). Ang source ay dapat naka-30d na."""
    import inspect

    src = inspect.getsource(br._compute_breadth_regime_uncached)
    assert "timedelta(days=30)" in src
    assert "timedelta(days=45)" not in src
