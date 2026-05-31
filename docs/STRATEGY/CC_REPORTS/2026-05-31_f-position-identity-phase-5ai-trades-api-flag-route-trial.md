# Phase 5AI - Trades API Flag Route Trial

Date: 2026-05-31

## Summary

Phase 5AI started the controlled live route trial for
`CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true`.

Because the live repository root (`D:\dev\chili-home-copilot`) is heavily dirty
and behind the merged branch, the web container was recreated from the clean
Phase 5AH worktree instead of pulling or overwriting the dirty root. Only the
`chili` web container was recreated; Postgres and trading workers were not
restarted.

## Runtime shape

The running web container now mounts:

```text
D:\dev\chili-home-copilot\project_ws\_worktrees\phase5ah-trades-api-open-flag-path\app -> /app/app
D:\dev\chili-home-copilot\project_ws\_worktrees\phase5ah-trades-api-open-flag-path\docs -> /app/docs
```

The live env file contains:

```text
CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true
```

Container env verified:

```text
CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=true
```

## Pre-flip gates

All gates were green before the route flag was exercised:

```text
Phase 5AH cutover probe: COMPLETE_POSITIVE
Phase 5AG open runtime adapter probe: COMPLETE_POSITIVE
Phase 5K live-path parity probe: COMPLETE_POSITIVE
Phase 5I post-rename soak probe: COMPLETE_POSITIVE
```

## Live route trial

Unauthenticated/guest route:

```text
/api/trading/trades               ok=True rows=50 suppressed=0
/api/trading/trades?status=open   ok=True rows=0  suppressed=0
/api/trading/trades?status=closed ok=True rows=50 suppressed=0
```

Authenticated user-1 route:

```text
/api/trading/trades               ok=True rows=50 suppressed=0 first_id=2139
/api/trading/trades?status=open   ok=True rows=5  suppressed=0 first_id=2139
/api/trading/trades?status=closed ok=True rows=50 suppressed=0 first_id=2130
```

Route logs showed the intended code paths:

```text
[phase5ah] /trades envelope runtime cutover active rows=50 status=None suppressed_stale=0
[phase5ah] /trades envelope runtime cutover active rows=5 status=open suppressed_stale=0
[phase5af] /trades envelope cutover active rows=50 status=closed
```

No fallback, traceback, or route exception lines were observed during the short
soak.

## Post-trial probes

After startup broker sync created one fresh Coinbase envelope, the gates stayed
green:

```text
Phase 5AH cutover probe: COMPLETE_POSITIVE
  open exact_match=true, old_rows=5, new_rows=5
  closed exact_match=true, old_rows=50, new_rows=50
  all accepted=true, tie_order_only=true

Phase 5AG open runtime adapter probe: COMPLETE_POSITIVE
Phase 5K live-path parity probe: COMPLETE_POSITIVE
Phase 5I post-rename soak probe: COMPLETE_POSITIVE
  fresh decisions=21, envelopes=21, closes=10
  hard linkage issues=0
  mismatched rows=0
```

## Architect verdict

The Phase 5AI route trial is healthy enough to leave the flag on for a short
soak. The one caveat is operational, not semantic: the web container is running
from the clean Phase 5AH worktree while the live root remains dirty and behind.
If someone recreates `chili` from the dirty root, this trial code will disappear
until the root is reconciled or the compose command points back at the clean
worktree.

The next engineering slice should harden the remaining mixed/all tie-order
drift so `/api/trading/trades` can report exact parity instead of
`tie_order_only=true`, then decide whether to keep the route flag permanently
on.

