"""Isolated durable-flush latency probe for captured PAPER.

The captured PAPER service is heavily multi-threaded.  CPython releases the GIL
around ``os.fsync`` and reacquires it before returning, so timing ``os.fsync`` in
that process can include unrelated GIL reacquisition delay.  This helper keeps
the durability operation real while moving its clock into a persistent,
single-threaded CPython 3.11 child.

The protocol is deliberately small and fail-closed:

* the child must be launched as ``python -I -S -B`` and verifies those flags;
* the exact helper source SHA-256 is supplied on argv and verified by the child;
* requests and responses use bounded JSON lines over anonymous pipes only;
* one private session nonce binds strictly increasing request sequences and a
  deterministic per-request nonce, so malformed/replayed messages terminate the
  session;
* every measurement revalidates the resolved probe root, ``st_dev`` and
  directory identity before and immediately after the probe file is opened;
* exactly 4096 bytes are written, flushed and file-fsynced, with only those
  operations timed by ``perf_counter_ns``;
* the temporary object must be removed before a successful response is emitted.

This module contains no service integration.  ``CapturedPaperPressureProbeClient``
is the narrow client capability a later, separately-reviewed integration may use.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping


REQUEST_SCHEMA = "chili.captured-paper-pressure-probe.request.v1"
RESPONSE_SCHEMA = "chili.captured-paper-pressure-probe.response.v1"
PRESSURE_WRITE_LATENCY_PROFILE = (
    "chili.capture-pressure.durable-write-fsync-helper-process.v1"
)
_HELD_SOURCE_LOADER = r"""
import hashlib, os, stat, sys
from pathlib import Path
p = Path(sys.argv[1])
if not p.is_absolute():
    raise SystemExit(91)
p = p.resolve(strict=True)
expected = sys.argv[2]
if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
    raise SystemExit(92)
def identity(value):
    return (int(value.st_dev), int(value.st_ino), int(value.st_size),
            int(value.st_mtime_ns), int(value.st_mode))
before = os.stat(p, follow_symlinks=False)
if not stat.S_ISREG(before.st_mode) or int(getattr(before, 'st_file_attributes', 0)) & 0x400:
    raise SystemExit(93)
first = p.read_bytes()
middle = os.stat(p, follow_symlinks=False)
second = p.read_bytes()
after = os.stat(p, follow_symlinks=False)
if identity(before) != identity(middle) or identity(middle) != identity(after):
    raise SystemExit(94)
if first != second or hashlib.sha256(first).hexdigest() != expected:
    raise SystemExit(95)
sys.argv[:] = [str(p), *sys.argv[3:]]
scope = {'__name__': '__main__', '__file__': str(p), '__package__': None,
         '__cached__': None, '__builtins__': __builtins__,
         '__chili_held_source_sha256__': expected}
exec(compile(first, str(p), 'exec', dont_inherit=True), scope, scope)
""".strip()
_HELD_BENCHMARK_SOURCE_LOADER = r"""
import hashlib, json, os, stat, sys
from pathlib import Path
from types import MappingProxyType
flags = sys.flags
if not (sys.implementation.name == 'cpython'
        and sys.version_info[:2] == (3, 11)
        and flags.isolated == 1 and flags.ignore_environment == 1
        and flags.no_site == 1 and flags.safe_path is True
        and flags.dont_write_bytecode == 1):
    raise SystemExit(90)
values = tuple(sys.argv[1:])
try:
    boundary = values.index('--')
except ValueError:
    raise SystemExit(91)
bootstrap = values[:boundary]
arguments = values[boundary + 1:]
roles = ('benchmark', 'contract', 'runtime', 'pressure_probe',
         'replay_errors', 'first_dip_tape_policy', 'stage0')
if len(bootstrap) != len(roles) * 2 + 5 or not arguments:
    raise SystemExit(92)
def identity(value):
    return (int(value.st_dev), int(value.st_ino), int(value.st_size),
            int(value.st_mtime_ns), int(value.st_mode))
held = {}
paths = set()
total_source_bytes = 0
for index, role in enumerate(roles):
    raw_path = Path(bootstrap[index * 2])
    expected = bootstrap[index * 2 + 1]
    if not raw_path.is_absolute():
        raise SystemExit(93)
    path = raw_path.resolve(strict=True)
    if path in paths:
        raise SystemExit(94)
    paths.add(path)
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        raise SystemExit(95)
    before = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode)
            or int(getattr(before, 'st_file_attributes', 0)) & 0x400):
        raise SystemExit(96)
    if int(before.st_size) <= 0 or int(before.st_size) > 32 * 1024 * 1024:
        raise SystemExit(102)
    total_source_bytes += int(before.st_size)
    if total_source_bytes > 128 * 1024 * 1024:
        raise SystemExit(103)
    descriptor = os.open(path, os.O_RDONLY | int(getattr(os, 'O_BINARY', 0)))
    try:
        opened = os.fstat(descriptor)
        if identity(opened) != identity(before):
            raise SystemExit(97)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    after = os.stat(path, follow_symlinks=False)
    source = b''.join(chunks)
    if identity(after) != identity(before) or hashlib.sha256(source).hexdigest() != expected:
        raise SystemExit(98)
    held[role] = MappingProxyType(
        {'path': str(path), 'sha256': expected, 'source': source}
    )
expected_python_executable_sha256 = bootstrap[-1]
benchmark_authority_manifest_sha256 = bootstrap[-2]
benchmark_authority_manifest_path_raw = Path(bootstrap[-3])
expected_dependency_identity = bootstrap[-4]
dependency_root_raw = Path(bootstrap[-5])
if (not dependency_root_raw.is_absolute()
        or not benchmark_authority_manifest_path_raw.is_absolute()
        or len(expected_dependency_identity) != 64
        or any(c not in '0123456789abcdef' for c in expected_dependency_identity)
        or len(benchmark_authority_manifest_sha256) != 64
        or any(c not in '0123456789abcdef'
               for c in benchmark_authority_manifest_sha256)
        or len(expected_python_executable_sha256) != 64
        or any(c not in '0123456789abcdef'
               for c in expected_python_executable_sha256)):
    raise SystemExit(99)
dependency_root = dependency_root_raw.resolve(strict=True)
benchmark_authority_manifest_path = (
    benchmark_authority_manifest_path_raw.resolve(strict=True))
benchmark_path = Path(held['benchmark']['path'])
candidate_root = benchmark_path.parent.parent
module_root = candidate_root / 'app' / 'services' / 'trading' / 'momentum_neural'
expected_paths = {
    'benchmark': benchmark_path,
    'pressure_probe': benchmark_path.with_name('captured_paper_pressure_probe.py'),
    'stage0': benchmark_path.with_name('captured_paper_isolated_stage0.py'),
    'contract': module_root / 'replay_capture_contract.py',
    'runtime': module_root / 'replay_capture_runtime.py',
    'replay_errors': module_root / 'replay_errors.py',
    'first_dip_tape_policy': module_root / 'first_dip_tape_policy.py',
}
if any(Path(held[role]['path']) != expected.resolve(strict=True)
       for role, expected in expected_paths.items()):
    raise SystemExit(104)
manifest_before = os.stat(benchmark_authority_manifest_path,
                          follow_symlinks=False)
if (not stat.S_ISREG(manifest_before.st_mode)
        or int(getattr(manifest_before, 'st_file_attributes', 0)) & 0x400
        or int(manifest_before.st_size) <= 0
        or int(manifest_before.st_size) > 4 * 1024 * 1024):
    raise SystemExit(106)
manifest_descriptor = os.open(
    benchmark_authority_manifest_path,
    os.O_RDONLY | int(getattr(os, 'O_BINARY', 0)))
try:
    manifest_opened = os.fstat(manifest_descriptor)
    if identity(manifest_opened) != identity(manifest_before):
        raise SystemExit(107)
    manifest_chunks = []
    while True:
        manifest_chunk = os.read(manifest_descriptor, 1024 * 1024)
        if not manifest_chunk:
            break
        manifest_chunks.append(manifest_chunk)
finally:
    os.close(manifest_descriptor)
manifest_after = os.stat(benchmark_authority_manifest_path,
                         follow_symlinks=False)
manifest_raw = b''.join(manifest_chunks)
if (identity(manifest_after) != identity(manifest_before)
        or hashlib.sha256(manifest_raw).hexdigest()
           != benchmark_authority_manifest_sha256
        or benchmark_authority_manifest_path.name
           != benchmark_authority_manifest_sha256 + '.json'
        or benchmark_authority_manifest_path.parent.name
           != benchmark_authority_manifest_sha256[:2]
        or benchmark_authority_manifest_path.parent.parent.name != 'manifest'
        or benchmark_authority_manifest_path.parent.parent.parent.name
           != 'authority'):
    raise SystemExit(108)
def reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError('duplicate or non-string JSON key')
        result[key] = value
    return result
def reject_json_constant(_value):
    raise ValueError('non-finite JSON number')
try:
    manifest = json.loads(
        manifest_raw.decode('utf-8'),
        object_pairs_hook=reject_duplicate_pairs,
        parse_constant=reject_json_constant)
    manifest_canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')
except (UnicodeDecodeError, ValueError, TypeError):
    raise SystemExit(109)
manifest_keys = {
    'account_scope', 'authority_mode', 'benchmark_arguments',
    'authority_programs',
    'candidate_root', 'expected_benchmark_schema_version',
    'expected_git_commit', 'execution_context', 'git', 'held_loader', 'output',
    'posture', 'python', 'python_dependency_root', 'schema_version',
    'source_roster', 'source_roster_sha256'}
expected_roster = [
    {'path': held[role]['path'], 'role': role,
     'sha256': held[role]['sha256']}
    for role in sorted(roles)]
roster_raw = json.dumps(
    expected_roster, ensure_ascii=False, sort_keys=True,
    separators=(',', ':'), allow_nan=False).encode('utf-8')
expected_posture = {
    'benchmark_execution_authorized': True,
    'broker_contact_authorized': False,
    'database_access_authorized': False,
    'host_activation_authorized': False,
    'live_cash_authorized': False,
    'order_submission_authorized': False,
    'provider_contact_authorized': False}
if (type(manifest) is not dict or set(manifest) != manifest_keys
        or manifest_canonical != manifest_raw
        or manifest.get('schema_version')
           != 'chili.captured-paper-benchmark-authority-manifest.v2'
        or manifest.get('account_scope') != 'alpaca:paper'
        or manifest.get('authority_mode')
           != 'diagnostic_capture_benchmark_only'
        or manifest.get('expected_benchmark_schema_version')
           != 'chili.replay-capture-benchmark.v7'
        or manifest.get('candidate_root') != str(candidate_root)
        or type(manifest.get('expected_git_commit')) is not str
        or len(manifest['expected_git_commit']) != 40
        or any(c not in '0123456789abcdef'
               for c in manifest['expected_git_commit'])
        or manifest.get('source_roster') != expected_roster
        or manifest.get('source_roster_sha256')
           != hashlib.sha256(roster_raw).hexdigest()
        or manifest.get('benchmark_arguments') != list(arguments)
        or manifest.get('posture') != expected_posture):
    raise SystemExit(110)
python_authority = manifest.get('python')
dependency_authority = manifest.get('python_dependency_root')
output_authority = manifest.get('output')
execution_context = manifest.get('execution_context')
git_authority = manifest.get('git')
program_authority = manifest.get('authority_programs')
held_loader_authority = manifest.get('held_loader')
if (type(python_authority) is not dict
        or set(python_authority) != {
            'executable_path', 'executable_sha256', 'implementation',
            'isolation_flags', 'version'}
        or python_authority.get('executable_path')
           != str(Path(sys.executable).resolve(strict=True))
        or python_authority.get('executable_sha256')
           != expected_python_executable_sha256
        or python_authority.get('implementation') != 'cpython'
        or python_authority.get('isolation_flags') != ['-I', '-S', '-B']
        or python_authority.get('version') != [3, 11]
        or type(dependency_authority) is not dict
        or set(dependency_authority) != {
            'identity', 'identity_sha256', 'path',
            'required_distributions', 'tree_sha256'}
        or dependency_authority.get('path') != str(dependency_root)
        or dependency_authority.get('identity_sha256')
           != expected_dependency_identity
        or type(git_authority) is not dict
        or set(git_authority) != {
            'executable_path', 'executable_sha256'}
        or type(program_authority) is not dict
        or set(program_authority) != {'builder', 'runner'}
        or any(type(program_authority.get(role)) is not dict
               or set(program_authority[role]) != {'path', 'sha256'}
               for role in ('builder', 'runner'))
        or type(held_loader_authority) is not dict
        or set(held_loader_authority) != {
            'sha256', 'source_role', 'variable'}
        or type(held_loader_authority.get('sha256')) is not str
        or len(held_loader_authority['sha256']) != 64
        or any(c not in '0123456789abcdef'
               for c in held_loader_authority['sha256'])
        or held_loader_authority.get('source_role') != 'pressure_probe'
        or held_loader_authority.get('variable')
           != '_HELD_BENCHMARK_SOURCE_LOADER'
        or type(output_authority) is not dict
        or set(output_authority) != {
            'root', 'root_identity', 'storage_volume_identity',
            'storage_volume_identity_sha256'}
        or type(execution_context) is not dict
        or set(execution_context) != {
            'cwd', 'environment', 'environment_sha256', 'shell', 'stderr',
            'stdin', 'stdout', 'timeout_seconds'}
        or execution_context.get('cwd')
           != str(Path.cwd().resolve(strict=True))
        or execution_context.get('shell') is not False
        or execution_context.get('stdin') != 'devnull'
        or execution_context.get('stdout') != 'bounded_binary_pipe_64mib'
        or execution_context.get('stderr') != 'bounded_binary_pipe_1mib'
        or execution_context.get('timeout_seconds') != 3600):
    raise SystemExit(111)
def verify_authority_program(raw_path, expected_sha, exact_path, limit):
    if (type(raw_path) is not str or not Path(raw_path).is_absolute()
            or type(expected_sha) is not str or len(expected_sha) != 64
            or any(c not in '0123456789abcdef' for c in expected_sha)):
        raise SystemExit(115)
    path = Path(raw_path).resolve(strict=True)
    if exact_path is not None and path != exact_path.resolve(strict=True):
        raise SystemExit(115)
    before = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode)
            or int(getattr(before, 'st_file_attributes', 0)) & 0x400
            or int(before.st_size) <= 0 or int(before.st_size) > limit):
        raise SystemExit(115)
    descriptor = os.open(path, os.O_RDONLY | int(getattr(os, 'O_BINARY', 0)))
    try:
        opened = os.fstat(descriptor)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        terminal = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.stat(path, follow_symlinks=False)
    def program_identity(value):
        return (int(value.st_dev), int(value.st_ino), int(value.st_size),
                int(value.st_mtime_ns), int(stat.S_IFMT(value.st_mode)))
    if (program_identity(before) != program_identity(opened)
            or program_identity(opened) != program_identity(terminal)
            or program_identity(terminal) != program_identity(after)
            or hashlib.sha256(b''.join(chunks)).hexdigest() != expected_sha):
        raise SystemExit(115)
    return path
verify_authority_program(
    git_authority.get('executable_path'),
    git_authority.get('executable_sha256'), None, 256 * 1024 * 1024)
verify_authority_program(
    program_authority['builder'].get('path'),
    program_authority['builder'].get('sha256'),
    candidate_root / 'scripts' / 'build_captured_paper_benchmark_authority.py',
    64 * 1024 * 1024)
verify_authority_program(
    program_authority['runner'].get('path'),
    program_authority['runner'].get('sha256'),
    candidate_root / 'scripts' / 'run_captured_paper_benchmark_authority.py',
    64 * 1024 * 1024)
environment_authority = execution_context.get('environment')
if (type(environment_authority) is not dict
        or any(type(key) is not str or type(value) is not str
               for key, value in environment_authority.items())
        or dict(os.environ) != environment_authority
        or execution_context.get('environment_sha256')
           != hashlib.sha256(json.dumps(
               environment_authority, ensure_ascii=False, sort_keys=True,
               separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()):
    raise SystemExit(112)
output_root_raw = output_authority.get('root')
if (type(output_root_raw) is not str or not Path(output_root_raw).is_absolute()
        or len(arguments) < 2 or arguments[0] != '--output-root'
        or arguments[1] != output_root_raw):
    raise SystemExit(113)
output_root = Path(output_root_raw).resolve(strict=True)
output_stat = os.stat(output_root, follow_symlinks=False)
output_identity = {
    'st_dev': int(output_stat.st_dev), 'st_ino': int(output_stat.st_ino),
    'st_mode': int(output_stat.st_mode),
    'st_mtime_ns': int(output_stat.st_mtime_ns)}
volume_identity = {
    'normalized_anchor': os.path.normcase(
        os.path.normpath(output_root.anchor or os.sep)),
    'schema_version': 'chili.capture-storage-volume-identity.v1',
    'st_dev': int(output_stat.st_dev)}
volume_raw = json.dumps(
    volume_identity, ensure_ascii=False, sort_keys=True,
    separators=(',', ':'), allow_nan=False).encode('utf-8')
if (not stat.S_ISDIR(output_stat.st_mode)
        or int(getattr(output_stat, 'st_file_attributes', 0)) & 0x400
        or output_authority.get('root_identity') != output_identity
        or output_authority.get('storage_volume_identity') != volume_identity
        or output_authority.get('storage_volume_identity_sha256')
           != hashlib.sha256(volume_raw).hexdigest()
        or any(output_root.iterdir())):
    raise SystemExit(114)
initial_paths = tuple(Path(item).resolve(strict=False) for item in sys.path if item)
if any(path == candidate_root or candidate_root in path.parents
       or path == dependency_root or dependency_root in path.parents
       for path in initial_paths):
    raise SystemExit(101)
for name in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT',
             'PYTHONUSERBASE', 'PYTHON_ZSTANDARD_IMPORT_POLICY'):
    os.environ.pop(name, None)
os.environ['PYTHONNOUSERSITE'] = '1'
stage0_row = held['stage0']
stage0_scope = {'__name__': '_chili_benchmark_stage0',
                '__file__': stage0_row['path'], '__package__': None,
                '__cached__': None, '__builtins__': __builtins__}
exec(compile(stage0_row['source'], stage0_row['path'], 'exec', dont_inherit=True),
     stage0_scope, stage0_scope)
executable = Path(sys.executable).resolve(strict=True)
executable_source = executable.read_bytes()
executable_sha = hashlib.sha256(executable_source).hexdigest()
if executable_sha != expected_python_executable_sha256:
    raise SystemExit(105)
tree = stage0_scope['_dependency_tree_inventory'](
    dependency_root, retain_mutation_guards=True)
identity_body = stage0_scope['_dependency_root_identity_from_inventory'](
    root=dependency_root, executable=executable,
    python_executable_sha256=executable_sha, tree=tree)
identity_sha = hashlib.sha256(
    stage0_scope['_canonical_json_bytes'](dict(identity_body))).hexdigest()
if (identity_sha != expected_dependency_identity
        or dependency_authority.get('identity') != dict(identity_body)
        or dependency_authority.get('tree_sha256') != tree.get('tree_sha256')):
    raise SystemExit(100)
finder = stage0_scope['_SealedDependencyFinder'](
    root=dependency_root, files=tree['files'], guards=tuple(tree['guards']))
deny = stage0_scope['_DenyDependencyPathFinder']()
for relative in tree['files']:
    parent = dependency_root.joinpath(*Path(relative).parts).parent
    sys.path_importer_cache[str(parent)] = deny
sys.path_importer_cache[str(dependency_root)] = deny
sys.path.append(str(dependency_root))
sys.meta_path.insert(0, finder)
sys.meta_path.insert(1, stage0_scope['_DenyUnsealedImportFinder'](
    dependency_root=dependency_root))
benchmark = held['benchmark']
sys.argv[:] = [benchmark['path'], *arguments]
scope = {'__name__': '__main__', '__file__': benchmark['path'],
         '__package__': None, '__cached__': None,
         '__builtins__': __builtins__,
         '__chili_held_benchmark_sources__': MappingProxyType(held),
         '__chili_benchmark_dependency_identity_sha256__': identity_sha,
         '__chili_benchmark_python_executable_sha256__': executable_sha,
         '__chili_benchmark_authority_manifest_sha256__':
             benchmark_authority_manifest_sha256}
exec(compile(benchmark['source'], benchmark['path'], 'exec', dont_inherit=True),
     scope, scope)
""".strip()
_MAX_JSON_LINE_BYTES = 4096
_MAX_RESPONSE_TIMEOUT_SECONDS = 60.0
_PROBE_BYTES = b"\0" * 4096
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_CODE_RE = re.compile(r"[a-z0-9_]{1,64}")
_REQUEST_KEYS = frozenset(
    {"schema", "op", "session_nonce", "sequence", "request_nonce"}
)
_BASE_RESPONSE_KEYS = frozenset(
    {"schema", "status", "session_nonce", "sequence", "request_nonce"}
)
_READY_RESPONSE_KEYS = frozenset(
    {
        *_BASE_RESPONSE_KEYS,
        "helper_sha256",
        "probe_root_identity_sha256",
        "python_major",
        "python_minor",
        "write_latency_profile",
    }
)
_MEASUREMENT_RESPONSE_KEYS = frozenset(
    {
        *_BASE_RESPONSE_KEYS,
        "latency_ns",
        "bytes_written",
        "fsync_completed",
        "cleanup_completed",
        "probe_root_identity_sha256",
        "write_latency_profile",
    }
)
_ERROR_RESPONSE_KEYS = frozenset({*_BASE_RESPONSE_KEYS, "error_code"})


class CapturedPaperPressureProbeError(RuntimeError):
    """Base class for a fail-closed helper/client failure."""


class CapturedPaperPressureProbeProtocolError(CapturedPaperPressureProbeError):
    """The helper protocol or peer response was not exact."""


class CapturedPaperPressureProbeMeasurementError(OSError):
    """The helper could not produce a complete durable measurement."""


class CapturedPaperPressureProbeUnavailableError(OSError):
    """The helper missed a bounded response deadline and was reaped."""


class _ProbeFailure(Exception):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True, slots=True)
class CapturedPaperPressureMeasurement:
    """One exact helper response after the probe object was cleaned up."""

    sequence: int
    request_nonce: str
    latency_ns: int
    bytes_written: int
    fsync_completed: bool
    cleanup_completed: bool
    probe_root_identity_sha256: str
    write_latency_profile: str

    @property
    def latency_milliseconds(self) -> float:
        return float(self.latency_ns) / 1_000_000.0


@dataclass(frozen=True, slots=True)
class _Request:
    op: str
    session_nonce: str
    sequence: int
    request_nonce: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_nonce(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CapturedPaperPressureProbeProtocolError(
            f"{name} must be a full lowercase SHA-256 token"
        )
    return value


def _request_nonce(session_nonce: str, sequence: int, op: str) -> str:
    """Derive a unique request token without retaining an unbounded replay set."""

    key = bytes.fromhex(_canonical_nonce(session_nonce, "session_nonce"))
    material = f"{REQUEST_SCHEMA}|{sequence}|{op}".encode("ascii")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CapturedPaperPressureProbeProtocolError(
                "JSON object contains a duplicate key"
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise CapturedPaperPressureProbeProtocolError(
        f"non-finite JSON constant is forbidden: {value}"
    )


def _decode_json_line(raw: bytes) -> dict[str, Any]:
    if (
        not isinstance(raw, bytes)
        or not raw
        or len(raw) > _MAX_JSON_LINE_BYTES
        or not raw.endswith(b"\n")
    ):
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe JSON line is missing or oversized"
        )
    try:
        decoded = raw[:-1].decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_json_object_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except CapturedPaperPressureProbeProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe JSON line is malformed"
        ) from exc
    if not isinstance(value, dict):
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe JSON value must be an object"
        )
    return value


def _encode_json_line(value: Mapping[str, Any]) -> bytes:
    try:
        raw = (
            json.dumps(
                dict(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe response is not canonical JSON"
        ) from exc
    if len(raw) > _MAX_JSON_LINE_BYTES:
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe JSON line exceeds the protocol bound"
        )
    return raw


def _parse_request(
    raw: bytes,
    *,
    expected_sequence: int,
    expected_session_nonce: str | None,
) -> _Request:
    value = _decode_json_line(raw)
    if set(value) != _REQUEST_KEYS or value.get("schema") != REQUEST_SCHEMA:
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe request schema is not exact"
        )
    op = value.get("op")
    if op not in {"init", "measure", "close"}:
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe request operation is invalid"
        )
    session_nonce = _canonical_nonce(value.get("session_nonce"), "session_nonce")
    sequence = value.get("sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence != expected_sequence
        or sequence < 0
    ):
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe request sequence is not the next monotonic value"
        )
    if (sequence == 0) != (op == "init"):
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe initialization sequence is invalid"
        )
    if expected_session_nonce is not None and session_nonce != expected_session_nonce:
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe request session does not match"
        )
    request_nonce = _canonical_nonce(value.get("request_nonce"), "request_nonce")
    expected_request_nonce = _request_nonce(session_nonce, sequence, op)
    if not hmac.compare_digest(request_nonce, expected_request_nonce):
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe request nonce does not match"
        )
    return _Request(
        op=op,
        session_nonce=session_nonce,
        sequence=sequence,
        request_nonce=request_nonce,
    )


def _root_identity_sha256(root: Path, st_dev: int, st_ino: int) -> str:
    body = {
        "resolved_probe_root": os.path.normcase(str(root)),
        "st_dev": int(st_dev),
        "st_ino": int(st_ino),
    }
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _probe_payload(request_nonce: str) -> bytes:
    """Return deterministic, non-compressible bytes bound to one request."""

    nonce = bytes.fromhex(_canonical_nonce(request_nonce, "request_nonce"))
    blocks: list[bytes] = []
    for counter in range(128):
        blocks.append(
            hmac.new(
                nonce,
                b"captured-paper-pressure-probe:" + counter.to_bytes(4, "big"),
                hashlib.sha256,
            ).digest()
        )
    payload = b"".join(blocks)
    if len(payload) != len(_PROBE_BYTES):
        raise _ProbeFailure("probe_payload_invalid")
    return payload


def _resolve_fixed_probe_root(
    raw_root: str | Path,
    expected_st_dev: int,
    expected_st_ino: int,
) -> Path:
    if (
        isinstance(expected_st_dev, bool)
        or not isinstance(expected_st_dev, int)
        or expected_st_dev < 0
        or isinstance(expected_st_ino, bool)
        or not isinstance(expected_st_ino, int)
        or expected_st_ino < 0
    ):
        raise _ProbeFailure("probe_root_identity_invalid")
    supplied = Path(raw_root)
    if not supplied.is_absolute():
        raise _ProbeFailure("probe_root_identity_invalid")
    try:
        root = supplied.resolve(strict=True)
        stat_result = root.stat()
    except OSError as exc:
        raise _ProbeFailure("probe_root_unavailable") from exc
    if (
        not root.is_dir()
        or int(stat_result.st_dev) != expected_st_dev
        or int(stat_result.st_ino) != expected_st_ino
    ):
        raise _ProbeFailure("probe_root_identity_mismatch")
    return root


def _revalidate_probe_root(
    root: Path,
    expected_st_dev: int,
    expected_st_ino: int,
) -> None:
    try:
        current = root.resolve(strict=True)
        stat_result = current.stat()
    except OSError as exc:
        raise _ProbeFailure("probe_root_unavailable") from exc
    if (
        current != root
        or not current.is_dir()
        or int(stat_result.st_dev) != expected_st_dev
        or int(stat_result.st_ino) != expected_st_ino
    ):
        raise _ProbeFailure("probe_root_identity_changed")


def _measure_once(
    root: Path,
    expected_st_dev: int,
    expected_st_ino: int,
    request_nonce: str,
) -> dict[str, Any]:
    """Measure exactly one durable 4096-byte write and require cleanup."""

    _revalidate_probe_root(root, expected_st_dev, expected_st_ino)
    payload = _probe_payload(request_nonce)
    descriptor = -1
    temporary = root / f".chili-pressure-probe-{request_nonce}.tmp"
    created = False
    latency_ns: int | None = None
    bytes_written: int | None = None
    pending_error: _ProbeFailure | None = None
    cleanup_error: _ProbeFailure | None = None
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        # Windows delete-on-close keeps cleanup bound to the opened file object
        # even if a hostile local actor renames/replaces the probe directory.
        # Other platforms retain the explicit pathname cleanup below.
        delete_on_close = int(getattr(os, "O_TEMPORARY", 0))
        flags |= delete_on_close
        descriptor = os.open(str(temporary), flags, 0o600)
        created = True
        try:
            current_root = root.resolve(strict=True)
            current_root_stat = current_root.stat()
            opened = temporary.resolve(strict=True)
            opened_stat = os.fstat(descriptor)
            path_stat = os.lstat(temporary)
        except OSError as exc:
            raise _ProbeFailure("probe_file_identity_unavailable") from exc
        if (
            current_root != root
            or not current_root.is_dir()
            or int(current_root_stat.st_dev) != expected_st_dev
            or int(current_root_stat.st_ino) != expected_st_ino
            or opened.parent != root
            or int(opened_stat.st_dev) != expected_st_dev
            or int(path_stat.st_dev) != int(opened_stat.st_dev)
            or int(path_stat.st_ino) != int(opened_stat.st_ino)
        ):
            raise _ProbeFailure("probe_file_identity_mismatch")
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            started_ns = time.perf_counter_ns()
            bytes_written = handle.write(payload)
            if bytes_written != len(payload):
                raise _ProbeFailure("probe_write_incomplete")
            handle.flush()
            os.fsync(handle.fileno())
            completed_ns = time.perf_counter_ns()
            latency_ns = completed_ns - started_ns
            if os.fstat(handle.fileno()).st_size != len(payload):
                raise _ProbeFailure("probe_size_mismatch")
        if (
            isinstance(latency_ns, bool)
            or not isinstance(latency_ns, int)
            or latency_ns <= 0
        ):
            raise _ProbeFailure("probe_clock_invalid")
    except _ProbeFailure as exc:
        pending_error = exc
    except OSError as exc:
        pending_error = _ProbeFailure("probe_durable_flush_failed")
        pending_error.__cause__ = exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = _ProbeFailure("probe_cleanup_failed")
                cleanup_error.__cause__ = exc
        if created:
            try:
                # O_TEMPORARY deletes the exact opened Windows file object on
                # close.  A pathname unlink after a root rename could target a
                # replacement object, so use it only where delete-on-close is
                # unavailable.
                if delete_on_close == 0:
                    os.unlink(temporary)
            except OSError as exc:
                cleanup_error = _ProbeFailure("probe_cleanup_failed")
                cleanup_error.__cause__ = exc
            if os.path.lexists(temporary):
                cleanup_error = _ProbeFailure("probe_cleanup_incomplete")
    if cleanup_error is not None:
        raise cleanup_error
    if pending_error is not None:
        raise pending_error
    if latency_ns is None or bytes_written != len(payload):
        raise _ProbeFailure("probe_measurement_incomplete")
    return {
        "latency_ns": latency_ns,
        "bytes_written": bytes_written,
        "fsync_completed": True,
        "cleanup_completed": True,
    }


def _write_line(stream: BinaryIO, value: Mapping[str, Any]) -> None:
    stream.write(_encode_json_line(value))
    stream.flush()


def _read_line(stream: BinaryIO) -> bytes | None:
    raw = stream.readline(_MAX_JSON_LINE_BYTES + 1)
    if raw == b"":
        return None
    if len(raw) > _MAX_JSON_LINE_BYTES or not raw.endswith(b"\n"):
        raise CapturedPaperPressureProbeProtocolError(
            "pressure-probe input line is missing or oversized"
        )
    return raw


def _error_response(
    error_code: str,
    *,
    request: _Request | None = None,
) -> dict[str, Any]:
    if _ERROR_CODE_RE.fullmatch(error_code) is None:
        error_code = "internal_error"
    return {
        "schema": RESPONSE_SCHEMA,
        "status": "error",
        "session_nonce": "" if request is None else request.session_nonce,
        "sequence": -1 if request is None else request.sequence,
        "request_nonce": "" if request is None else request.request_nonce,
        "error_code": error_code,
    }


def _verify_child_runtime(expected_helper_sha256: str) -> str:
    expected = _canonical_nonce(expected_helper_sha256, "helper_sha256")
    flags = sys.flags
    if not (
        sys.version_info[:2] == (3, 11)
        and int(flags.isolated) == 1
        and int(flags.no_site) == 1
        and int(flags.dont_write_bytecode) == 1
        and int(flags.ignore_environment) == 1
        and bool(getattr(flags, "safe_path", True))
    ):
        raise _ProbeFailure("python_runtime_flags_invalid")
    helper_path = Path(__file__).resolve(strict=True)
    observed = _sha256_file(helper_path)
    if not hmac.compare_digest(observed, expected):
        raise _ProbeFailure("helper_sha256_mismatch")
    return observed


def _serve(
    *,
    expected_helper_sha256: str,
    raw_probe_root: str,
    expected_st_dev: int,
    expected_st_ino: int,
    stdin: BinaryIO,
    stdout: BinaryIO,
) -> int:
    try:
        helper_sha256 = _verify_child_runtime(expected_helper_sha256)
        root = _resolve_fixed_probe_root(
            raw_probe_root,
            expected_st_dev,
            expected_st_ino,
        )
    except (CapturedPaperPressureProbeProtocolError, _ProbeFailure) as exc:
        code = exc.error_code if isinstance(exc, _ProbeFailure) else "startup_invalid"
        _write_line(stdout, _error_response(code))
        return 2

    expected_sequence = 0
    session_nonce: str | None = None
    while True:
        request: _Request | None = None
        try:
            raw = _read_line(stdin)
            if raw is None:
                return 3
            request = _parse_request(
                raw,
                expected_sequence=expected_sequence,
                expected_session_nonce=session_nonce,
            )
            if expected_sequence == 0:
                if request.op != "init":
                    raise CapturedPaperPressureProbeProtocolError(
                        "the first pressure-probe request must initialize the session"
                    )
                session_nonce = request.session_nonce
                root_identity = _root_identity_sha256(
                    root,
                    expected_st_dev,
                    expected_st_ino,
                )
                _write_line(
                    stdout,
                    {
                        "schema": RESPONSE_SCHEMA,
                        "status": "ready",
                        "session_nonce": session_nonce,
                        "sequence": request.sequence,
                        "request_nonce": request.request_nonce,
                        "helper_sha256": helper_sha256,
                        "probe_root_identity_sha256": root_identity,
                        "python_major": 3,
                        "python_minor": 11,
                        "write_latency_profile": PRESSURE_WRITE_LATENCY_PROFILE,
                    },
                )
            elif request.op == "measure":
                measurement = _measure_once(
                    root,
                    expected_st_dev,
                    expected_st_ino,
                    request.request_nonce,
                )
                _write_line(
                    stdout,
                    {
                        "schema": RESPONSE_SCHEMA,
                        "status": "ok",
                        "session_nonce": session_nonce,
                        "sequence": request.sequence,
                        "request_nonce": request.request_nonce,
                        **measurement,
                        "probe_root_identity_sha256": _root_identity_sha256(
                            root,
                            expected_st_dev,
                            expected_st_ino,
                        ),
                        "write_latency_profile": PRESSURE_WRITE_LATENCY_PROFILE,
                    },
                )
            elif request.op == "close":
                _write_line(
                    stdout,
                    {
                        "schema": RESPONSE_SCHEMA,
                        "status": "closed",
                        "session_nonce": session_nonce,
                        "sequence": request.sequence,
                        "request_nonce": request.request_nonce,
                    },
                )
                return 0
            else:
                raise CapturedPaperPressureProbeProtocolError(
                    "pressure-probe operation is invalid for this sequence"
                )
            expected_sequence += 1
        except CapturedPaperPressureProbeProtocolError:
            _write_line(stdout, _error_response("protocol_invalid", request=request))
            return 4
        except _ProbeFailure as exc:
            _write_line(stdout, _error_response(exc.error_code, request=request))
            return 5
        except OSError:
            _write_line(stdout, _error_response("probe_io_failed", request=request))
            return 6


def _minimal_child_environment() -> dict[str, str]:
    """Do not copy provider/broker secrets into the broker-incapable helper."""

    allowed = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _close_windows_handle(handle: int | None) -> bool:
    if handle is None:
        return True
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return bool(kernel32.CloseHandle(ctypes.c_void_p(handle)))


def _create_kill_on_close_job(process_id: int) -> int | None:
    """Assign the helper to an owner-held Windows kill-on-close job.

    The helper waits for an authenticated ``init`` request before it can touch
    storage, so assignment occurs before any probe operation.  If the service
    is terminated abnormally, Windows closes its last job handle and kills a
    child even when that child is blocked below Python in a durable flush.
    """

    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
    job_value = int(job)
    process_handle: int | None = None
    try:
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            ctypes.c_void_p(job_value),
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise OSError(
                ctypes.get_last_error(),
                "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed",
            )
        process = kernel32.OpenProcess(0x0101, False, int(process_id))
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        process_handle = int(process)
        if not kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(job_value),
            ctypes.c_void_p(process_handle),
        ):
            raise OSError(
                ctypes.get_last_error(),
                "AssignProcessToJobObject failed",
            )
        return job_value
    except BaseException:
        _close_windows_handle(job_value)
        raise
    finally:
        _close_windows_handle(process_handle)


class CapturedPaperPressureProbeClient:
    """Sequential, timeout-bounded owner of one persistent private-pipe helper."""

    def __init__(
        self,
        *,
        python_executable: str | Path,
        probe_root: str | Path,
        helper_path: str | Path,
        expected_helper_sha256: str,
        response_timeout_seconds: float = 5.0,
    ) -> None:
        timeout = float(response_timeout_seconds)
        if (
            not math.isfinite(timeout)
            or timeout <= 0.0
            or timeout > _MAX_RESPONSE_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "pressure-probe response timeout must be in (0, 60] seconds"
            )
        self._python_executable = Path(python_executable).resolve(strict=True)
        if not self._python_executable.is_file():
            raise ValueError("pressure-probe Python executable is not a file")
        self._helper_path = Path(helper_path).resolve(strict=True)
        if not self._helper_path.is_file():
            raise ValueError("pressure-probe helper is not a file")
        self._probe_root = Path(probe_root).resolve(strict=True)
        if not self._probe_root.is_dir():
            raise ValueError("pressure-probe root is not a directory")
        probe_root_stat = self._probe_root.stat()
        self._probe_root_st_dev = int(probe_root_stat.st_dev)
        self._probe_root_st_ino = int(probe_root_stat.st_ino)
        observed_helper_sha256 = _sha256_file(self._helper_path)
        self._helper_sha256 = _canonical_nonce(
            expected_helper_sha256, "expected_helper_sha256"
        )
        if not hmac.compare_digest(
            observed_helper_sha256, self._helper_sha256
        ):
            raise CapturedPaperPressureProbeProtocolError(
                "pressure-probe helper bytes do not match sealed authority"
            )
        self._root_identity_sha256 = _root_identity_sha256(
            self._probe_root,
            self._probe_root_st_dev,
            self._probe_root_st_ino,
        )
        self._timeout = timeout
        self._session_nonce = secrets.token_hex(32)
        self._sequence = -1
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._reader_thread: threading.Thread | None = None
        self._job_handle: int | None = None
        self._inflight_candidate: Path | None = None
        self._lock = threading.RLock()
        self._ever_started = False
        self._closed = False

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid

    @property
    def helper_sha256(self) -> str:
        return self._helper_sha256

    @property
    def probe_root_identity_sha256(self) -> str:
        return self._root_identity_sha256

    def _reader_main(self, stream: BinaryIO) -> None:
        try:
            while True:
                raw = stream.readline(_MAX_JSON_LINE_BYTES + 1)
                self._responses.put(raw if raw else None)
                if not raw:
                    return
        except BaseException:
            self._responses.put(None)

    def _abort_locked(self, *, permanent: bool = True) -> None:
        process = self._process
        if process is None:
            reader = self._reader_thread
            job_closed = _close_windows_handle(self._job_handle)
            if job_closed:
                self._job_handle = None
            if (
                (reader is not None and reader.is_alive())
                or not job_closed
            ):
                self._closed = True
                raise CapturedPaperPressureProbeProtocolError(
                    "pressure-probe process cleanup did not complete"
                )
            self._reader_thread = None
            self._closed = permanent
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        job_close_failed = False
        if self._job_handle is not None:
            if _close_windows_handle(self._job_handle):
                self._job_handle = None
            else:
                job_close_failed = True
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        reader = self._reader_thread
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)
        candidate = self._inflight_candidate
        candidate_present = False
        if candidate is not None:
            try:
                if candidate.parent != self._probe_root:
                    raise CapturedPaperPressureProbeProtocolError(
                        "pressure-probe cleanup target escaped its fixed root"
                    )
                if os.path.lexists(candidate):
                    os.unlink(candidate)
                candidate_present = os.path.lexists(candidate)
            except (OSError, CapturedPaperPressureProbeProtocolError):
                candidate_present = True
        cleanup_incomplete = bool(
            process.poll() is None
            or (reader is not None and reader.is_alive())
            or candidate_present
            or job_close_failed
        )
        if cleanup_incomplete:
            # Retain every identity/handle so a later explicit close can retry
            # cleanup and callers cannot mistake an orphan for a closed client.
            self._closed = True
            raise CapturedPaperPressureProbeProtocolError(
                "pressure-probe process cleanup did not complete"
            )
        self._process = None
        self._reader_thread = None
        self._inflight_candidate = None
        self._closed = permanent
        self._sequence = -1
        self._responses = queue.SimpleQueue()
        if not permanent:
            self._session_nonce = secrets.token_hex(32)

    def _validate_base_response(
        self,
        value: dict[str, Any],
        *,
        sequence: int,
        request_nonce: str,
    ) -> str:
        status = value.get("status")
        if (
            value.get("schema") != RESPONSE_SCHEMA
            or value.get("session_nonce") != self._session_nonce
            or value.get("sequence") != sequence
            or value.get("request_nonce") != request_nonce
            or type(value.get("sequence")) is not int
            or not isinstance(status, str)
        ):
            raise CapturedPaperPressureProbeProtocolError(
                "pressure-probe response identity is not exact"
            )
        return status

    def _exchange(
        self, op: str, *, response_timeout_seconds: float | None = None
    ) -> tuple[dict[str, Any], int]:
        process = self._process
        if (
            process is None
            or process.poll() is not None
            or process.stdin is None
        ):
            raise CapturedPaperPressureProbeProtocolError(
                "pressure-probe helper is not running"
            )
        sequence = self._sequence + 1
        request_nonce = _request_nonce(self._session_nonce, sequence, op)
        if op == "measure":
            candidate = (
                self._probe_root
                / f".chili-pressure-probe-{request_nonce}.tmp"
            )
            if candidate.parent != self._probe_root or os.path.lexists(candidate):
                raise CapturedPaperPressureProbeProtocolError(
                    "pressure-probe candidate is not exclusively available"
                )
            self._inflight_candidate = candidate
        request = {
            "schema": REQUEST_SCHEMA,
            "op": op,
            "session_nonce": self._session_nonce,
            "sequence": sequence,
            "request_nonce": request_nonce,
        }
        timeout = (
            self._timeout
            if response_timeout_seconds is None
            else min(self._timeout, float(response_timeout_seconds))
        )
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise CapturedPaperPressureProbeProtocolError(
                "pressure-probe response bound is invalid"
            )
        started_ns = time.perf_counter_ns()
        try:
            process.stdin.write(_encode_json_line(request))
            process.stdin.flush()
            before_wait_ns = time.perf_counter_ns()
            elapsed_before_wait_ns = before_wait_ns - started_ns
            timeout_ns = int(timeout * 1_000_000_000.0)
            if elapsed_before_wait_ns < 0 or elapsed_before_wait_ns >= timeout_ns:
                raise CapturedPaperPressureProbeUnavailableError(
                    "pressure-probe request exceeded its wall-RTT bound"
                )
            raw = self._responses.get(
                timeout=float(timeout_ns - elapsed_before_wait_ns) / 1_000_000_000.0
            )
        except CapturedPaperPressureProbeUnavailableError:
            raise
        except queue.Empty as exc:
            raise CapturedPaperPressureProbeUnavailableError(
                "pressure-probe helper missed its bounded response deadline"
            ) from exc
        except (BrokenPipeError, OSError) as exc:
            raise CapturedPaperPressureProbeProtocolError(
                "pressure-probe helper pipe failed"
            ) from exc
        completed_ns = time.perf_counter_ns()
        round_trip_ns = completed_ns - started_ns
        if round_trip_ns < 0 or round_trip_ns > int(
            timeout * 1_000_000_000.0
        ):
            raise CapturedPaperPressureProbeUnavailableError(
                "pressure-probe response exceeded its wall-RTT bound"
            )
        if raw is None:
            raise CapturedPaperPressureProbeProtocolError(
                "pressure-probe helper closed its response pipe"
            )
        value = _decode_json_line(raw)
        status = self._validate_base_response(
            value, sequence=sequence, request_nonce=request_nonce
        )
        if status == "error":
            if set(value) != _ERROR_RESPONSE_KEYS or _ERROR_CODE_RE.fullmatch(
                str(value.get("error_code") or "")
            ) is None:
                raise CapturedPaperPressureProbeProtocolError(
                    "pressure-probe error response is malformed"
                )
            raise CapturedPaperPressureProbeMeasurementError(
                str(value["error_code"])
            )
        self._sequence = sequence
        return value, round_trip_ns

    def _start_locked(
        self,
        *,
        response_timeout_seconds: float | None = None,
        permanent_on_failure: bool,
    ) -> None:
        if self._closed or self._process is not None:
            raise CapturedPaperPressureProbeProtocolError(
                "pressure-probe client cannot start in its current state"
            )
        try:
            argv = [
                str(self._python_executable),
                "-I",
                "-S",
                "-B",
                "-c",
                _HELD_SOURCE_LOADER,
                str(self._helper_path),
                self._helper_sha256,
                "--serve",
                "--self-sha256",
                self._helper_sha256,
                "--probe-root",
                str(self._probe_root),
                "--probe-root-st-dev",
                str(self._probe_root_st_dev),
                "--probe-root-st-ino",
                str(self._probe_root_st_ino),
            ]
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                bufsize=0,
                env=_minimal_child_environment(),
                creationflags=creationflags,
            )
            self._process = process
            self._job_handle = _create_kill_on_close_job(process.pid)
            if process.stdout is None:
                raise CapturedPaperPressureProbeProtocolError(
                    "pressure-probe response pipe is unavailable"
                )
            self._reader_thread = threading.Thread(
                target=self._reader_main,
                args=(process.stdout,),
                name="captured-paper-pressure-probe-reader",
                daemon=True,
            )
            self._reader_thread.start()
            response, _round_trip_ns = self._exchange(
                "init", response_timeout_seconds=response_timeout_seconds
            )
            if (
                set(response) != _READY_RESPONSE_KEYS
                or response.get("status") != "ready"
                or response.get("helper_sha256") != self._helper_sha256
                or response.get("probe_root_identity_sha256")
                != self._root_identity_sha256
                or type(response.get("python_major")) is not int
                or response.get("python_major") != 3
                or type(response.get("python_minor")) is not int
                or response.get("python_minor") != 11
                or response.get("write_latency_profile")
                != PRESSURE_WRITE_LATENCY_PROFILE
            ):
                raise CapturedPaperPressureProbeProtocolError(
                    "pressure-probe ready response is not exact"
                )
        except (
            CapturedPaperPressureProbeMeasurementError,
            CapturedPaperPressureProbeUnavailableError,
        ):
            self._abort_locked(permanent=permanent_on_failure)
            raise
        except BaseException:
            self._abort_locked(permanent=True)
            raise

    def start(self) -> None:
        with self._lock:
            if self._ever_started:
                raise CapturedPaperPressureProbeProtocolError(
                    "pressure-probe client start is one-shot"
                )
            self._ever_started = True
            self._start_locked(permanent_on_failure=True)

    def measure(
        self, *, response_timeout_seconds: float | None = None
    ) -> CapturedPaperPressureMeasurement:
        with self._lock:
            operation_timeout = (
                self._timeout
                if response_timeout_seconds is None
                else min(self._timeout, float(response_timeout_seconds))
            )
            if not math.isfinite(operation_timeout) or operation_timeout <= 0.0:
                raise CapturedPaperPressureProbeUnavailableError(
                    "pressure-probe measurement budget is unavailable"
                )
            operation_started_ns = time.perf_counter_ns()
            try:
                if self._process is None and not self._closed and self._ever_started:
                    self._start_locked(
                        response_timeout_seconds=operation_timeout,
                        permanent_on_failure=False,
                    )
                elapsed_ns = time.perf_counter_ns() - operation_started_ns
                remaining_seconds = (
                    operation_timeout - float(elapsed_ns) / 1_000_000_000.0
                )
                if elapsed_ns < 0 or remaining_seconds <= 0.0:
                    raise CapturedPaperPressureProbeUnavailableError(
                        "pressure-probe measurement budget was exhausted"
                    )
                response, round_trip_ns = self._exchange(
                    "measure",
                    response_timeout_seconds=remaining_seconds,
                )
                if set(response) != _MEASUREMENT_RESPONSE_KEYS:
                    raise CapturedPaperPressureProbeProtocolError(
                        "pressure-probe measurement response is not exact"
                    )
                latency_ns = response.get("latency_ns")
                if (
                    response.get("status") != "ok"
                    or isinstance(latency_ns, bool)
                    or not isinstance(latency_ns, int)
                    or latency_ns <= 0
                    or latency_ns > round_trip_ns
                    or latency_ns
                    > int(
                        min(
                            self._timeout,
                            remaining_seconds,
                        )
                        * 1_000_000_000.0
                    )
                    or type(response.get("bytes_written")) is not int
                    or response.get("bytes_written") != len(_PROBE_BYTES)
                    or response.get("fsync_completed") is not True
                    or response.get("cleanup_completed") is not True
                    or response.get("probe_root_identity_sha256")
                    != self._root_identity_sha256
                    or response.get("write_latency_profile")
                    != PRESSURE_WRITE_LATENCY_PROFILE
                ):
                    raise CapturedPaperPressureProbeProtocolError(
                        "pressure-probe measurement is incomplete"
                    )
                measurement = CapturedPaperPressureMeasurement(
                    sequence=int(response["sequence"]),
                    request_nonce=str(response["request_nonce"]),
                    latency_ns=latency_ns,
                    bytes_written=int(response["bytes_written"]),
                    fsync_completed=True,
                    cleanup_completed=True,
                    probe_root_identity_sha256=str(
                        response["probe_root_identity_sha256"]
                    ),
                    write_latency_profile=PRESSURE_WRITE_LATENCY_PROFILE,
                )
                self._inflight_candidate = None
                return measurement
            except (
                CapturedPaperPressureProbeMeasurementError,
                CapturedPaperPressureProbeUnavailableError,
            ):
                self._abort_locked(permanent=False)
                raise
            except BaseException:
                self._abort_locked(permanent=True)
                raise

    def close(self) -> None:
        with self._lock:
            if (
                self._closed
                and self._process is None
                and (
                    self._reader_thread is None
                    or not self._reader_thread.is_alive()
                )
                and self._inflight_candidate is None
                and self._job_handle is None
            ):
                return
            process = self._process
            if process is None:
                self._abort_locked(permanent=True)
                return
            try:
                response, _round_trip_ns = self._exchange("close")
                if set(response) != _BASE_RESPONSE_KEYS or response.get(
                    "status"
                ) != "closed":
                    raise CapturedPaperPressureProbeProtocolError(
                        "pressure-probe close response is not exact"
                    )
                process.wait(timeout=self._timeout)
                if process.returncode != 0:
                    raise CapturedPaperPressureProbeProtocolError(
                        "pressure-probe helper did not close cleanly"
                    )
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
                reader = self._reader_thread
                if reader is not None and reader.is_alive():
                    reader.join(timeout=1.0)
                if reader is not None and reader.is_alive():
                    raise CapturedPaperPressureProbeProtocolError(
                        "pressure-probe response reader did not quiesce"
                    )
                self._reader_thread = None
                self._process = None
                if not _close_windows_handle(self._job_handle):
                    raise CapturedPaperPressureProbeProtocolError(
                        "pressure-probe job handle did not close"
                    )
                self._job_handle = None
                self._closed = True
            except BaseException:
                self._abort_locked()
                raise

    def __enter__(self) -> "CapturedPaperPressureProbeClient":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.close()
        else:
            with self._lock:
                self._abort_locked()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--self-sha256", required=True)
    parser.add_argument("--probe-root", required=True)
    parser.add_argument("--probe-root-st-dev", required=True, type=int)
    parser.add_argument("--probe-root-st-ino", required=True, type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return _serve(
        expected_helper_sha256=str(args.self_sha256),
        raw_probe_root=str(args.probe_root),
        expected_st_dev=int(args.probe_root_st_dev),
        expected_st_ino=int(args.probe_root_st_ino),
        stdin=sys.stdin.buffer,
        stdout=sys.stdout.buffer,
    )


if __name__ == "__main__":
    raise SystemExit(main())
