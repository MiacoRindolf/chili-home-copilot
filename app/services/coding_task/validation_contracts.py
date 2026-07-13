"""Stable test-contract evidence for bounded autonomous repair.

Aggregate pass/fail counts are not enough to prove progress: a patch can make
one test green while breaking a different test and retain the same totals.
This module extracts stable test identities from the bounded runners CHILI
uses so repair acceptance can preserve every previously green contract.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


_STATUS_VALUES = frozenset({"passed", "failed", "error", "skipped"})
_PYTEST_STATUS_RE = re.compile(
    r"^\s*(?P<test>.+?::[^\r\n]+?)\s+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED)(?:\s|$)",
    re.MULTILINE,
)
_PYTEST_SUMMARY_RE = re.compile(
    r"^\s*(?P<status>FAILED|ERROR)\s+(?P<test>[^\r\n]+?::[^\r\n\s]+)",
    re.IGNORECASE | re.MULTILINE,
)
_NODE_STATUS_RE = re.compile(
    r"^\s*(?P<status>[\u2714\u2716])\s+(?P<name>.+?)"
    r"(?:\s+\(\d+(?:\.\d+)?\s*ms\))?\s*$",
    re.MULTILINE,
)
_SECTION_RE = re.compile(r"^\[(?P<path>tests[/\\][^\]]+)\]\s*$")


def normalize_contract_id(value: object) -> str:
    """Normalize paths and volatile render details without merging tests."""
    text = str(value or "").strip().replace("\\", "/")
    tests_at = text.lower().find("tests/")
    if tests_at >= 0:
        text = text[tests_at:]
    text = re.sub(r"\s+\[\s*\d+%\s*\]\s*$", "", text)
    text = re.sub(r"\s+\(\d+(?:\.\d+)?\s*ms\)\s*$", "", text)
    text = re.sub(r"0x[0-9a-f]+", "0x<addr>", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().casefold()


def normalize_failure_text(value: object) -> str:
    """Remove run-local noise while retaining causal failure semantics."""
    text = str(value or "").lower().replace("\\", "/")
    text = re.sub(
        r"chili-(?:fix|final-adjudication|baseline-feedback)-[^/\s]+",
        "chili-temp",
        text,
    )
    text = re.sub(r"\b[a-z]:/[^\s:]+", "<path>", text)
    text = re.sub(r"/(?:tmp|var/tmp)/[^\s:]+", "<path>", text)
    text = re.sub(r"0x[0-9a-f]+", "0x<addr>", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b", "<time>", text)
    text = re.sub(r"(?<=:)(?:\d+)(?::\d+)?", "#", text)
    return re.sub(r"\s+", " ", text).strip()


def _explicit_status(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    status: dict[str, str] = {}
    for key, value in raw.items():
        normalized = normalize_contract_id(key)
        state = str(value or "").strip().lower()
        if normalized and state in _STATUS_VALUES:
            status[normalized] = state
    return status


def _pytest_status(output: str) -> dict[str, str]:
    status: dict[str, str] = {}
    for match in _PYTEST_STATUS_RE.finditer(output):
        test_id = normalize_contract_id(match.group("test"))
        state = match.group("status").lower()
        if test_id:
            status[test_id] = state
    for match in _PYTEST_SUMMARY_RE.finditer(output):
        test_id = normalize_contract_id(match.group("test"))
        if test_id and test_id not in status:
            status[test_id] = match.group("status").lower()
    return status


def _sectioned_status(output: str) -> dict[str, str]:
    status: dict[str, str] = {}
    current_file = ""
    for line in output.splitlines():
        section = _SECTION_RE.match(line.strip())
        if section:
            current_file = normalize_contract_id(section.group("path"))
            continue
        node = _NODE_STATUS_RE.match(line)
        if node:
            name = normalize_contract_id(node.group("name"))
            if name.rstrip(":") == "failing tests":
                continue
            key = normalize_contract_id(
                f"{current_file}::{name}" if current_file else f"node::{name}"
            )
            status[key] = "passed" if node.group("status") == "\u2714" else "failed"
            continue
        bad_state = re.search(r"\bBad state:\s*(.+)$", line, re.IGNORECASE)
        if bad_state and current_file:
            key = normalize_contract_id(f"{current_file}::{bad_state.group(1)}")
            status[key] = "failed"
    return status


def test_contract_evidence(
    result: Mapping[str, Any],
    *,
    runner_hint: str = "",
) -> dict[str, Any]:
    """Return stable pass/fail identities from one validation result."""
    explicit = _explicit_status(
        result.get("test_contract_status") or result.get("contract_status")
    )
    output = "\n".join(
        str(result.get(key) or "") for key in ("output", "stdout", "stderr")
    )
    hint = str(
        runner_hint or result.get("runner") or result.get("step_key") or ""
    ).lower()
    parsed: dict[str, str] = dict(explicit)
    if "pytest" in hint or "::" in output:
        parsed.update(_pytest_status(output))
    parsed.update(_sectioned_status(output))

    passed = sorted(key for key, value in parsed.items() if value == "passed")
    failed = sorted(
        key for key, value in parsed.items() if value in {"failed", "error"}
    )
    observed = sorted(parsed)
    explicit_complete = result.get("test_contracts_complete")
    complete = bool(explicit_complete) if explicit_complete is not None else bool(
        observed
        and (
            _PYTEST_STATUS_RE.search(output)
            or _NODE_STATUS_RE.search(output)
            or bool(result.get("passed"))
        )
    )
    return {
        "passed_ids": passed,
        "failed_ids": failed,
        "observed_ids": observed,
        "identity_available": bool(observed),
        "complete": complete,
    }


def contract_regressions(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[str]:
    """Return previously passing contracts known to no longer pass."""
    regressions = set(before.get("passed_ids") or []) - set(
        after.get("passed_ids") or []
    )
    if after.get("complete"):
        return sorted(regressions)

    observed = set(after.get("observed_ids") or [])
    observed.update(after.get("passed_ids") or [])
    observed.update(after.get("failed_ids") or [])
    return sorted(regressions & observed)


def contract_progressed(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> bool:
    """Require a resolved stable failure with no previously green regression."""
    if contract_regressions(before, after):
        return False
    before_failed = set(before.get("failed_ids") or [])
    after_failed = set(after.get("failed_ids") or [])
    after_passed = set(after.get("passed_ids") or [])
    resolved = before_failed & after_passed
    if resolved:
        return True
    return bool(
        before.get("complete")
        and after.get("complete")
        and before_failed
        and after_failed < before_failed
    )
