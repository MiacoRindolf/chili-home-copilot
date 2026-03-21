# Marketplace module contract (third-party modules)

## Goal

Optional modules under `data/modules/` (or installable packages) load via [`load_third_party_module`](../app/main.py) and expose a FastAPI `APIRouter`.

## Contract (v1)

1. **Package layout:** each module is a directory with at minimum:
   - `manifest.json` — metadata (name, version, entry router)
   - Python package implementing `register(app: FastAPI)` or the loader convention documented in [`app/modules/__init__.py`](../app/modules/__init__.py) (follow existing `MarketplaceModule` rows in DB).

2. **Isolation:** modules must not patch CHILI core globals; use dependency injection and `get_db` / `get_identity_ctx` like core routers.

3. **Versioning:** pin a **semver** in manifest; breaking API changes require a major bump and a note in the module’s `CHANGELOG.md`.

4. **Security:** marketplace code runs in the same process as CHILI — only install trusted sources; prefer separate git repos with signed tags for “official” modules.

## Future: separate git repo per module

- Publish as a **pip** package (`pip install git+https://...`) or submodule.
- CI: run `ruff` + `pytest` in the module repo; smoke-test against CHILI’s OpenAPI.

## Related

- [REFACTOR_AUDIT.md](REFACTOR_AUDIT.md) — redundancy / loader audit
- [TOOLING.md](TOOLING.md) if present
