# CHILI client — `<project-name>`

Template for **external frontends** (web, desktop, mobile shell) that talk to a CHILI backend.

## Configuration

| Env | Example | Purpose |
|-----|-----------|---------|
| `CHILI_API_BASE` | `https://localhost:8000` | REST API origin (same host as main app) |
| `CHILI_BRAIN_WAKE_SECRET` | *(optional)* | If calling `GET/POST /api/v1/brain-next-cycle`, send header `X-Chili-Brain-Wake-Secret` (see [BRAIN_EXTERNAL_UI.md](../BRAIN_EXTERNAL_UI.md)) |

## Conventions

1. **Cookies:** `chili_device_token` is set on the **CHILI origin** only. SPAs on another port must use a **wake secret** or a **reverse proxy** so API calls are same-origin.
2. **Versioning:** pin CHILI release or commit; track breaking changes via `docs/MIGRATION_*.md` in the backend repo.
3. **OpenAPI:** fetch `https://<host>/openapi.json` for codegen (optional).

## Boilerplate

- Replace `<project-name>` and add license.
- Add CI: lint + typecheck + contract tests against a running CHILI test stack (`TEST_DATABASE_URL`).
