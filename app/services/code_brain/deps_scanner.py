"""Dependency health scanner: check for outdated or vulnerable packages."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy.orm import Session

from ...models.code_brain import CodeDepAlert, CodeRepo
from .runtime import resolve_repo_runtime_path

logger = logging.getLogger(__name__)

_PYPI_CACHE: Dict[str, Tuple[str, float]] = {}
_NPM_CACHE: Dict[str, Tuple[str, float]] = {}
_CACHE_TTL = 3600  # 1 hour


def _parse_version(v: str) -> Tuple[int, ...]:
    """Parse a version string into a comparable tuple."""
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:3]) if parts else (0,)


def _parse_requirements(repo_path: Path) -> List[Dict[str, str]]:
    """Parse requirements.txt and pyproject.toml for Python deps."""
    deps: List[Dict[str, str]] = []
    seen: set[str] = set()

    req_file = repo_path / "requirements.txt"
    if req_file.exists():
        try:
            for line in req_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                m = re.match(r"([a-zA-Z0-9_.-]+)\s*([><=!~]+\s*[\d.]+)?", line)
                if m:
                    name = m.group(1).lower().replace("-", "_")
                    version = m.group(2).strip().lstrip(">=<~!=") if m.group(2) else None
                    if name not in seen:
                        seen.add(name)
                        deps.append({"name": name, "current_version": version, "ecosystem": "pip"})
        except Exception:
            pass

    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
            in_deps = False
            for line in content.splitlines():
                if re.match(r"\[project\.dependencies\]|\[tool\.poetry\.dependencies\]", line.strip()):
                    in_deps = True
                    continue
                if in_deps and line.strip().startswith("["):
                    break
                if in_deps:
                    m = re.match(r'"?([a-zA-Z0-9_.-]+)"?\s*[>=<~!]*\s*"?([\d.]*)"?', line.strip())
                    if m:
                        name = m.group(1).lower().replace("-", "_")
                        version = m.group(2) or None
                        if name not in seen:
                            seen.add(name)
                            deps.append({"name": name, "current_version": version, "ecosystem": "pip"})
        except Exception:
            pass

    return deps


def _parse_package_json(repo_path: Path) -> List[Dict[str, str]]:
    """Parse package.json for npm deps."""
    pkg_file = repo_path / "package.json"
    if not pkg_file.exists():
        return []

    deps: List[Dict[str, str]] = []
    seen: set[str] = set()
    try:
        data = json.loads(pkg_file.read_text(encoding="utf-8", errors="replace"))
        for section in ("dependencies", "devDependencies"):
            for name, version_spec in (data.get(section) or {}).items():
                if name not in seen:
                    seen.add(name)
                    version = re.sub(r"[^0-9.]", "", version_spec) if version_spec else None
                    deps.append({"name": name, "current_version": version or None, "ecosystem": "npm"})
    except Exception:
        pass
    return deps


def _check_pypi_latest(package_name: str) -> Optional[str]:
    """Query PyPI for the latest version. Results are cached."""
    now = time.time()
    if package_name in _PYPI_CACHE:
        ver, ts = _PYPI_CACHE[package_name]
        if now - ts < _CACHE_TTL:
            return ver

    try:
        r = requests.get(f"https://pypi.org/pypi/{package_name}/json", timeout=5)
        if r.status_code == 200:
            ver = r.json().get("info", {}).get("version", "")
            _PYPI_CACHE[package_name] = (ver, now)
            return ver
    except Exception:
        pass
    return None


def _check_npm_latest(package_name: str) -> Optional[str]:
    """Query npm registry for the latest version. Results are cached."""
    now = time.time()
    if package_name in _NPM_CACHE:
        ver, ts = _NPM_CACHE[package_name]
        if now - ts < _CACHE_TTL:
            return ver

    try:
        r = requests.get(f"https://registry.npmjs.org/{package_name}/latest", timeout=5)
        if r.status_code == 200:
            ver = r.json().get("version", "")
            _NPM_CACHE[package_name] = (ver, now)
            return ver
    except Exception:
        pass
    return None


def _classify_severity(current: Optional[str], latest: Optional[str]) -> str:
    if not current or not latest:
        return "info"
    cur = _parse_version(current)
    lat = _parse_version(latest)
    if cur >= lat:
        return "ok"
    major_diff = lat[0] - cur[0] if len(cur) > 0 and len(lat) > 0 else 0
    if major_diff >= 2:
        return "critical"
    if major_diff >= 1:
        return "warn"
    return "info"


def scan_dependencies(db: Session, repo_id: int) -> Dict[str, Any]:
    """Scan deps for a repo, check latest versions, create/update alerts."""
    repo = db.query(CodeRepo).filter(CodeRepo.id == repo_id).first()
    if not repo:
        return {"error": "Repo not found"}

    repo_path = resolve_repo_runtime_path(repo)
    if repo_path is None or not repo_path.is_dir():
        return {"error": "Registered workspace is not reachable from the current runtime."}

    all_deps = _parse_requirements(repo_path) + _parse_package_json(repo_path)
    if not all_deps:
        return {"scanned": 0, "alerts": 0}

    # Mark existing alerts as potentially resolved
    db.query(CodeDepAlert).filter(
        CodeDepAlert.repo_id == repo_id,
        CodeDepAlert.resolved.is_(False),
    ).update({"resolved": True})

    alert_count = 0
    for dep in all_deps[:100]:
        name = dep["name"]
        eco = dep["ecosystem"]
        current = dep.get("current_version")

        if eco == "pip":
            latest = _check_pypi_latest(name)
        else:
            latest = _check_npm_latest(name)

        if not latest:
            continue

        severity = _classify_severity(current, latest)
        if severity == "ok":
            continue

        existing = (
            db.query(CodeDepAlert)
            .filter(
                CodeDepAlert.repo_id == repo_id,
                CodeDepAlert.package_name == name,
            )
            .first()
        )
        if existing:
            existing.current_version = current
            existing.latest_version = latest
            existing.severity = severity
            existing.resolved = False
            existing.detected_at = __import__("datetime").datetime.utcnow()
        else:
            db.add(CodeDepAlert(
                repo_id=repo_id,
                package_name=name,
                current_version=current,
                latest_version=latest,
                severity=severity,
                alert_type="outdated",
                ecosystem=eco,
            ))
        alert_count += 1

    db.commit()
    return {"scanned": len(all_deps), "alerts": alert_count}


def get_dep_health(db: Session, repo_id: Optional[int] = None) -> Dict[str, Any]:
    """Return active dependency alerts grouped by severity."""
    q = db.query(CodeDepAlert).filter(CodeDepAlert.resolved.is_(False))
    if repo_id is not None:
        q = q.filter(CodeDepAlert.repo_id == repo_id)
    alerts = q.order_by(CodeDepAlert.severity.desc(), CodeDepAlert.detected_at.desc()).all()

    grouped: Dict[str, List[Dict[str, Any]]] = {"critical": [], "warn": [], "info": []}
    for a in alerts:
        item = {
            "id": a.id,
            "package": a.package_name,
            "current": a.current_version,
            "latest": a.latest_version,
            "ecosystem": a.ecosystem,
            "alert_type": a.alert_type,
            "detected_at": a.detected_at.isoformat() if a.detected_at else None,
        }
        grouped.setdefault(a.severity, []).append(item)

    return {
        "total": len(alerts),
        "critical": len(grouped.get("critical", [])),
        "warn": len(grouped.get("warn", [])),
        "info": len(grouped.get("info", [])),
        "alerts": grouped,
    }
