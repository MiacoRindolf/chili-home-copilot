"""Build one dedicated, hash-bound Alpaca PAPER environment file.

This builder projects a large desktop ``.env`` onto the exact credential and
provider inputs needed by the captured-paper worker.  It performs no database,
broker, provider, process, task-scheduler, or service I/O.  The output is
validated with :func:`install_captured_paper_runtime_environment` against an
isolated mapping before it is published without overwrite.

Success output contains only source/output hashes and domain-separated secret
fingerprints.  Secret values and local paths are never serialized in a receipt
or CLI result.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import MappingProxyType
from typing import Mapping, Sequence
import uuid

from dotenv.parser import parse_stream

from scripts import captured_paper_runtime_env as runtime_env


BUILDER_SCHEMA_VERSION = "chili.captured-paper-runtime-env-builder.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IQFEED_BRIDGE_BUILD_RE = re.compile(
    r"^iqfeed-l1-exact-print-provenance-v3\+sha256:[0-9a-f]{16}$"
)
_IQFEED_NOTIFY_CHANNEL_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_VALUE_BYTES = 1024 * 1024

_REQUIRED_SOURCE_KEYS = (
    "DATABASE_URL",
    "CHILI_ALPACA_API_KEY",
    "CHILI_ALPACA_API_SECRET",
    "CHILI_ALPACA_DATA_FEED",
    "CHILI_AUTOTRADER_USER_ID",
)
_OPTIONAL_PROVIDER_KEYS = (
    "MASSIVE_API_KEY",
    "POLYGON_API_KEY",
    "CHILI_ORTEX_API_KEY",
)
_SUPPLIED_KEYS = (
    "CHILI_ALPACA_EXPECTED_ACCOUNT_ID",
    "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD",
    "IQFEED_NOTIFY_CHANNEL",
)
_OUTPUT_KEYS = frozenset(
    {*_REQUIRED_SOURCE_KEYS, *_OPTIONAL_PROVIDER_KEYS, *_SUPPLIED_KEYS}
)
_SECRET_KEYS = frozenset(
    {
        "DATABASE_URL",
        "CHILI_ALPACA_API_KEY",
        "CHILI_ALPACA_API_SECRET",
        "MASSIVE_API_KEY",
        "POLYGON_API_KEY",
        "CHILI_ORTEX_API_KEY",
    }
)


class CapturedPaperRuntimeEnvBuildError(RuntimeError):
    """Sanitized local builder rejection."""

    def __init__(self, message: str, *, code: str = "BUILD_REJECTED") -> None:
        super().__init__(message)
        self.code = str(code)


class _SanitizedArgumentParser(argparse.ArgumentParser):
    """Reject invalid CLI input without echoing paths or credential-shaped text."""

    def error(self, message: str) -> None:  # pragma: no cover - message is untrusted
        del message
        raise CapturedPaperRuntimeEnvBuildError(
            "command line arguments were rejected", code="INVALID_ARGUMENTS"
        )


@dataclass(frozen=True, slots=True)
class CapturedPaperRuntimeEnvBuildReceipt:
    source_sha256: str
    output_sha256: str
    secret_fingerprints: Mapping[str, str]
    schema_version: str = BUILDER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        # Deliberately omit local paths, identities, policy values, and all
        # credential material.  The hashes bind those bytes out of band.
        return {
            "schema_version": self.schema_version,
            "source_sha256": self.source_sha256,
            "output_sha256": self.output_sha256,
            "secret_fingerprints": dict(self.secret_fingerprints),
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_uuid(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "expected Alpaca PAPER account id is not a canonical lower-case UUID",
            code="INVALID_ACCOUNT_ID",
        ) from exc
    if raw != raw.lower() or str(parsed) != raw:
        raise CapturedPaperRuntimeEnvBuildError(
            "expected Alpaca PAPER account id is not a canonical lower-case UUID",
            code="INVALID_ACCOUNT_ID",
        )
    return raw


def _bridge_build(value: str) -> str:
    normalized = str(value or "").strip()
    if _IQFEED_BRIDGE_BUILD_RE.fullmatch(normalized) is None:
        raise CapturedPaperRuntimeEnvBuildError(
            "IQFeed build is not the exact v3 provenance identity",
            code="INVALID_IQFEED_BUILD",
        )
    return normalized


def _notify_channel(value: str) -> str:
    normalized = str(value or "").strip()
    if _IQFEED_NOTIFY_CHANNEL_RE.fullmatch(normalized) is None:
        raise CapturedPaperRuntimeEnvBuildError(
            "IQFeed notify channel is not canonical lower-case PostgreSQL syntax",
            code="INVALID_NOTIFY_CHANNEL",
        )
    return normalized


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        try:
            metadata = os.lstat(cursor)
        except OSError as exc:
            raise CapturedPaperRuntimeEnvBuildError(
                "local path could not be inspected",
                code="INVALID_LOCAL_PATH",
            ) from exc
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & _REPARSE_ATTRIBUTE:
            raise CapturedPaperRuntimeEnvBuildError(
                "local path traverses a reparse point",
                code="INVALID_LOCAL_PATH",
            )
        parent = cursor.parent
        if parent == cursor:
            return
        cursor = parent


def _lexical_local_absolute(value: str | Path, *, field: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise CapturedPaperRuntimeEnvBuildError(
            f"{field} is empty", code="INVALID_LOCAL_PATH"
        )
    path = Path(raw)
    if not path.is_absolute() or raw.startswith(("\\\\", "//")):
        raise CapturedPaperRuntimeEnvBuildError(
            f"{field} must be an absolute local path",
            code="INVALID_LOCAL_PATH",
        )
    drive, tail = os.path.splitdrive(str(path))
    if not drive or ":" in tail:
        raise CapturedPaperRuntimeEnvBuildError(
            f"{field} is not a plain local path",
            code="INVALID_LOCAL_PATH",
        )
    return Path(os.path.abspath(path))


def _existing_local_dir(value: str | Path, *, field: str) -> Path:
    path = _lexical_local_absolute(value, field=field)
    _reject_reparse_chain(path)
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            f"{field} is unavailable", code="INVALID_LOCAL_PATH"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CapturedPaperRuntimeEnvBuildError(
            f"{field} is not a directory", code="INVALID_LOCAL_PATH"
        )
    return path


def _within(path: Path, roots: Sequence[Path]) -> bool:
    candidate = os.path.normcase(str(path))
    for root in roots:
        try:
            if os.path.commonpath((candidate, os.path.normcase(str(root)))) == os.path.normcase(
                str(root)
            ):
                return True
        except ValueError:
            continue
    return False


def _source_file(
    value: str | Path,
    *,
    allow_read_roots: Sequence[str | Path],
) -> Path:
    roots = tuple(
        _existing_local_dir(root, field="allow-read root")
        for root in allow_read_roots
    )
    if not roots:
        raise CapturedPaperRuntimeEnvBuildError(
            "at least one allow-read root is required", code="READ_NOT_ALLOWED"
        )
    path = _lexical_local_absolute(value, field="source environment")
    _reject_reparse_chain(path)
    if not _within(path, roots):
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment is outside the allow-read roots",
            code="READ_NOT_ALLOWED",
        )
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment is unavailable", code="SOURCE_UNAVAILABLE"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment is not a regular file",
            code="SOURCE_UNAVAILABLE",
        )
    return path


def _output_file(
    value: str | Path,
    *,
    allow_write_roots: Sequence[str | Path],
) -> Path:
    roots = tuple(
        _existing_local_dir(root, field="allow-write root")
        for root in allow_write_roots
    )
    if not roots:
        raise CapturedPaperRuntimeEnvBuildError(
            "at least one allow-write root is required", code="WRITE_NOT_ALLOWED"
        )
    path = _lexical_local_absolute(value, field="output environment")
    parent = path.parent
    _reject_reparse_chain(parent)
    if not _within(path, roots):
        raise CapturedPaperRuntimeEnvBuildError(
            "output environment is outside the allow-write roots",
            code="WRITE_NOT_ALLOWED",
        )
    try:
        metadata = parent.stat()
    except OSError as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "output parent is unavailable", code="WRITE_NOT_ALLOWED"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise CapturedPaperRuntimeEnvBuildError(
            "output parent is not a directory", code="WRITE_NOT_ALLOWED"
        )
    if path.exists():
        _reject_reparse_chain(path)
        if not path.is_file():
            raise CapturedPaperRuntimeEnvBuildError(
                "output path is not a regular file", code="OUTPUT_CONFLICT"
            )
    return path


def _stable_source_bytes(path: Path, *, expected_sha256: str) -> bytes:
    expected = str(expected_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(expected) is None:
        raise CapturedPaperRuntimeEnvBuildError(
            "source SHA-256 is malformed", code="INVALID_SOURCE_SHA256"
        )
    before = path.stat()
    if before.st_size <= 0 or before.st_size > _MAX_SOURCE_BYTES:
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment size is outside the bounded contract",
            code="SOURCE_UNAVAILABLE",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment could not be read", code="SOURCE_UNAVAILABLE"
        ) from exc
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment changed while being read", code="SOURCE_DRIFT"
        )
    if _sha256_bytes(raw) != expected:
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment hash mismatch", code="SOURCE_HASH_MISMATCH"
        )
    return raw


def _parse_source(raw: bytes) -> Mapping[str, str]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment is not UTF-8", code="SOURCE_PARSE_ERROR"
        ) from exc

    try:
        bindings = tuple(parse_stream(io.StringIO(text)))
    except Exception as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "source environment could not be parsed",
            code="SOURCE_PARSE_ERROR",
        ) from exc

    # Use python-dotenv's own grammar for the duplicate fence.  A regex misses
    # valid quoted keys (for example ``'CHILI_ALPACA_API_SECRET'=...``), which
    # would otherwise reintroduce last-write-wins ambiguity.  Repeated keys for
    # unrelated desktop applications remain irrelevant because they can never
    # enter the exact output projection.
    curated_source_keys = _OUTPUT_KEYS.difference(_SUPPLIED_KEYS)
    seen: set[str] = set()
    normalized: dict[str, str] = {}
    for binding in bindings:
        if binding.error:
            raise CapturedPaperRuntimeEnvBuildError(
                "source environment could not be parsed",
                code="SOURCE_PARSE_ERROR",
            )
        key = str(binding.key or "").strip().upper()
        if key not in curated_source_keys:
            continue
        if key in seen:
            raise CapturedPaperRuntimeEnvBuildError(
                "source environment contains a duplicate assignment",
                code="DUPLICATE_SOURCE_KEY",
            )
        seen.add(key)
        raw_value = binding.value
        if raw_value is None:
            raise CapturedPaperRuntimeEnvBuildError(
                "a curated source assignment has no value",
                code="SOURCE_PARSE_ERROR",
            )
        value = str(raw_value)
        if (
            "\x00" in value
            or "\r" in value
            or "\n" in value
            or len(value.encode("utf-8")) > _MAX_VALUE_BYTES
        ):
            raise CapturedPaperRuntimeEnvBuildError(
                "a curated source value is invalid",
                code="SOURCE_PARSE_ERROR",
            )
        normalized[key] = value
    return MappingProxyType(normalized)


def _project_values(
    source: Mapping[str, str],
    *,
    expected_account_id: str,
    iqfeed_bridge_build: str,
    iqfeed_notify_channel: str,
) -> Mapping[str, str]:
    missing = sorted(
        key for key in _REQUIRED_SOURCE_KEYS if not str(source.get(key, "")).strip()
    )
    if missing:
        raise CapturedPaperRuntimeEnvBuildError(
            "one or more required curated PAPER inputs are missing",
            code="MISSING_REQUIRED_INPUT",
        )
    raw_feed = str(source["CHILI_ALPACA_DATA_FEED"])
    feed = raw_feed.strip()
    if raw_feed != feed or feed not in {"iex", "sip"}:
        raise CapturedPaperRuntimeEnvBuildError(
            "Alpaca PAPER data feed is not canonical iex or sip",
            code="INVALID_DATA_FEED",
        )
    raw_user_id = str(source["CHILI_AUTOTRADER_USER_ID"])
    user_id = raw_user_id.strip()
    if (
        raw_user_id != user_id
        or re.fullmatch(r"[1-9][0-9]*", user_id) is None
        or int(user_id) > 2_147_483_647
    ):
        raise CapturedPaperRuntimeEnvBuildError(
            "autotrader user id is not a canonical positive DB integer",
            code="INVALID_AUTOTRADER_USER",
        )

    projected = {
        key: str(source[key])
        for key in _REQUIRED_SOURCE_KEYS
    }
    for key in _OPTIONAL_PROVIDER_KEYS:
        value = str(source.get(key, ""))
        if value.strip():
            projected[key] = value
    projected.update(
        {
            "CHILI_ALPACA_EXPECTED_ACCOUNT_ID": _canonical_uuid(
                expected_account_id
            ),
            "CHILI_IQFEED_L1_AUTHORITATIVE_BRIDGE_BUILD": _bridge_build(
                iqfeed_bridge_build
            ),
            "IQFEED_NOTIFY_CHANNEL": _notify_channel(iqfeed_notify_channel),
        }
    )
    if not set(projected).issubset(_OUTPUT_KEYS):
        raise AssertionError("captured PAPER projection escaped its exact key set")
    return MappingProxyType(projected)


def _dotenv_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _render_environment(values: Mapping[str, str]) -> bytes:
    keys = [
        *_REQUIRED_SOURCE_KEYS,
        *(key for key in _OPTIONAL_PROVIDER_KEYS if key in values),
        *_SUPPLIED_KEYS,
    ]
    if set(keys) != set(values) or len(keys) != len(values):
        raise CapturedPaperRuntimeEnvBuildError(
            "projected environment key set is invalid",
            code="INTERNAL_PROJECTION_ERROR",
        )
    rendered = "".join(f"{key}={_dotenv_quote(values[key])}\n" for key in keys)
    return rendered.encode("utf-8")


def _read_existing_exact(path: Path, expected: bytes) -> bool:
    if not path.exists():
        return False
    _reject_reparse_chain(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise CapturedPaperRuntimeEnvBuildError(
            "output path is not a regular file", code="OUTPUT_CONFLICT"
        )
    try:
        observed = path.read_bytes()
    except OSError as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "existing output could not be read", code="OUTPUT_CONFLICT"
        ) from exc
    after = path.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CapturedPaperRuntimeEnvBuildError(
            "existing output changed while being read", code="OUTPUT_CONFLICT"
        )
    if observed != expected:
        raise CapturedPaperRuntimeEnvBuildError(
            "existing output differs; overwrite is forbidden",
            code="OUTPUT_CONFLICT",
        )
    return True


def _validate_runtime_output(
    path: Path,
    *,
    output_sha256: str,
    expected_account_id: str,
    projected: Mapping[str, str],
) -> runtime_env.CapturedPaperRuntimeEnvironmentReceipt:
    isolated_environment: dict[str, str] = {}
    try:
        receipt = runtime_env.install_captured_paper_runtime_environment(
            path,
            expected_env_sha256=output_sha256,
            expected_account_id=expected_account_id,
            environ=isolated_environment,
        )
    except runtime_env.CapturedPaperRuntimeEnvError as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "projected environment failed isolated runtime validation",
            code="RUNTIME_VALIDATION_FAILED",
        ) from exc
    if (
        type(receipt) is not runtime_env.CapturedPaperRuntimeEnvironmentReceipt
        or receipt.source_sha256 != output_sha256
        or receipt.expected_account_id != expected_account_id
        or receipt.first_dip_policy_mode != "candidate"
    ):
        raise CapturedPaperRuntimeEnvBuildError(
            "runtime validation receipt identity is inconsistent",
            code="RUNTIME_VALIDATION_FAILED",
        )
    expected_secret_fingerprints = {
        key: runtime_env._secret_fingerprint(key, projected[key])
        for key in projected
        if key in _SECRET_KEYS
    }
    if dict(receipt.secret_fingerprints) != dict(
        sorted(expected_secret_fingerprints.items())
    ):
        raise CapturedPaperRuntimeEnvBuildError(
            "runtime secret fingerprint projection changed during validation",
            code="RUNTIME_VALIDATION_FAILED",
        )
    for key, value in projected.items():
        if key in _SECRET_KEYS:
            continue
        if receipt.effective_config.get(key) != value:
            raise CapturedPaperRuntimeEnvBuildError(
                "runtime nonsecret projection changed during validation",
                code="RUNTIME_VALIDATION_FAILED",
            )
    return receipt


def _write_pending(path: Path, raw: bytes) -> Path:
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        descriptor = os.open(
            pending,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass
        raise CapturedPaperRuntimeEnvBuildError(
            "pending output could not be written", code="OUTPUT_WRITE_FAILED"
        ) from exc
    return pending


def _publish_no_overwrite(pending: Path, output: Path, expected: bytes) -> None:
    try:
        os.link(pending, output)
    except FileExistsError:
        _read_existing_exact(output, expected)
    except OSError as exc:
        raise CapturedPaperRuntimeEnvBuildError(
            "atomic no-overwrite publication failed",
            code="OUTPUT_WRITE_FAILED",
        ) from exc


def build_captured_paper_runtime_env(
    source_env: str | Path,
    *,
    expected_source_sha256: str,
    output_env: str | Path,
    expected_account_id: str,
    iqfeed_bridge_build: str,
    iqfeed_notify_channel: str,
    allow_read_roots: Sequence[str | Path],
    allow_write_roots: Sequence[str | Path],
) -> CapturedPaperRuntimeEnvBuildReceipt:
    """Project, validate, and publish one dedicated PAPER environment."""

    source = _source_file(source_env, allow_read_roots=allow_read_roots)
    output = _output_file(output_env, allow_write_roots=allow_write_roots)
    if os.path.normcase(str(source)) == os.path.normcase(str(output)):
        raise CapturedPaperRuntimeEnvBuildError(
            "source and output environments must be different files",
            code="OUTPUT_CONFLICT",
        )
    raw_source = _stable_source_bytes(
        source, expected_sha256=expected_source_sha256
    )
    source_sha256 = _sha256_bytes(raw_source)
    parsed = _parse_source(raw_source)
    account_id = _canonical_uuid(expected_account_id)
    projected = _project_values(
        parsed,
        expected_account_id=account_id,
        iqfeed_bridge_build=iqfeed_bridge_build,
        iqfeed_notify_channel=iqfeed_notify_channel,
    )
    raw_output = _render_environment(projected)
    output_sha256 = _sha256_bytes(raw_output)

    if _read_existing_exact(output, raw_output):
        runtime_receipt = _validate_runtime_output(
            output,
            output_sha256=output_sha256,
            expected_account_id=account_id,
            projected=projected,
        )
    else:
        pending = _write_pending(output, raw_output)
        try:
            runtime_receipt = _validate_runtime_output(
                pending,
                output_sha256=output_sha256,
                expected_account_id=account_id,
                projected=projected,
            )
            _publish_no_overwrite(pending, output, raw_output)
        finally:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
    if not _read_existing_exact(output, raw_output):
        raise CapturedPaperRuntimeEnvBuildError(
            "validated output was not published",
            code="OUTPUT_CONFLICT",
        )

    return CapturedPaperRuntimeEnvBuildReceipt(
        source_sha256=source_sha256,
        output_sha256=output_sha256,
        secret_fingerprints=MappingProxyType(
            dict(sorted(runtime_receipt.secret_fingerprints.items()))
        ),
    )


def _parser() -> argparse.ArgumentParser:
    parser = _SanitizedArgumentParser(
        description="Build an offline, dedicated captured Alpaca PAPER env"
    )
    parser.add_argument("--source-env", action="append", required=True)
    parser.add_argument("--source-sha256", action="append", required=True)
    parser.add_argument("--output-env", action="append", required=True)
    parser.add_argument("--expected-account-id", action="append", required=True)
    parser.add_argument("--iqfeed-bridge-build", action="append", required=True)
    parser.add_argument("--iqfeed-notify-channel", action="append", required=True)
    parser.add_argument("--allow-read-root", action="append", required=True)
    parser.add_argument("--allow-write-root", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        scalar_names = (
            "source_env",
            "source_sha256",
            "output_env",
            "expected_account_id",
            "iqfeed_bridge_build",
            "iqfeed_notify_channel",
        )
        if any(len(getattr(args, name)) != 1 for name in scalar_names):
            raise CapturedPaperRuntimeEnvBuildError(
                "a security argument was supplied more than once",
                code="DUPLICATE_SECURITY_ARGUMENT",
            )
        values = {name: getattr(args, name)[0] for name in scalar_names}
        receipt = build_captured_paper_runtime_env(
            values["source_env"],
            expected_source_sha256=values["source_sha256"],
            output_env=values["output_env"],
            expected_account_id=values["expected_account_id"],
            iqfeed_bridge_build=values["iqfeed_bridge_build"],
            iqfeed_notify_channel=values["iqfeed_notify_channel"],
            allow_read_roots=args.allow_read_root,
            allow_write_roots=args.allow_write_root,
        )
    except CapturedPaperRuntimeEnvBuildError as exc:
        print(
            json.dumps(
                {"error_code": exc.code, "environment_published": False},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "error_code": "UNEXPECTED_BUILDER_FAILURE",
                    "environment_published": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    print(json.dumps(receipt.to_dict(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))


__all__ = [
    "BUILDER_SCHEMA_VERSION",
    "CapturedPaperRuntimeEnvBuildError",
    "CapturedPaperRuntimeEnvBuildReceipt",
    "build_captured_paper_runtime_env",
    "main",
]
