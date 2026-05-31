from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = REPO_ROOT / "scripts" / "dispatch-phase5n-source-posture-watch-out.txt"


def _run_phase5m_probe() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python", "scripts/d-phase5m-source-posture-probe.py"],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
        timeout=120,
    )


def _verdict(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("VERDICT_STATUS="):
            return line.split("=", 1)[1].strip()
    return "UNKNOWN"


def build_watch_output(probe: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    verdict = _verdict(probe.stdout)
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"# phase5n source-posture watch -- {now}",
        f"VERDICT_STATUS={verdict}",
        f"PROBE_EXIT_CODE={probe.returncode}",
        "",
        "## Phase 5M Probe Output",
        probe.stdout.rstrip() or "(no stdout)",
    ]
    if probe.stderr.strip():
        lines.extend(["", "## Probe stderr", probe.stderr.rstrip()])
    if verdict != "COMPLETE_POSITIVE" or probe.returncode != 0:
        lines.extend(
            [
                "",
                "## Operator Guidance",
                "Source posture is not green. Review:",
                "docs/RUNBOOKS/PHASE5M_DEPLOYMENT_SOURCE_POSTURE.md",
                "",
                "After any source-mount correction, re-run:",
                "python scripts/d-phase5m-source-posture-probe.py",
                "$env:DATABASE_URL='postgresql://chili:chili@localhost:5433/chili'",
                "python scripts/d-phase5k-live-path-parity-probe.py",
                "python scripts/d-phase5i-post-rename-soak-probe.py",
                "",
                "Guardrails: do not restart Postgres, flip Phase 5 flags, clean the dirty root,",
                "or mutate runtime unless the probe reports dirty-root usage.",
            ]
        )
    return verdict, "\n".join(lines).rstrip() + "\n"


def main() -> int:
    output_path = Path(
        os.environ.get("CHILI_PHASE5N_WATCH_OUTPUT", str(DEFAULT_OUTPUT_PATH))
    )
    probe = _run_phase5m_probe()
    verdict, output = build_watch_output(probe)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if verdict == "COMPLETE_POSITIVE" and probe.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

