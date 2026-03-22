# Docker Compose — full stack

## Services

| Service | Image | Port | Role |
|---------|--------|------|------|
| `postgres` | postgres:16 | 5433→5432 | Database |
| `ollama` | ollama | 11434 | Local LLM |
| `chili` | `chili-app:local` | 8000 | Main FastAPI app (**HTTPS** — self-signed cert baked into the image; browser will warn) |
| `brain` | `chili-brain:local` | 8090 | Brain HTTP API (`chili-brain/Dockerfile`) — **HTTP** on the Docker network so the worker client does not need custom TLS trust |
| `brain-worker` | `chili-app:local` | — | Continuous learning loop (`scripts/brain_worker.py`). **Start/Stop in the Brain UI** drives this service via the Docker API (the `chili` service mounts `/var/run/docker.sock`). |

Shared volume: **`chili_data`** → `/app/data` (status files, caches, ML pickles).

## One command

From the repository root (with `.env` containing at least `DATABASE_URL` if you override):

```powershell
docker compose up --build
```

- **CHILI in the browser:** `https://localhost:8000/chat` (self-signed certificate; trust the warning for local dev).
- **Database URL inside Compose:** services use `postgresql://chili:chili@postgres:5432/chili` (overrides host `.env` for DB host).
- **Shared secrets:** set `CHILI_BRAIN_INTERNAL_SECRET` in `.env` for Brain ↔ worker delegation (match `BRAIN_INTERNAL_SECRET` / brain client).
- **TLS:** the `chili` service sets **`CHILI_TLS=1` in `docker-compose.yml`** (not taken from host `.env`), so the app always speaks **HTTPS** on 8000 unless you override with `docker compose run -e CHILI_TLS=0` or a compose override. Previously, `CHILI_TLS=${CHILI_TLS:-1}` could pick up **`CHILI_TLS=0` from the project `.env`** and serve plain HTTP while the browser used `https://` → **`PR_END_OF_FILE_ERROR`**. The `brain` service stays HTTP between containers unless you add your own reverse proxy or mTLS.

### Firefox: `PR_END_OF_FILE_ERROR` on `https://localhost:8000`

Firefox often resolves `localhost` to **IPv6** (`::1`) first. The Docker image includes **127.0.0.1** and **`::1`** in the self-signed cert SAN. If you still see this error after `docker compose build --no-cache chili`, try **`https://127.0.0.1:8000/brain`** or use **HTTP** with `CHILI_TLS=0` and open **`http://localhost:8000/brain`**.

## Modes

1. **Default worker (in-process learning):** `CHILI_USE_BRAIN_SERVICE=0` — worker runs `run_learning_cycle` inside the worker container; `brain` is optional for manual HTTP triggers (`POST /v1/run-learning-cycle`).
2. **Delegated learning:** set `CHILI_USE_BRAIN_SERVICE=1` — worker calls the **`brain`** service over HTTP; heavy work runs in the Brain container.

## CHILI-only dev (no Docker)

Use `scripts/start-https.ps1` and run `python scripts/brain_worker.py` on the host as before.

## See also

- [chili-brain/README.md](../chili-brain/README.md)
- [MIGRATION_BRAIN_SERVICE.md](MIGRATION_BRAIN_SERVICE.md)
