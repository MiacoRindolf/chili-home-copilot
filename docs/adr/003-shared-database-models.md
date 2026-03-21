# ADR 003: Shared PostgreSQL and ORM models

## Status

Accepted (initial phase)

## Context

Brain domains persist `LearningEvent`, `ScanPattern`, project agent tables, etc. Moving Brain to another service could imply a second database and sync complexity.

## Decision

1. **Single PostgreSQL** for the product: Brain service and CHILI use the **same `DATABASE_URL`** (Compose overrides host to `postgres`).
2. **Single SQLAlchemy model layer** remains under **`app/models/`** in the monorepo; Brain container **includes the same `app/` package** during the strangler phase (see Brain `Dockerfile`).
3. **Future optional extraction:** A shared **`chili-models`** package (pip installable) can be factored out **after** boundaries stabilize, without blocking the first Brain deployment.

## Consequences

- **Positive:** No cross-DB consistency issues; migrations stay in one [`app/migrations.py`](../app/migrations.py) pipeline.
- **Negative:** Brain and CHILI must run **compatible** code versions against the same schema; coordinate deploys or use backward-compatible migrations.

## Related

- [001-brain-service-boundaries.md](001-brain-service-boundaries.md)
