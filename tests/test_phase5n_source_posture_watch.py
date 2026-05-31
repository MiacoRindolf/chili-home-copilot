from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "d-phase5n-source-posture-watch.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5n_source_posture_watch", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_watch_output_stays_concise_on_green() -> None:
    module = _load_module()
    probe = subprocess.CompletedProcess(
        args=["python", "probe.py"],
        returncode=0,
        stdout="VERDICT_STATUS=COMPLETE_POSITIVE\nUSING_DIRTY_ROOT=false\n",
        stderr="",
    )

    verdict, output = module.build_watch_output(probe)

    assert verdict == "COMPLETE_POSITIVE"
    assert "Operator Guidance" not in output
    assert "USING_DIRTY_ROOT=false" in output


def test_watch_output_alerts_with_repair_guidance_on_drift() -> None:
    module = _load_module()
    probe = subprocess.CompletedProcess(
        args=["python", "probe.py"],
        returncode=2,
        stdout="VERDICT_STATUS=ALERT\nUSING_DIRTY_ROOT=true\n",
        stderr="",
    )

    verdict, output = module.build_watch_output(probe)

    assert verdict == "ALERT"
    assert "Operator Guidance" in output
    assert "PHASE5M_DEPLOYMENT_SOURCE_POSTURE.md" in output
    assert "d-phase5k-live-path-parity-probe.py" in output
    assert "do not restart Postgres" in output

