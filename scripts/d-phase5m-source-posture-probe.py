from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


DEFAULT_CONTAINERS = (
    "chili-home-copilot-chili-1",
    "chili-home-copilot-autotrader-worker-1",
    "chili-home-copilot-scheduler-worker-1",
    "chili-home-copilot-broker-sync-worker-1",
)

APP_DESTINATIONS = {"/workspace", "/app/app", "/app/docs"}
PHASE5_FLAGS = (
    "CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES",
    "CHILI_PHASE5K_COINBASE_CAP_USE_ENVELOPES",
)


@dataclass(frozen=True)
class GitPosture:
    root: str
    branch: str
    commit: str
    dirty: bool
    ancestor_of_base: bool | None


def _run(args: list[str], *, cwd: str | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _norm(path: str) -> str:
    return str(PureWindowsPath(path.replace("/", "\\")))


def source_to_repo_root(source: str, destination: str) -> str | None:
    if destination not in APP_DESTINATIONS:
        return None
    source_path = PureWindowsPath(source.replace("/", "\\"))
    if destination in {"/app/app", "/app/docs"}:
        return _norm(str(source_path.parent))
    return _norm(str(source_path))


def docker_inspect(container: str) -> dict[str, Any] | None:
    proc = _run(["docker", "inspect", container], timeout=30)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not payload:
        return None
    return payload[0]


def container_env(inspect_payload: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in inspect_payload.get("Config", {}).get("Env", []) or []:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key in PHASE5_FLAGS:
            out[key] = value
    return out


def relevant_roots(inspect_payload: dict[str, Any]) -> list[str]:
    roots: list[str] = []
    for mount in inspect_payload.get("Mounts", []) or []:
        dest = str(mount.get("Destination") or "")
        source = str(mount.get("Source") or "")
        root = source_to_repo_root(source, dest)
        if root and root not in roots:
            roots.append(root)
    return roots


def git_posture(root: str, *, base_ref: str) -> GitPosture:
    branch = _run(["git", "-C", root, "branch", "--show-current"]).stdout.strip()
    commit = _run(["git", "-C", root, "rev-parse", "--short=12", "HEAD"]).stdout.strip()
    dirty = bool(_run(["git", "-C", root, "status", "--porcelain"]).stdout.strip())
    ancestor_proc = _run(["git", "-C", root, "merge-base", "--is-ancestor", "HEAD", base_ref])
    ancestor: bool | None
    if ancestor_proc.returncode == 0:
        ancestor = True
    elif ancestor_proc.returncode == 1:
        ancestor = False
    else:
        ancestor = None
    return GitPosture(
        root=root,
        branch=branch or "(detached)",
        commit=commit or "(unknown)",
        dirty=dirty,
        ancestor_of_base=ancestor,
    )


def build_report(
    *,
    containers: tuple[str, ...] = DEFAULT_CONTAINERS,
    dirty_root: str = r"D:\dev\chili-home-copilot",
    base_ref: str = "origin/codex/brain-work-done-marker-recovery",
) -> dict[str, Any]:
    services: list[dict[str, Any]] = []
    roots: dict[str, GitPosture] = {}
    missing: list[str] = []

    for container in containers:
        payload = docker_inspect(container)
        if payload is None:
            missing.append(container)
            continue
        roots_for_container = relevant_roots(payload)
        for root in roots_for_container:
            if root not in roots:
                roots[root] = git_posture(root, base_ref=base_ref)
        services.append(
            {
                "name": container,
                "status": payload.get("State", {}).get("Status"),
                "healthy": payload.get("State", {}).get("Health", {}).get("Status"),
                "roots": roots_for_container,
                "phase5_flags": container_env(payload),
            }
        )

    dirty_root_norm = _norm(dirty_root)
    all_roots = sorted(roots)
    app_roots = [root for root in all_roots if root != dirty_root_norm]
    using_dirty_root = any(root == dirty_root_norm for root in all_roots)
    dirty_worktrees = [root for root, posture in roots.items() if posture.dirty]
    non_ancestor_roots = [
        root for root, posture in roots.items() if posture.ancestor_of_base is False
    ]
    unknown_base_roots = [
        root for root, posture in roots.items() if posture.ancestor_of_base is None
    ]

    flag_values: dict[str, set[str]] = {name: set() for name in PHASE5_FLAGS}
    for service in services:
        for flag in PHASE5_FLAGS:
            if flag in service["phase5_flags"]:
                flag_values[flag].add(service["phase5_flags"][flag].lower())

    flag_mismatches = {
        flag: sorted(values)
        for flag, values in flag_values.items()
        if len(values) > 1
    }

    verdict = "COMPLETE_POSITIVE"
    reasons: list[str] = []
    if missing:
        verdict = "ALERT"
        reasons.append(f"missing_containers={len(missing)}")
    if using_dirty_root:
        verdict = "ALERT"
        reasons.append("live_service_uses_dirty_root")
    if dirty_worktrees:
        verdict = "ALERT"
        reasons.append(f"dirty_worktrees={len(dirty_worktrees)}")
    if non_ancestor_roots:
        verdict = "ALERT"
        reasons.append(f"non_ancestor_roots={len(non_ancestor_roots)}")
    if flag_mismatches:
        verdict = "ALERT"
        reasons.append("phase5_flag_mismatch")
    if not services:
        verdict = "REGRESSION"
        reasons.append("no_services_inspected")

    return {
        "verdict": verdict,
        "reason": ";".join(reasons) if reasons else "source posture clean",
        "base_ref": base_ref,
        "dirty_root": dirty_root_norm,
        "services": services,
        "roots": {
            root: {
                "branch": posture.branch,
                "commit": posture.commit,
                "dirty": posture.dirty,
                "ancestor_of_base": posture.ancestor_of_base,
            }
            for root, posture in roots.items()
        },
        "using_dirty_root": using_dirty_root,
        "dirty_worktrees": dirty_worktrees,
        "non_ancestor_roots": non_ancestor_roots,
        "unknown_base_roots": unknown_base_roots,
        "flag_mismatches": flag_mismatches,
        "app_roots": app_roots,
    }


def main() -> int:
    report = build_report(
        dirty_root=os.environ.get("CHILI_PHASE5M_DIRTY_ROOT", r"D:\dev\chili-home-copilot"),
        base_ref=os.environ.get("CHILI_PHASE5M_BASE_REF", "origin/codex/brain-work-done-marker-recovery"),
    )
    print(f"VERDICT_STATUS={report['verdict']}")
    print(f"VERDICT_REASON={report['reason']}")
    print(f"BASE_REF={report['base_ref']}")
    print(f"USING_DIRTY_ROOT={str(report['using_dirty_root']).lower()}")
    print(f"DIRTY_WORKTREES={len(report['dirty_worktrees'])}")
    print(f"NON_ANCESTOR_ROOTS={len(report['non_ancestor_roots'])}")
    print(f"FLAG_MISMATCHES={len(report['flag_mismatches'])}")
    for root, posture in sorted(report["roots"].items()):
        print(
            "ROOT "
            f"path={root} branch={posture['branch']} commit={posture['commit']} "
            f"dirty={str(posture['dirty']).lower()} "
            f"ancestor_of_base={str(posture['ancestor_of_base']).lower()}"
        )
    for service in report["services"]:
        roots = ",".join(service["roots"]) or "(none)"
        flags = ",".join(
            f"{k}={v}" for k, v in sorted(service["phase5_flags"].items())
        )
        print(
            "SERVICE "
            f"name={service['name']} status={service['status']} health={service['healthy']} "
            f"roots={roots} flags={flags}"
        )
    return 0 if report["verdict"] == "COMPLETE_POSITIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())

