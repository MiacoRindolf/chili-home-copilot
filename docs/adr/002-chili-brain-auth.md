# ADR 002: Authentication between CHILI and Brain service

## Status

Accepted

## Context

CHILI identifies users via `chili_device_token` ([`app/pairing.py`](../app/pairing.py)). External SPAs on another origin cannot send that cookie. Internal service calls (CHILI → Brain, worker → Brain) must not rely on browser cookies.

## Decision

1. **Machine-to-machine:** `POST /v1/run-learning-cycle` and future internal routes require optional **`Authorization: Bearer <CHILI_BRAIN_INTERNAL_SECRET>`** when `CHILI_BRAIN_INTERNAL_SECRET` is set in the Brain container environment.
2. **Empty secret (development only):** If the secret is **unset**, the Brain service **does not** enforce Bearer auth (convenient for local dev; **do not** expose to untrusted networks).
3. **Browser / external SPAs:** Continue using [`BRAIN_V1_WAKE_SECRET`](../.env.example) + `X-Chili-Brain-Wake-Secret` on CHILI’s **`/api/v1/brain-next-cycle`** (implemented on main app), or call Brain service with Bearer secret from server-side proxies.
4. **Future:** If Brain is exposed beyond localhost, require TLS and a non-empty secret; consider mTLS for homelab.

## Consequences

- **Positive:** Simple solo-dev setup; clear upgrade path.
- **Negative:** Misconfiguration risk if Brain port is forwarded without a secret.

## Related

- [001-brain-service-boundaries.md](001-brain-service-boundaries.md)
