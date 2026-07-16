"""Regression tests for the parallel momentum live-runner batch.

The live-runner batch ticks each open live session with per-session network I/O
(Coinbase quote/product + OHLCV entry-trigger fetch). It used to run those ticks
SERIALLY, so a batch took the sum of every session's latency and overran the 30s
APScheduler cadence once ~5 live sessions were open. The batch now fans the ticks
out across a small bounded pool (``_dispatch_live_runner_ticks``).

These tests pin the *scheduling/concurrency* contract ONLY — they assert that
every session is still ticked exactly once, that the parallel path is a behavioural
no-op versus the legacy serial loop, that one bad session can't abort the batch,
that each tick gets its OWN DB Session, and that the worker cap derives from the
live concurrency cap. They deliberately do NOT exercise any entry/exit/risk logic
(that lives in ``tick_live_session`` and is unchanged).
"""

from __future__ import annotations

import threading

from app.services.trading_scheduler import _dispatch_live_runner_ticks


# ── Pure dispatcher contract ────────────────────────────────────────────


def test_dispatch_ticks_every_session_exactly_once_parallel():
    """Parallel fan-out must tick each session exactly once — no drops, no dupes."""
    seen: list[int] = []
    lock = threading.Lock()

    def tick_one(sid: int) -> tuple[bool, int]:
        with lock:
            seen.append(sid)
        return True, 10

    ids = [11, 22, 33, 44, 55]
    ticked, timings = _dispatch_live_runner_ticks(ids, workers=5, tick_one=tick_one)

    assert ticked == 5
    assert sorted(seen) == ids  # each exactly once
    assert set(timings.keys()) == set(ids)
    assert all(ms == 10 for ms in timings.values())


def test_dispatch_serial_parallel_parity():
    """Same input → same (ticked count, set of sids) whether serial or parallel.

    This is the core regression guard: parallelism must not change WHICH sessions
    advanced or how many succeeded — only the wall-clock to do so.
    """

    def make_tick():
        seen: list[int] = []
        lock = threading.Lock()

        def tick_one(sid: int) -> tuple[bool, int]:
            with lock:
                seen.append(sid)
            return (sid % 2 == 0), sid  # even sids "succeed"

        return tick_one, seen

    ids = [1, 2, 3, 4, 6]
    t_serial, seen_serial = make_tick()
    serial = _dispatch_live_runner_ticks(ids, workers=1, tick_one=t_serial)
    t_par, seen_par = make_tick()
    parallel = _dispatch_live_runner_ticks(ids, workers=5, tick_one=t_par)

    # 2, 4, 6 succeed → ticked == 3 in both modes.
    assert serial[0] == parallel[0] == 3
    assert set(serial[1].keys()) == set(parallel[1].keys()) == set(ids)
    assert sorted(seen_serial) == sorted(seen_par) == sorted(ids)


def test_dispatch_isolates_one_failing_session():
    """A raising tick must be contained — the other sessions still advance."""

    def tick_one(sid: int) -> tuple[bool, int]:
        if sid == 3:
            raise RuntimeError("boom")  # simulate an unexpected escape
        return True, 5

    ids = [1, 2, 3, 4, 5]
    ticked, timings = _dispatch_live_runner_ticks(ids, workers=5, tick_one=tick_one)

    assert ticked == 4  # all but sid=3
    assert set(timings.keys()) == set(ids)  # every session still recorded
    assert timings[3] == 0  # failed session → 0ms sentinel


def test_dispatch_serial_path_runs_on_calling_thread():
    """workers<=1 must use the in-line serial loop (no thread pool spun up)."""
    main_ident = threading.get_ident()
    idents: list[int] = []

    def tick_one(sid: int) -> tuple[bool, int]:
        idents.append(threading.get_ident())
        return True, 1

    _dispatch_live_runner_ticks([1, 2, 3], workers=1, tick_one=tick_one)

    assert idents and all(i == main_ident for i in idents)


def test_dispatch_single_session_never_spawns_pool():
    """A lone runnable session takes the serial path even with a high worker cap."""
    main_ident = threading.get_ident()
    idents: list[int] = []

    def tick_one(sid: int) -> tuple[bool, int]:
        idents.append(threading.get_ident())
        return True, 1

    ticked, timings = _dispatch_live_runner_ticks([7], workers=8, tick_one=tick_one)

    assert ticked == 1
    assert idents == [main_ident]


def test_dispatch_actually_runs_concurrently():
    """Prove the pool runs ticks simultaneously, not just round-robin serially.

    Each tick blocks on a Barrier requiring all `workers` threads to arrive. A
    serial loop could never get a second thread to the barrier, so it would time
    out (BrokenBarrierError) and peak concurrency would be 1. True concurrency
    releases the barrier and drives peak to `workers`.
    """
    workers = 3
    barrier = threading.Barrier(workers, timeout=5)
    lock = threading.Lock()
    inflight = {"n": 0}
    peak = {"n": 0}

    def tick_one(sid: int) -> tuple[bool, int]:
        with lock:
            inflight["n"] += 1
            peak["n"] = max(peak["n"], inflight["n"])
        try:
            barrier.wait()
        except threading.BrokenBarrierError:  # pragma: no cover - only if serial
            pass
        with lock:
            inflight["n"] -= 1
        return True, 1

    ticked, _ = _dispatch_live_runner_ticks([1, 2, 3], workers=workers, tick_one=tick_one)

    assert ticked == 3
    assert peak["n"] == workers  # all three ran at the same time


# ── Job wiring: own session per tick + worker-cap derivation ─────────────


class _FakeSession:
    def __init__(self, name: str) -> None:
        self.name = name
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def _wire_batch(
    monkeypatch,
    *,
    n_sessions: int,
    batch_workers: int,
    max_concurrent: int,
    tick_result=None,
    post_commit_handler=None,
):
    """Drive _run_momentum_live_runner_batch_job with fakes; return captured state."""
    import app.db as app_db
    from app.config import settings
    from app.services import trading_scheduler
    from app.services.trading.momentum_neural import live_runner
    from app.services.trading.momentum_neural import captured_paper_dispatcher

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_batch_workers", batch_workers, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_risk_max_concurrent_live_sessions", max_concurrent, raising=False
    )

    created: list[_FakeSession] = []
    created_lock = threading.Lock()

    def _session_factory():
        with created_lock:
            s = _FakeSession(f"s{len(created)}")
            created.append(s)
        return s

    sess_objs = [type("_S", (), {"id": 100 + i})() for i in range(n_sessions)]
    monkeypatch.setattr(live_runner, "list_runnable_live_sessions", lambda db, **_: sess_objs)

    ticks: list[tuple[str, int]] = []
    ticks_lock = threading.Lock()

    def _fake_tick(db, sid, **_):
        with ticks_lock:
            ticks.append((db.name, int(sid)))
        return {"ok": True} if tick_result is None else tick_result

    monkeypatch.setattr(captured_paper_dispatcher, "dispatch_live_runner_tick", _fake_tick)
    post_commit_calls = []
    post_commit_commit_counts = []

    def _fake_post_commit(request):
        post_commit_calls.append(request)
        post_commit_commit_counts.append(tuple(s.commits for s in created))
        if post_commit_handler is not None:
            return post_commit_handler(request)
        return None

    monkeypatch.setattr(
        captured_paper_dispatcher,
        "dispatch_captured_paper_post_commit",
        _fake_post_commit,
    )

    def _bare_tick_is_forbidden(*_args, **_kwargs):
        raise AssertionError("scheduler bypassed captured-paper dispatcher")

    monkeypatch.setattr(live_runner, "tick_live_session", _bare_tick_is_forbidden)

    captured: dict[str, object] = {}
    real_dispatch = trading_scheduler._dispatch_live_runner_ticks

    def _spy_dispatch(session_ids, *, workers, tick_one):
        captured["workers"] = workers
        captured["ids"] = list(session_ids)
        result = real_dispatch(session_ids, workers=workers, tick_one=tick_one)
        captured["dispatch_result"] = result
        return result

    monkeypatch.setattr(trading_scheduler, "_dispatch_live_runner_ticks", _spy_dispatch)
    monkeypatch.setattr(app_db, "SessionLocal", _session_factory)
    monkeypatch.setattr(trading_scheduler, "run_scheduler_job_guarded", lambda _id, fn: fn())

    trading_scheduler._run_momentum_live_runner_batch_job()

    captured["post_commit_calls"] = post_commit_calls
    captured["post_commit_commit_counts"] = post_commit_commit_counts
    return created, ticks, captured


def test_batch_gives_each_tick_its_own_session_and_derives_worker_cap(monkeypatch):
    """3 sessions, default knob (0) → workers derive from max_concurrent (=5)→min=3.

    Each tick must run on its OWN DB Session and commit + close it; the single
    listing session is separate. This pins the 'never share a Session across
    threads' rule that makes the parallel path safe.
    """
    created, ticks, captured = _wire_batch(
        monkeypatch, n_sessions=3, batch_workers=0, max_concurrent=5
    )

    # workers derived from the concurrency cap, never more than #sessions.
    assert captured["workers"] == 3
    assert sorted(captured["ids"]) == [100, 101, 102]

    # 1 listing session + 3 per-tick sessions, all distinct, all closed.
    assert len(created) == 1 + 3
    assert len({s.name for s in created}) == 4
    assert all(s.closed for s in created)

    # Each session id ticked exactly once, each on a DISTINCT (own) DB session.
    assert sorted(sid for _, sid in ticks) == [100, 101, 102]
    tick_session_names = [name for name, _ in ticks]
    assert len(set(tick_session_names)) == 3  # own session per tick

    # Per-tick sessions committed once (success) and were rolled back in finally.
    per_tick = [s for s in created if s.name in set(tick_session_names)]
    assert all(s.commits == 1 and s.rollbacks >= 1 for s in per_tick)


def test_batch_respects_explicit_worker_override(monkeypatch):
    """An explicit batch_workers > 0 wins over the derived cap (still ≤ #sessions)."""
    _, ticks, captured = _wire_batch(
        monkeypatch, n_sessions=5, batch_workers=2, max_concurrent=5
    )
    assert captured["workers"] == 2  # override, not the derived 5
    assert sorted(sid for _, sid in ticks) == [100, 101, 102, 103, 104]


def test_batch_single_session_uses_one_worker(monkeypatch):
    """One runnable session → workers clamped to 1 (serial path)."""
    _, ticks, captured = _wire_batch(
        monkeypatch, n_sessions=1, batch_workers=0, max_concurrent=5
    )
    assert captured["workers"] == 1
    assert [sid for _, sid in ticks] == [100]


def _captured_completion_request():
    from datetime import datetime, timezone

    from app.services.trading.momentum_neural import (
        captured_paper_entry_intent as contract,
    )

    route = contract.CapturedPaperRouteToken(
        session_id=100,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id="d7cc580c-2b8f-432f-b771-1cecfb3fe87a",
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation="f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3",
        first_dip_policy_mode="candidate",
    )
    intent = contract.CapturedPaperEntryIntent(
        route_token=route,
        intent_generation="39f55a65-e6f2-4ccc-bd02-f50dc9c27c69",
        decision_id="captured-paper-decision-100",
        client_order_id="chili_ml_ACTU_100_1",
        setup_family="first_dip_reclaim",
        decision_at=datetime(2026, 7, 15, 16, 30, tzinfo=timezone.utc),
        structural_stop_price="2.50",
        entry_limit_ceiling_price="3.00",
        account_receipt_sha256="d" * 64,
        bbo_receipt_sha256="e" * 64,
        setup_evidence_sha256="f" * 64,
        policy_sha256="1" * 64,
        feature_flags_sha256="2" * 64,
    )
    return contract.CapturedPaperPostCommitRequest(
        intent=intent,
        completion_generation="73dbcf92-94ea-436e-978c-b0e31ce7252d",
    )


def test_batch_commits_phase_one_before_exact_post_commit_completion(monkeypatch):
    completion = _captured_completion_request()
    state = {}

    def complete(request):
        state["request"] = request

    created, _ticks, captured = _wire_batch(
        monkeypatch,
        n_sessions=1,
        batch_workers=1,
        max_concurrent=1,
        tick_result=completion,
        post_commit_handler=complete,
    )

    assert captured["post_commit_calls"] == [completion]
    # The listing session is first and the tick-owned session is second.  The
    # completion dispatcher observed the latter only after its commit.
    assert captured["post_commit_commit_counts"] == [(0, 1)]
    assert state["request"] is completion
    per_tick = created[-1]
    assert per_tick.commits == 1
    assert per_tick.rollbacks == 0
    assert captured["dispatch_result"][0] == 1


def test_batch_completion_failure_marks_tick_failed_without_phase_one_rollback(
    monkeypatch,
):
    completion = _captured_completion_request()

    def fail_completion(_request):
        raise RuntimeError("retry completion")

    created, _ticks, captured = _wire_batch(
        monkeypatch,
        n_sessions=1,
        batch_workers=1,
        max_concurrent=1,
        tick_result=completion,
        post_commit_handler=fail_completion,
    )

    assert captured["post_commit_calls"] == [completion]
    per_tick = created[-1]
    assert per_tick.commits == 1
    assert per_tick.rollbacks == 0
    assert captured["dispatch_result"][0] == 0
