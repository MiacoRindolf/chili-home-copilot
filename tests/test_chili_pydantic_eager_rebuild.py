"""Tests for f-leak-4 phase 2 (eager pydantic model_rebuild on startup).

Pre-fix: chili main app leaked +63 MB/min (3.7 GB/hr). Top retained
closure was pydantic's per-request deferred-validation rebuild path
(set_model_mocks.<locals>.attempt_rebuild_fn.<locals>.handler, count
1488). Fix is to call Model.model_rebuild() once at startup so the
per-request rebuild path doesn't fire during request handling.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Ang TUNAY na hangganan ng function, hindi isang nakapirming bilang ng
# character. Ang lumang `src[idx:idx+6200]` ay sumasaklaw lamang ng 5.3%
# ng `start_scheduler` noong 2026-08-25 at tumigil na sa pagbabantay.
from tests.source_region import function_body


def test_eager_rebuild_function_exists():
    from app.main import _eager_pydantic_model_rebuild
    assert callable(_eager_pydantic_model_rebuild)


def test_eager_rebuild_runs_on_real_schemas():
    """Run the rebuild against the actual app.schemas / app.models
    packages. Must complete without raising and rebuild a non-trivial
    number of models (we expect 50+ given the 14+ schema modules)."""
    from app.main import _eager_pydantic_model_rebuild
    n = _eager_pydantic_model_rebuild()
    assert n >= 30, (
        f"expected to rebuild at least 30 models from app.schemas + "
        f"app.models, got {n}"
    )


def test_lifespan_calls_eager_rebuild():
    """Source-text guard: the lifespan context manager must call the
    eager rebuild. Pin it so a future refactor can't silently remove
    the wiring."""
    body = function_body(REPO / "app/main.py", "lifespan")
    assert "_eager_pydantic_model_rebuild()" in body, (
        "lifespan must call _eager_pydantic_model_rebuild() at startup"
    )


def test_eager_rebuild_swallows_per_model_failures():
    """Source guard: per-model rebuild errors must be swallowed
    (continue) so a single broken model can't break startup. Pin the
    try/except inside the model loop."""
    body = function_body(REPO / "app/main.py", "_eager_pydantic_model_rebuild")
    # The core requirement: model_rebuild() inside a try/except continue.
    assert "obj.model_rebuild()" in body
    assert "continue" in body, (
        "per-model failures must continue, not break the loop"
    )


def test_lifespan_swallows_eager_rebuild_failure():
    """Source guard: even if _eager_pydantic_model_rebuild itself
    raises, startup must not abort. Pin the outer try/except."""
    body = function_body(REPO / "app/main.py", "lifespan")
    rebuild_pos = body.find("_eager_pydantic_model_rebuild()")
    assert rebuild_pos > 0
    # Look at the surrounding 200 chars before the call.
    surrounding = body[max(0, rebuild_pos - 200):rebuild_pos]
    assert "try:" in surrounding, (
        "_eager_pydantic_model_rebuild() call must be inside a try: "
        "block in lifespan"
    )
