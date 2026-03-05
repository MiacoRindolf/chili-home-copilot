# CHILI Modules & Marketplace

This document describes how **CHILI modules** work and how third‑party
modules can be packaged for the future marketplace.

## 1. Module Types

- **Core modules** – shipped with CHILI and always available:
  - Chat + LLM, home dashboard, identity/pairing, health, metrics, RAG,
    personality, memory, vision, web search.
- **First‑party optional modules** – implemented in the repo and toggled
  via `CHILI_MODULES` (planner, intercom, voice, projects).
- **Third‑party marketplace modules** – downloaded into `data/modules/`
  and loaded dynamically at runtime.

## 2. Configuration

All module settings live in `app/config.py`:

- `chili_modules`: comma‑separated list of built‑in module names
  (`planner,intercom,voice,projects`). Empty string means “enable all
  known built‑in modules”.
- `module_registry_url`: optional HTTPS URL for the marketplace
  registry index (see below). When empty, CHILI treats the registry as
  offline and only shows locally‑installed modules.

Set `MODULE_REGISTRY_URL` in your `.env` file to point CHILI at a
trusted registry JSON document.

## 3. Third‑Party Module Layout

Every third‑party module is a **Python package with a manifest**:

```text
my-awesome-module/
  chili_module.yaml
  my_module/
    __init__.py
    entry.py
    ... your code ...
```

### 3.1 `chili_module.yaml` manifest

Required fields:

- `name`: Human‑readable name, e.g. `"My Awesome Module"`.
- `slug`: Unique, URL‑safe identifier, e.g. `"my-awesome-module"`.
- `version`: Semantic version string, e.g. `"1.0.0"`.
- `summary`: Short, one‑line summary for cards.
- `description`: Longer Markdown description for the details panel.
- `entrypoint`: Python path to a callable that returns a `ModuleInfo`
  instance, e.g. `"my_module.entry:get_module_info"`.

Optional fields:

- `permissions`: List of strings describing capabilities the module
  expects, e.g. `["db", "internet", "filesystem"]`. These are **advisory
  only** and used to display warnings in the UI.
- `homepage_url`, `repo_url`, `icon_url`: URLs for documentation,
  source, and icon assets.

The entrypoint function should look roughly like:

```python
from app.modules import ModuleInfo
from fastapi import APIRouter


def get_module_info(app) -> ModuleInfo:
    router = APIRouter()

    @router.get("/my-module/health")
    def health():
        return {"ok": True}

    return ModuleInfo(
        name="My Awesome Module",
        label="My Module",
        router=router,
        nav={"path": "/my-module", "label": "My Module", "icon": "🧩"},
        templates_dir=None,
        planner_actions="",
        register_handlers=None,
    )
```

## 4. Registry Index JSON

The **registry index** is a JSON document that lists available modules
and their metadata. CHILI fetches it from `settings.module_registry_url`
when the marketplace page or API is used.

Shape of the JSON document:

```json
{
  "modules": [
    {
      "slug": "my-awesome-module",
      "name": "My Awesome Module",
      "summary": "Adds X to CHILI.",
      "version": "1.0.0",
      "icon_url": "https://example.com/icons/my-module.png",
      "tags": ["productivity", "planner"],
      "compatibility": ">=0.1.0",
      "download_url": "https://example.com/chili-modules/my-awesome-module-1.0.0.zip",
      "homepage_url": "https://example.com/my-module",
      "repo_url": "https://github.com/me/my-awesome-module",
      "checksum": "sha256:...",
      "permissions": ["db", "internet"]
    }
  ]
}
```

Notes:

- `checksum` is optional but recommended; if present CHILI will verify
  the downloaded archive before installing.
- `compatibility` is advisory and compared against CHILI’s version in
  the registry client.

## 5. Installation Paths

- All third‑party modules are extracted under `data/modules/{slug}-{version}/`.
- The marketplace database record stores:
  - `slug`, `name`, `version`, `summary`, `description`
  - `local_path` (absolute path to the installed directory)
  - `enabled` flag
  - metadata such as `installed_at`, `source`, and `last_checked_at`.

The dynamic loader in `app/modules/__init__.py` reads the manifest from
`local_path/chili_module.yaml`, imports the entrypoint, and wires the
returned `ModuleInfo` into the running FastAPI app (routers, nav,
tool‑handlers).

## 6. Safety

- Only install modules from **trusted registries** over HTTPS.
- The installer validates archives to avoid path‑traversal attacks and
ignores any file outside the intended module directory.
- Modules execute arbitrary Python code on your CHILI server; review
module source and permissions before installing.

