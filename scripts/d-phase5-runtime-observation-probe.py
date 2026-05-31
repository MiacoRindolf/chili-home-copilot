"""Phase 5 runtime observation rollup probe.

This read-only probe packages the Phase 5 observation checklist into one
operator-friendly command. It intentionally does not advance the broad rename:
the compatibility boundary remains the product unless a concrete production
issue appears.

Machine-readable header:

    VERDICT_STATUS=<IN_FLIGHT|COMPLETE_POSITIVE|REGRESSION|ALERT>
    VERDICT_REASON=<short reason>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]

APP_SERVICES = (
    "chili",
    "scheduler-worker",
    "autotrader-worker",
    "broker-sync-worker",
)

SCHEMA_ERROR_RE = re.compile(
    r"NoReferencedTableError|UndefinedTable|UndefinedColumn|"
    r"relation .* does not exist|column .* does not exist|"
    r"ERROR|CRITICAL|Traceback",
    re.IGNORECASE,
)
PHASE5_SUBJECT_RE = re.compile(
    r"trading_trades|trading_management_envelopes|phase5|trade_id|Trade",
    re.IGNORECASE,
)
POSTGRES_VERSION_NOISE_RE = re.compile(
    r'column "version" does not exist|schema_version\.version',
    re.IGNORECASE,
)
POSTGRES_TEST_FIXTURE_NOISE_RE = re.compile(
    r"TRUNCATE .* RESTART IDENTITY CASCADE|deadlock detected",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class CommandResult:
    name: str
    returncode: int
    stdout: str
    stderr: str


def _run(name: str, args: list[str], *, timeout: int = 180) -> CommandResult:
    env = dict(os.environ)
    env.setdefault("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
    try:
        proc = subprocess.run(
            args,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception as exc:
        return CommandResult(name=name, returncode=127, stdout="", stderr=str(exc))
    return CommandResult(
        name=name,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _status_from_output(output: str) -> str | None:
    for line in output.splitlines():
        if line.startswith("VERDICT_STATUS="):
            return line.split("=", 1)[1].strip()
    return None


def _json_from_output(output: str) -> dict[str, Any] | None:
    try:
        return json.loads(output)
    except Exception:
        return None


def _count_matches(text: str, pattern: re.Pattern[str]) -> int:
    return sum(1 for line in text.splitlines() if pattern.search(line))


def _count_phase5_schema_errors(text: str) -> int:
    count = 0
    for line in text.splitlines():
        if POSTGRES_VERSION_NOISE_RE.search(line):
            continue
        if SCHEMA_ERROR_RE.search(line) and PHASE5_SUBJECT_RE.search(line):
            count += 1
    return count


def _run_phase5k() -> CommandResult:
    return _run(
        "phase5k_live_path_parity",
        [sys.executable, "scripts/d-phase5k-live-path-parity-probe.py"],
        timeout=240,
    )


def _run_phase5i() -> CommandResult:
    return _run(
        "phase5i_post_rename_soak",
        [sys.executable, "scripts/d-phase5i-post-rename-soak-probe.py"],
        timeout=240,
    )


def _run_reader_canary() -> CommandResult:
    return _run(
        "phase5_reader_canary",
        [
            sys.executable,
            "scripts/analyze_phase5_remaining_trade_refs.py",
            "--json",
            "--include",
            "app",
            "--fail-on-unexpected-runtime",
        ],
        timeout=120,
    )


def _docker_logs(services: tuple[str, ...], since_minutes: int) -> CommandResult:
    return _run(
        "docker_logs_" + "_".join(services),
        ["docker", "compose", "logs", "--since", f"{since_minutes}m", *services],
        timeout=120,
    )


def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-minutes", type=int, default=120)
    parser.add_argument(
        "--market-window-complete",
        action="store_true",
        help=(
            "Allow COMPLETE_POSITIVE when all checks are green. Without this, "
            "the probe remains IN_FLIGHT because weekend/overnight checks do "
            "not prove a normal market-session soak."
        ),
    )
    args = parser.parse_args()

    print(f"# phase5 runtime observation probe -- {datetime.now(timezone.utc).isoformat()}")
    print(f"SINCE_MINUTES={args.since_minutes}")
    print(f"MARKET_WINDOW_COMPLETE={str(args.market_window_complete).lower()}")

    phase5k = _run_phase5k()
    phase5i = _run_phase5i()
    canary = _run_reader_canary()
    app_logs = _docker_logs(APP_SERVICES, args.since_minutes)
    postgres_logs = _docker_logs(("postgres",), args.since_minutes)

    phase5k_status = _status_from_output(phase5k.stdout)
    phase5i_status = _status_from_output(phase5i.stdout)
    canary_json = _json_from_output(canary.stdout)
    canary_ok = bool(canary_json and canary_json.get("ok") is True)
    unexpected_runtime_readers = (
        canary_json.get("unexpected_runtime_readers", []) if canary_json else None
    )
    unexpected_runtime_mutations = (
        canary_json.get("unexpected_runtime_mutations", []) if canary_json else None
    )

    app_phase5_schema_errors = _count_phase5_schema_errors(app_logs.stdout + app_logs.stderr)
    postgres_phase5_schema_errors = _count_phase5_schema_errors(
        postgres_logs.stdout + postgres_logs.stderr
    )
    postgres_version_noise = _count_matches(
        postgres_logs.stdout + postgres_logs.stderr,
        POSTGRES_VERSION_NOISE_RE,
    )
    postgres_test_fixture_noise = _count_matches(
        postgres_logs.stdout + postgres_logs.stderr,
        POSTGRES_TEST_FIXTURE_NOISE_RE,
    )

    print(f"PHASE5K_STATUS={phase5k_status}")
    print(f"PHASE5I_STATUS={phase5i_status}")
    print(f"READER_CANARY_OK={str(canary_ok).lower()}")
    print(f"UNEXPECTED_RUNTIME_READERS={unexpected_runtime_readers}")
    print(f"UNEXPECTED_RUNTIME_MUTATIONS={unexpected_runtime_mutations}")
    print(f"APP_PHASE5_SCHEMA_ERRORS={app_phase5_schema_errors}")
    print(f"POSTGRES_PHASE5_SCHEMA_ERRORS={postgres_phase5_schema_errors}")
    print(f"POSTGRES_SCHEMA_VERSION_VERSION_NOISE={postgres_version_noise}")
    print(f"POSTGRES_TEST_FIXTURE_NOISE={postgres_test_fixture_noise}")

    blockers: list[str] = []
    if phase5k.returncode != 0 or phase5k_status != "COMPLETE_POSITIVE":
        blockers.append(f"Phase5K={phase5k_status or 'missing'}")
    if phase5i.returncode != 0 or phase5i_status != "COMPLETE_POSITIVE":
        blockers.append(f"Phase5I={phase5i_status or 'missing'}")
    if canary.returncode != 0 or not canary_ok:
        blockers.append("reader_canary_not_clean")
    if unexpected_runtime_readers:
        blockers.append("unexpected_runtime_readers")
    if unexpected_runtime_mutations:
        blockers.append("unexpected_runtime_mutations")
    if app_phase5_schema_errors:
        blockers.append("app_phase5_schema_errors")
    if postgres_phase5_schema_errors:
        blockers.append("postgres_phase5_schema_errors")

    if blockers:
        print("VERDICT_STATUS=REGRESSION")
        print("VERDICT_REASON=" + ", ".join(blockers))
        return 2

    if not args.market_window_complete:
        print("VERDICT_STATUS=IN_FLIGHT")
        print("VERDICT_REASON=mechanical checks green; wait for a normal market-window soak before closeout")
        return 0

    print("VERDICT_STATUS=COMPLETE_POSITIVE")
    print("VERDICT_REASON=mechanical checks green across declared market window")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
