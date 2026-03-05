from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, FastAPI

from ..config import settings
from ..logger import log_error, log_info


@dataclass
class ModuleInfo:
    """Metadata for an optional CHILI module (planner, intercom, etc.)."""

    name: str
    label: str
    router: Optional[APIRouter] = None
    nav: Optional[Dict[str, Any]] = None  # e.g. {"path": "/planner", "label": "Planner", "icon": "📋"}
    templates_dir: Optional[Path] = None
    planner_actions: str = ""
    register_handlers: Optional[Callable[[Dict[str, Callable], set[str]], None]] = None


def _parse_enabled_modules(raw: Optional[str]) -> List[str]:
    if not raw:
        # Default: all known modules enabled
        return ["planner", "intercom", "voice", "projects"]
    parts = [p.strip().lower() for p in raw.split(",")]
    return [p for p in parts if p]


def get_enabled_module_names() -> List[str]:
    """Return list of enabled module names from config (normalized)."""
    return _parse_enabled_modules(getattr(settings, "chili_modules", ""))


_LOADED_MODULES: Optional[List[ModuleInfo]] = None


def load_enabled_modules() -> List[ModuleInfo]:
    """Instantiate ModuleInfo objects for all enabled optional modules.

    This keeps imports local so disabling a module avoids importing its router/service.
    """
    global _LOADED_MODULES
    if _LOADED_MODULES is not None:
        return _LOADED_MODULES

    enabled = set(get_enabled_module_names())
    modules: List[ModuleInfo] = []

    # Planner module
    if "planner" in enabled:
        try:
            from ..routers import planner as planner_router
            from ..prompts import load_prompt

            planner_actions = ""
            try:
                # Optional extra instructions specific to the project planner
                planner_actions = load_prompt("planner_actions")
            except FileNotFoundError:
                planner_actions = ""

            modules.append(
                ModuleInfo(
                    name="planner",
                    label="Project Planner",
                    router=planner_router.router,
                    nav={"path": "/planner", "label": "Planner", "icon": "🗂"},
                    templates_dir=Path(__file__).resolve().parents[1] / "templates",
                    planner_actions=planner_actions,
                )
            )
        except Exception:
            # If anything goes wrong, fail soft and keep core app working.
            pass

    # Intercom module
    if "intercom" in enabled:
        try:
            from ..routers import intercom as intercom_router

            modules.append(
                ModuleInfo(
                    name="intercom",
                    label="Intercom",
                    router=intercom_router.router,
                    nav={"path": "/intercom", "label": "Intercom", "icon": "📣"},
                    templates_dir=Path(__file__).resolve().parents[1] / "templates",
                )
            )
        except Exception:
            pass

    # Voice module (API only, no dedicated nav link)
    if "voice" in enabled:
        try:
            from ..routers import voice as voice_router

            modules.append(
                ModuleInfo(
                    name="voice",
                    label="Voice",
                    router=voice_router.router,
                    nav=None,
                    templates_dir=None,
                )
            )
        except Exception:
            pass

    # Projects / knowledge-base module
    if "projects" in enabled:
        try:
            from ..routers import projects as projects_router

            modules.append(
                ModuleInfo(
                    name="projects",
                    label="Project Space",
                    router=projects_router.router,
                    nav=None,
                    templates_dir=None,
                )
            )
        except Exception:
            pass

    _LOADED_MODULES = modules
    return modules


def is_module_enabled(name: str) -> bool:
    return name.lower() in {m.name for m in load_enabled_modules()}


def get_nav_modules() -> List[Dict[str, Any]]:
    """Return navigation items for enabled modules."""
    items: List[Dict[str, Any]] = []
    for m in load_enabled_modules():
        if m.nav:
            items.append(m.nav)
    return items


def load_third_party_module(app: FastAPI, root: Path) -> Optional[ModuleInfo]:
    """Load a third-party module from an installed directory.

    The directory must contain a ``chili_module.yaml`` manifest with an
    ``entrypoint`` field like ``"my_module.entry:get_module_info"``.

    This function:
    - imports the entrypoint
    - calls it to obtain a ``ModuleInfo``
    - includes the router (if any)
    - appends a nav item (if any) to ``app.state.nav_modules``
    """
    trace_id = "module_loader"
    try:
        import yaml  # type: ignore[import]
    except Exception as exc:  # pragma: no cover - optional dependency
        log_error(trace_id, f"pyyaml_not_installed error={exc}")
        return None

    manifest_path = root / "chili_module.yaml"
    if not manifest_path.exists():
        log_error(trace_id, f"missing_manifest path={manifest_path}")
        return None

    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # pragma: no cover - malformed YAML
        log_error(trace_id, f"manifest_parse_failed path={manifest_path} error={exc}")
        return None

    entrypoint = data.get("entrypoint")
    if not isinstance(entrypoint, str) or ":" not in entrypoint:
        log_error(trace_id, f"invalid_entrypoint manifest={manifest_path}")
        return None

    module_name, func_name = entrypoint.split(":", 1)
    if not module_name or not func_name:
        log_error(trace_id, f"invalid_entrypoint_parts manifest={manifest_path}")
        return None

    # Ensure the module root is importable.
    import sys
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    try:
        import importlib

        mod = importlib.import_module(module_name)
        fn = getattr(mod, func_name)
    except Exception as exc:  # pragma: no cover - dynamic import
        log_error(trace_id, f"entrypoint_import_failed module={module_name} func={func_name} error={exc}")
        return None

    try:
        info: ModuleInfo = fn(app)  # type: ignore[assignment]
    except Exception as exc:  # pragma: no cover - third-party code
        log_error(trace_id, f"entrypoint_call_failed module={module_name} func={func_name} error={exc}")
        return None

    # Wire router.
    if info.router:
        try:
            app.include_router(info.router)
        except Exception as exc:  # pragma: no cover - FastAPI wiring
            log_error(trace_id, f"router_include_failed name={info.name} error={exc}")

    # Wire navigation.
    if info.nav:
        try:
            nav_list = getattr(app.state, "nav_modules", []) or []
            # ``nav`` is typically a dict like {"path": "/foo", "label": "Foo", "icon": "🧩"}
            nav_list.append(info.nav)
            app.state.nav_modules = nav_list
        except Exception as exc:  # pragma: no cover - defensive
            log_error(trace_id, f"nav_append_failed name={info.name} error={exc}")

    log_info(trace_id, f"third_party_module_loaded name={info.name}")
    return info


