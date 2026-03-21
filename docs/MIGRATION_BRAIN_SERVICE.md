# Migration: Brain HTTP service

## Overview

The **`chili-brain/`** package provides a separate FastAPI app that currently **delegates** to `app.services.trading.learning` (strangler pattern). CHILI’s **`scripts/brain_worker.py`** can run the cycle **in-process** (default) or **delegate** to Brain via HTTP when `CHILI_USE_BRAIN_SERVICE=1`.

## Environment variables

| Variable | Where | Purpose |
|----------|--------|---------|
| `BRAIN_SERVICE_URL` | CHILI / worker | Base URL of Brain service, e.g. `http://brain:8090` (Compose) or `http://127.0.0.1:8090` |
| `BRAIN_INTERNAL_SECRET` | CHILI / worker | Must match Brain’s `CHILI_BRAIN_INTERNAL_SECRET` for `Authorization: Bearer` |
| `CHILI_BRAIN_INTERNAL_SECRET` | Brain container | If set, protects `POST /v1/run-learning-cycle` |
| `CHILI_USE_BRAIN_SERVICE` | `brain-worker` only | `1` / `true` / `yes` → worker calls HTTP instead of in-process `run_learning_cycle` |

## Breaking changes

- None for default setups (worker unchanged unless you enable delegation).
- Future phases may **remove** duplicate worker control routes; see `docs/REFACTOR_AUDIT.md`.

## Rollback

- Set `CHILI_USE_BRAIN_SERVICE=0` or unset.
- Stop the `brain` Compose service; worker uses in-process learning again.
