# QUEUED: f-chili-env-pin-pytest-asyncio

**Source:** CC_REPORT §54 + Open Question §5 in `2026-05-23_f-brain-runtime-tab-redesign.md`, ACK'd in the corresponding COWORK_REVIEW.
**Scope:** Conda env spec + small CLAUDE.md note. ~10 minutes.
**Risk:** Low. Only touches developer environment, not runtime code.

## Goal

Pin `pytest-asyncio` (and any related conftest-collection-blocking pytest plugins discovered while investigating) in whatever conda env spec file `chili-env` is built from, so a fresh checkout + `conda env create` produces a working pytest installation. CC's 2026-05-23 session had to ad-hoc upgrade `pytest-asyncio` from 0.23.3 mid-session because `'Package' object has no attribute 'obj'` errored during pytest collection.

## Tasks

1. **Locate the env spec.** Likely `environment.yml` at repo root, or possibly `chili-env.yml` / `requirements*.txt`. If both a conda yaml and a pip requirements file exist, pin in the one that's authoritative for `conda env create -n chili-env -f <file>`.
2. **Pin `pytest-asyncio`** to the version CC's session ended up with. Run `conda run -n chili-env python -c "import pytest_asyncio; print(pytest_asyncio.__version__)"` to discover the working version. Use exact pin (`==`) given the collection failure was version-sensitive.
3. **Spot-check related collection-time plugins.** While there, capture versions of `pytest`, `pytest-anyio`, `pytest-mock`, `pytest-cov`, `pytest-xdist` — pin any whose absence/version drift could plausibly explain the collection failure pattern.
4. **Update `CLAUDE.md`** under "Environment & runtime" — add a one-line note: "If pytest fails to collect with `'Package' object has no attribute 'obj'`, the env's pytest-asyncio is stale; recreate from the pinned env spec."
5. **Verify.** `conda run -n chili-env pytest --collect-only tests/test_brain_runtime_endpoints.py -q` should return cleanly.

## Acceptance

- Env spec file has explicit `pytest-asyncio==<version>` line.
- CLAUDE.md mentions the env-recreation diagnostic.
- `pytest --collect-only` works without ad-hoc upgrades.
- Commit message describes the rationale (tie to 2026-05-23 session).

## Commit message

```
chore(chili-env): pin pytest-asyncio to unblock pytest collection

Pinned after 2026-05-23 brain-runtime-tab-redesign session had to ad-hoc
upgrade pytest-asyncio from 0.23.3 to make collection succeed. Without
a pin, a fresh `conda env create` or another developer's checkout hits
'Package' object has no attribute 'obj' during pytest collection.
```

## Out of scope

- Migrating to a different test framework or pytest major-version upgrade.
- Pinning the entire env (only test-collection-critical plugins).
- Reproducing the original failure (CC already saw it; we're closing the prevention loop).
