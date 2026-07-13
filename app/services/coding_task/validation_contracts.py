"""Stable test-contract evidence for bounded autonomous repair.

Aggregate pass/fail counts are not enough to prove progress: a patch can make
one test green while breaking a different test and retain the same totals.
This module extracts stable test identities from the bounded runners CHILI
uses so repair acceptance can preserve every previously green contract.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping


MAX_FAILED_CONTRACT_IDS = 32
MAX_FAILURE_FACTS = 12
MAX_CONTRACT_ID_CHARS = 240
MAX_FAILURE_FACT_CHARS = 240

_STATUS_VALUES = frozenset({"passed", "failed", "error", "skipped"})
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|msec|milliseconds?|s|sec|seconds?)\b",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w])(?:file:///)?[a-z]:/(?:[^\s:;,\]\[()'\"]+/)*"
    r"[^\s:;,\]\[()'\"]+",
    re.IGNORECASE,
)
_POSIX_VOLATILE_PATH_RE = re.compile(
    r"(?<![\w:])/(?:tmp|var/tmp|private/tmp|home/[^/\s]+|users/[^/\s]+|"
    r"workspace|builds/[^/\s]+)(?:/[^\s:;,\]\[()'\"]+)+",
    re.IGNORECASE,
)
_SOURCE_LOCATION_RE = re.compile(
    r"((?:[\w.-]+/)+[\w.-]+\.[a-z0-9]+):\d+(?::\d+)?",
    re.IGNORECASE,
)
_PYTEST_STATUS_RE = re.compile(
    r"^\s*(?P<test>.+?::[^\r\n]+?)[ \t]+"
    r"(?P<status>PASSED|FAILED|ERROR|SKIPPED)(?:[ \t]|$)",
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
_LABELED_VALUE_RE = re.compile(
    r"^(?P<label>expected|actual|received)\s*:\s*(?P<value>.+?)\s*,?$",
    re.IGNORECASE,
)
_INLINE_EXPECTED_ACTUAL_RE = re.compile(
    r"^expected\s*:\s*(?P<expected>.+?)\s+"
    r"(?:actual|received)\s*:\s*(?P<actual>.+)$",
    re.IGNORECASE,
)
_INLINE_ACTUAL_EXPECTED_RE = re.compile(
    r"^(?:actual|received)\s*:\s*(?P<actual>.+?)\s+"
    r"expected\s*:\s*(?P<expected>.+)$",
    re.IGNORECASE,
)
_NODE_DIFF_HEADER_RE = re.compile(
    r"^\s*\+\s*actual\s+-\s*expected\s*$",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"^(?P<actual>.{1,100}?)\s+(?P<operator>!==|===|!=|==)\s+"
    r"(?P<expected>.{1,100})$"
)
_EXCEPTION_RE = re.compile(
    r"(?P<kind>"
    r"(?:sqlite3\.)?OperationalError|SQLiteError|SqliteException|DatabaseError|"
    r"ModuleNotFoundError|AssertionError|ReferenceError|NameError|TypeError|"
    r"RangeError|SyntaxError|KeyError|AttributeError|ValueError|ImportError|"
    r"RuntimeError|StateError"
    r")(?:\s*\[[^\]\r\n]+\])?\s*:\s*(?P<message>[^\r\n]+)",
    re.IGNORECASE,
)
_DART_TYPE_ERROR_RE = re.compile(
    r"\btype\s+.+?\s+is\s+not\s+a\s+subtype\s+of\s+type\s+.+$",
    re.IGNORECASE,
)
_COMPILER_ERROR_RE = re.compile(
    r"^.+?\.(?:dart|py|[cm]?[jt]sx?)"
    r"(?::\d+(?::\d+)?|\(\d+\s*,\s*\d+\)):\s*"
    r"error(?:\s+(?P<code>[A-Z]+\d+))?\s*:\s*(?P<message>.+)$",
    re.IGNORECASE,
)

_CANONICAL_ERROR_NAMES = {
    "assertionerror": "AssertionError",
    "attributeerror": "AttributeError",
    "databaseerror": "DatabaseError",
    "importerror": "ImportError",
    "keyerror": "KeyError",
    "modulenotfounderror": "ModuleNotFoundError",
    "nameerror": "NameError",
    "operationalerror": "OperationalError",
    "rangeerror": "RangeError",
    "referenceerror": "ReferenceError",
    "runtimeerror": "RuntimeError",
    "sqliteerror": "SQLiteError",
    "sqliteexception": "SqliteException",
    "stateerror": "StateError",
    "syntaxerror": "SyntaxError",
    "typeerror": "TypeError",
    "valueerror": "ValueError",
}


def normalize_contract_id(value: object) -> str:
    """Normalize paths and volatile render details without merging tests."""
    text = _ANSI_ESCAPE_RE.sub("", str(value or "")).strip().replace("\\", "/")
    tests_at = text.lower().find("tests/")
    if tests_at >= 0:
        text = text[tests_at:]
    text = re.sub(r"\s+\[\s*\d+%\s*\]\s*$", "", text)
    text = re.sub(r"\s+\(\d+(?:\.\d+)?\s*ms\)\s*$", "", text)
    text = re.sub(r"0x[0-9a-f]+", "0x<addr>", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().casefold()


def normalize_failure_text(value: object) -> str:
    """Remove run-local noise while retaining causal failure semantics."""
    text = _normalize_volatile_text(value)
    text = re.sub(r"(?<=:)(?:\d+)(?::\d+)?", "#", text)
    return text.casefold()


def _normalize_volatile_text(value: object) -> str:
    """Normalize run-local render details without changing diagnostic values."""
    text = _ANSI_ESCAPE_RE.sub("", str(value or "")).replace("\\", "/")
    text = re.sub(
        r"chili-(?:fix|final-adjudication|baseline-feedback)-[^/\s]+",
        "chili-temp",
        text,
        flags=re.IGNORECASE,
    )
    text = _ISO_TIMESTAMP_RE.sub("<time>", text)
    text = _DURATION_RE.sub("<time>", text)
    text = _ADDRESS_RE.sub("0x<addr>", text)
    text = _UUID_RE.sub("<id>", text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("<path>", text)
    text = _POSIX_VOLATILE_PATH_RE.sub("<path>", text)
    text = _SOURCE_LOCATION_RE.sub(r"\1:#", text)
    text = re.sub(r"(<path>):\d+(?::\d+)?", r"\1:#", text)
    return re.sub(r"\s+", " ", text).strip()


def _diagnostic_line(value: object) -> str:
    line = _ANSI_ESCAPE_RE.sub("", str(value or "")).strip()
    return re.sub(r"^E\s+", "", line).strip()


def _clip_text(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3].rstrip() + "..."


def _clip_contract_id(value: str, limit: int) -> str:
    if limit <= 0 or len(value) <= limit:
        return value[:limit]
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    if limit <= len(digest) + 1:
        return digest[:limit]
    prefix = value[: limit - len(digest) - 1].rstrip()
    return f"{prefix}#{digest}"


def _difference_fact(expected: object, actual: object) -> str:
    expected_text = _normalize_volatile_text(expected).strip(" ,")
    actual_text = _normalize_volatile_text(actual).strip(" ,")
    if not expected_text or not actual_text:
        return ""
    if expected_text.casefold() == actual_text.casefold():
        return ""
    return f"expected: {expected_text}; actual: {actual_text}"


def _labeled_difference_facts(lines: list[str]) -> list[tuple[int, int, str]]:
    candidates: list[tuple[int, int, str]] = []
    pending: dict[str, tuple[int, str]] = {}
    for index, raw_line in enumerate(lines):
        line = _diagnostic_line(raw_line)
        inline = _INLINE_EXPECTED_ACTUAL_RE.match(line)
        if not inline:
            inline = _INLINE_ACTUAL_EXPECTED_RE.match(line)
        if inline:
            fact = _difference_fact(inline.group("expected"), inline.group("actual"))
            if fact:
                candidates.append((index, 0, fact))
            pending.clear()
            continue

        label = _LABELED_VALUE_RE.match(line)
        if not label:
            continue
        side = "expected" if label.group("label").casefold() == "expected" else "actual"
        pending[side] = (index, label.group("value"))
        other = "actual" if side == "expected" else "expected"
        if other not in pending:
            continue
        first_index = min(pending[side][0], pending[other][0])
        last_index = max(pending[side][0], pending[other][0])
        if last_index - first_index > 8:
            pending.pop(other, None)
            continue
        fact = _difference_fact(pending["expected"][1], pending["actual"][1])
        if fact:
            candidates.append((last_index, 0, fact))
        pending.clear()
    return candidates


def _node_diff_facts(lines: list[str]) -> list[tuple[int, int, str]]:
    candidates: list[tuple[int, int, str]] = []
    for index, raw_line in enumerate(lines):
        if not _NODE_DIFF_HEADER_RE.match(_diagnostic_line(raw_line)):
            continue
        actual: list[str] = []
        expected: list[str] = []
        for following in lines[index + 1 : index + 17]:
            diff = re.match(r"^\s*([+-])\s?(.*\S)\s*$", following)
            if not diff or diff.group(2).casefold() == "actual - expected":
                continue
            values = actual if diff.group(1) == "+" else expected
            if len(values) < 3:
                values.append(diff.group(2).strip())
        if actual and expected:
            fact = _difference_fact(" | ".join(expected), " | ".join(actual))
            if fact:
                candidates.append((index, 0, fact))
    return candidates


def _comparison_facts(lines: list[str]) -> list[tuple[int, int, str]]:
    candidates: list[tuple[int, int, str]] = []
    for index, raw_line in enumerate(lines):
        line = _diagnostic_line(raw_line)
        assertion = re.match(r"^(?:AssertionError:\s*)?assert\s+(.+)$", line)
        if assertion:
            candidates.append((index, 1, f"assertion: {assertion.group(1).strip()}"))
            continue
        if not line.casefold().startswith("expected values"):
            continue
        for following in lines[index + 1 : index + 7]:
            comparison = _COMPARISON_RE.match(_diagnostic_line(following))
            if comparison:
                fact = _difference_fact(
                    comparison.group("expected"),
                    comparison.group("actual"),
                )
                if fact:
                    candidates.append((index, 0, fact))
                break
    return candidates


def _analyzer_facts(lines: list[str]) -> list[tuple[int, int, str]]:
    candidates: list[tuple[int, int, str]] = []
    for index, raw_line in enumerate(lines):
        line = _diagnostic_line(raw_line)
        machine = line.split("|")
        if (
            len(machine) >= 8
            and machine[0].casefold() in {"error", "warning", "info"}
            and machine[2].strip()
        ):
            code = machine[2].strip()
            message = "|".join(machine[7:]).strip()
            if message:
                candidates.append((index, 2, f"analyzer {code}: {message}"))
            continue

        human = re.split(r"\s+-\s+", line)
        if (
            len(human) >= 4
            and human[0].casefold() in {"error", "warning", "info"}
        ):
            code = human[-1].strip()
            message = " - ".join(part.strip() for part in human[2:-1]).strip()
            if code and message:
                candidates.append((index, 2, f"analyzer {code}: {message}"))
            continue

        compiler = _COMPILER_ERROR_RE.match(line)
        if compiler:
            code = compiler.group("code")
            label = f"compiler {code}" if code else "compiler error"
            candidates.append((index, 2, f"{label}: {compiler.group('message').strip()}"))
    return candidates


def _runtime_error_facts(lines: list[str]) -> list[tuple[int, int, str]]:
    candidates: list[tuple[int, int, str]] = []
    for index, raw_line in enumerate(lines):
        line = _diagnostic_line(raw_line)
        bad_state = re.search(r"\bBad state:\s*(.+)$", line, re.IGNORECASE)
        if bad_state:
            candidates.append((index, 2, f"Bad state: {bad_state.group(1).strip()}"))

        dart_type = _DART_TYPE_ERROR_RE.search(line)
        if dart_type:
            candidates.append((index, 2, f"TypeError: {dart_type.group(0).strip()}"))

        error = _EXCEPTION_RE.search(line)
        if not error:
            continue
        raw_kind = error.group("kind").split(".")[-1]
        kind = _CANONICAL_ERROR_NAMES.get(raw_kind.casefold(), raw_kind)
        message = error.group("message").strip()
        if kind == "AssertionError" and message.casefold().startswith("assert "):
            continue
        candidates.append((index, 2, f"{kind}: {message}"))
    return candidates


def _bounded_facts(
    candidates: list[tuple[int, int, str]],
    *,
    max_facts: int,
    max_fact_chars: int,
) -> list[str]:
    if max_facts <= 0 or max_fact_chars <= 0:
        return []
    facts: list[str] = []
    seen: set[str] = set()
    ordered = sorted(candidates, key=lambda item: (item[1], item[0], item[2]))
    for _index, _priority, candidate in ordered:
        normalized = _normalize_volatile_text(candidate)
        clipped = _clip_text(normalized, max_fact_chars)
        key = _normalize_volatile_text(clipped).casefold()
        if not clipped or not key or key in seen:
            continue
        seen.add(key)
        facts.append(clipped)
        if len(facts) >= max_facts:
            break
    return facts


def _bounded_contract_ids(
    values: object,
    *,
    max_ids: int,
    max_id_chars: int,
) -> list[str]:
    if max_ids <= 0 or max_id_chars <= 0 or not isinstance(values, list):
        return []
    bounded: list[str] = []
    seen: set[str] = set()
    for value in sorted(normalize_contract_id(item) for item in values):
        clipped = _clip_contract_id(value, max_id_chars)
        if not clipped or clipped in seen:
            continue
        seen.add(clipped)
        bounded.append(clipped)
        if len(bounded) >= max_ids:
            break
    return bounded


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


def failure_delta_evidence(
    result: Mapping[str, Any],
    *,
    runner_hint: str = "",
    max_failed_ids: int = MAX_FAILED_CONTRACT_IDS,
    max_facts: int = MAX_FAILURE_FACTS,
    max_contract_id_chars: int = MAX_CONTRACT_ID_CHARS,
    max_fact_chars: int = MAX_FAILURE_FACT_CHARS,
) -> dict[str, list[str]]:
    """Extract bounded stable contracts and causal facts from one validation result."""
    contract_evidence = test_contract_evidence(result, runner_hint=runner_hint)
    lines = "\n".join(
        str(result.get(key) or "") for key in ("output", "stdout", "stderr")
    ).splitlines()
    candidates: list[tuple[int, int, str]] = []
    candidates.extend(_labeled_difference_facts(lines))
    candidates.extend(_node_diff_facts(lines))
    candidates.extend(_comparison_facts(lines))
    candidates.extend(_analyzer_facts(lines))
    candidates.extend(_runtime_error_facts(lines))
    return {
        "failed_ids": _bounded_contract_ids(
            contract_evidence["failed_ids"],
            max_ids=max_failed_ids,
            max_id_chars=max_contract_id_chars,
        ),
        "facts": _bounded_facts(
            candidates,
            max_facts=max_facts,
            max_fact_chars=max_fact_chars,
        ),
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
