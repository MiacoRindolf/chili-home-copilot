"""Stdlib-only isolated bootstrap for captured Alpaca PAPER entrypoints.

This file is executed only as a content-addressed staged copy with
``python -I -S -B``.  It authenticates the activation manifest, the complete
local code roster, the explicit system dependency-root identity, and the
selected target *before* admitting either import root.  The selected target is
compiled from the already verified bytes held in memory, so the final handoff
does not reopen a mutable path.

No project package, ``site``, dotenv loader, or third-party module may be
imported here.  Keep this module standard-library-only and side-effect-free
until :func:`_admit_and_execute` completes every verification step.
"""

from __future__ import annotations

import hashlib
import io
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
from types import MappingProxyType
from typing import Any, Mapping, Sequence


STAGE0_SCHEMA_VERSION = "chili.captured-paper-isolated-stage0.v2"
DEPENDENCY_ROOT_IDENTITY_SCHEMA_VERSION = (
    "chili.captured-paper-python-dependency-root-identity.v2"
)
_MANIFEST_SCHEMAS = frozenset(
    {"chili.captured-paper-preactivation.v2", "chili.captured-paper-activation.v3"}
)
_TARGET_ROLES = frozenset({"activation_service", "captured_paper_host_cutover"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_REPARSE_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_LOCAL_SOURCE_FILES = 4_096
_MAX_LOCAL_SOURCE_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_DEPENDENCY_FILES = 8_192
_MAX_DEPENDENCY_TOTAL_BYTES = 512 * 1024 * 1024
_DEPENDENCY_TREE_SCHEMA_VERSION = "chili.python-dependency-tree.v2"
_DEPENDENCY_EXCLUSION_POLICY = "exclude-__pycache__-pyc-pyo.v1"


class IsolatedStage0Error(RuntimeError):
    """Stable fail-closed bootstrap error without authority-bearing detail."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(self.code)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IsolatedStage0Error("NON_CANONICAL_JSON") from exc


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(candidate) is None:
        raise IsolatedStage0Error("INVALID_SHA256")
    return candidate


def _strict_json(raw: bytes) -> Mapping[str, Any]:
    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise IsolatedStage0Error("INVALID_JSON") from exc
    if not isinstance(value, dict) or raw != _canonical_json_bytes(value):
        raise IsolatedStage0Error("NON_CANONICAL_JSON")
    return MappingProxyType(value)


def _is_local_absolute(path: Path) -> bool:
    value = str(path)
    if os.name == "nt":
        return bool(path.is_absolute() and not value.startswith(("\\\\", "//")))
    return path.is_absolute()


def _reject_reparse_chain(path: Path) -> None:
    cursor = path
    while True:
        try:
            metadata = os.lstat(cursor)
        except OSError as exc:
            raise IsolatedStage0Error("PATH_UNAVAILABLE") from exc
        if stat.S_ISLNK(metadata.st_mode) or (
            int(getattr(metadata, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE
        ):
            raise IsolatedStage0Error("REPARSE_PATH_REJECTED")
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent


def _strict_path(value: Any, *, require_file: bool | None) -> Path:
    path = Path(str(value or ""))
    if not _is_local_absolute(path):
        raise IsolatedStage0Error("NONLOCAL_PATH")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise IsolatedStage0Error("PATH_UNAVAILABLE") from exc
    _reject_reparse_chain(resolved)
    if require_file is True and not resolved.is_file():
        raise IsolatedStage0Error("REGULAR_FILE_REQUIRED")
    if require_file is False and not resolved.is_dir():
        raise IsolatedStage0Error("DIRECTORY_REQUIRED")
    return resolved


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _stable_read(path: Path, *, expected_sha256: str, max_bytes: int) -> bytes:
    expected = _sha(expected_sha256)
    _reject_reparse_chain(path)
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise IsolatedStage0Error("INVALID_FILE")
    first = path.read_bytes()
    middle = os.stat(path, follow_symlinks=False)
    second = path.read_bytes()
    after = os.stat(path, follow_symlinks=False)
    identity = lambda item: (
        int(item.st_dev),
        int(item.st_ino),
        int(item.st_size),
        int(item.st_mtime_ns),
        int(item.st_mode),
    )
    if (
        identity(before) != identity(middle)
        or identity(middle) != identity(after)
        or first != second
        or _sha256_bytes(first) != expected
    ):
        raise IsolatedStage0Error("FILE_DRIFT")
    return first


def _dependency_path_is_excluded(relative: str) -> bool:
    parts = tuple(part.casefold() for part in Path(relative).parts)
    return "__pycache__" in parts or relative.casefold().endswith((".pyc", ".pyo"))


def _open_dependency_guard(path: Path) -> Any:
    """Open one dependency file while denying mutation where the host permits it.

    Captured PAPER is a Windows-hosted lane.  ``CreateFileW`` with only
    ``FILE_SHARE_READ`` permits the interpreter/loader to read the file while
    denying writes, renames, and deletion until the retained handle closes.
    Other platforms retain a read descriptor and rely on the import-time hash
    verifier; the attestation names that weaker mode instead of overstating it.
    """

    if os.name != "nt":
        return path.open("rb", buffering=0)
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ; deliberately no WRITE/DELETE sharing
            None,
            3,  # OPEN_EXISTING
            0x08000000,  # FILE_FLAG_SEQUENTIAL_SCAN
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in (None, invalid):
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
        except BaseException:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
            raise
        return os.fdopen(descriptor, "rb", buffering=0)
    except (ImportError, OSError, ValueError) as exc:
        raise IsolatedStage0Error("DEPENDENCY_MUTATION_GUARD_FAILED") from exc


def _dependency_tree_inventory(
    root: Path, *, retain_mutation_guards: bool = False
) -> Mapping[str, Any]:
    """Hash every admitted dependency byte without importing or running site.

    The returned private ``files`` and ``guards`` members are deliberately not
    included in the public root identity.  They let stage0 enforce the frozen
    inventory after admission rather than trusting a one-time Merkle scan.
    """

    _reject_reparse_chain(root)
    first_paths: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if _dependency_path_is_excluded(relative):
            continue
        first_paths.append((relative, path))
    first_paths.sort(key=lambda item: (item[0].casefold(), item[0]))
    casefolded: set[str] = set()
    rows: list[Mapping[str, Any]] = []
    total_bytes = 0
    file_count = 0
    directory_count = 0
    files: dict[str, Mapping[str, Any]] = {}
    guards: list[Any] = []
    try:
        for relative, path in first_paths:
            folded = relative.casefold()
            if folded in casefolded:
                raise IsolatedStage0Error("DEPENDENCY_CASEFOLD_COLLISION")
            casefolded.add(folded)
            before = os.lstat(path)
            if stat.S_ISLNK(before.st_mode) or (
                int(getattr(before, "st_file_attributes", 0)) & _REPARSE_ATTRIBUTE
            ):
                raise IsolatedStage0Error("REPARSE_PATH_REJECTED")
            if stat.S_ISDIR(before.st_mode):
                rows.append({"path": relative, "type": "directory"})
                directory_count += 1
                continue
            if not stat.S_ISREG(before.st_mode):
                raise IsolatedStage0Error("DEPENDENCY_SPECIAL_FILE")
            digest = hashlib.sha256()
            observed_size = 0
            try:
                handle = (
                    _open_dependency_guard(path)
                    if retain_mutation_guards
                    else path.open("rb", buffering=0)
                )
                if retain_mutation_guards:
                    guards.append(handle)
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    observed_size += len(chunk)
                    digest.update(chunk)
                if not retain_mutation_guards:
                    handle.close()
                else:
                    handle.seek(0)
            except OSError as exc:
                raise IsolatedStage0Error("DEPENDENCY_READ_FAILED") from exc
            after = os.stat(path, follow_symlinks=False)
            guarded = os.fstat(handle.fileno()) if retain_mutation_guards else after
            identity = lambda item: (
                int(item.st_dev), int(item.st_ino), int(item.st_size),
                int(item.st_mtime_ns),
            )
            if (
                not stat.S_ISREG(guarded.st_mode)
                or identity(before) != identity(after)
                or identity(after) != identity(guarded)
                or observed_size != int(before.st_size)
            ):
                raise IsolatedStage0Error("DEPENDENCY_TREE_DRIFT")
            row = {
                "path": relative,
                "sha256": digest.hexdigest(),
                "size_bytes": observed_size,
                "type": "file",
            }
            rows.append(row)
            files[relative] = MappingProxyType(dict(row))
            file_count += 1
            total_bytes += observed_size
            if (
                file_count > _MAX_DEPENDENCY_FILES
                or total_bytes > _MAX_DEPENDENCY_TOTAL_BYTES
            ):
                raise IsolatedStage0Error("DEPENDENCY_RESOURCE_BUDGET_EXCEEDED")
    except BaseException:
        for handle in guards:
            try:
                handle.close()
            except OSError:
                pass
        raise
    second = sorted(
        (
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if not _dependency_path_is_excluded(path.relative_to(root).as_posix())
        ),
        key=lambda value: (value.casefold(), value),
    )
    if [relative for relative, _path in first_paths] != second:
        raise IsolatedStage0Error("DEPENDENCY_TREE_DRIFT")
    body = {
        "entries": rows,
        "exclusion_policy": _DEPENDENCY_EXCLUSION_POLICY,
        "schema_version": _DEPENDENCY_TREE_SCHEMA_VERSION,
    }
    return MappingProxyType(
        {
            "directory_count": directory_count,
            "exclusion_policy": _DEPENDENCY_EXCLUSION_POLICY,
            "files": MappingProxyType(files),
            "file_count": file_count,
            "guards": tuple(guards),
            "mutation_guard_mode": (
                "windows-deny-write-delete-held-handles.v1"
                if retain_mutation_guards and os.name == "nt"
                else "import-time-hash-held-read-handles.v1"
                if retain_mutation_guards
                else "offline-inventory-only.v1"
            ),
            "total_bytes": total_bytes,
            "tree_sha256": _sha256_bytes(_canonical_json_bytes(body)),
        }
    )


def dependency_root_identity(
    *,
    dependency_root: Path,
    python_executable: Path,
    python_executable_sha256: str,
) -> Mapping[str, Any]:
    """Return the canonical identity body shared with the offline builder."""

    root = _strict_path(dependency_root, require_file=False)
    executable = _strict_path(python_executable, require_file=True)
    tree = _dependency_tree_inventory(root)
    return _dependency_root_identity_from_inventory(
        root=root,
        executable=executable,
        python_executable_sha256=python_executable_sha256,
        tree=tree,
    )


def _dependency_root_identity_from_inventory(
    *,
    root: Path,
    executable: Path,
    python_executable_sha256: str,
    tree: Mapping[str, Any],
) -> Mapping[str, Any]:
    metadata = os.stat(root, follow_symlinks=False)
    return MappingProxyType(
        {
            "path": str(root),
            "python_executable_path": str(executable),
            "python_executable_sha256": _sha(python_executable_sha256),
            "schema_version": DEPENDENCY_ROOT_IDENTITY_SCHEMA_VERSION,
            "st_dev": int(metadata.st_dev),
            "st_ino": int(metadata.st_ino),
            "st_mode": int(metadata.st_mode),
            "st_mtime_ns": int(metadata.st_mtime_ns),
            "tree_directory_count": tree["directory_count"],
            "tree_exclusion_policy": tree["exclusion_policy"],
            "tree_file_count": tree["file_count"],
            "tree_sha256": tree["tree_sha256"],
            "tree_total_bytes": tree["total_bytes"],
        }
    )


def dependency_root_identity_sha256(
    *,
    dependency_root: Path,
    python_executable: Path,
    python_executable_sha256: str,
) -> str:
    return _sha256_bytes(
        _canonical_json_bytes(
            dict(
                dependency_root_identity(
                    dependency_root=dependency_root,
                    python_executable=python_executable,
                    python_executable_sha256=python_executable_sha256,
                )
            )
        )
    )


def _parse_argv(argv: Sequence[str]) -> tuple[Mapping[str, str], tuple[str, ...]]:
    values = tuple(str(item) for item in argv)
    try:
        boundary = values.index("--")
    except ValueError as exc:
        raise IsolatedStage0Error("ARGV_INVALID") from exc
    bootstrap = values[:boundary]
    target_arguments = values[boundary + 1 :]
    if len(bootstrap) != 12 or not target_arguments:
        raise IsolatedStage0Error("ARGV_INVALID")
    expected = (
        "--manifest",
        "--manifest-sha256",
        "--candidate-root",
        "--target-role",
        "--target",
        "--target-sha256",
    )
    parsed: dict[str, str] = {}
    for index, option in enumerate(expected):
        if bootstrap[index * 2] != option or not bootstrap[index * 2 + 1]:
            raise IsolatedStage0Error("ARGV_INVALID")
        parsed[option] = bootstrap[index * 2 + 1]
    if parsed["--target-role"] not in _TARGET_ROLES:
        raise IsolatedStage0Error("TARGET_ROLE_INVALID")
    return MappingProxyType(parsed), target_arguments


def _assert_initial_isolation(candidate_root: Path) -> tuple[Path, ...]:
    flags = sys.flags
    if not (
        int(flags.isolated) == 1
        and int(flags.no_site) == 1
        and int(flags.dont_write_bytecode) == 1
        and "site" not in sys.modules
    ):
        raise IsolatedStage0Error("PYTHON_ISOLATION_REQUIRED")
    initial: list[Path] = []
    for item in sys.path:
        if not item:
            raise IsolatedStage0Error("INITIAL_IMPORT_PATH_INVALID")
        candidate = Path(item)
        if not _is_local_absolute(candidate):
            raise IsolatedStage0Error("INITIAL_IMPORT_PATH_INVALID")
        if not candidate.exists():
            # CPython commonly includes a nonexistent stdlib zip placeholder.
            # It admits no bytes and is unnecessary after stage0 verification.
            continue
        resolved = _strict_path(candidate, require_file=None)
        if resolved == candidate_root or _inside(resolved, candidate_root):
            raise IsolatedStage0Error("CANDIDATE_IMPORTED_BEFORE_VERIFICATION")
        initial.append(resolved)
    return tuple(initial)


def _assert_no_unsealed_bootstrap_modules(
    candidate_root: Path, *, roster_paths: frozenset[Path]
) -> None:
    for relative in (
        "sitecustomize.py",
        "usercustomize.py",
        "dotenv.py",
        "dotenv/__init__.py",
        "scripts/__init__.py",
        "app/__init__.py",
    ):
        candidate = candidate_root / Path(relative)
        if not candidate.exists():
            continue
        resolved = _strict_path(candidate, require_file=True)
        if relative in {
            "sitecustomize.py",
            "usercustomize.py",
            "dotenv.py",
            "dotenv/__init__.py",
        } or resolved not in roster_paths:
            raise IsolatedStage0Error("UNSEALED_BOOTSTRAP_MODULE")


class _HeldSourceLoader(importlib.abc.Loader):
    def __init__(self, *, name: str, path: Path, raw: bytes, is_package: bool) -> None:
        self._name = name
        self._path = path
        self._raw = raw
        self._is_package = is_package

    def create_module(self, _spec: Any) -> None:
        return None

    def exec_module(self, module: Any) -> None:
        module.__file__ = str(self._path)
        module.__cached__ = None
        module.__package__ = self._name if self._is_package else self._name.rpartition(".")[0]
        if self._is_package:
            module.__path__ = []
        code = compile(self._raw, str(self._path), "exec", dont_inherit=True)
        exec(code, module.__dict__, module.__dict__)


class _SealedLocalFinder(importlib.abc.MetaPathFinder):
    """Serve app/scripts only from held verified bytes, never pathnames."""

    def __init__(self, modules: Mapping[str, tuple[Path, bytes, bool]]) -> None:
        self._modules = MappingProxyType(dict(modules))

    def find_spec(
        self, fullname: str, _path: Any = None, _target: Any = None
    ) -> Any:
        entry = self._modules.get(fullname)
        if entry is not None:
            path, raw, is_package = entry
            loader = _HeldSourceLoader(
                name=fullname, path=path, raw=raw, is_package=is_package
            )
            return importlib.machinery.ModuleSpec(
                fullname,
                loader,
                origin=str(path),
                is_package=is_package,
            )
        if fullname in {"app", "scripts"}:
            spec = importlib.machinery.ModuleSpec(
                fullname, loader=None, origin="captured-paper-sealed-namespace"
            )
            spec.submodule_search_locations = []
            return spec
        if fullname.startswith(("app.", "scripts.")):
            raise ImportError(f"unsealed local module rejected: {fullname}")
        return None


class _VerifiedDependencySourceLoader(importlib.abc.Loader):
    """Compile one dependency source from bytes reverified at import time."""

    def __init__(
        self,
        *,
        name: str,
        path: Path,
        raw: bytes,
        is_package: bool,
        root: Path,
        files: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._name = name
        self._path = path
        self._raw = raw
        self._is_package = is_package
        self._root = root
        self._files = files

    def create_module(self, _spec: Any) -> None:
        return None

    def exec_module(self, module: Any) -> None:
        module.__file__ = str(self._path)
        module.__cached__ = None
        module.__package__ = self._name if self._is_package else self._name.rpartition(".")[0]
        if self._is_package:
            module.__path__ = [str(self._path.parent)]
        code = compile(self._raw, str(self._path), "exec", dont_inherit=True)
        exec(code, module.__dict__, module.__dict__)

    def get_data(self, pathname: str) -> bytes:
        """Support pkgutil/importlib resource reads only for frozen files."""

        path = _strict_path(pathname, require_file=True)
        if not _inside(path, self._root):
            raise OSError("resource outside sealed dependency root")
        relative = path.relative_to(self._root).as_posix()
        row = self._files.get(relative)
        if row is None:
            raise OSError("resource not present in sealed dependency inventory")
        try:
            return _stable_read(
                path,
                expected_sha256=str(row["sha256"]),
                max_bytes=int(row["size_bytes"]),
            )
        except IsolatedStage0Error as exc:
            raise OSError("sealed dependency resource changed") from exc

    def get_resource_reader(self, fullname: str) -> Any:
        if not self._is_package or fullname != self._name:
            return None
        return _VerifiedDependencyResourceReader(
            package_root=self._path.parent,
            dependency_root=self._root,
            files=self._files,
        )


class _VerifiedDependencyResourceReader(importlib.abc.ResourceReader):
    """Expose package data only from hash-bound, mutation-guarded files."""

    def __init__(
        self,
        *,
        package_root: Path,
        dependency_root: Path,
        files: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._package_root = package_root
        self._dependency_root = dependency_root
        self._files = files

    def _resource(self, resource: str) -> tuple[Path, Mapping[str, Any]]:
        if (
            not resource
            or Path(resource).name != resource
            or resource in {".", ".."}
        ):
            raise FileNotFoundError(resource)
        path = self._package_root / resource
        relative = path.relative_to(self._dependency_root).as_posix()
        row = self._files.get(relative)
        if row is None:
            raise FileNotFoundError(resource)
        return path, row

    def open_resource(self, resource: str) -> Any:
        path, row = self._resource(resource)
        return io.BytesIO(
            _stable_read(
                path,
                expected_sha256=str(row["sha256"]),
                max_bytes=int(row["size_bytes"]),
            )
        )

    def resource_path(self, resource: str) -> str:
        path, row = self._resource(resource)
        _stable_read(
            path,
            expected_sha256=str(row["sha256"]),
            max_bytes=int(row["size_bytes"]),
        )
        return str(path)

    def is_resource(self, name: str) -> bool:
        try:
            self._resource(name)
            return True
        except FileNotFoundError:
            return False

    def contents(self) -> Sequence[str]:
        prefix = self._package_root.relative_to(self._dependency_root).as_posix()
        prefix = f"{prefix}/" if prefix else ""
        values: set[str] = set()
        for relative in self._files:
            if relative.startswith(prefix):
                tail = relative[len(prefix) :]
                if tail:
                    values.add(tail.partition("/")[0])
        return tuple(sorted(values, key=lambda value: (value.casefold(), value)))


class _VerifiedDependencyExtensionLoader(importlib.abc.Loader):
    """Verify a native extension immediately before both loader phases."""

    def __init__(
        self, *, name: str, path: Path, expected_sha256: str, size_bytes: int
    ) -> None:
        self._path = path
        self._expected_sha256 = expected_sha256
        self._size_bytes = size_bytes
        self._delegate = importlib.machinery.ExtensionFileLoader(name, str(path))

    def _verify(self) -> None:
        _stable_read(
            self._path,
            expected_sha256=self._expected_sha256,
            max_bytes=self._size_bytes,
        )

    def create_module(self, spec: Any) -> Any:
        self._verify()
        return self._delegate.create_module(spec)

    def exec_module(self, module: Any) -> None:
        self._verify()
        self._delegate.exec_module(module)


class _DenyDependencyPathFinder(importlib.abc.PathEntryFinder):
    """Prevent PathFinder from bypassing the sealed meta finder."""

    def find_spec(self, _fullname: str, _target: Any = None) -> None:
        return None

    def invalidate_caches(self) -> None:
        return None


class _DenyUnsealedImportFinder(importlib.abc.MetaPathFinder):
    """Stop non-stdlib imports from falling through to interpreter paths."""

    def __init__(self, *, dependency_root: Path) -> None:
        self._dependency_root = dependency_root

    def find_spec(
        self, fullname: str, _path: Any = None, _target: Any = None
    ) -> None:
        top_level = fullname.partition(".")[0]
        stdlib = getattr(sys, "stdlib_module_names", frozenset())
        if fullname in stdlib or top_level in stdlib:
            return None
        parent_name = fullname.rpartition(".")[0]
        parent_module = sys.modules.get(parent_name) if parent_name else None
        parent_file = getattr(parent_module, "__file__", None)
        if parent_file:
            try:
                parent_path = Path(str(parent_file)).resolve(strict=False)
            except (OSError, RuntimeError):
                parent_path = Path()
            if parent_path and _inside(parent_path, self._dependency_root):
                # A verified dependency may install a virtual-submodule finder
                # (for example six.moves).  Physical path fallback remains
                # denied by _DenyDependencyPathFinder.
                return None
        raise ImportError(f"unsealed import rejected: {fullname}")


class _SealedDependencyFinder(importlib.abc.MetaPathFinder):
    """Admit only modules represented by the frozen dependency inventory."""

    def __init__(
        self,
        *,
        root: Path,
        files: Mapping[str, Mapping[str, Any]],
        guards: Sequence[Any],
    ) -> None:
        self._root = root
        self._files = MappingProxyType(dict(files))
        # Retaining the guard objects is part of the security boundary.  On
        # Windows their share mode denies mutation for the process lifetime.
        self._guards = tuple(guards)
        self._lock = threading.RLock()
        modules: dict[str, tuple[str, Path, Mapping[str, Any], bool]] = {}
        namespace_paths: dict[str, Path] = {}
        extension_suffixes = tuple(
            sorted(importlib.machinery.EXTENSION_SUFFIXES, key=len, reverse=True)
        )
        for relative, row in self._files.items():
            path = root / Path(relative)
            parts = list(Path(relative).parts)
            if not parts:
                continue
            filename = parts.pop()
            kind = ""
            is_package = False
            if filename.casefold().endswith(".py"):
                leaf = filename[:-3]
                kind = "source"
                is_package = leaf == "__init__"
                if not is_package:
                    parts.append(leaf)
            else:
                leaf = ""
                for suffix in extension_suffixes:
                    if filename.casefold().endswith(suffix.casefold()):
                        leaf = filename[: -len(suffix)]
                        break
                if not leaf:
                    continue
                kind = "extension"
                parts.append(leaf)
            if not parts or any(not str(part).isidentifier() for part in parts):
                continue
            name = ".".join(map(str, parts))
            if name in modules:
                existing = modules[name]
                if existing[0] == "extension" and kind == "source":
                    continue
                if existing[0] == "source" and kind == "extension":
                    modules[name] = (kind, path, row, is_package)
                    continue
                raise IsolatedStage0Error("DEPENDENCY_MODULE_COLLISION")
            modules[name] = (kind, path, row, is_package)
            package_parts = parts[:-1] if not is_package else parts
            for index in range(1, len(package_parts) + 1):
                namespace_name = ".".join(map(str, package_parts[:index]))
                namespace_paths.setdefault(
                    namespace_name, root.joinpath(*map(str, package_parts[:index]))
                )
        self._modules = MappingProxyType(modules)
        self._namespaces = MappingProxyType(
            {
                name: path
                for name, path in namespace_paths.items()
                if name not in modules
            }
        )
        self._sealed_top_levels = frozenset(
            name.partition(".")[0] for name in (*modules, *self._namespaces)
        )

    def find_spec(
        self, fullname: str, path: Any = None, target: Any = None
    ) -> Any:
        del target
        if fullname in getattr(sys, "stdlib_module_names", frozenset()):
            return None
        entry = self._modules.get(fullname)
        if entry is not None:
            kind, module_path, row, is_package = entry
            with self._lock:
                raw = _stable_read(
                    module_path,
                    expected_sha256=str(row["sha256"]),
                    max_bytes=int(row["size_bytes"]),
                )
            if kind == "source":
                loader: Any = _VerifiedDependencySourceLoader(
                    name=fullname,
                    path=module_path,
                    raw=raw,
                    is_package=is_package,
                    root=self._root,
                    files=self._files,
                )
            else:
                loader = _VerifiedDependencyExtensionLoader(
                    name=fullname,
                    path=module_path,
                    expected_sha256=str(row["sha256"]),
                    size_bytes=int(row["size_bytes"]),
                )
            spec = importlib.machinery.ModuleSpec(
                fullname,
                loader,
                origin=str(module_path),
                is_package=is_package,
            )
            if is_package:
                spec.submodule_search_locations = [str(module_path.parent)]
            return spec
        namespace = self._namespaces.get(fullname)
        if namespace is not None:
            spec = importlib.machinery.ModuleSpec(
                fullname,
                loader=None,
                origin="captured-paper-sealed-dependency-namespace",
                is_package=True,
            )
            spec.submodule_search_locations = [str(namespace)]
            return spec
        under_dependency_path = False
        if path is not None:
            for item in path:
                try:
                    candidate = Path(str(item)).resolve(strict=False)
                except (OSError, RuntimeError):
                    continue
                if candidate == self._root or _inside(candidate, self._root):
                    under_dependency_path = True
                    break
        parent_name = fullname.rpartition(".")[0]
        parent_module = sys.modules.get(parent_name) if parent_name else None
        parent_file = getattr(parent_module, "__file__", None)
        if parent_file:
            try:
                parent_path = Path(str(parent_file)).resolve(strict=False)
            except (OSError, RuntimeError):
                parent_path = Path()
            if parent_path and _inside(parent_path, self._root):
                # Sealed packages such as ``six`` install deterministic virtual
                # submodule finders.  Let those already-verified package bytes
                # synthesize the module; PathFinder remains denied for every
                # physical dependency directory, so this does not admit a new
                # pathname or an unrostered file.
                return None
        if fullname.partition(".")[0] in self._sealed_top_levels or under_dependency_path:
            raise ImportError(f"unsealed dependency module rejected: {fullname}")
        return None


def _module_name(candidate_root: Path, path: Path) -> tuple[str, bool] | None:
    try:
        relative = path.relative_to(candidate_root)
    except ValueError:
        return None
    parts = list(relative.parts)
    if len(parts) < 2 or parts[0] not in {"app", "scripts"} or path.suffix != ".py":
        return None
    leaf = parts.pop()[:-3]
    is_package = leaf == "__init__"
    if not is_package:
        parts.append(leaf)
    if not parts or any(not str(part).isidentifier() for part in parts):
        return None
    return ".".join(map(str, parts)), is_package


def _verify_manifest_and_target(
    parsed: Mapping[str, str], *, stage0_path: Path, initial_paths: Sequence[Path]
) -> tuple[
    bytes,
    Path,
    Path,
    Mapping[str, Any],
    Mapping[str, tuple[Path, bytes, bool]],
    Mapping[str, Any],
    str,
]:
    manifest_path = _strict_path(parsed["--manifest"], require_file=True)
    manifest_sha = _sha(parsed["--manifest-sha256"])
    manifest_raw = _stable_read(
        manifest_path, expected_sha256=manifest_sha, max_bytes=_MAX_MANIFEST_BYTES
    )
    document = _strict_json(manifest_raw)
    if document.get("schema_version") not in _MANIFEST_SCHEMAS:
        raise IsolatedStage0Error("MANIFEST_SCHEMA_INVALID")
    claimed = _sha(document.get("activation_manifest_sha256"))
    body = dict(document)
    body.pop("activation_manifest_sha256", None)
    if _sha256_bytes(_canonical_json_bytes(body)) != claimed:
        raise IsolatedStage0Error("MANIFEST_SELF_DIGEST_INVALID")
    if (
        manifest_path.name.casefold() != f"{manifest_sha}.json"
        or manifest_path.parent.name.casefold() != manifest_sha[:2]
    ):
        raise IsolatedStage0Error("MANIFEST_NOT_CONTENT_ADDRESSED")

    cutover = document.get("cutover")
    code_build = document.get("code_build")
    if not isinstance(cutover, dict) or not isinstance(code_build, dict):
        raise IsolatedStage0Error("MANIFEST_BINDING_INVALID")
    candidate_root = _strict_path(parsed["--candidate-root"], require_file=False)
    declared_candidate = _strict_path(cutover.get("candidate_root"), require_file=False)
    if candidate_root != declared_candidate:
        raise IsolatedStage0Error("CANDIDATE_ROOT_MISMATCH")
    initial_paths = tuple(initial_paths) or _assert_initial_isolation(candidate_root)

    rows = code_build.get("artifacts")
    if (
        code_build.get("schema_version") != "chili.captured-paper-code-build.v1"
        or not isinstance(rows, list)
        or len(rows) > _MAX_LOCAL_SOURCE_FILES
    ):
        raise IsolatedStage0Error("CODE_BUILD_INVALID")
    normalized: list[dict[str, str]] = []
    role_paths: dict[str, Path] = {}
    role_hashes: dict[str, str] = {}
    roster_paths: set[Path] = set()
    held_modules: dict[str, tuple[Path, bytes, bool]] = {}
    local_source_total_bytes = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"role", "path", "sha256"}:
            raise IsolatedStage0Error("CODE_BUILD_INVALID")
        role = str(row.get("role") or "")
        if not role or role in role_paths:
            raise IsolatedStage0Error("CODE_BUILD_INVALID")
        path = _strict_path(row.get("path"), require_file=True)
        if not _inside(path, candidate_root) or path in roster_paths:
            raise IsolatedStage0Error("CODE_BUILD_PATH_INVALID")
        digest = _sha(row.get("sha256"))
        held_raw = _stable_read(
            path, expected_sha256=digest, max_bytes=_MAX_SOURCE_BYTES
        )
        local_source_total_bytes += len(held_raw)
        if local_source_total_bytes > _MAX_LOCAL_SOURCE_TOTAL_BYTES:
            raise IsolatedStage0Error("LOCAL_SOURCE_RESOURCE_BUDGET_EXCEEDED")
        role_paths[role] = path
        role_hashes[role] = digest
        roster_paths.add(path)
        module_identity = _module_name(candidate_root, path)
        if module_identity is not None:
            module_name, is_package = module_identity
            if module_name in held_modules:
                raise IsolatedStage0Error("CODE_BUILD_MODULE_COLLISION")
            held_modules[module_name] = (path, held_raw, is_package)
        normalized.append({"role": role, "path": str(path), "sha256": digest})
    if normalized != sorted(normalized, key=lambda item: item["role"]):
        raise IsolatedStage0Error("CODE_BUILD_UNSORTED")
    code_body = {
        "schema_version": "chili.captured-paper-code-build.v1",
        "artifacts": normalized,
    }
    if _sha256_bytes(_canonical_json_bytes(code_body)) != _sha(
        code_build.get("code_build_sha256")
    ):
        raise IsolatedStage0Error("CODE_BUILD_DIGEST_INVALID")
    required_roles = {
        "activation_stage0",
        "activation_service",
        "captured_paper_host_cutover",
    }
    if not required_roles.issubset(role_paths):
        raise IsolatedStage0Error("CODE_BUILD_ROSTER_INVALID")
    _assert_no_unsealed_bootstrap_modules(
        candidate_root, roster_paths=frozenset(roster_paths)
    )

    stage0_sha = _sha(cutover.get("stage0_sha256"))
    stage0_source = _strict_path(cutover.get("stage0_source_path"), require_file=True)
    staged_stage0 = _strict_path(cutover.get("stage0_path"), require_file=True)
    if not (
        role_paths["activation_stage0"] == stage0_source
        and role_hashes["activation_stage0"] == stage0_sha
        and staged_stage0 == stage0_path
        and stage0_path.name.casefold() == f"{stage0_sha}.py"
        and stage0_path.parent.name.casefold() == stage0_sha
        and _stable_read(
            stage0_path, expected_sha256=stage0_sha, max_bytes=_MAX_SOURCE_BYTES
        )
        == _stable_read(
            stage0_source, expected_sha256=stage0_sha, max_bytes=_MAX_SOURCE_BYTES
        )
    ):
        raise IsolatedStage0Error("STAGE0_BINDING_INVALID")

    generation = str(document.get("activation_generation") or "").lower()
    artifact_root = _strict_path(cutover.get("activation_artifact_root"), require_file=False)
    if (
        _UUID_RE.fullmatch(generation) is None
        or stage0_path.parent.parent.name.casefold() != generation
        or stage0_path.parent.parent.parent != artifact_root
    ):
        raise IsolatedStage0Error("STAGE0_NOT_CONTENT_ADDRESSED")

    executable = _strict_path(sys.executable, require_file=True)
    executable_sha = _sha(cutover.get("python_executable_sha256"))
    executable_raw = _stable_read(
        executable, expected_sha256=executable_sha, max_bytes=_MAX_SOURCE_BYTES
    )
    del executable_raw
    if executable != _strict_path(cutover.get("python_executable_path"), require_file=True):
        raise IsolatedStage0Error("PYTHON_EXECUTABLE_MISMATCH")
    dependency_root = _strict_path(
        cutover.get("python_dependency_root"), require_file=False
    )
    if dependency_root in initial_paths:
        raise IsolatedStage0Error("DEPENDENCY_IMPORTED_BEFORE_VERIFICATION")
    dependency_tree = _dependency_tree_inventory(
        dependency_root, retain_mutation_guards=True
    )
    if not (
        dependency_root.name.casefold() == "site-packages"
        and dependency_root.parent.name.casefold()
        == str(dependency_tree["tree_sha256"])
        and dependency_root.parent.parent.name.casefold() == "dependencies"
        and dependency_root.parent.parent.parent == artifact_root / generation
    ):
        raise IsolatedStage0Error("DEPENDENCY_ROOT_NOT_CONTENT_ADDRESSED")
    dependency_identity = _dependency_root_identity_from_inventory(
        root=dependency_root,
        executable=executable,
        python_executable_sha256=executable_sha,
        tree=dependency_tree,
    )
    identity_sha = _sha256_bytes(_canonical_json_bytes(dict(dependency_identity)))
    if identity_sha != _sha(cutover.get("python_dependency_root_identity_sha256")):
        raise IsolatedStage0Error("DEPENDENCY_ROOT_IDENTITY_MISMATCH")

    target_role = parsed["--target-role"]
    target_path = _strict_path(parsed["--target"], require_file=True)
    target_sha = _sha(parsed["--target-sha256"])
    target_raw = _stable_read(
        target_path, expected_sha256=target_sha, max_bytes=_MAX_SOURCE_BYTES
    )
    source_raw = _stable_read(
        role_paths[target_role],
        expected_sha256=role_hashes[target_role],
        max_bytes=_MAX_SOURCE_BYTES,
    )
    if target_role == "activation_service":
        expected_target = _strict_path(cutover.get("service_path"), require_file=True)
        expected_sha = _sha(cutover.get("service_sha256"))
    else:
        expected_target = role_paths[target_role]
        expected_sha = role_hashes[target_role]
    if not (
        target_path == expected_target
        and target_sha == expected_sha == role_hashes[target_role]
        and target_raw == source_raw
    ):
        raise IsolatedStage0Error("TARGET_BINDING_INVALID")
    return (
        target_raw,
        target_path,
        dependency_root,
        document,
        MappingProxyType(held_modules),
        dependency_tree,
        identity_sha,
    )


def _admit_and_execute(argv: Sequence[str]) -> int:
    parsed, target_arguments = _parse_argv(argv)
    candidate_root = _strict_path(parsed["--candidate-root"], require_file=False)
    initial_paths = _assert_initial_isolation(candidate_root)
    stage0_path = _strict_path(sys.argv[0], require_file=True)
    (
        target_raw,
        target_path,
        dependency_root,
        document,
        held_modules,
        dependency_tree,
        dependency_identity_sha256,
    ) = _verify_manifest_and_target(parsed, stage0_path=stage0_path, initial_paths=initial_paths)

    # Do not call site.addsitedir: .pth processing and sitecustomize are never
    # admitted.  app/scripts are served from held verified buffers and every
    # third-party module is reverified by the dependency finder.  The physical
    # dependency root is present only so metadata/resource APIs can discover
    # frozen bytes; PathFinder is denied for it and all inventoried children.
    # candidate_root is never admitted as a mutable pathname.
    sys.path[:] = [str(path) for path in initial_paths]
    dependency_finder = _SealedDependencyFinder(
        root=dependency_root,
        files=MappingProxyType(dependency_tree["files"]),
        guards=tuple(dependency_tree["guards"]),
    )
    deny_finder = _DenyDependencyPathFinder()
    for relative in dependency_tree["files"]:
        parent = dependency_root.joinpath(*Path(relative).parts).parent
        sys.path_importer_cache[str(parent)] = deny_finder
    sys.path_importer_cache[str(dependency_root)] = deny_finder
    sys.path.append(str(dependency_root))
    sys.meta_path.insert(0, dependency_finder)
    sys.meta_path.insert(0, _SealedLocalFinder(held_modules))
    sys.meta_path.insert(
        2, _DenyUnsealedImportFinder(dependency_root=dependency_root)
    )
    os.environ.pop("PYTHONPATH", None)
    os.environ["PYTHONNOUSERSITE"] = "1"
    for name in (
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
    ):
        os.environ.pop(name, None)

    attestation = MappingProxyType(
        {
            "schema_version": STAGE0_SCHEMA_VERSION,
            "stage0_path": str(Path(sys.argv[0]).resolve(strict=True)),
            "target_path": str(target_path),
            "target_sha256": _sha256_bytes(target_raw),
            "target_role": parsed["--target-role"],
            "candidate_root": str(candidate_root),
            "dependency_root": str(dependency_root),
            "dependency_exclusion_policy": dependency_tree["exclusion_policy"],
            "dependency_file_count": dependency_tree["file_count"],
            "dependency_mutation_guard_mode": dependency_tree["mutation_guard_mode"],
            "dependency_tree_sha256": dependency_tree["tree_sha256"],
            "dependency_tree_total_bytes": dependency_tree["total_bytes"],
            "dependency_root_identity_sha256": dependency_identity_sha256,
            "local_module_count": len(held_modules),
            "local_roster_sha256": _sha(
                MappingProxyType(document["code_build"]).get("code_build_sha256")
            ),
            "python_executable_path": str(
                _strict_path(sys.executable, require_file=True)
            ),
            "python_executable_sha256": _sha(
                document["cutover"]["python_executable_sha256"]
            ),
            "manifest_path": str(_strict_path(parsed["--manifest"], require_file=True)),
            "manifest_sha256": _sha(parsed["--manifest-sha256"]),
            "code_build_sha256": _sha(
                MappingProxyType(document["code_build"]).get("code_build_sha256")
            ),
        }
    )
    setattr(sys, "_captured_paper_isolated_stage0", attestation)
    sys.argv[:] = [str(target_path), *target_arguments]
    globals_dict: dict[str, Any] = {
        "__name__": "__main__",
        "__file__": str(target_path),
        "__package__": None,
        "__cached__": None,
        "__builtins__": __builtins__,
    }
    code = compile(target_raw, str(target_path), "exec", dont_inherit=True)
    exec(code, globals_dict, globals_dict)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # The sealed dependency inventory runs before the target service can
    # install its own diagnostics.  Emit one bounded all-thread snapshot if
    # that admission phase itself stalls; successful targets replace this
    # one-shot timer when their main function starts.
    import faulthandler

    faulthandler.dump_traceback_later(30.0, repeat=False, file=sys.stderr)
    try:
        return _admit_and_execute(tuple(sys.argv[1:] if argv is None else argv))
    except IsolatedStage0Error as exc:
        sys.stderr.write(
            _canonical_json_bytes(
                {
                    "live_cash_authorized": False,
                    "orders_submitted": False,
                    "reason_code": exc.code,
                    "verdict": "ISOLATED_STAGE0_REJECTED",
                    "workers_started": False,
                }
            ).decode("utf-8")
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEPENDENCY_ROOT_IDENTITY_SCHEMA_VERSION",
    "IsolatedStage0Error",
    "STAGE0_SCHEMA_VERSION",
    "dependency_root_identity",
    "dependency_root_identity_sha256",
    "main",
]
