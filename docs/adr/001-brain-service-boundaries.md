# ADR 001: Brain HTTP service boundaries

## Status

Accepted (Phase 1–2 refactor)

## Context

CHILI currently runs trading learning, code brain, reasoning brain, and project brain **inside** the main FastAPI process, with a separate **`brain_worker`** process for the full trading learning cycle. The product goal is a **dedicated Brain service** (HTTP + worker) that owns brain-domain work, while CHILI remains the conversational core, identity, and primary API gateway.

## Decision

1. **Brain service** exposes a **small, versioned HTTP API** (`/v1/...`) for operations that will move out of CHILI (starting with `POST /v1/run-learning-cycle`).
2. **Strangler pattern:** the first implementation **imports existing** `app.services.*` and `app.db` from a **shared tree** copied into the Brain container image (same repo build context). Logic is **not duplicated** initially.
3. **Phased migration:** Trading learning first; Code → Reasoning → Project cycles gain endpoints as they are extracted.
4. **Worker process:** May run as a **separate Compose service** using the **same** main app image (`scripts/brain_worker.py`) or eventually the Brain image only; both are documented in `docs/DOCKER_FULL_STACK.md`.

## Consequences

- **Positive:** Clear deployment unit for scaling/restarting brain work without recycling CHILI.
- **Negative:** Two images or two services to build until extraction is complete; operational overhead for a solo dev mitigated by Compose.

## Related

- [003-shared-database-models.md](003-shared-database-models.md)
- [002-chili-brain-auth.md](002-chili-brain-auth.md)
