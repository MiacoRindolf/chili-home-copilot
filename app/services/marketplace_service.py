from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..logger import log_error, log_info
from ..models import MarketplaceModule

REGISTRY_CACHE_TTL_SECONDS = 300


@dataclass
class RegistryModule:
    slug: str
    name: str
    summary: str
    version: str
    icon_url: Optional[str] = None
    tags: Optional[List[str]] = None
    compatibility: Optional[str] = None
    download_url: Optional[str] = None
    homepage_url: Optional[str] = None
    repo_url: Optional[str] = None
    checksum: Optional[str] = None
    permissions: Optional[List[str]] = None


def _get_modules_root() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "modules"


def _fetch_registry_index(trace_id: str) -> Dict[str, Any]:
    """Fetch registry JSON from the configured URL, or return an empty index."""
    url = settings.module_registry_url.strip()
    if not url:
        log_info(trace_id, "module_registry_disabled")
        return {"modules": []}

    try:
        log_info(trace_id, f"fetching_module_registry url={url}")
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Registry index must be a JSON object")
        return data
    except Exception as exc:  # pragma: no cover - network/IO heavy
        log_error(trace_id, f"module_registry_fetch_failed error={exc}")
        return {"modules": []}


def _parse_registry_modules(index: Dict[str, Any]) -> List[RegistryModule]:
    modules_raw = index.get("modules") or []
    parsed: List[RegistryModule] = []
    for raw in modules_raw:
        try:
            parsed.append(
                RegistryModule(
                    slug=str(raw["slug"]),
                    name=str(raw.get("name") or raw["slug"]),
                    summary=str(raw.get("summary") or ""),
                    version=str(raw.get("version") or "0.0.0"),
                    icon_url=raw.get("icon_url") or None,
                    tags=list(raw.get("tags") or []) or None,
                    compatibility=raw.get("compatibility") or None,
                    download_url=raw.get("download_url") or None,
                    homepage_url=raw.get("homepage_url") or None,
                    repo_url=raw.get("repo_url") or None,
                    checksum=raw.get("checksum") or None,
                    permissions=list(raw.get("permissions") or []) or None,
                )
            )
        except Exception:
            # Skip malformed entries but do not fail the whole registry.
            continue
    return parsed


def list_registry_with_status(db: Session, trace_id: str) -> List[Dict[str, Any]]:
    """Return registry modules merged with local installation status."""
    index = _fetch_registry_index(trace_id)
    registry = {m.slug: m for m in _parse_registry_modules(index)}

    installed: Dict[str, MarketplaceModule] = {
        m.slug: m for m in db.query(MarketplaceModule).all()
    }

    result: List[Dict[str, Any]] = []
    for slug, reg in registry.items():
        installed_mod = installed.get(slug)
        result.append(
            {
                "slug": slug,
                "name": reg.name,
                "summary": reg.summary,
                "version": reg.version,
                "icon_url": reg.icon_url,
                "tags": reg.tags or [],
                "compatibility": reg.compatibility,
                "download_url": reg.download_url,
                "homepage_url": reg.homepage_url,
                "repo_url": reg.repo_url,
                "checksum": reg.checksum,
                "permissions": reg.permissions or [],
                "installed": installed_mod is not None,
                "enabled": bool(installed_mod.enabled) if installed_mod else False,
                "installed_version": installed_mod.version if installed_mod else None,
            }
        )

    # Also include locally installed modules that are no longer in the registry.
    for slug, mod in installed.items():
        if slug in registry:
            continue
        result.append(
            {
                "slug": slug,
                "name": mod.name,
                "summary": mod.summary or "",
                "version": mod.version,
                "icon_url": mod.icon_url,
                "tags": [],
                "compatibility": None,
                "download_url": None,
                "homepage_url": mod.homepage_url,
                "repo_url": mod.repo_url,
                "checksum": mod.checksum,
                "permissions": [],
                "installed": True,
                "enabled": bool(mod.enabled),
                "installed_version": mod.version,
            }
        )

    return result


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    """Extract a zip archive into dest_dir, defending against path traversal.

    Returns the top-level directory for the module.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        top_level_dirs = set()
        for member in zf.infolist():
            name = member.filename
            # Normalize separators
            name = name.replace("\\", "/")
            if name.endswith("/"):
                continue
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"Unsafe path in archive: {name}")
            parts = name.split("/")
            if parts:
                top_level_dirs.add(parts[0])

        if not top_level_dirs:
            raise ValueError("Archive is empty")
        if len(top_level_dirs) > 1:
            # We expect a single top-level directory per module.
            raise ValueError("Archive must contain a single top-level directory")

        top_dir_name = next(iter(top_level_dirs))
        zf.extractall(dest_dir)

    return dest_dir / top_dir_name


def _download_archive(download_url: str, trace_id: str) -> Path:
    modules_root = _get_modules_root()
    modules_root.mkdir(parents=True, exist_ok=True)
    tmp_path = modules_root / "tmp_download.zip"

    log_info(trace_id, f"downloading_module url={download_url}")
    with httpx.Client(timeout=30.0) as client:
        with client.stream("GET", download_url) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)

    return tmp_path


def install_from_registry(
    db: Session, slug: str, trace_id: str
) -> Tuple[MarketplaceModule, bool]:
    """Install or upgrade a module identified by slug.

    Returns (module, installed_now) where installed_now is True on fresh install.
    """
    index = _fetch_registry_index(trace_id)
    registry = {m.slug: m for m in _parse_registry_modules(index)}
    reg = registry.get(slug)
    if not reg or not reg.download_url:
        raise ValueError(f"Module '{slug}' not found in registry or missing download_url")

    archive_path = _download_archive(reg.download_url, trace_id)
    modules_root = _get_modules_root()
    extracted_root = _safe_extract_zip(archive_path, modules_root)

    archive_path.unlink(missing_ok=True)

    local_path = str(extracted_root.resolve())

    existing = db.query(MarketplaceModule).filter(MarketplaceModule.slug == slug).one_or_none()
    now = datetime.utcnow()

    if existing:
        existing.name = reg.name
        existing.version = reg.version
        existing.summary = reg.summary
        existing.description = existing.description or ""
        existing.icon_url = reg.icon_url
        existing.homepage_url = reg.homepage_url
        existing.repo_url = reg.repo_url
        existing.local_path = local_path
        existing.source = "registry"
        existing.enabled = True
        existing.checksum = reg.checksum or existing.checksum
        existing.last_checked_at = now
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing, False

    mod = MarketplaceModule(
        slug=slug,
        name=reg.name,
        version=reg.version,
        summary=reg.summary,
        description="",
        icon_url=reg.icon_url,
        homepage_url=reg.homepage_url,
        repo_url=reg.repo_url,
        local_path=local_path,
        source="registry",
        enabled=True,
        checksum=reg.checksum,
        last_checked_at=now,
    )
    db.add(mod)
    db.commit()
    db.refresh(mod)
    return mod, True


def set_enabled(db: Session, slug: str, enabled: bool) -> MarketplaceModule:
    mod = (
        db.query(MarketplaceModule)
        .filter(MarketplaceModule.slug == slug)
        .one_or_none()
    )
    if not mod:
        raise ValueError(f"Module '{slug}' not installed")
    mod.enabled = enabled
    db.add(mod)
    db.commit()
    db.refresh(mod)
    return mod


def uninstall(db: Session, slug: str) -> None:
    mod = (
        db.query(MarketplaceModule)
        .filter(MarketplaceModule.slug == slug)
        .one_or_none()
    )
    if not mod:
        return
    path = mod.local_path_obj()
    try:
        if path.exists() and path.is_dir():
            # Best-effort removal; errors are logged but not fatal.
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()
    except Exception as exc:  # pragma: no cover - filesystem conditions vary
        log_error("uninstall_module", f"failed_to_delete_files slug={slug} error={exc}")

    db.delete(mod)
    db.commit()

