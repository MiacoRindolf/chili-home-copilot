"""Production driver wrappers admit before creating a caller Session/row lock."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import captured_paper_dispatcher as dispatch
from app.services.trading.momentum_neural import held_market_snapshot as snapshot
from app.services.trading.momentum_neural import live_runner_loop as loop


@pytest.mark.parametrize("driver", ["waker", "event", "batch"])
@pytest.mark.parametrize("factory_fails", [False, True])
def test_actual_driver_admission_precedes_session_creation_and_releases_on_failure(monkeypatch, driver, factory_fails):
    chronology = []
    lease = SimpleNamespace(release=lambda: chronology.append("lease_returned"))

    def admit():
        chronology.append("admitted")
        return lease, None

    monkeypatch.setattr(snapshot, "_MANAGER", SimpleNamespace(try_lease=admit, budget={"fixture": True}))
    db = SimpleNamespace(commit=lambda: chronology.append("caller_commit"),
                         rollback=lambda: chronology.append("caller_rollback"),
                         close=lambda: chronology.append("caller_close"))

    def factory():
        assert snapshot._ADMISSION.get()["lease"] is lease
        chronology.append("session_created")
        if factory_fails:
            raise RuntimeError("caller Session factory failed")
        return db

    def caller(caller_db, session_id):
        assert snapshot._ADMISSION.get()["ordinary_session_id"] == session_id
        assert caller_db is db
        chronology.append("locked_caller_seam")
        return {"ok": True}

    def routed(caller_db, session_id):
        return dispatch._ordinary_tick(caller_db, session_id, non_paper_tick=caller)

    monkeypatch.setattr(dispatch, "dispatch_live_runner_tick", routed)
    monkeypatch.setattr(loop, "dispatch_live_runner_tick", routed)
    monkeypatch.setattr(loop, "SessionLocal", factory)
    if driver == "waker":
        run = lambda: dispatch.run_live_runner_tick_two_phase(factory, 27)
    elif driver == "event":
        run = lambda: loop.LiveRunnerLoop._tick_session(SimpleNamespace(_captured_paper_scope=None), 27)
    else:
        # The batch function is a nested closure. Compile its exact AST with
        # explicit injected dependencies; do not start scheduler jobs/import main.
        path = Path(__file__).parents[1] / "app/services/trading_scheduler.py"
        subject = next(n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                       if isinstance(n, ast.FunctionDef) and n.name == "_tick_one_pass")
        namespace = {"__package__": "app.services", "_held_market_snapshot": snapshot,
                     "SessionLocal": factory, "dispatch_live_runner_tick": routed,
                     "CapturedPaperPostCommitRequest": dispatch.CapturedPaperPostCommitRequest,
                     "logger": logging.getLogger(__name__)}
        exec(compile(ast.Module(body=[subject], type_ignores=[]), str(path), "exec"), namespace)
        run = lambda: namespace["_tick_one_pass"](27)
    if factory_fails:
        with pytest.raises(RuntimeError, match="caller Session factory failed"):
            run()
        assert chronology == ["admitted", "session_created", "lease_returned"]
    else:
        run()
        assert chronology[:3] == ["admitted", "session_created", "locked_caller_seam"]
        assert chronology[-1] == "lease_returned"
        assert chronology.index("caller_close") < chronology.index("lease_returned")
    assert snapshot._ADMISSION.get() is None
