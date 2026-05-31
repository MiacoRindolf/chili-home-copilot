# Phase 5N - Source Posture Watch

Date: 2026-05-31

## Summary

Phase 5N turns the Phase 5M source-posture probe into a lightweight recurring
guard with stable output.

The watch wrapper runs `scripts/d-phase5m-source-posture-probe.py`, writes the
result to `scripts/dispatch-phase5n-source-posture-watch-out.txt`, and stays
quiet on `COMPLETE_POSITIVE`. On `ALERT` or `REGRESSION`, it includes the
Phase 5M runbook and the exact Phase 5K/5I follow-up probes to run after any
source-mount correction.

## What Landed

- `scripts/d-phase5n-source-posture-watch.py`
- `tests/test_phase5n_source_posture_watch.py`

The existing app automation `phase5m-source-posture-guard` should call the
Phase 5N wrapper instead of the raw Phase 5M probe so the latest verdict is
persisted to a stable file.

## Validation

```text
tests/test_phase5n_source_posture_watch.py -> 2 passed
scripts/d-phase5n-source-posture-watch.py  -> COMPLETE_POSITIVE
Phase 5K live-path parity probe            -> COMPLETE_POSITIVE
Phase 5I post-rename soak probe            -> COMPLETE_POSITIVE
```

## Architect Verdict

This closes the most practical operational gap found in Phase 5M. Runtime
source posture is no longer a one-off manual check; it is a small recurring
guard that catches dirty-root drift before the next cutover or worker restart
turns into a mystery.

