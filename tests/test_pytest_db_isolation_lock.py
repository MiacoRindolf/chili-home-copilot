from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import conftest


def test_db_fixture_holds_advisory_lock_across_test_body() -> None:
    lock_src = inspect.getsource(conftest._pytest_db_isolation_lock)
    fixture_src = inspect.getsource(conftest.db)

    assert "pg_advisory_lock" in lock_src
    assert "pg_advisory_unlock" in lock_src
    assert "NullPool" in lock_src
    assert "with _pytest_db_isolation_lock()" in fixture_src
    assert "yield session" in fixture_src
