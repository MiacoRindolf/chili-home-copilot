"""Typed, bounded evidence probes for premium-independent diagnosis.

The executor intentionally has no raw-command probe. Every operation is built
from a small catalog, validates repository-relative paths, runs without a
shell, strips credentials from subprocess environments, and caps time/output.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..coding_task.envelope import subprocess_safe_env


PROBE_SCHEMA = "chili.diagnostic-probe.v1"
READ_ONLY_PROBE_KINDS = frozenset(
    {"repo_state", "search", "file_excerpt", "git_history", "git_diff"}
)
ISOLATED_PROBE_KINDS = frozenset({"compile", "targeted_test"})
PROBE_KINDS = READ_ONLY_PROBE_KINDS | ISOLATED_PROBE_KINDS
MAX_PROBES = 6
MAX_PATHS = 6
MAX_OUTPUT_CHARS = 12_000
MAX_QUERY_CHARS = 180
MAX_TIMEOUT_SEC = 90.0
_SAFE_SELECTOR_RE = re.compile(
    r"^tests/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z_][A-Za-z0-9_]*(?:\[[A-Za-z0-9_.-]+\])?)*$"
)
_PROMPT_TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]{3,}\b")
_PROMPT_STOP = frozenset(
    {
        "about",
        "after",
        "before",
        "code",
        "diagnose",
        "different",
        "failure",
        "from",
        "input",
        "same",
        "test",
        "that",
        "this",
        "with",
    }
)


def _clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _clean_probe_id(value: object, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "").strip()).strip("-")
    return clean[:100] or fallback


def _safe_rel_path(value: object) -> str:
    raw = str(value or "").replace("\\", "/").strip().strip("/")
    if (
        not raw
        or len(raw) > 320
        or Path(raw).is_absolute()
        or ".." in Path(raw).parts
        or any(ord(char) < 32 for char in raw)
    ):
        return ""
    return raw


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalize_probe_spec(raw: Mapping[str, Any] | None, index: int = 0) -> dict[str, Any]:
    raw = raw if isinstance(raw, Mapping) else {}
    kind = str(raw.get("kind") or "").strip().lower()
    paths: list[str] = []
    for value in raw.get("paths") or []:
        rel = _safe_rel_path(value)
        if rel and rel not in paths:
            paths.append(rel)
        if len(paths) >= MAX_PATHS:
            break
    one_path = _safe_rel_path(raw.get("path"))
    if one_path and one_path not in paths:
        paths.insert(0, one_path)
    query = _clip(raw.get("query"), MAX_QUERY_CHARS)
    if any(ord(char) < 32 and char not in "\t" for char in query):
        query = ""
    selector = str(raw.get("selector") or "").replace("\\", "/").strip()
    dimension = str(raw.get("dimension") or "unknown").strip().lower()
    return {
        "schema": PROBE_SCHEMA,
        "probe_id": _clean_probe_id(raw.get("probe_id"), f"probe-{index + 1}"),
        "kind": kind,
        "paths": paths[:MAX_PATHS],
        "query": query,
        "selector": selector[:320],
        "start_line": _bounded_int(raw.get("start_line"), 1, 1, 1_000_000),
        "max_lines": _bounded_int(raw.get("max_lines"), 80, 1, 120),
        "max_results": _bounded_int(raw.get("max_results"), 40, 1, 100),
        "timeout_sec": _bounded_float(raw.get("timeout_sec"), 30.0, 1.0, MAX_TIMEOUT_SEC),
        "dimension": dimension,
    }


def validate_probe_spec(probe: Mapping[str, Any], safety: str) -> list[str]:
    errors: list[str] = []
    kind = str(probe.get("kind") or "")
    paths = [str(value) for value in probe.get("paths") or []]
    if kind not in PROBE_KINDS:
        errors.append(f"Unknown diagnostic probe kind: {kind or 'missing'}.")
        return errors
    expected_safety = "read_only" if kind in READ_ONLY_PROBE_KINDS else "isolated"
    if safety != expected_safety:
        errors.append(f"Probe {kind} requires safety={expected_safety}.")
    if kind in {"file_excerpt", "git_history", "git_diff", "compile"} and not paths:
        errors.append(f"Probe {kind} requires at least one repository-relative path.")
    if kind == "search" and not str(probe.get("query") or ""):
        errors.append("Probe search requires a fixed-string query.")
    if kind == "targeted_test" and not _SAFE_SELECTOR_RE.fullmatch(
        str(probe.get("selector") or "")
    ):
        errors.append("Probe targeted_test requires a selector under tests/ with no shell syntax.")
    for path in paths:
        if not _safe_rel_path(path):
            errors.append(f"Probe path is unsafe: {path!r}.")
    return sorted(dict.fromkeys(errors))


def probes_from_packet(packet: Mapping[str, Any], max_probes: int = MAX_PROBES) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, experiment in enumerate(packet.get("experiments") or []):
        if not isinstance(experiment, Mapping) or not bool(experiment.get("auto_execute")):
            continue
        raw_probe = experiment.get("probe")
        probe = normalize_probe_spec(raw_probe if isinstance(raw_probe, Mapping) else {}, index)
        probe_id = str(probe.get("probe_id") or "")
        if probe_id in seen:
            continue
        if validate_probe_spec(probe, str(experiment.get("safety") or "")):
            continue
        seen.add(probe_id)
        probes.append({**probe, "safety": str(experiment.get("safety") or "")})
        if len(probes) >= max(0, min(MAX_PROBES, int(max_probes))):
            break
    return probes


def default_followup_probes(
    report: Mapping[str, Any],
    candidate_paths: Sequence[str],
    prompt: str,
) -> list[dict[str, Any]]:
    """Provide a conservative fallback when the local judge asks for evidence
    but emits no executable probe. These operations cannot mutate source.
    """
    if str(report.get("decision") or "") != "instrument_first":
        return []
    probes = [
        normalize_probe_spec(
            {
                "probe_id": "default-repo-state",
                "kind": "repo_state",
                "dimension": "code",
                "timeout_sec": 10,
            }
        )
    ]
    safe_candidates = [path for path in (_safe_rel_path(value) for value in candidate_paths) if path]
    if safe_candidates:
        probes.append(
            normalize_probe_spec(
                {
                    "probe_id": "default-git-history",
                    "kind": "git_history",
                    "paths": safe_candidates[:2],
                    "dimension": "code",
                    "max_results": 12,
                    "timeout_sec": 15,
                },
                1,
            )
        )
    query = next(
        (
            token
            for token in _PROMPT_TOKEN_RE.findall(prompt or "")
            if token.lower() not in _PROMPT_STOP and not token.isdigit()
        ),
        "",
    )
    if query:
        probes.append(
            normalize_probe_spec(
                {
                    "probe_id": "default-fixed-search",
                    "kind": "search",
                    "query": query,
                    "paths": safe_candidates[:3],
                    "dimension": "unknown",
                    "max_results": 30,
                    "timeout_sec": 20,
                },
                2,
            )
        )
    return [
        {**probe, "safety": "read_only"}
        for probe in probes[:3]
        if not validate_probe_spec(probe, "read_only")
    ]


def _safe_repo_path(root: Path, rel: str, *, must_exist: bool = True) -> Path | None:
    safe = _safe_rel_path(rel)
    if not safe:
        return None
    candidate = (root / safe).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if must_exist and not candidate.exists():
        return None
    return candidate


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout_sec: float,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, int]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            env=dict(env or subprocess_safe_env()),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            shell=False,
        )
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        return completed.returncode, _clip(output, MAX_OUTPUT_CHARS), int(
            (time.monotonic() - started) * 1000
        )
    except subprocess.TimeoutExpired as exc:
        output = f"Probe timed out after {timeout_sec:.1f}s. {exc.stdout or ''} {exc.stderr or ''}"
        return 124, _clip(output, MAX_OUTPUT_CHARS), int((time.monotonic() - started) * 1000)
    except (FileNotFoundError, OSError) as exc:
        return 127, _clip(f"Probe executable failed: {exc}", MAX_OUTPUT_CHARS), int(
            (time.monotonic() - started) * 1000
        )


def _search_fallback(root: Path, probe: Mapping[str, Any]) -> tuple[int, str, int]:
    started = time.monotonic()
    query = str(probe.get("query") or "")
    max_results = int(probe.get("max_results") or 40)
    roots = [
        candidate
        for candidate in (
            _safe_repo_path(root, value)
            for value in probe.get("paths") or []
        )
        if candidate is not None
    ] or [root]
    lines: list[str] = []
    scanned = 0
    for search_root in roots:
        candidates = [search_root] if search_root.is_file() else search_root.rglob("*")
        for path in candidates:
            if not path.is_file() or ".git" in path.parts:
                continue
            scanned += 1
            if scanned > 300:
                break
            try:
                if path.stat().st_size > 500_000:
                    continue
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                ):
                    if query in line:
                        lines.append(
                            f"{path.relative_to(root).as_posix()}:{line_number}:{_clip(line.strip(), 400)}"
                        )
                        if len(lines) >= max_results:
                            break
            except OSError:
                continue
            if len(lines) >= max_results:
                break
        if len(lines) >= max_results or scanned > 300:
            break
    output = "\n".join(lines) if lines else f"No fixed-string matches for {query!r}."
    return (0 if lines else 1), _clip(output, MAX_OUTPUT_CHARS), int(
        (time.monotonic() - started) * 1000
    )


def _extract_git_archive(root: Path, destination: Path, timeout_sec: float) -> tuple[bool, str]:
    archive = destination.parent / "repo.tar"
    code, output, _duration = _run(
        ["git", "archive", "--format=tar", f"--output={archive}", "HEAD"],
        cwd=root,
        timeout_sec=timeout_sec,
    )
    if code != 0 or not archive.is_file():
        return False, output or "git archive failed"
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r") as handle:
            for member in handle.getmembers():
                if member.issym() or member.islnk():
                    continue
                target = (destination / member.name).resolve()
                try:
                    target.relative_to(destination.resolve())
                except ValueError:
                    return False, "git archive contained an unsafe path"
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = handle.extractfile(member)
                if source is None:
                    continue
                with source, target.open("wb") as output_file:
                    shutil.copyfileobj(source, output_file)
        return True, ""
    except (tarfile.TarError, OSError) as exc:
        return False, f"git archive extraction failed: {exc}"


def _execute_one(root: Path, probe: Mapping[str, Any]) -> dict[str, Any]:
    probe_id = str(probe.get("probe_id") or "probe")
    kind = str(probe.get("kind") or "")
    safety = str(probe.get("safety") or "")
    timeout_sec = float(probe.get("timeout_sec") or 30.0)
    errors = validate_probe_spec(probe, safety)
    if errors:
        return {
            "probe_id": probe_id,
            "kind": kind,
            "safety": safety,
            "status": "blocked",
            "exit_code": None,
            "output": "; ".join(errors),
            "duration_ms": 0,
            "dimension": str(probe.get("dimension") or "unknown"),
        }

    code = 0
    output = ""
    duration_ms = 0
    paths = [str(value) for value in probe.get("paths") or []]
    if kind == "repo_state":
        code, output, duration_ms = _run(
            ["git", "status", "--short", "--branch", "--untracked-files=no"],
            cwd=root,
            timeout_sec=timeout_sec,
        )
    elif kind in {"git_history", "git_diff"}:
        valid_paths = [
            str(Path(path).as_posix())
            for path in paths
            if _safe_repo_path(root, path) is not None
        ]
        if not valid_paths:
            code, output, duration_ms = 2, "No valid probe paths exist.", 0
        elif kind == "git_history":
            code, output, duration_ms = _run(
                [
                    "git",
                    "log",
                    "--no-decorate",
                    f"-n{int(probe.get('max_results') or 12)}",
                    "--oneline",
                    "--",
                    *valid_paths,
                ],
                cwd=root,
                timeout_sec=timeout_sec,
            )
        else:
            code, output, duration_ms = _run(
                ["git", "diff", "--no-ext-diff", "--unified=3", "--", *valid_paths],
                cwd=root,
                timeout_sec=timeout_sec,
            )
    elif kind == "search":
        rg = shutil.which("rg")
        if rg:
            valid_paths = [
                path for path in paths if _safe_repo_path(root, path) is not None
            ]
            code, output, duration_ms = _run(
                [
                    rg,
                    "--line-number",
                    "--fixed-strings",
                    "--no-heading",
                    "--with-filename",
                    f"--max-count={int(probe.get('max_results') or 40)}",
                    str(probe.get("query") or ""),
                    *(valid_paths or ["."]),
                ],
                cwd=root,
                timeout_sec=timeout_sec,
            )
        else:
            code, output, duration_ms = _search_fallback(root, probe)
    elif kind == "file_excerpt":
        path = _safe_repo_path(root, paths[0])
        started = time.monotonic()
        if path is None or not path.is_file():
            code, output = 2, "Probe file does not exist."
        else:
            try:
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                lines = raw.decode("utf-8", errors="replace").splitlines()
                start = int(probe.get("start_line") or 1)
                maximum = int(probe.get("max_lines") or 80)
                selected = lines[start - 1 : start - 1 + maximum]
                body = "\n".join(
                    f"{start + offset}:{line}" for offset, line in enumerate(selected)
                )
                output = f"sha256={digest} bytes={len(raw)}\n{body}"
            except OSError as exc:
                code, output = 2, f"Probe file read failed: {exc}"
        duration_ms = int((time.monotonic() - started) * 1000)
    elif kind == "compile":
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="chili-diagnostic-compile-") as temp:
            temp_root = Path(temp)
            copied: list[str] = []
            for rel in paths:
                source = _safe_repo_path(root, rel)
                if source is None or not source.is_file() or source.suffix.lower() != ".py":
                    continue
                target = temp_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                copied.append(str(target))
            if not copied:
                code, output = 2, "Compile probe has no existing Python files."
            else:
                code, output, _ = _run(
                    [sys.executable, "-m", "py_compile", *copied],
                    cwd=temp_root,
                    timeout_sec=timeout_sec,
                )
                output = output or f"Compiled {len(copied)} Python file(s) successfully."
        duration_ms = int((time.monotonic() - started) * 1000)
    elif kind == "targeted_test":
        started = time.monotonic()
        selector = str(probe.get("selector") or "")
        selector_path = selector.split("::", 1)[0]
        if _safe_repo_path(root, selector_path) is None:
            code, output = 2, "Targeted test selector does not exist in the repository."
        else:
            with tempfile.TemporaryDirectory(prefix="chili-diagnostic-test-") as temp:
                snapshot = Path(temp) / "repo"
                ok, archive_error = _extract_git_archive(root, snapshot, min(timeout_sec, 30.0))
                if not ok:
                    code, output = 2, archive_error
                else:
                    env = subprocess_safe_env()
                    env.update(
                        {
                            "CHILI_AUTONOMY_PROBE": "1",
                            "CHILI_DISABLE_LIVE_TRADING": "1",
                            "HTTP_PROXY": "http://127.0.0.1:9",
                            "HTTPS_PROXY": "http://127.0.0.1:9",
                            "ALL_PROXY": "http://127.0.0.1:9",
                            "NO_PROXY": "127.0.0.1,localhost",
                        }
                    )
                    code, output, _ = _run(
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            selector,
                            "-q",
                            "--disable-warnings",
                            "--maxfail=1",
                        ],
                        cwd=snapshot,
                        timeout_sec=timeout_sec,
                        env=env,
                    )
        duration_ms = int((time.monotonic() - started) * 1000)

    completed = code in ({0, 1} if kind == "search" else {0})
    return {
        "probe_id": probe_id,
        "kind": kind,
        "safety": safety,
        "status": "completed" if completed else ("timeout" if code == 124 else "failed"),
        "exit_code": code,
        "output": _clip(output or "Probe completed with no output.", MAX_OUTPUT_CHARS),
        "duration_ms": duration_ms,
        "dimension": str(probe.get("dimension") or "unknown"),
    }


def execute_safe_probes(
    repo_path: Path,
    probes: Sequence[Mapping[str, Any]],
    *,
    max_probes: int = MAX_PROBES,
    time_budget_sec: float = 120.0,
) -> dict[str, Any]:
    root = repo_path.resolve()
    if not root.is_dir():
        return {
            "schema": "chili.diagnostic-probe-run.v1",
            "results": [],
            "evidence": [],
            "errors": ["Repository path is unavailable."],
            "duration_ms": 0,
        }
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(probes[: max(0, min(MAX_PROBES, int(max_probes)))]):
        probe = normalize_probe_spec(raw, index)
        normalized.append({**probe, "safety": str(raw.get("safety") or "")})
    for probe in normalized:
        if time.monotonic() - started >= max(1.0, float(time_budget_sec)):
            results.append(
                {
                    "probe_id": probe["probe_id"],
                    "kind": probe["kind"],
                    "safety": probe["safety"],
                    "status": "blocked",
                    "exit_code": None,
                    "output": "Probe run time budget was exhausted.",
                    "duration_ms": 0,
                    "dimension": probe["dimension"],
                }
            )
            continue
        results.append(_execute_one(root, probe))

    evidence = []
    for index, result in enumerate(results):
        if result.get("status") not in {"completed", "failed", "timeout"}:
            continue
        evidence.append(
            {
                "evidence_id": f"probe-{_clean_probe_id(result.get('probe_id'), str(index + 1))}",
                "statement": (
                    f"Typed {result.get('kind')} probe status={result.get('status')} "
                    f"exit={result.get('exit_code')}: {_clip(result.get('output'), 900)}"
                ),
                "dimension": str(result.get("dimension") or "unknown"),
                "kind": "experiment",
                "provenance": f"diagnostic_probe:{result.get('probe_id')}",
                "independence_key": f"diagnostic_probe:{result.get('probe_id')}",
                "reliability": 0.95 if result.get("status") == "completed" else 0.7,
                "discriminating": True,
                "experiment_id": str(result.get("probe_id") or ""),
            }
        )
    return {
        "schema": "chili.diagnostic-probe-run.v1",
        "results": results,
        "evidence": evidence,
        "errors": [],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
