from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5ah-trades-api-cutover-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5ah_trades_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compare_status_requires_exact_payloads(monkeypatch) -> None:
    module = _load_module()

    class _Ts:
        @staticmethod
        def get_trades(_db, _user_id, status=None):
            return [{"id": 1}, {"id": 2}]

    monkeypatch.setattr(module, "ts", _Ts)
    monkeypatch.setattr(
        module,
        "load_trades_api_envelope_objects",
        lambda _db, user_id, status=None, limit=50: [{"id": 2}, {"id": 1}],
    )
    monkeypatch.setattr(
        module,
        "_payload",
        lambda _db, rows, _status: {
            "trades": rows,
            "suppressed_stale_trades": [],
            "suppressed_stale_count": 0,
        },
    )

    check = module._compare_status(object(), user_id=1, status=None)

    assert check["exact_match"] is False
    assert check["accepted"] is False


def test_probe_rejects_non_test_database_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()
    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)

    try:
        module._assert_probe_database_allowed(
            "postgresql://chili:chili@localhost:5433/chili"
        )
    except RuntimeError as exc:
        assert module.LIVE_PROBE_OPT_IN in str(exc)
    else:
        raise AssertionError("expected live database to require explicit opt-in")
