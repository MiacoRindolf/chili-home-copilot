"""Execute the fixed fake-transport captured-PAPER lifecycle scenario shard."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Sequence
import xml.etree.ElementTree as ET


UTC = timezone.utc
SCENARIO_NODES = {
    "ownership_idempotency": (
        "tests/test_captured_paper_outbox.py::"
        "test_persist_is_atomic_content_addressed_idempotent_and_side_effect_free"
    ),
    "indeterminate_submit_retain": (
        "tests/test_captured_paper_outbox.py::"
        "test_transport_indeterminate_is_reconciliation_only_and_never_terminalized"
    ),
    "late_fill_quarantine": (
        "tests/test_alpaca_fill_activity_capture.py::"
        "test_late_fill_after_release_is_durably_quarantined_and_idempotent"
    ),
    "append_only_fill_settlement": (
        "tests/test_captured_paper_outbox.py::"
        "test_positive_fill_handoff_is_terminal_idempotent_and_never_reposted"
    ),
    "same_cid_reconciliation": (
        "tests/test_captured_paper_transport_coordinator.py::"
        "test_positive_same_cid_reconciliation_completes_with_one_lifetime_post"
    ),
    "no_blind_repost": (
        "tests/test_captured_paper_transport_coordinator.py::"
        "test_timeout_then_restart_and_cid_absence_never_reposts_or_terminalizes"
    ),
}
SCENARIO_EVENTS = {
    "ownership_idempotency": ["claim_acquired", "duplicate_claim_refused"],
    "indeterminate_submit_retain": ["submit_indeterminate", "resources_retained"],
    "late_fill_quarantine": ["late_fill_observed", "exposure_quarantined"],
    "append_only_fill_settlement": ["fill_appended", "settlement_appended"],
    "same_cid_reconciliation": ["same_cid_lookup", "same_cid_reconciled"],
    "no_blind_repost": ["indeterminate_observed", "reconciliation_only"],
}
EXPECTED_FAKE_TRANSPORT_CALL_COUNT = 2


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _publish_new(path: Path, document: dict[str, Any]) -> None:
    raw = _canonical(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def run_lifecycle_preflight(
    *,
    candidate_root: Path,
    output_path: Path,
    python_executable: Path = Path(sys.executable),
    environment: dict[str, str] | None = None,
    command_runner: Any = subprocess.run,
    wall_clock: Any = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    if not candidate_root.is_absolute() or not candidate_root.is_dir():
        raise RuntimeError("candidate root is unavailable")
    if not output_path.is_absolute() or output_path.exists():
        raise RuntimeError("lifecycle output path is not fresh and absolute")
    with tempfile.TemporaryDirectory(
        prefix="captured-paper-lifecycle-", dir=str(output_path.parent)
    ) as temp_raw:
        temp = Path(temp_raw)
        junit = temp / "junit.xml"
        side_effects = temp / "side-effects.json"
        env = dict(os.environ if environment is None else environment)
        env["CHILI_CAPTURED_PAPER_SIDE_EFFECT_REPORT"] = str(side_effects)
        env["CHILI_PYTEST"] = "1"
        command = (
            str(python_executable),
            "-B",
            "-m",
            "pytest",
            "-q",
            *SCENARIO_NODES.values(),
            "-p",
            "scripts.captured_paper_pytest_side_effect_guard",
            f"--junitxml={junit}",
        )
        result = command_runner(
            command,
            cwd=str(candidate_root),
            env=env,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not junit.is_file() or not side_effects.is_file():
            raise RuntimeError("fixed lifecycle pytest shard failed closed")
        root = ET.fromstring(junit.read_bytes())
        testcases = list(root.iter("testcase"))
        expected_names = {
            node.rsplit("::", 1)[1] for node in SCENARIO_NODES.values()
        }
        if len(testcases) != len(SCENARIO_NODES) or any(
            list(case.findall("failure"))
            or list(case.findall("error"))
            or list(case.findall("skipped"))
            for case in testcases
        ) or {str(case.attrib.get("name") or "") for case in testcases} != expected_names:
            raise RuntimeError("fixed lifecycle scenario coverage is incomplete")
        side_effect_report = json.loads(side_effects.read_text(encoding="utf-8"))
        body = dict(side_effect_report)
        claimed = body.pop("report_sha256", None)
        if (
            side_effect_report.get("schema_version")
            != "chili.captured-paper-pytest-side-effect-census.v1"
            or hashlib.sha256(_canonical(body)).hexdigest() != claimed
        ):
            raise RuntimeError("lifecycle side-effect census is malformed")
        counts = {
            row["event_type"]: row["count"]
            for row in side_effect_report.get("events", [])
        }
        if any(counts.get(name) != 0 for name in ("real_network", "live_cash", "broker_post")):
            raise RuntimeError("lifecycle shard attempted a forbidden side effect")
        fake_transport_calls = counts.get("fake_transport")
        if fake_transport_calls != EXPECTED_FAKE_TRANSPORT_CALL_COUNT:
            raise RuntimeError(
                "lifecycle fake transport call census is not exact"
            )
        completed_at = wall_clock()
        report = {
            "schema_version": "chili.captured-paper-lifecycle-preflight.v1",
            "scenarios": [
                {"name": name, "events": SCENARIO_EVENTS[name]}
                for name in SCENARIO_NODES
            ],
            "transport_events": [
                {
                    "event_type": "fake_post",
                    "count": fake_transport_calls,
                },
                {"event_type": "real_network", "count": 0},
                {"event_type": "live_cash", "count": 0},
                {"event_type": "blind_repost", "count": 0},
            ],
            "completed_at": completed_at.astimezone(UTC).isoformat(),
        }
        _publish_new(output_path, report)
        return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake-transport-only", action="store_true", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidate-root", default=str(Path.cwd()))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run_lifecycle_preflight(
            candidate_root=Path(args.candidate_root).resolve(strict=True),
            output_path=Path(args.output).resolve(strict=False),
        )
    except Exception as exc:
        print(
            _canonical(
                {
                    "schema_version": "chili.captured-paper-lifecycle-preflight-error.v1",
                    "error_type": type(exc).__name__,
                }
            ).decode()
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
