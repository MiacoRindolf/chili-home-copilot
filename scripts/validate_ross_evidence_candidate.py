"""Fail-closed validation for Ross evidence candidates.

This validator deliberately does not consume the validator shipped in any
external Ross-oracle package.  Its trust anchor is a locally supplied authority
manifest plus an allowlisted evidence directory.  Candidate assertions never
upgrade their own coverage, ReplayV3, executable-pricing, or implementation
grade.

The ordinary path requires every label to remain byte-semantically equivalent
to the verified authority baseline.  The current authority is transition-frozen
because it contains no independently verifiable sealed capture or NBBO
inventory.  Ed25519 receipt validation remains dormant plumbing for a future,
separately reviewed authority; a signature alone cannot change the current
baseline.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


AUTHORITY_SCHEMA = "chili.ross-evidence-authority.v1"
CANDIDATE_SCHEMA = "chili.ross-evidence-candidate.v1"
GRADER_RECEIPT_SCHEMA = "chili.replay-v3-ross-grader-receipt.v1"
REPLAY_BINDING_SCHEMA = "chili.replay-v3-evidence-binding.v1"
IMPLEMENTATION_GRADES = {
    "CERTIFIABLE",
    "DIAGNOSTIC_ONLY",
    "UNAVAILABLE",
    "UNRESOLVED",
}
EXECUTABLE_PRICING_STATUSES = {
    "VERIFIED",
    "UNAVAILABLE",
    "NOT_APPLICABLE_NO_EXECUTION",
}
EXPECTED_BASELINE_COUNTS = {
    "CERTIFIABLE": 0,
    "DIAGNOSTIC_ONLY": 4,
    "UNAVAILABLE": 2,
    "UNRESOLVED": 6,
}
EXPECTED_AUTHORITY_ID = "ross-merged-baseline-2026-07-15-v1"
# Canonical digest of tests/fixtures/ross_replay/ross_candidate_authority_manifest.json
# including the trusted ReplayV3 grader public key.  Every authority mutation
# needs an explicit code review and digest update.
EXPECTED_AUTHORITY_PAYLOAD_SHA256 = (
    "31f4a8b7fa0163b5f6baf315fdc4ea677c88b23cc54e176d5b9edd4ef328db64"
)
# The reviewed authority contains video-semantic evidence only.  It has no
# independently resolvable sealed capture, ReplayV3 frontier, or NBBO record
# inventory, so a signature cannot promote any label under this authority.
# Enabling transitions requires a separately reviewed authority schema/digest
# whose capture artifacts can be verified here without trusting the candidate
# or receipt to attest their own truth.
CURRENT_AUTHORITY_TRANSITIONS_ENABLED = False

MAX_AUTHORITY_JSON_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_JSON_BYTES = 8 * 1024 * 1024
MAX_GRADER_RECEIPT_JSON_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_FILE_BYTES = 64 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
CANONICAL_BASELINE_GRADES = {
    "P5qdiBNct1c::ZDAI::second_pullback": "DIAGNOSTIC_ONLY",
    "P5qdiBNct1c::SDOT::vwap_reclaim": "UNRESOLVED",
    "P5qdiBNct1c::SDOT::opening_rejection": "UNRESOLVED",
    "550XNdh4y5k::SILO::dip_break": "UNAVAILABLE",
    "550XNdh4y5k::SILO::vwap_rejection": "UNRESOLVED",
    "550XNdh4y5k::CLRO::double_top_flush": "DIAGNOSTIC_ONLY",
    "S2sOq-stPgA::QTTB::veto": "DIAGNOSTIC_ONLY",
    "S2sOq-stPgA::PLSM::first_dip": "UNAVAILABLE",
    "S2sOq-stPgA::PLSM::backside": "DIAGNOSTIC_ONLY",
    "S2sOq-stPgA::VEEE::pullback": "UNRESOLVED",
    "ChLgwLS9eJY::NXTC::breakout": "UNRESOLVED",
    "ChLgwLS9eJY::UBXG::vwap_bounce": "UNRESOLVED",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
WINDOW_KEYS = {"start", "end", "timezone"}
LABEL_KEYS = {
    "canonical_id",
    "semantic_support",
    "recorded_data_coverage",
    "executable_pricing",
    "implementation_grade",
    "event_window",
    "phase_window",
    "warmup_window",
    "coverage_windows",
    "citations",
    "replay_binding",
}


class CandidateValidationError(ValueError):
    """A stable, fail-closed validation failure."""

    def __init__(self, code: str, message: str, *, path: str = "$") -> None:
        super().__init__(f"{code} at {path}: {message}")
        self.code = code
        self.path = path
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class ValidationReport:
    authority_id: str
    authority_payload_sha256: str
    candidate_sha256: str
    label_count: int
    grade_counts: Mapping[str, int]
    transition_receipt_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            # Deliberately avoid the generic word ``VALID`` here.  This result is
            # only a frozen taxonomy/citation match; it is not a ReplayV3 grade,
            # certification receipt, or activation gate.
            "status": "BASELINE_POLICY_MATCH",
            "certification_eligible": False,
            "gate_authority": False,
            "scoreable_ross_phases": 0,
            "authority_id": self.authority_id,
            "authority_payload_sha256": self.authority_payload_sha256,
            "candidate_sha256": self.candidate_sha256,
            "label_count": self.label_count,
            "grade_counts": dict(self.grade_counts),
            "transition_receipt_sha256": self.transition_receipt_sha256,
            "claims": {
                "profitability": False,
                "ross_parity": False,
                "broker_readiness": False,
            },
        }


def _reject_constants(raw: str) -> None:
    raise CandidateValidationError(
        "JSON_NONFINITE",
        f"non-finite JSON number {raw!r} is forbidden",
    )


def _pairs_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CandidateValidationError(
                "JSON_DUPLICATE_KEY",
                f"duplicate object key {key!r}",
            )
        result[key] = value
    return result


def _load_json_file(
    path: Path,
    *,
    role: str,
    max_bytes: int,
) -> tuple[dict[str, Any], bytes]:
    checked = _validated_local_regular_file(path, role=role, max_bytes=max_bytes)
    raw = _read_bounded_regular_file(
        checked,
        role=role,
        max_bytes=max_bytes,
        error_prefix="INPUT",
    )
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constants,
        )
    except CandidateValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError(
            "JSON_INVALID", f"invalid {role} JSON: {exc}", path=str(path)
        ) from exc
    if not isinstance(value, dict):
        raise CandidateValidationError(
            "JSON_ROOT_TYPE", f"{role} root must be an object", path=str(path)
        )
    return value, raw


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return _sha256(_canonical_bytes(value))


def _expect_object(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CandidateValidationError("TYPE_OBJECT", "must be an object", path=path)
    return value


def _expect_list(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise CandidateValidationError("TYPE_ARRAY", "must be an array", path=path)
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise CandidateValidationError(
            "SCHEMA_KEYS",
            f"exact keys required; missing={missing}, extra={extra}",
            path=path,
        )


def _hex64(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise CandidateValidationError(
            "SHA256_FORMAT", "must be a lowercase 64-character SHA-256", path=path
        )
    return value


def _authority_payload(authority: Mapping[str, Any]) -> dict[str, Any]:
    """Return the complete stable trust payload."""

    return dict(authority)


def authority_payload_sha256(authority: Mapping[str, Any]) -> str:
    return _canonical_sha256(_authority_payload(authority))


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _windows_drive_type(anchor: str) -> int:
    import ctypes

    return int(ctypes.windll.kernel32.GetDriveTypeW(anchor))


def _lexically_local_absolute_path(
    path: Path,
    *,
    role: str,
    network_code: str,
    invalid_code: str,
) -> Path:
    """Reject network/device paths before touching filesystem metadata."""

    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise CandidateValidationError(
            invalid_code,
            f"{role} path must be a non-empty local filesystem path",
            path=str(raw),
        )
    if raw.startswith(("\\\\", "//")):
        raise CandidateValidationError(
            network_code,
            f"UNC/device {role} paths are forbidden",
            path=raw,
        )
    absolute = Path(os.path.abspath(raw))
    absolute_raw = str(absolute)
    if absolute_raw.startswith(("\\\\", "//")):
        raise CandidateValidationError(
            network_code,
            f"UNC/device {role} paths are forbidden",
            path=raw,
        )
    if os.name == "nt":
        anchor = absolute.anchor
        if not anchor or anchor.startswith(("\\\\", "//")):
            raise CandidateValidationError(
                network_code,
                f"{role} must be on a local fixed drive",
                path=raw,
            )
        # This check deliberately precedes lstat/resolve/open.  A mapped drive
        # can otherwise cause network I/O merely while trying to reject it.
        drive_type = _windows_drive_type(anchor)
        if drive_type != 3:  # DRIVE_FIXED
            raise CandidateValidationError(
                network_code,
                f"{role} must be on a local fixed drive; drive_type={drive_type}",
                path=raw,
            )
        relative_text = absolute_raw[len(anchor) :]
        if ":" in relative_text:
            raise CandidateValidationError(
                invalid_code,
                f"alternate data streams are forbidden for {role}",
                path=raw,
            )
    return absolute


def _lstat_no_reparse_components(
    absolute: Path,
    *,
    role: str,
    unavailable_code: str,
    reparse_code: str,
) -> os.stat_result:
    """Walk root-to-leaf without following any symlink/reparse component."""

    parts = absolute.parts
    if not parts or not absolute.anchor:
        raise CandidateValidationError(
            unavailable_code,
            f"{role} path is not absolute",
            path=str(absolute),
        )
    current = Path(parts[0])
    final_stat: os.stat_result | None = None
    for index in range(len(parts)):
        if index:
            current = current / parts[index]
        try:
            item_lstat = current.lstat()
        except OSError as exc:
            raise CandidateValidationError(
                unavailable_code,
                f"cannot stat {role} component {current}: {exc}",
                path=str(absolute),
            ) from exc
        if stat.S_ISLNK(item_lstat.st_mode) or _is_reparse(item_lstat):
            raise CandidateValidationError(
                reparse_code,
                f"symlink/reparse components are forbidden for {role}: {current}",
                path=str(absolute),
            )
        final_stat = item_lstat
    if final_stat is None:
        raise CandidateValidationError(
            unavailable_code,
            f"{role} has no filesystem components",
            path=str(absolute),
        )
    return final_stat


def _validated_local_regular_file(
    path: Path,
    *,
    role: str,
    max_bytes: int,
) -> Path:
    absolute = _lexically_local_absolute_path(
        path,
        role=role,
        network_code="INPUT_NETWORK",
        invalid_code="INPUT_PATH",
    )
    final_stat = _lstat_no_reparse_components(
        absolute,
        role=role,
        unavailable_code="INPUT_UNREADABLE",
        reparse_code="INPUT_REPARSE",
    )
    if not stat.S_ISREG(final_stat.st_mode):
        raise CandidateValidationError(
            "INPUT_NOT_REGULAR",
            f"{role} must be a regular file",
            path=str(absolute),
        )
    if final_stat.st_size > max_bytes:
        raise CandidateValidationError(
            "INPUT_TOO_LARGE",
            f"{role} exceeds the {max_bytes}-byte input limit",
            path=str(absolute),
        )
    return absolute


def _resolved_local_evidence_root(root: Path) -> Path:
    absolute = _lexically_local_absolute_path(
        root,
        role="evidence root",
        network_code="EVIDENCE_ROOT_NETWORK",
        invalid_code="EVIDENCE_ROOT",
    )
    root_lstat = _lstat_no_reparse_components(
        absolute,
        role="evidence root",
        unavailable_code="EVIDENCE_ROOT",
        reparse_code="EVIDENCE_ROOT",
    )
    if not stat.S_ISDIR(root_lstat.st_mode):
        raise CandidateValidationError(
            "EVIDENCE_ROOT",
            "evidence root must be a real non-reparse directory",
            path=str(absolute),
        )
    return absolute


def _safe_relative_path(raw: Any, *, path: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw:
        raise CandidateValidationError("CITATION_PATH", "path must be non-empty", path=path)
    if "\x00" in raw or "\\" in raw or ":" in raw:
        raise CandidateValidationError(
            "CITATION_PATH", "backslash, drive, URI, and UNC paths are forbidden", path=path
        )
    rel = PurePosixPath(raw)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
        raise CandidateValidationError(
            "CITATION_PATH", "path must be normalized and relative", path=path
        )
    return rel


def _safe_evidence_file(root: Path, raw_path: Any, *, path: str) -> Path:
    rel = _safe_relative_path(raw_path, path=path)
    resolved_root = _resolved_local_evidence_root(root)
    current = resolved_root.joinpath(*rel.parts)
    try:
        current.relative_to(resolved_root)
    except ValueError as exc:
        raise CandidateValidationError(
            "CITATION_ESCAPE", "citation escapes the allowlisted evidence root", path=path
        ) from exc
    before = _lstat_no_reparse_components(
        current,
        role="citation",
        unavailable_code="CITATION_MISSING",
        reparse_code="CITATION_REPARSE",
    )
    if not stat.S_ISREG(before.st_mode):
        raise CandidateValidationError(
            "CITATION_NOT_REGULAR", "citation must resolve to a regular file", path=path
        )
    return current


def _read_bounded_regular_file(
    target: Path,
    *,
    role: str,
    max_bytes: int,
    error_prefix: str,
    expected_size: int | None = None,
) -> bytes:
    """Stream a regular file within a hard bound and detect path swaps."""

    unavailable_code = (
        "CITATION_MISSING"
        if error_prefix == "CITATION"
        else f"{error_prefix}_UNREADABLE"
    )
    before = _lstat_no_reparse_components(
        target,
        role=role,
        unavailable_code=unavailable_code,
        reparse_code=f"{error_prefix}_REPARSE",
    )
    if not stat.S_ISREG(before.st_mode):
        raise CandidateValidationError(
            f"{error_prefix}_NOT_REGULAR",
            f"{role} is not a sealed regular file",
            path=str(target),
        )
    if expected_size is not None and before.st_size != expected_size:
        raise CandidateValidationError(
            f"{error_prefix}_SIZE",
            f"{role} size differs from the sealed size before read",
            path=str(target),
        )
    if before.st_size > max_bytes:
        raise CandidateValidationError(
            f"{error_prefix}_TOO_LARGE",
            f"{role} exceeds the {max_bytes}-byte read limit",
            path=str(target),
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise CandidateValidationError(
            f"{error_prefix}_OPEN",
            f"cannot open {role} without following links: {exc}",
            path=str(target),
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise CandidateValidationError(
                f"{error_prefix}_NOT_REGULAR",
                f"opened {role} is not a regular file",
                path=str(target),
            )
        if expected_size is not None and opened.st_size != expected_size:
            raise CandidateValidationError(
                f"{error_prefix}_SIZE",
                f"opened {role} size differs from the sealed size",
                path=str(target),
            )
        if opened.st_size > max_bytes:
            raise CandidateValidationError(
                f"{error_prefix}_TOO_LARGE",
                f"opened {role} exceeds the {max_bytes}-byte read limit",
                path=str(target),
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise CandidateValidationError(
                    f"{error_prefix}_TOO_LARGE",
                    f"{role} grew beyond the {max_bytes}-byte read limit",
                    path=str(target),
                )
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    after = _lstat_no_reparse_components(
        target,
        role=role,
        unavailable_code=unavailable_code,
        reparse_code=f"{error_prefix}_REPARSE",
    )
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_opened = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_opened or identity_opened != identity_after:
        raise CandidateValidationError(
            f"{error_prefix}_CHANGED",
            f"{role} identity changed during validation",
            path=str(target),
        )
    if expected_size is not None and len(raw) != expected_size:
        raise CandidateValidationError(
            f"{error_prefix}_SIZE",
            f"{role} byte count differs from the sealed size",
            path=str(target),
        )
    return raw


def _read_regular_file_no_reparse(
    target: Path,
    *,
    path: str,
    expected_size: int,
) -> bytes:
    return _read_bounded_regular_file(
        target,
        role="citation",
        max_bytes=MAX_EVIDENCE_FILE_BYTES,
        error_prefix="CITATION",
        expected_size=expected_size,
    )


def _json_pointer(document: Any, pointer: str, *, path: str) -> Any:
    if pointer == "":
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise CandidateValidationError(
            "JSON_POINTER", "JSON pointer must be empty or start with '/'", path=path
        )
    current = document
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise CandidateValidationError(
                "JSON_POINTER", "invalid RFC 6901 escape", path=path
            )
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise CandidateValidationError(
                    "JSON_POINTER", "array token must be a canonical index", path=path
                )
            index = int(token)
            if index >= len(current):
                raise CandidateValidationError(
                    "JSON_POINTER_MISSING", "array index does not exist", path=path
                )
            current = current[index]
        elif isinstance(current, dict):
            if token not in current:
                raise CandidateValidationError(
                    "JSON_POINTER_MISSING", f"object key {token!r} does not exist", path=path
                )
            current = current[token]
        else:
            raise CandidateValidationError(
                "JSON_POINTER_MISSING", "pointer traverses a scalar", path=path
            )
    return current


def _parse_json_bytes(raw: bytes, *, path: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constants,
        )
    except CandidateValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateValidationError(
            "CITATION_JSON_INVALID", f"citation is not valid JSON: {exc}", path=path
        ) from exc


def _evidence_inventory(authority: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = _expect_list(authority.get("evidence_files"), path="$.authority.evidence_files")
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw_row in enumerate(rows):
        row_path = f"$.authority.evidence_files[{index}]"
        row = _expect_object(raw_row, path=row_path)
        _exact_keys(
            row,
            {"path", "size_bytes", "sha256", "json_targets", "line_targets"},
            path=row_path,
        )
        rel = _safe_relative_path(row["path"], path=f"{row_path}.path").as_posix()
        if rel in result:
            raise CandidateValidationError(
                "AUTHORITY_DUPLICATE_EVIDENCE", f"duplicate evidence path {rel!r}", path=row_path
            )
        if (
            not isinstance(row["size_bytes"], int)
            or isinstance(row["size_bytes"], bool)
            or row["size_bytes"] < 0
            or row["size_bytes"] > MAX_EVIDENCE_FILE_BYTES
        ):
            raise CandidateValidationError(
                "AUTHORITY_EVIDENCE_SIZE",
                f"size_bytes must be an integer from 0 through {MAX_EVIDENCE_FILE_BYTES}",
                path=row_path,
            )
        _hex64(row["sha256"], path=f"{row_path}.sha256")
        _expect_object(row["json_targets"], path=f"{row_path}.json_targets")
        _expect_object(row["line_targets"], path=f"{row_path}.line_targets")
        result[rel] = row
    return result


def _verify_citation(
    citation: Mapping[str, Any],
    *,
    evidence_root: Path,
    inventory: Mapping[str, Mapping[str, Any]],
    path: str,
) -> tuple[str, str, str]:
    common = {"path", "size_bytes", "sha256"}
    json_keys = common | {"json_pointer", "target_sha256"}
    line_keys = common | {"line_start", "line_end", "target_sha256"}
    actual_keys = frozenset(citation)
    if actual_keys not in {frozenset(json_keys), frozenset(line_keys)}:
        raise CandidateValidationError(
            "CITATION_SCHEMA",
            "citation must bind exactly one JSON pointer or exact line range",
            path=path,
        )
    rel = _safe_relative_path(citation["path"], path=f"{path}.path").as_posix()
    authority_file = inventory.get(rel)
    if authority_file is None:
        raise CandidateValidationError(
            "CITATION_NOT_ALLOWLISTED", "file is absent from authority inventory", path=path
        )
    if (
        not isinstance(citation["size_bytes"], int)
        or isinstance(citation["size_bytes"], bool)
        or citation["size_bytes"] < 0
        or citation["size_bytes"] > MAX_EVIDENCE_FILE_BYTES
    ):
        raise CandidateValidationError(
            "CITATION_SIZE",
            f"citation size must be an integer from 0 through {MAX_EVIDENCE_FILE_BYTES}",
            path=path,
        )
    if citation["size_bytes"] != authority_file["size_bytes"]:
        raise CandidateValidationError(
            "CITATION_SIZE", "declared size differs from authority inventory", path=path
        )
    digest = _hex64(citation["sha256"], path=f"{path}.sha256")
    if digest != authority_file["sha256"]:
        raise CandidateValidationError(
            "CITATION_SHA256", "declared digest differs from authority inventory", path=path
        )
    target_digest = _hex64(citation["target_sha256"], path=f"{path}.target_sha256")
    target = _safe_evidence_file(evidence_root, rel, path=path)
    raw = _read_regular_file_no_reparse(
        target,
        path=path,
        expected_size=citation["size_bytes"],
    )
    if _sha256(raw) != digest:
        raise CandidateValidationError(
            "CITATION_SHA256", "actual file digest differs from sealed digest", path=path
        )
    if "json_pointer" in citation:
        pointer = citation["json_pointer"]
        allowed_digest = authority_file["json_targets"].get(pointer)
        if allowed_digest != target_digest:
            raise CandidateValidationError(
                "CITATION_TARGET_NOT_ALLOWLISTED",
                "JSON pointer/digest pair is absent from authority inventory",
                path=path,
            )
        pointed = _json_pointer(_parse_json_bytes(raw, path=path), pointer, path=path)
        actual_target_digest = _canonical_sha256(pointed)
        target_key = f"json:{pointer}"
    else:
        start = citation["line_start"]
        end = citation["line_end"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 1
            or end < start
        ):
            raise CandidateValidationError(
                "CITATION_LINES", "line range must be positive and ordered", path=path
            )
        range_key = f"{start}:{end}"
        allowed_digest = authority_file["line_targets"].get(range_key)
        if allowed_digest != target_digest:
            raise CandidateValidationError(
                "CITATION_TARGET_NOT_ALLOWLISTED",
                "line range/digest pair is absent from authority inventory",
                path=path,
            )
        lines = raw.splitlines(keepends=True)
        if end > len(lines):
            raise CandidateValidationError(
                "CITATION_LINES", "line range exceeds the file", path=path
            )
        actual_target_digest = _sha256(b"".join(lines[start - 1 : end]))
        target_key = f"line:{range_key}"
    if actual_target_digest != target_digest:
        raise CandidateValidationError(
            "CITATION_TARGET_SHA256", "target content digest does not match", path=path
        )
    return rel, target_key, target_digest


def _parse_timestamp(value: Any, *, path: str) -> datetime:
    if not isinstance(value, str):
        raise CandidateValidationError("TIMESTAMP_TYPE", "must be an ISO timestamp", path=path)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CandidateValidationError("TIMESTAMP_FORMAT", "invalid ISO timestamp", path=path) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CandidateValidationError("TIMESTAMP_ZONE", "timestamp must include an offset", path=path)
    return parsed


def _validate_window(value: Any, *, path: str) -> None:
    if value is None:
        return
    window = _expect_object(value, path=path)
    _exact_keys(window, WINDOW_KEYS, path=path)
    timezone_name = window["timezone"]
    if not isinstance(timezone_name, str):
        raise CandidateValidationError("TIMEZONE", "timezone must be an IANA name", path=path)
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CandidateValidationError("TIMEZONE", "unknown IANA timezone", path=path) from exc
    start = _parse_timestamp(window["start"], path=f"{path}.start")
    end = _parse_timestamp(window["end"], path=f"{path}.end")
    if start > end:
        raise CandidateValidationError("WINDOW_ORDER", "window start is after end", path=path)
    for timestamp, key in ((start, "start"), (end, "end")):
        local = timestamp.astimezone(zone)
        if timestamp.utcoffset() != local.utcoffset():
            raise CandidateValidationError(
                "TIMEZONE_OFFSET",
                "timestamp offset is inconsistent with the named timezone",
                path=f"{path}.{key}",
            )


def _validate_coverage_windows(value: Any, *, path: str) -> list[Mapping[str, Any]]:
    rows = _expect_list(value, path=path)
    result: list[Mapping[str, Any]] = []
    expected = WINDOW_KEYS | {
        "stream",
        "capture_manifest_sha256",
        "query_sha256",
        "source_frontier_sha256",
    }
    for index, raw in enumerate(rows):
        item_path = f"{path}[{index}]"
        row = _expect_object(raw, path=item_path)
        _exact_keys(row, expected, path=item_path)
        _validate_window({key: row[key] for key in WINDOW_KEYS}, path=item_path)
        if not isinstance(row["stream"], str) or not row["stream"]:
            raise CandidateValidationError("COVERAGE_STREAM", "stream must be non-empty", path=item_path)
        for key in ("capture_manifest_sha256", "query_sha256", "source_frontier_sha256"):
            _hex64(row[key], path=f"{item_path}.{key}")
        result.append(row)
    return result


def _validate_replay_binding(value: Any, *, path: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    binding = _expect_object(value, path=path)
    expected = {
        "schema_version",
        "capture_manifest_sha256",
        "query_sha256",
        "source_frontier_sha256",
        "coverage_grade_receipt_sha256",
    }
    _exact_keys(binding, expected, path=path)
    if binding["schema_version"] != REPLAY_BINDING_SCHEMA:
        raise CandidateValidationError("REPLAY_BINDING_SCHEMA", "unsupported binding schema", path=path)
    for key in expected - {"schema_version"}:
        _hex64(binding[key], path=f"{path}.{key}")
    return binding


def _validate_price_leg(value: Any, *, path: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    leg = _expect_object(value, path=path)
    expected = {
        "timestamp",
        "price",
        "source_stream",
        "record_sha256",
        "query_sha256",
        "source_frontier_sha256",
    }
    _exact_keys(leg, expected, path=path)
    _parse_timestamp(leg["timestamp"], path=f"{path}.timestamp")
    price = leg["price"]
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        raise CandidateValidationError("PRICE", "price must be a finite positive number", path=path)
    if not math.isfinite(float(price)) or float(price) <= 0:
        raise CandidateValidationError("PRICE", "price must be a finite positive number", path=path)
    if leg["source_stream"] != "NBBO":
        raise CandidateValidationError(
            "EXECUTABLE_SOURCE", "only contemporaneous NBBO is executable", path=path
        )
    for key in ("record_sha256", "query_sha256", "source_frontier_sha256"):
        _hex64(leg[key], path=f"{path}.{key}")
    return leg


def _validate_executable_pricing(
    value: Any,
    *,
    coverage_windows: Sequence[Mapping[str, Any]],
    path: str,
) -> Mapping[str, Any]:
    pricing = _expect_object(value, path=path)
    _exact_keys(pricing, {"status", "ask_entry", "bid_exit"}, path=path)
    if (
        not isinstance(pricing["status"], str)
        or pricing["status"] not in EXECUTABLE_PRICING_STATUSES
    ):
        raise CandidateValidationError(
            "PRICING_STATUS",
            f"status must be one of {sorted(EXECUTABLE_PRICING_STATUSES)}",
            path=path,
        )
    ask = _validate_price_leg(pricing["ask_entry"], path=f"{path}.ask_entry")
    bid = _validate_price_leg(pricing["bid_exit"], path=f"{path}.bid_exit")
    if (ask is None) != (bid is None):
        raise CandidateValidationError(
            "EXECUTABLE_LEGS", "ask-entry and bid-exit must be supplied together", path=path
        )
    if ask is None:
        if pricing["status"] == "VERIFIED":
            raise CandidateValidationError(
                "EXECUTABLE_LEGS", "VERIFIED pricing requires both NBBO legs", path=path
            )
        return pricing
    if pricing["status"] != "VERIFIED":
        raise CandidateValidationError(
            "PRICING_STATUS", "NBBO legs require VERIFIED status", path=path
        )
    ask_at = _parse_timestamp(ask["timestamp"], path=f"{path}.ask_entry.timestamp")
    bid_at = _parse_timestamp(bid["timestamp"], path=f"{path}.bid_exit.timestamp")
    if ask_at > bid_at:
        raise CandidateValidationError(
            "EXECUTABLE_ORDER", "ask-entry occurs after bid-exit", path=path
        )
    for leg_name, leg, timestamp in (("ask_entry", ask, ask_at), ("bid_exit", bid, bid_at)):
        matching = [
            window
            for window in coverage_windows
            if window["stream"] == "NBBO"
            and _parse_timestamp(window["start"], path=path) <= timestamp
            and timestamp <= _parse_timestamp(window["end"], path=path)
            and window["query_sha256"] == leg["query_sha256"]
            and window["source_frontier_sha256"] == leg["source_frontier_sha256"]
        ]
        if not matching:
            raise CandidateValidationError(
                "NBBO_OUTSIDE_COVERAGE",
                f"{leg_name} is not bound to an authoritative NBBO coverage window/query/frontier",
                path=f"{path}.{leg_name}",
            )
    return pricing


def _walk_scalars(value: Any, path: str = "$") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_scalars(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_scalars(child, f"{path}[{index}]")
    else:
        yield path, value


def _decimal_equal(value: Any, protected: str) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value)) == Decimal(protected)
        except InvalidOperation:
            return False
    if isinstance(value, str):
        for token in re.findall(r"(?<![0-9.])[+-]?[0-9]+(?:\.[0-9]+)?(?![0-9.])", value):
            try:
                if Decimal(token) == Decimal(protected):
                    return True
            except InvalidOperation:
                continue
    return False


def _protected_match(value: Any, fact: Mapping[str, Any]) -> bool:
    kind = fact.get("kind")
    protected = fact.get("value")
    if not isinstance(protected, str):
        return False
    if kind == "number":
        return _decimal_equal(value, protected)
    if kind == "time_hhmm" and isinstance(value, str):
        return re.search(rf"(?<![0-9]){re.escape(protected)}(?::[0-9]{{2}})?(?![0-9])", value) is not None
    if kind == "text" and isinstance(value, str):
        return protected.casefold() in value.casefold()
    return False


def _reject_protected_aliases(
    label: Mapping[str, Any],
    protected_facts: Sequence[Any],
    *,
    path: str,
) -> None:
    canonical_id = label["canonical_id"]
    for index, raw_fact in enumerate(protected_facts):
        fact = _expect_object(raw_fact, path=f"$.authority.protected_facts[{index}]")
        _exact_keys(
            fact,
            {"fact_id", "label_contains", "kind", "value", "allowed_candidate_paths"},
            path=f"$.authority.protected_facts[{index}]",
        )
        selector = fact["label_contains"]
        if not isinstance(selector, str) or selector not in canonical_id:
            continue
        allowed = _expect_list(
            fact["allowed_candidate_paths"],
            path=f"$.authority.protected_facts[{index}].allowed_candidate_paths",
        )
        for scalar_path, scalar in _walk_scalars(label, path):
            if scalar_path in allowed:
                continue
            if _protected_match(scalar, fact):
                raise CandidateValidationError(
                    "PROTECTED_FACT_ALIAS",
                    f"protected fact {fact['fact_id']!r} cannot be used as candidate evidence",
                    path=scalar_path,
                )


def _label_index(rows: Any, *, role: str) -> dict[str, Mapping[str, Any]]:
    labels = _expect_list(rows, path=f"$.{role}.labels")
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw_label in enumerate(labels):
        path = f"$.{role}.labels[{index}]"
        label = _expect_object(raw_label, path=path)
        canonical_id = label.get("canonical_id")
        if not isinstance(canonical_id, str) or not canonical_id:
            raise CandidateValidationError("LABEL_ID", "canonical_id must be non-empty", path=path)
        if canonical_id in result:
            raise CandidateValidationError("LABEL_DUPLICATE", "duplicate canonical_id", path=path)
        result[canonical_id] = label
    return result


def _validate_authority(authority: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    expected_keys = {
        "schema_version",
        "authority_id",
        "baseline_grade_counts",
        "labels",
        "evidence_files",
        "protected_facts",
        "trusted_grader",
        "disclaimer",
    }
    _exact_keys(authority, expected_keys, path="$.authority")
    if authority["schema_version"] != AUTHORITY_SCHEMA:
        raise CandidateValidationError("AUTHORITY_SCHEMA", "unsupported authority schema")
    if authority["authority_id"] != EXPECTED_AUTHORITY_ID:
        raise CandidateValidationError(
            "AUTHORITY_ID", f"authority_id must be {EXPECTED_AUTHORITY_ID!r}"
        )
    if authority["baseline_grade_counts"] != EXPECTED_BASELINE_COUNTS:
        raise CandidateValidationError(
            "BASELINE_COUNTS",
            f"authority baseline must be exactly {EXPECTED_BASELINE_COUNTS}",
            path="$.authority.baseline_grade_counts",
        )
    labels = _label_index(authority["labels"], role="authority")
    observed_mapping = {
        canonical_id: label.get("implementation_grade")
        for canonical_id, label in labels.items()
    }
    if observed_mapping != CANONICAL_BASELINE_GRADES:
        raise CandidateValidationError(
            "CANONICAL_BASELINE",
            "authority label identities/grades differ from the verified canonical baseline",
            path="$.authority.labels",
        )
    counts = Counter(label.get("implementation_grade") for label in labels.values())
    observed = {grade: counts.get(grade, 0) for grade in IMPLEMENTATION_GRADES}
    if observed != EXPECTED_BASELINE_COUNTS or len(labels) != 12:
        raise CandidateValidationError(
            "BASELINE_LABELS",
            f"authority labels must preserve 12-label baseline {EXPECTED_BASELINE_COUNTS}; got {observed}",
            path="$.authority.labels",
        )
    for canonical_id, label in labels.items():
        expected = LABEL_KEYS - {"citations", "replay_binding"} | {"citation_targets"}
        _exact_keys(label, expected, path=f"$.authority.labels[{canonical_id!r}]")
        if label["implementation_grade"] not in IMPLEMENTATION_GRADES:
            raise CandidateValidationError("GRADE", "unknown implementation grade")
        for window_name in ("event_window", "phase_window", "warmup_window"):
            _validate_window(label[window_name], path=f"$.authority.labels[{canonical_id!r}].{window_name}")
        _validate_coverage_windows(
            label["coverage_windows"],
            path=f"$.authority.labels[{canonical_id!r}].coverage_windows",
        )
    inventory = _evidence_inventory(authority)
    _expect_list(authority["protected_facts"], path="$.authority.protected_facts")
    trusted_grader = _expect_object(
        authority["trusted_grader"], path="$.authority.trusted_grader"
    )
    _exact_keys(
        trusted_grader,
        {"grader_id", "ed25519_public_key_base64"},
        path="$.authority.trusted_grader",
    )
    if not isinstance(trusted_grader["grader_id"], str) or not trusted_grader["grader_id"]:
        raise CandidateValidationError(
            "GRADER_ID", "grader_id must be non-empty", path="$.authority.trusted_grader"
        )
    try:
        public_key = base64.b64decode(
            trusted_grader["ed25519_public_key_base64"], validate=True
        )
    except (TypeError, ValueError, binascii.Error) as exc:
        raise CandidateValidationError(
            "GRADER_PUBLIC_KEY",
            "trusted grader public key must be canonical base64",
            path="$.authority.trusted_grader",
        ) from exc
    if len(public_key) != 32:
        raise CandidateValidationError(
            "GRADER_PUBLIC_KEY",
            "trusted Ed25519 public key must contain 32 bytes",
            path="$.authority.trusted_grader",
        )
    return labels, inventory


def _validate_candidate_label_shape(label: Mapping[str, Any], *, path: str) -> None:
    _exact_keys(label, LABEL_KEYS, path=path)
    if label["implementation_grade"] not in IMPLEMENTATION_GRADES:
        raise CandidateValidationError("GRADE", "unknown implementation grade", path=path)
    for window_name in ("event_window", "phase_window", "warmup_window"):
        _validate_window(label[window_name], path=f"{path}.{window_name}")
    coverage = _validate_coverage_windows(label["coverage_windows"], path=f"{path}.coverage_windows")
    _validate_executable_pricing(
        label["executable_pricing"], coverage_windows=coverage, path=f"{path}.executable_pricing"
    )
    _validate_replay_binding(label["replay_binding"], path=f"{path}.replay_binding")
    _expect_list(label["citations"], path=f"{path}.citations")


def _bound_fields_equal(candidate: Mapping[str, Any], authority: Mapping[str, Any]) -> bool:
    for key in LABEL_KEYS - {"citations", "replay_binding"}:
        if candidate[key] != authority[key]:
            return False
    if candidate["replay_binding"] is not None:
        return False
    return True


def _require_transition_authority() -> None:
    if not CURRENT_AUTHORITY_TRANSITIONS_ENABLED:
        raise CandidateValidationError(
            "AUTHORITY_TRANSITIONS_FROZEN",
            "current authority has no independently verifiable sealed capture/NBBO artifacts; "
            "signed transitions are disabled",
            path="$.authority",
        )


def _validate_receipt(
    receipt_path: Path,
    *,
    authority: Mapping[str, Any],
    authority_payload_digest: str,
    candidate_raw: bytes,
    changed: Mapping[str, Mapping[str, Any]],
) -> tuple[Mapping[str, Any], str]:
    _require_transition_authority()
    receipt, receipt_raw = _load_json_file(
        receipt_path,
        role="grader receipt",
        max_bytes=MAX_GRADER_RECEIPT_JSON_BYTES,
    )
    receipt_digest = _sha256(receipt_raw)
    expected = {
        "schema_version",
        "receipt_id",
        "grader_id",
        "status",
        "authority_payload_sha256",
        "candidate_sha256",
        "capture_binding",
        "transitions",
        "signature_ed25519",
    }
    _exact_keys(receipt, expected, path="$.receipt")
    if receipt["schema_version"] != GRADER_RECEIPT_SCHEMA or receipt["status"] != "PASS":
        raise CandidateValidationError(
            "RECEIPT_STATUS", "receipt must be a PASS from the supported ReplayV3 grader"
        )
    trusted_grader = _expect_object(
        authority["trusted_grader"], path="$.authority.trusted_grader"
    )
    if receipt["grader_id"] != trusted_grader["grader_id"]:
        raise CandidateValidationError("RECEIPT_GRADER", "grader identity differs from trust entry")
    if receipt["authority_payload_sha256"] != authority_payload_digest:
        raise CandidateValidationError("RECEIPT_AUTHORITY", "receipt binds a different authority")
    if receipt["candidate_sha256"] != _sha256(candidate_raw):
        raise CandidateValidationError("RECEIPT_CANDIDATE", "receipt does not bind candidate bytes")
    signature_raw = receipt["signature_ed25519"]
    try:
        signature = base64.b64decode(signature_raw, validate=True)
        public_key_raw = base64.b64decode(
            trusted_grader["ed25519_public_key_base64"], validate=True
        )
    except (TypeError, ValueError, binascii.Error) as exc:
        raise CandidateValidationError(
            "RECEIPT_SIGNATURE", "receipt signature is not canonical base64"
        ) from exc
    if len(signature) != 64:
        raise CandidateValidationError(
            "RECEIPT_SIGNATURE", "Ed25519 receipt signature must contain 64 bytes"
        )
    signed_payload = {key: value for key, value in receipt.items() if key != "signature_ed25519"}
    try:
        Ed25519PublicKey.from_public_bytes(public_key_raw).verify(
            signature, _canonical_bytes(signed_payload)
        )
    except (InvalidSignature, ValueError) as exc:
        raise CandidateValidationError(
            "RECEIPT_SIGNATURE", "receipt is not signed by the sealed ReplayV3 grader"
        ) from exc
    binding = _validate_replay_binding(receipt["capture_binding"], path="$.receipt.capture_binding")
    if binding is None:
        raise CandidateValidationError(
            "RECEIPT_CAPTURE_BINDING",
            "receipt capture binding must be a non-null sealed ReplayV3 binding",
            path="$.receipt.capture_binding",
        )
    transitions = _expect_list(receipt["transitions"], path="$.receipt.transitions")
    receipt_transitions: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(transitions):
        path = f"$.receipt.transitions[{index}]"
        row = _expect_object(raw, path=path)
        _exact_keys(
            row,
            {
                "canonical_id",
                "from_grade",
                "to_grade",
                "candidate_label_sha256",
                "event_window_sha256",
                "phase_window_sha256",
                "warmup_window_sha256",
                "coverage_windows_sha256",
                "executable_pricing_sha256",
            },
            path=path,
        )
        canonical_id = row["canonical_id"]
        if canonical_id in receipt_transitions:
            raise CandidateValidationError("RECEIPT_TRANSITION_DUPLICATE", "duplicate transition", path=path)
        _hex64(row["candidate_label_sha256"], path=f"{path}.candidate_label_sha256")
        for digest_key in (
            "event_window_sha256",
            "phase_window_sha256",
            "warmup_window_sha256",
            "coverage_windows_sha256",
            "executable_pricing_sha256",
        ):
            _hex64(row[digest_key], path=f"{path}.{digest_key}")
        receipt_transitions[canonical_id] = row
    if set(receipt_transitions) != set(changed):
        raise CandidateValidationError(
            "RECEIPT_TRANSITION_SET", "receipt must bind exactly every changed label"
        )
    authority_labels = _label_index(authority["labels"], role="authority")
    for canonical_id, candidate_label in changed.items():
        row = receipt_transitions[canonical_id]
        if row["from_grade"] != authority_labels[canonical_id]["implementation_grade"]:
            raise CandidateValidationError("RECEIPT_FROM_GRADE", "from_grade differs from authority")
        if row["to_grade"] != candidate_label["implementation_grade"]:
            raise CandidateValidationError("RECEIPT_TO_GRADE", "to_grade differs from candidate")
        if row["candidate_label_sha256"] != _canonical_sha256(candidate_label):
            raise CandidateValidationError("RECEIPT_LABEL", "transition does not bind candidate label")
        for field_name in (
            "event_window",
            "phase_window",
            "warmup_window",
            "coverage_windows",
            "executable_pricing",
        ):
            if row[f"{field_name}_sha256"] != _canonical_sha256(candidate_label[field_name]):
                raise CandidateValidationError(
                    "RECEIPT_FIELD_BINDING",
                    f"transition does not bind {field_name}",
                )
        if candidate_label["replay_binding"] != binding:
            raise CandidateValidationError(
                "RECEIPT_REPLAY_BINDING",
                "changed label must carry the receipt's exact capture/query/frontier binding",
            )
    return receipt, receipt_digest


def validate_candidate(
    *,
    candidate_path: Path,
    authority_path: Path,
    evidence_root: Path,
    grader_receipt_path: Path | None = None,
) -> ValidationReport:
    # Perform every drive/UNC decision before metadata or content access.  This
    # keeps a later remote argument from causing egress after an earlier local
    # input has already been opened.
    _lexically_local_absolute_path(
        candidate_path,
        role="candidate",
        network_code="INPUT_NETWORK",
        invalid_code="INPUT_PATH",
    )
    _lexically_local_absolute_path(
        authority_path,
        role="authority",
        network_code="INPUT_NETWORK",
        invalid_code="INPUT_PATH",
    )
    _lexically_local_absolute_path(
        evidence_root,
        role="evidence root",
        network_code="EVIDENCE_ROOT_NETWORK",
        invalid_code="EVIDENCE_ROOT",
    )
    if grader_receipt_path is not None:
        _lexically_local_absolute_path(
            grader_receipt_path,
            role="grader receipt",
            network_code="INPUT_NETWORK",
            invalid_code="INPUT_PATH",
        )

    # Reject every reparse component and non-regular top-level input before
    # reading any JSON bytes.  The bounded reader repeats the checks around the
    # actual handle open to detect swaps.
    candidate_path = _validated_local_regular_file(
        candidate_path,
        role="candidate",
        max_bytes=MAX_CANDIDATE_JSON_BYTES,
    )
    authority_path = _validated_local_regular_file(
        authority_path,
        role="authority",
        max_bytes=MAX_AUTHORITY_JSON_BYTES,
    )
    evidence_root = _resolved_local_evidence_root(evidence_root)
    if grader_receipt_path is not None:
        grader_receipt_path = _validated_local_regular_file(
            grader_receipt_path,
            role="grader receipt",
            max_bytes=MAX_GRADER_RECEIPT_JSON_BYTES,
        )

    authority, _authority_raw = _load_json_file(
        authority_path,
        role="authority",
        max_bytes=MAX_AUTHORITY_JSON_BYTES,
    )
    authority_labels, inventory = _validate_authority(authority)
    payload_digest = authority_payload_sha256(authority)
    if payload_digest != EXPECTED_AUTHORITY_PAYLOAD_SHA256:
        raise CandidateValidationError(
            "AUTHORITY_PAYLOAD",
            "authority evidence payload is not the reviewed canonical payload",
            path="$.authority",
        )
    candidate, candidate_raw = _load_json_file(
        candidate_path,
        role="candidate",
        max_bytes=MAX_CANDIDATE_JSON_BYTES,
    )
    _exact_keys(
        candidate,
        {"schema_version", "authority_payload_sha256", "labels"},
        path="$.candidate",
    )
    if candidate["schema_version"] != CANDIDATE_SCHEMA:
        raise CandidateValidationError("CANDIDATE_SCHEMA", "unsupported candidate schema")
    if candidate["authority_payload_sha256"] != payload_digest:
        raise CandidateValidationError(
            "CANDIDATE_AUTHORITY", "candidate does not bind the authority payload"
        )
    candidate_labels = _label_index(candidate["labels"], role="candidate")
    if set(candidate_labels) != set(authority_labels):
        missing = sorted(set(authority_labels) - set(candidate_labels))
        extra = sorted(set(candidate_labels) - set(authority_labels))
        raise CandidateValidationError(
            "LABEL_SET", f"candidate must contain exactly canonical labels; missing={missing}, extra={extra}"
        )
    changed: dict[str, Mapping[str, Any]] = {}
    protected_facts = _expect_list(authority["protected_facts"], path="$.authority.protected_facts")
    for canonical_id, label in candidate_labels.items():
        path = f"$.candidate.labels[{canonical_id!r}]"
        _validate_candidate_label_shape(label, path=path)
        _reject_protected_aliases(label, protected_facts, path=path)
        authority_label = authority_labels[canonical_id]
        citations = _expect_list(label["citations"], path=f"{path}.citations")
        observed_targets: set[tuple[str, str, str]] = set()
        for index, raw_citation in enumerate(citations):
            citation = _expect_object(raw_citation, path=f"{path}.citations[{index}]")
            target = _verify_citation(
                citation,
                evidence_root=evidence_root,
                inventory=inventory,
                path=f"{path}.citations[{index}]",
            )
            if target in observed_targets:
                raise CandidateValidationError(
                    "CITATION_DUPLICATE", "duplicate citation target", path=f"{path}.citations[{index}]"
                )
            observed_targets.add(target)
        expected_targets = {
            (row["path"], row["target"], row["target_sha256"])
            for row in _expect_list(
                authority_label["citation_targets"],
                path=f"$.authority.labels[{canonical_id!r}].citation_targets",
            )
        }
        if observed_targets != expected_targets:
            raise CandidateValidationError(
                "CITATION_SET",
                "candidate citations must exactly match authority-bound targets",
                path=f"{path}.citations",
            )
        if not _bound_fields_equal(label, authority_label):
            changed[canonical_id] = label
    receipt_digest: str | None = None
    if changed:
        if grader_receipt_path is None:
            raise CandidateValidationError(
                "GRADE_OR_EVIDENCE_MUTATION",
                "candidate differs from canonical baseline without a sealed ReplayV3 grader receipt",
            )
        _require_transition_authority()
        _, receipt_digest = _validate_receipt(
            grader_receipt_path,
            authority=authority,
            authority_payload_digest=payload_digest,
            candidate_raw=candidate_raw,
            changed=changed,
        )
    elif grader_receipt_path is not None:
        raise CandidateValidationError(
            "RECEIPT_UNNEEDED", "a transition receipt is forbidden when no label changed"
        )
    counts = Counter(label["implementation_grade"] for label in candidate_labels.values())
    grade_counts = {grade: counts.get(grade, 0) for grade in sorted(IMPLEMENTATION_GRADES)}
    if not changed and grade_counts != {grade: EXPECTED_BASELINE_COUNTS[grade] for grade in sorted(IMPLEMENTATION_GRADES)}:
        raise CandidateValidationError("BASELINE_COUNTS", "candidate grade counts mutated")
    return ValidationReport(
        authority_id=authority["authority_id"],
        authority_payload_sha256=payload_digest,
        candidate_sha256=_sha256(candidate_raw),
        label_count=len(candidate_labels),
        grade_counts=grade_counts,
        transition_receipt_sha256=receipt_digest,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--grader-receipt", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = validate_candidate(
            candidate_path=args.candidate,
            authority_path=args.authority,
            evidence_root=args.evidence_root,
            grader_receipt_path=args.grader_receipt,
        )
    except CandidateValidationError as exc:
        print(json.dumps({"status": "INVALID", "error": exc.to_dict()}, sort_keys=True))
        return 2
    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
