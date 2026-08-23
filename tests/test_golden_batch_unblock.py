"""Golden-batch unblock (2026-08-23) — tatlong depektong pumipigil sa scorecard.

Ang 222-window golden library ang TANGING paraan para patunayan na net-positive
ang isang pagbabago sa trading logic nang hindi naghihintay ng araw-araw na live
session. Naka-block ito dahil sa tatlong magkakahiwalay na bagay:

1. Ang sink mining ay nag-i-import ng buong `app` package, na kumukumpara ng
   Settings() sa import time. Ang `database_url` ay REQUIRED nang walang default
   mula #1024, at ang batch HOST worktree ay walang .env — kaya ValidationError
   sa loob ng blanket except => `mine_error` => coverage_unavailable ang BAWAT
   window, kahit perpektong tumakbo ang replay.
2. Ang advisory lock ay nasa sariling connection na puwedeng TAHIMIK na mamatay,
   na nagpapalaya ng lock nang walang nakakaalam — dalawang run ang nagbura ng
   board ng isa't isa.
3. Ang session row na nabura ng ibang proseso ay tahimik na nagiging
   `final_state=<gone>` na may zero events (cascade), na mukhang lehitimong zero.

Runnable: pytest tests/test_golden_batch_unblock.py -v
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import replay_v3


# ── 1. mine_sink DATABASE_URL bind ─────────────────────────────────────────

def _batch_source() -> str:
    import pathlib

    p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "replay_benchmark_batch.py"
    return p.read_text(encoding="utf-8", errors="replace")


def test_mine_sink_binds_database_url_before_the_app_import():
    src = _batch_source()
    lo = src.index("def mine_sink(")
    seg = src[lo:lo + 6000]
    bind = seg.index('os.environ.setdefault("DATABASE_URL", sink_url)')
    imp = seg.index("from app.services.trading.momentum_neural.setup_trace_audit")
    assert bind < imp, "ang bind ay dapat BAGO ang import na bumubuo ng Settings()"


def test_the_import_time_trap_is_documented():
    src = _batch_source()
    seg = src[src.index("def mine_sink("):]
    assert "coverage_unavailable" in seg
    assert "database_url" in seg.lower()


# ── 2. advisory-lock liveness ──────────────────────────────────────────────

def test_lock_liveness_probe_exists_and_fails_closed():
    src = _batch_source()
    assert "def assert_batch_lock_still_held()" in src
    seg = src[src.index("def assert_batch_lock_still_held()"):]
    seg = seg[:seg.index("print(f\"[batch] {len(specs)} windows queued")]
    # fail-closed sa PAREHONG kaso: patay na connection at nawalang lock
    assert seg.count("raise SystemExit(") == 2
    assert "pg_locks" in seg and "pg_backend_pid()" in seg and "granted" in seg


def test_lock_is_probed_at_every_window_boundary():
    src = _batch_source()
    loop = src[src.index("    try:\n        for w in specs:"):]
    head = loop[:400]
    assert "assert_batch_lock_still_held()" in head, (
        "ang probe ay dapat sa TAAS ng per-window loop, hindi minsanan"
    )


def test_lock_liveness_explains_the_cascade():
    src = _batch_source()
    seg = src[src.index("def assert_batch_lock_still_held()"):][:2200]
    assert "cascade" in seg.lower()
    assert "<gone>" in seg


# ── 3. vanished session row = malakas na pagsabog ──────────────────────────

class _Driver:
    """Pinakamaliit na stand-in ng driver _state() contract."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.seed = SimpleNamespace(session_id=4242, symbol="HUIZ")

    def _session(self):
        return self._rows.pop(0) if self._rows else None

    _state = replay_v3.ReplayV3Driver._state


def test_state_reads_normally_while_the_row_exists():
    d = _Driver([SimpleNamespace(state="watching_live"),
                 SimpleNamespace(state="live_entered")])
    assert d._state() == "watching_live"
    assert d._state() == "live_entered"


def test_missing_row_before_ever_seeing_one_is_not_an_error():
    """Ang seed na hindi kailanman umiral ay ibang depekto — huwag itong ilihis."""
    d = _Driver([])
    assert d._state() == "<gone>"


def test_row_that_vanishes_mid_run_raises_loudly():
    d = _Driver([SimpleNamespace(state="live_entered")])
    assert d._state() == "live_entered"
    with pytest.raises(replay_v3.ReplaySessionRowVanishedError) as exc:
        d._state()
    msg = str(exc.value)
    assert "4242" in msg and "HUIZ" in msg
    assert "another process" in msg.lower()


def test_error_message_names_the_real_cause_not_a_code_bug():
    """Ang buong punto: ang '<gone>' ay HINDI code regression."""
    doc = replay_v3.ReplaySessionRowVanishedError.__doc__ or ""
    assert "advisory-lock" in doc
    assert "cascade" in doc.lower()
    src = inspect.getsource(replay_v3.ReplayV3Driver._state)
    assert "concurrent batch" in src or "another process" in src.lower()
