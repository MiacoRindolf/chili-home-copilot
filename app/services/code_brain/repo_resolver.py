"""Dynamic repo resolver — accept any flavor of input, register the right thing.

The user gives us a string (in the desktop app's Queue tab, an API request,
or a SQL one-liner) and we figure out what they meant:

  * **Local Windows path** like ``C:\\dev\\my-project`` or ``D:/code/foo`` —
    we map it to the container side via the ``/host_dev`` mount and require
    that the directory contains ``.git/``. No clone.
  * **Local container path** like ``/workspace`` or ``/host_dev/foo`` —
    used as-is. We still require ``.git/``.
  * **Git HTTPS URL** like ``https://github.com/USER/REPO`` or ``...git`` —
    we clone into ``/workspace_managed/<USER>__<REPO>`` using the dispatch
    PAT for auth (no separate credential setup).
  * **GitHub SSH URL** like ``git@github.com:USER/REPO.git`` — translated to
    HTTPS and cloned the same way (we always use HTTPS+PAT, never SSH).
  * **GitHub shorthand** like ``MiacoRindolf/chili-home-copilot`` — same
    as a github.com URL.
  * **Bare name** like ``chili-home-copilot`` — looked up in ``code_repos``;
    if found, returned. If not found, raises (so we don't accidentally
    clone something with a typo'd name).

Everything goes through :func:`resolve_or_register`, which returns the
``CodeRepo`` row. Existing rows are reused (so the operator can paste the
same path twice without making duplicates).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.code_brain import CodeRepo

logger = logging.getLogger(__name__)


# Where the brain clones github.com URLs into. Backed by the
# ``chili_dispatch_clones`` named volume in docker-compose.yml.
_MANAGED_CLONE_ROOT = "/workspace_managed"

# Where the brain looks for already-on-disk local projects, mounted from
# the host's ``C:\dev`` directory.
_HOST_DEV_MOUNT = "/host_dev"


class InputKind(str, Enum):
    LOCAL_HOST_PATH = "local_host_path"          # C:\dev\foo or /host_dev/foo
    LOCAL_CONTAINER_PATH = "local_container_path"  # /workspace etc
    GIT_HTTPS_URL = "git_https_url"
    GIT_SSH_URL = "git_ssh_url"
    GITHUB_SHORTHAND = "github_shorthand"        # USER/REPO
    REPO_NAME = "repo_name"                      # bare name lookup
    UNKNOWN = "unknown"


@dataclass
class ParsedInput:
    kind: InputKind
    raw: str
    # Populated based on kind:
    host_path: Optional[str] = None     # original Windows path or /host_dev/...
    container_path: Optional[str] = None
    https_url: Optional[str] = None     # always normalized to https://github.com/...
    repo_name: Optional[str] = None     # short name for code_repos.name
    owner: Optional[str] = None         # for shorthand and URLs


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_GITHUB_SHORTHAND_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_GITHUB_HTTPS_RE = re.compile(r"^https?://(?:www\.)?github\.com/([^/]+)/([^/?#]+?)(?:\.git)?/?$", re.IGNORECASE)
_GITHUB_SSH_RE = re.compile(r"^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?/?$")


def _windows_to_container_path(host_path: str) -> Optional[str]:
    """Map ``C:\\dev\\foo`` → ``/host_dev/foo``. Returns None if the path is
    not under C:\\dev (we only mount that one drive root).
    """
    p = host_path.strip().replace("\\", "/")
    # Strip drive letter and check it's under "dev"
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if not m:
        return None
    drive = m.group(1).lower()
    rest = m.group(2).rstrip("/")
    if drive != "c":
        # Only C: is mounted today. Could extend later by adding more
        # drive mounts in docker-compose.yml.
        return None
    parts = rest.split("/")
    if not parts or parts[0].lower() != "dev":
        return None
    inner = "/".join(parts[1:])
    if inner:
        return f"{_HOST_DEV_MOUNT}/{inner}"
    return _HOST_DEV_MOUNT


def parse_input(raw: str) -> ParsedInput:
    s = (raw or "").strip()
    if not s:
        return ParsedInput(kind=InputKind.UNKNOWN, raw=raw)

    # 1. Container-side absolute path (already inside the brain's filesystem)
    if s.startswith("/"):
        return ParsedInput(
            kind=InputKind.LOCAL_CONTAINER_PATH,
            raw=raw,
            container_path=s.rstrip("/"),
            repo_name=Path(s).name or None,
        )

    # 2. Windows host path
    if _WINDOWS_PATH_RE.match(s):
        cp = _windows_to_container_path(s)
        return ParsedInput(
            kind=InputKind.LOCAL_HOST_PATH,
            raw=raw,
            host_path=s,
            container_path=cp,
            repo_name=Path(s.replace("\\", "/")).name or None,
        )

    # 3. SSH URL (git@github.com:owner/repo.git)
    m = _GITHUB_SSH_RE.match(s)
    if m:
        owner, repo = m.group(1), m.group(2)
        return ParsedInput(
            kind=InputKind.GIT_SSH_URL,
            raw=raw,
            https_url=f"https://github.com/{owner}/{repo}.git",
            owner=owner,
            repo_name=repo,
        )

    # 4. HTTPS URL
    m = _GITHUB_HTTPS_RE.match(s)
    if m:
        owner, repo = m.group(1), m.group(2)
        if not s.endswith(".git"):
            url = f"https://github.com/{owner}/{repo}.git"
        else:
            url = s
        return ParsedInput(
            kind=InputKind.GIT_HTTPS_URL,
            raw=raw,
            https_url=url,
            owner=owner,
            repo_name=repo,
        )

    # 5. GitHub USER/REPO shorthand
    if _GITHUB_SHORTHAND_RE.match(s):
        owner, repo = s.split("/", 1)
        return ParsedInput(
            kind=InputKind.GITHUB_SHORTHAND,
            raw=raw,
            https_url=f"https://github.com/{owner}/{repo}.git",
            owner=owner,
            repo_name=repo,
        )

    # 6. Bare name → look up existing
    if re.match(r"^[A-Za-z0-9_.\-]+$", s):
        return ParsedInput(kind=InputKind.REPO_NAME, raw=raw, repo_name=s)

    return ParsedInput(kind=InputKind.UNKNOWN, raw=raw)


# ---------------------------------------------------------------------------
# Helpers for clone + registration
# ---------------------------------------------------------------------------

def _existing_by_path(db: Session, host_path: Optional[str], container_path: Optional[str]) -> Optional[CodeRepo]:
    if not host_path and not container_path:
        return None
    q = db.query(CodeRepo)
    if host_path:
        for r in q.filter(CodeRepo.host_path == host_path).all():
            return r
    if container_path:
        for r in q.filter(CodeRepo.container_path == container_path).all():
            return r
    return None


def _existing_by_name(db: Session, name: str) -> Optional[CodeRepo]:
    return db.query(CodeRepo).filter(CodeRepo.name == name).first()


def _has_git_dir(container_path: str) -> bool:
    p = Path(container_path)
    return p.is_dir() and (p / ".git").exists()


def _git_init_managed(target: str) -> Tuple[bool, str]:
    p = Path(target)
    p.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "init"], cwd=str(p), capture_output=True, text=True)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "git init failed").strip()[:400]
    return True, "initialized"


def _clone_with_pat(https_url: str, target: str) -> Tuple[bool, str]:
    """Clone ``https_url`` into ``target`` using the dispatch PAT.

    Token is injected only into the subprocess argv at clone time and never
    written to disk (we strip it from the remote URL after clone via
    ``git remote set-url`` to a token-less https URL).
    """
    token = (os.environ.get("CHILI_DISPATCH_GITHUB_TOKEN") or "").strip()
    if not token:
        return False, "CHILI_DISPATCH_GITHUB_TOKEN not set in scheduler-worker env"

    # Rewrite https_url to inject the token for this clone only.
    pat_url = re.sub(r"^https://", f"https://x-access-token:{token}@", https_url)

    target_p = Path(target)
    if target_p.exists():
        # Already cloned — caller should have detected this, but handle it.
        return True, f"target already exists at {target}"

    target_p.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "clone", pat_url, target],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        # Strip token before returning the error so it doesn't end up in
        # logs or the audit row.
        err = re.sub(r"x-access-token:[^@]+@", "x-access-token:***@", (proc.stderr or "")).strip()[:600]
        return False, f"clone failed: {err}"

    # Strip token from the persisted remote so the .git/config doesn't store it.
    subprocess.run(
        ["git", "-C", target, "remote", "set-url", "origin", https_url],
        capture_output=True, text=True,
    )
    return True, "cloned"


def _insert_repo(
    db: Session,
    *,
    name: str,
    host_path: Optional[str],
    container_path: str,
    default_branch: str = "main",
) -> CodeRepo:
    repo = CodeRepo(
        name=name,
        host_path=(host_path or None),
        container_path=container_path,
        path=container_path,  # 'path' is legacy column; mirror container_path
    )
    # Some optional fields exist on the model but aren't always populated
    # in this branch's schema; setattr defensively to avoid AttributeError.
    for attr, val in (
        ("default_branch", default_branch),
        ("reachable_in_scheduler", True),
        ("reachable_in_web", False),
    ):
        if hasattr(CodeRepo, attr):
            setattr(repo, attr, val)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    logger.info(
        "[repo_resolver] registered code_repos id=%s name=%s host=%s container=%s",
        repo.id, name, host_path, container_path,
    )
    return repo


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

@dataclass
class ResolveResult:
    repo: CodeRepo
    parsed: ParsedInput
    created: bool       # True if we just inserted the row
    cloned: bool        # True if we just cloned the repo
    git_initialized: bool  # True if we just ran `git init` in a fresh local dir
    notes: list[str]    # human-readable trace


def resolve_or_register(db: Session, raw_input: str, *, allow_clone: bool = True) -> ResolveResult:
    """Single entry point. See module docstring."""
    parsed = parse_input(raw_input)
    notes: list[str] = [f"parsed kind={parsed.kind.value}"]

    if parsed.kind == InputKind.UNKNOWN:
        raise ValueError(f"could not interpret input {raw_input!r}")

    # Fast path: bare name lookup.
    if parsed.kind == InputKind.REPO_NAME:
        existing = _existing_by_name(db, parsed.repo_name or "")
        if not existing:
            raise ValueError(f"no code_repos row named {parsed.repo_name!r}; pass a path or URL to register")
        notes.append(f"matched existing id={existing.id}")
        return ResolveResult(repo=existing, parsed=parsed, created=False, cloned=False, git_initialized=False, notes=notes)

    # Local paths: validate, register-if-new.
    if parsed.kind in (InputKind.LOCAL_HOST_PATH, InputKind.LOCAL_CONTAINER_PATH):
        cp = parsed.container_path
        if not cp:
            raise ValueError(
                f"local path {parsed.host_path!r} is not under the /host_dev mount "
                "(only C:\\dev\\* is exposed today)"
            )
        existing = _existing_by_path(db, parsed.host_path, cp)
        if existing:
            notes.append(f"matched existing id={existing.id} by path")
            return ResolveResult(repo=existing, parsed=parsed, created=False, cloned=False, git_initialized=False, notes=notes)

        # New local registration. Require either an existing .git or auto-init.
        gi = False
        if not _has_git_dir(cp):
            ok, msg = _git_init_managed(cp)
            notes.append(f"git init: {msg}")
            if not ok:
                raise RuntimeError(f"path {cp!r} is not a git repo and `git init` failed: {msg}")
            gi = True

        # Pick a name: prefer parsed.repo_name (last folder), then container basename.
        name = parsed.repo_name or PurePosixPath(cp).name or f"local_{cp.replace('/', '_')}"
        repo = _insert_repo(db, name=name, host_path=parsed.host_path, container_path=cp)
        notes.append(f"created code_repos id={repo.id}")
        return ResolveResult(repo=repo, parsed=parsed, created=True, cloned=False, git_initialized=gi, notes=notes)

    # Remote URLs / shorthand: clone into managed area if we don't already have it.
    if parsed.kind in (InputKind.GIT_HTTPS_URL, InputKind.GIT_SSH_URL, InputKind.GITHUB_SHORTHAND):
        if not allow_clone:
            raise ValueError(f"input {parsed.raw!r} requires a clone but allow_clone=False")
        owner = parsed.owner or "unknown"
        repo_name = parsed.repo_name or "repo"
        # Use a flat directory name so URLs don't conflict (e.g. owner__name).
        dirname = f"{owner}__{repo_name}"
        cp = f"{_MANAGED_CLONE_ROOT}/{dirname}"

        # If we already cloned this URL before, reuse.
        existing = _existing_by_path(db, host_path=None, container_path=cp)
        if existing:
            notes.append(f"matched existing managed clone id={existing.id} at {cp}")
            return ResolveResult(repo=existing, parsed=parsed, created=False, cloned=False, git_initialized=False, notes=notes)

        was_cloned = False
        if not Path(cp).exists():
            ok, msg = _clone_with_pat(parsed.https_url or "", cp)
            notes.append(f"clone: {msg}")
            if not ok:
                raise RuntimeError(msg)
            was_cloned = True
        else:
            notes.append(f"target {cp} already on disk; skipping clone")

        name = repo_name
        repo = _insert_repo(db, name=name, host_path=None, container_path=cp)
        notes.append(f"created code_repos id={repo.id}")
        return ResolveResult(repo=repo, parsed=parsed, created=True, cloned=was_cloned, git_initialized=False, notes=notes)

    raise ValueError(f"unhandled parsed.kind={parsed.kind}")
