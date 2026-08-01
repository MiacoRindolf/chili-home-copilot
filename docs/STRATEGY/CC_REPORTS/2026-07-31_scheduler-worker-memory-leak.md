# CC Report — scheduler-worker memory "leak" root-caused: glibc arena retention, not a Python leak

**Date:** 2026-07-31 (operator-directed task, not from NEXT_TASK.md — NEXT_TASK ross-capture-P1a remains PENDING)
**Symptom:** `chili-clean-recovery-scheduler` (image `chili-app`, `CHILI_SCHEDULER_ROLE=rnd_only`) reached
9.9GB RSS after ~13h; 5.6GB within ~1.5h of a restart. With the WSL2 VM capped at 23.47GB this drove OOM
SIGKILLs that killed two replay containers on 07-31. Operator mitigation was a restart every few hours.

## Measurement (live container, 07-31/08-01)

- `docker stats`: 4.6GiB at 3h uptime (~1.5GB/h early ramp).
- Built-in `[mem_watcher]` (5-min in-process tick) over 2.5h: **RSS 4277→4745MB (+468MB) while
  `py_objects` stayed flat at ~600k** and every top type count (dict/list/tuple/ndarray blocks)
  oscillated with no monotonic growth. Weakref (`ReferenceType`) spikes of +10–50k per tick fully
  reverted — SQLAlchemy identity-map churn, collected.
- `/proc/1/smaps` decomposition: 4631MB anonymous RSS across 454 regions, of which **34 regions of
  ~64MB (1716MB) — the classic glibc per-thread arena signature** — plus 10 regions >68MB (1137MB)
  and 104 regions 10–68MB (1273MB).
- Container has 6 cores → default glibc arena cap = 8×6 = **48 arenas**; no `MALLOC_*` env was set.

## Mechanism (flow-level, hindi binary)

1. rnd_only jobs (prescreen + market scan fire ~30s after boot; viability refreshes, imminent
   scanners, drains, snapshot jobs every seconds–minutes) allocate large **transient**
   pandas/numpy working sets across the worker thread pools (`min(80, max(24, cpu*3))` threads).
2. Each thread gets assigned a glibc arena (up to 48). An arena grows to the high-water mark of the
   largest working set any of its threads ever handled — and **glibc never returns that memory to
   the OS on free** (only the main-heap top trims; mid-size allocations sit in arenas).
3. Different threads run different heavy jobs over hours → each arena's high-water ratchets up →
   RSS climbs monotonically (~190MB/h overnight, faster during market hours), exactly like a leak.
4. Restart resets the high-water → "5.6GB within 1.5h" is the startup jobs re-establishing it.

There is **no unbounded Python cache** driving this: flat object counts across hours rule out a
dict/list/DataFrame accumulation in the job paths (a cache would show monotonic type-count growth
in mem_watcher's `top_delta_since_last`).

## Proof (controlled 2×2 on the same image, 24 threads × variable-size pandas churn, 75s)

| Config | Peak RSS | Idle RSS after all work stopped |
|---|---|---|
| default allocator | 1394MB | 945MB |
| `MALLOC_ARENA_MAX=2` | 1176MB | 498MB |
| default + `malloc_trim(0)` every 5s | 1180MB | 719MB |
| **`MALLOC_ARENA_MAX=2` + `malloc_trim(0)`** | **897MB** | **67MB** |

Combined fix: peak −36%, idle retention −93% (945→67MB, 14×).

## Fix (both default-ON, live)

1. **`scripts/docker-entrypoint-chili.sh`** — `export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"`
   before both exec paths, so every service on the chili-app image (web, scheduler, brain,
   momentum-exec, workers) gets it; env-overridable (the ONE documented setting).
2. **`app/services/diagnostics/mem_watcher.py`** — every mem_watcher tick now calls glibc
   `malloc_trim(0)` (all-arena trim, glibc ≥2.8) after `gc.collect()`, and logs
   `malloc_trim_reclaimed=XMB` in the existing line. Linux-glibc-guarded via ctypes, never raises,
   no-ops on Windows/musl. Opt-out: `CHILI_MEM_WATCHER_MALLOC_TRIM=0`. Scheduler ticks every 5min
   (APScheduler job), web every 60s (lifespan thread) — both share this tick.

## Tests

- `tests/test_mem_watcher_malloc_trim.py` (4 passed on Windows = glibc-absent path): tick never
  raises + still logs `vm_rss=`, trim default-ON, env opt-out parsing, `_run_malloc_trim` no-raise.
- In-container (Debian image, new mem_watcher mounted): libc loads, tick emits
  `malloc_trim_reclaimed=` in the log line, no exceptions.

## Deployment + verification

- New image built from main+fix; `chili-clean-recovery-scheduler` recreated on it with its original
  env/binds/network (+ entrypoint now exports `MALLOC_ARENA_MAX=2`).
- Verified in-container binding (report binding, not defaults): `MALLOC_ARENA_MAX=2` present in
  PID 1 env; first mem_watcher tick logs `malloc_trim_reclaimed=`.
- Expectation for the soak: RSS plateaus in the low single-GB instead of ratcheting ~200–400MB/h;
  the every-few-hours restart mitigation can be retired after a clean 24h curve.

## Follow-ups

- `chili-clean-recovery-brain` (3.3GB at 12h) runs the same image → gets `MALLOC_ARENA_MAX=2` on
  its next recreate; confirm it also runs a mem_watcher tick path if trim is wanted there.
- If daytime RSS still creeps with trim active, the residual is genuinely-held memory — re-run the
  smaps decomposition and hunt caches then (mem_watcher deltas would now show the type).
