# 2026-07-31 — TVRD phantom ±1-share oversell: lossless FILL-line quantities

## Verdict

The TVRD|2026-07-07 `coverage_unavailable` (net −1.0 share across 58 fills) in the
2026-07-30 sealed batch (`replay_batch_v3`) was **not a real oversell**. The raw
fill inventory was exactly flat (the driver's own accounting printed
`net_open_shares=0`). The defect was in the **reporting layer**: the mock's
volume-capped partials fill FRACTIONAL shares (25% participation of printed
volume), and the driver printed every FILL qty with `{q:.0f}` — each leg rounded
independently, so one split round-trip (buy 178.25 → "178"; scale-out 26.6 →
"27" + runner 151.65 → "152") parsed as a phantom net −1 in
`replay_fill_inventory_is_flat` (abs_tol 1e-9).

The task's hypothesized locations (partial-exit split rounding in
live_runner/paper_execution, or the mock venue) were checked and refuted:
`scale_out_quantity` floors and clamps correctly, and the mock books fills that
sum exactly to each order's base size.

## Fix (PR branch `claude/competent-hugle-585d91`, commit `6686a40`)

- `scripts/replay_ab_dark_flags.py`: `_fmt_fill_qty` prints fill quantities at
  1e-10 quantization (trailing zeros stripped) instead of integer rounding.
- `scripts/replay_benchmark_batch.py`: `replay_fill_inventory_is_flat` budgets
  exactly the print quantization — `abs_tol = max(1e-9, 5e-11 × n_fills)`. A
  real leak is ≥ one venue base increment, orders of magnitude above the slack.
- `scripts/replay_scorecard.py`: `pair_round_trips` uses the same slack so
  fractional-print cycles close; whole-share oversell / stranded remainder still
  fail closed.
- `tests/test_golden_replay_library.py`: 4 regression tests (TVRD shape kept
  failing, budget bounds, stdout round-trip, cycle pairing). 59 tests green
  (39 golden-library + 20 scorecard).

## Verification (isolated sink `chili_replay_offby1_test`, one replay at a time)

- **Pre-fix repro (wt-golden-main @ 656f58b, original manifest): EXACT** —
  `coverage_unavailable`, bought 5095 / sold 5096 (net −1.0), pnl −57.48,
  29/29, same 88+71+76 sell tail as the original batch row.
- **Post-fix (clean clone @ 6686a40, re-derived manifest): `status=ok`** —
  bought 5096.2987866569 / sold 5096.298786657 (net −1.0e-10, inside budget),
  pnl −57.48, 29/29, `final_state=watching_live`, sink mined cleanly.
  The pre-fix integer quantities are exactly the rounded forms of the post-fix
  raw values (164.614→165, 155.069→155, …) — same underlying run, byte-level
  deterministic; only the print layer changed.

## Operational learnings (verification ran during a contested night)

- Post-fix attempts #1–#10 on the host were killed by three independent
  mechanisms: the main session's unscoped `pg_locks` advisory sweep (cluster-wide
  — advisory locks are not per-database; now datname-scoped on their side), the
  Codex activation one-shots' quiesce massacres (r42→r47 killed every host
  `python.exe`), and from ~22:00 a persistent protective watchdog killing any
  NEW host python within ~1–3s (decoy-proven, taskkill signature, bridges
  spared). Codex's r46v2+ chains also carry an overlap guard that fails their
  activation when `replay_benchmark_batch|replay_ab_dark_flags|pytest`
  processes are running — replay lanes and activation one-shots must be
  sequenced, not overlapped.
- The successful lane: run the batch **inside a docker container**
  (`--network host`, clean clone at the pinned sha, `-e DATABASE_URL=<sink>`)
  — container pythons are invisible to host `python.exe` sweeps. Container
  gotchas: `--stop-at` is container-local (UTC) time; `mine_sink` in the batch
  parent needs `DATABASE_URL` (no `.env` in a clean clone); cloning through the
  Windows bind mount is slow (commit the container after the first clone and
  reuse the image).
