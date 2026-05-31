from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import conftest


def test_db_fixture_holds_advisory_lock_across_test_body() -> None:
    lock_src = inspect.getsource(conftest._pytest_db_isolation_lock)
    engine_src = inspect.getsource(conftest._get_pytest_db_lock_engine)
    fixture_src = inspect.getsource(conftest.db)

    assert "pg_try_advisory_lock" in lock_src
    assert "pg_advisory_unlock" in lock_src
    assert "pool_size=1" in engine_src
    assert "max_overflow=0" in engine_src
    assert "with _pytest_db_isolation_lock()" in fixture_src
    assert "yield session" in fixture_src
