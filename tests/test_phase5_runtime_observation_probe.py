from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "d-phase5-runtime-observation-probe.py"


def _load_probe_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase5_runtime_observation_probe", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ok_result(module: ModuleType, name: str, stdout: str = ""):
    return module.CommandResult(name=name, returncode=0, stdout=stdout, stderr="")


def _install_green_probe_stubs(monkeypatch, module: ModuleType) -> None:
    monkeypatch.setattr(
        module,
        "_run_phase5k",
        lambda: _ok_result(module, "phase5k", "VERDICT_STATUS=COMPLETE_POSITIVE\n"),
    )
    monkeypatch.setattr(
        module,
        "_run_phase5i",
        lambda: _ok_result(module, "phase5i", "VERDICT_STATUS=COMPLETE_POSITIVE\n"),
    )
    monkeypatch.setattr(
        module,
        "_run_reader_canary",
        lambda: _ok_result(
            module,
            "reader_canary",
            '{"ok": true, "unexpected_runtime_readers": [], "unexpected_runtime_mutations": []}',
        ),
    )
    monkeypatch.setattr(module, "_docker_logs", lambda _services, _since: _ok_result(module, "logs", ""))


def test_phase5_runtime_observation_probe_stays_in_flight_until_market_window(
    monkeypatch,
    capsys,
) -> None:
    module = _load_probe_module()
    _install_green_probe_stubs(monkeypatch, module)
    monkeypatch.setattr(sys, "argv", ["probe", "--since-minutes", "10"])

    assert module._main() == 0

    out = capsys.readouterr().out
    assert "PHASE5K_STATUS=COMPLETE_POSITIVE" in out
    assert "PHASE5I_STATUS=COMPLETE_POSITIVE" in out
    assert "READER_CANARY_OK=true" in out
    assert "VERDICT_STATUS=IN_FLIGHT" in out
    assert "normal market-window soak" in out


def test_phase5_runtime_observation_probe_can_close_after_declared_market_window(
    monkeypatch,
    capsys,
) -> None:
    module = _load_probe_module()
    _install_green_probe_stubs(monkeypatch, module)
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--since-minutes", "390", "--market-window-complete"],
    )

    assert module._main() == 0

    out = capsys.readouterr().out
    assert "VERDICT_STATUS=COMPLETE_POSITIVE" in out
    assert "mechanical checks green across declared market window" in out


def test_phase5_runtime_observation_probe_flags_phase5_schema_errors(
    monkeypatch,
    capsys,
) -> None:
    module = _load_probe_module()
    _install_green_probe_stubs(monkeypatch, module)

    def fake_logs(services, _since):
        if services == module.APP_SERVICES:
            return _ok_result(
                module,
                "app_logs",
                'ERROR: relation "trading_trades" does not exist\n',
            )
        return _ok_result(module, "postgres_logs", "")

    monkeypatch.setattr(module, "_docker_logs", fake_logs)
    monkeypatch.setattr(sys, "argv", ["probe"])

    assert module._main() == 2

    out = capsys.readouterr().out
    assert "APP_PHASE5_SCHEMA_ERRORS=1" in out
    assert "VERDICT_STATUS=REGRESSION" in out
    assert "app_phase5_schema_errors" in out


def test_phase5_runtime_observation_probe_classifies_version_query_as_noise(
    monkeypatch,
    capsys,
) -> None:
    module = _load_probe_module()
    _install_green_probe_stubs(monkeypatch, module)

    version_noise = (
        'ERROR: column "version" does not exist at character 50\n'
        'HINT: Perhaps you meant to reference the column "schema_version.version_id".\n'
    )

    def fake_logs(services, _since):
        if services == ("postgres",):
            return _ok_result(module, "postgres_logs", version_noise)
        return _ok_result(module, "app_logs", "")

    monkeypatch.setattr(module, "_docker_logs", fake_logs)
    monkeypatch.setattr(sys, "argv", ["probe"])

    assert module._main() == 0

    out = capsys.readouterr().out
    assert "POSTGRES_SCHEMA_VERSION_VERSION_NOISE=2" in out
    assert "POSTGRES_PHASE5_SCHEMA_ERRORS=0" in out
    assert "VERDICT_STATUS=IN_FLIGHT" in out


def test_run_supplies_default_database_url(monkeypatch) -> None:
    module = _load_probe_module()
    captured_env = {}

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(*_args, **kwargs):
        captured_env.update(kwargs["env"])
        return _Proc()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = module._run("demo", [sys.executable, "--version"])

    assert result.returncode == 0
    assert captured_env["DATABASE_URL"] == "postgresql://chili:chili@localhost:5433/chili"
    assert os.environ.get("DATABASE_URL") is None
