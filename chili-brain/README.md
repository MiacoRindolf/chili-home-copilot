# CHILI Brain HTTP service

Separate FastAPI app that **delegates** to `app.services.trading.learning` (and future brain domains) using the **strangler** pattern: the `app/` tree is copied into the image alongside this package until logic is split into shared libraries.

## Run locally (from repo root)

```powershell
$env:PYTHONPATH = "$PWD;$PWD\chili-brain"
$env:CHILI_BRAIN_INTERNAL_SECRET = "dev-secret"
conda activate chili-env
uvicorn chili_brain_service.main:app --host 127.0.0.1 --port 8090
```

`PYTHONPATH` must include the repo root (for `app`) and `chili-brain` (for `chili_brain_service`).

Or use Docker Compose (`brain` service) — see [docs/DOCKER_FULL_STACK.md](../docs/DOCKER_FULL_STACK.md).

## Endpoints

| Method | Path | Notes |
|--------|------|--------|
| GET | `/health` | Liveness |
| POST | `/v1/run-learning-cycle` | Full trading learning cycle |
| GET | `/v1/capabilities` | What is implemented vs planned |
| POST | `/v1/run-code-learning-cycle` | Returns **501** until extraction |
| POST | `/v1/run-reasoning-cycle` | Returns **501** until extraction |
| POST | `/v1/run-project-brain-cycle` | Returns **501** until extraction |

Set `CHILI_BRAIN_INTERNAL_SECRET` and send `Authorization: Bearer <secret>` for protected routes.

OpenAPI: `/docs`, `/openapi.json`.

## Testing

Contract tests live under **`chili-brain/tests/`** (not `tests/` at repo root) so they do not load the PostgreSQL-only `tests/conftest.py`.

```powershell
conda activate chili-env
pytest chili-brain/tests -v
```
