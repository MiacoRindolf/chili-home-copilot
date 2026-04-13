#!/usr/bin/env python3
"""Phase 0: compare prod ``brain_runtime.release.git_commit`` to ``origin/main`` (or a given SHA).

Usage (repo root, with network)::

    conda run -n chili-env python scripts/verify_phase0_release_sha.py
    conda run -n chili-env python scripts/verify_phase0_release_sha.py e463f7b6cc3cb77cbb41b9b92b255b964803b778

Env::

    CHILI_PHASE0_STATUS_URL — default https://getchili.app/api/trading/scan/status

Exit code 0 only on full SHA match (case-insensitive).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def _expected_sha(argv: list[str]) -> str:
    if len(argv) > 1:
        return argv[1].strip().lower()
    out = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        capture_output=True,
        text=True,
        check=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    return out.stdout.strip().lower()


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    url = os.environ.get(
        "CHILI_PHASE0_STATUS_URL",
        "https://getchili.app/api/trading/scan/status",
    )
    expected = _expected_sha(sys.argv)
    req = urllib.request.Request(url, headers={"User-Agent": "chili-phase0-verify/1"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.load(resp)
    except urllib.error.URLError as e:
        print(f"FAIL: could not fetch {url}: {e}", file=sys.stderr)
        return 2
    rel = (data.get("brain_runtime") or {}).get("release") or {}
    got = (rel.get("git_commit") or "").strip().lower()
    src = rel.get("git_commit_source")
    print(f"URL:     {url}")
    print(f"expect:  {expected}")
    print(f"prod:    {got or '(missing)'}")
    print(f"source:  {src or '(unknown; redeploy app for git_commit_source field)'}")
    if not got:
        print("FAIL: release.git_commit missing", file=sys.stderr)
        return 1
    if got != expected:
        print(
            "FAIL: mismatch. If source=environment, update or remove CHILI_GIT_COMMIT / "
            "GIT_COMMIT on the host, or rebuild the Docker image with "
            "build-arg CHILI_GIT_COMMIT=$(git rev-parse HEAD).",
            file=sys.stderr,
        )
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
