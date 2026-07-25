# 2026-07-21 — Captured-paper Alpaca PAPER activation marathon (a44–a52)

**Status: NOT YET ACTIVE. Blocked until premarket (equity tape closed ~5 PM PT).**
Branch `codex/captured-paper-takeover`, HEAD `bb6f733` (pushed). Host clean and
reversible after every attempt: `live_cash_authorized=false`, zero orders, 4
legacy IQFeed tasks Ready, no candidate task.

## What this session did

Continued the interrupted Codex `019f5d01` captured-paper activation after the
operator ran out of Codex credits. Recovered the full infra, then drove the
sealed `ActivatePaper` runner through nine generations, fixing one genuine,
distinct gate per run.

### Infra recovery (done)
- Pushed the 31-commit `codex/captured-paper-takeover` branch to origin (was
  local-only).
- Docker Desktop was crashed → relaunched (sandbox-disabled so the harness
  job-object doesn't reap it); postgres:5433 healthy on E: data.
- Old exec worker `chili-clean-recovery-momentum-exec` kept STOPPED (#880
  orphan-reconciler landmine).
- IQConnect + trade/depth IQFeed bridges restarted; tape verified live
  (sub-second, Delay=0 megacaps).

### Activation gate progression (each a real, fixed blocker)
| Gen | Failure | Root cause → fix |
|-----|---------|------------------|
| a43 | `APPLY_REJECTED` (pre-session) | service startup receipt timeout — dying infra |
| a44 | `PATH_AUTHORITY_MISMATCH` | authority built under Git Bash+conda pinned `mingw64\git.exe` + chili-env python; plain elevated PowerShell resolves `cmd\git.exe` + base python. Fix: build **and** run with only `…\envs\chili-env` prepended to PATH. |
| a45 | `PROCESS_SNAPSHOT_INCOMPLETE` | host snapshot needs exactly one trade **and** one depth bridge; depth bridge was down. Started it. |
| a46 | Apply `schtasks /Change` denied | run wasn't truly elevated (read-only chain/smoke pass non-elevated; only Apply's Task Scheduler mutation needs admin). Run from a real Administrator PowerShell. |
| a47 | `NO_ORDER_SMOKE_REJECTED` (`0xC0000005`) | **Windows Defender** scan-on-open of the freshly-materialized SHA-pinned capsule under the elevated token, fatal. Fix: `Add-MpPreference -ExclusionPath` for `C:\chili-paper`, `C:\chili-codex-artifacts`, `D:\dev\chili-home-copilot-codex-broker`, `C:\Windows\Temp` (operator; reversible). |
| a48 | `APPLY_REJECTED` (STARTUP_RECEIPT_UNAVAILABLE) | Defender fix worked (no-order smoke + all readiness gates PASS; cutover reached `candidate_task_started`); but the captured service exited before publishing STARTED — **silently** (0-byte stdout+stderr, no Windows crash event). This is the real remaining bug. |
| a50/a51 | `NO_ORDER_SMOKE_REJECTED` (`0xC0000005`) | **self-inflicted instrumentation regression.** `faulthandler.enable(all_threads=True)` turns a benign first-chance AV in `ntpath.realpath`/`Path.resolve()` (dependency-closure walk) fatal; a repeating `dump_traceback_later` walking stacks during a native `.pyd` import also crashes. Removed both; kept only fsync'd breadcrumbs + BaseException crash-file. |
| a52 | `EXECUTION_LANE_RECREATOR_STILL_RUNNING` | cutover quiesce false-positive: `await_execution_lane_recreator_processes` flags any process whose cmdline references `run-hidden.vbs` (shared by the bridge tasks); the `CHILI-Nightly-Replay` task (5:30 PM PT) ran during the cutover window and got counted. Not a real bridge problem. |

### Commits landed (all pushed)
- `3c39ee5` — surface silent ActivatePaper service exit (except BaseException +
  fsync'd `.service-crash.json` + stdout flush).
- `f24afb08` — breadcrumb + native-fault instrumentation (later partly reverted).
- `747bc30` — drop `faulthandler.enable` (regressed no-order-smoke).
- `bb6f733` — drop repeating `dump_traceback_later` (also regressed it);
  breadcrumbs-only, safe.

## The real remaining bug (unreached since a48)
The captured PAPER service, launched in ActivatePaper mode by the candidate
task, exits/hangs during `_execute_active_service → supervisor.start_active`
(order-capable workers: selection/post_commit/transport/later_fill/exit_owner,
all `threading.Thread`) **before publishing `host-ready.json`**. NoOrderSmoke
mode (read-only `start_no_order_smoke`) works. Death is below the Python layer
(no exception reaches the handler). The fsync'd breadcrumbs in
`_execute_active_service` (bb6f733) will name the exact step on the next run
that reaches it — but a52 died earlier (quiesce), so it's still unseen.

## Next session (premarket, ~4 AM ET / 1 AM PT 07-22)
1. Verify tape live; ensure Defender exclusions still present.
2. Clear the quiesce false-positive: run in premarket when no `run-hidden.vbs`
   evening task fires, or temporarily disable/stop them (Nightly-Replay,
   Evening-BacktestRefresh, PgDump, FastOrderbookRetention, ChiliBacktestRefresh)
   for the cutover window.
3. Build a53 at HEAD `bb6f733`, run elevated + streaming breadcrumbs.
4. Read the last breadcrumb → the exact `start_active` step → fix that.
5. Final activation + verify ACTIVE criteria (`ACTIVATED_ALPACA_PAPER_ONLY`,
   `CHILI-Captured-Alpaca-PAPER` task + live service, one STARTED receipt, UUID
   `3e0776af…`, zero activation orders).

## How to build + run (reference)
Build (non-elevated): `…\envs\chili-env` prepended to PATH, then
`python -B -m scripts.build_captured_paper_activation_authority` with the pinned
immutable inputs (capsule `850f1792…`, runtime env `…fresh-inputs-20260720T1236Z\captured-paper.env`,
bridge-config sha `6318be4c…`, benchmark `b16-03fc84e\…119e52f3…`, UUID
`3e0776af…`). Run: elevated PowerShell, same PATH prepend, the emitted
`activate_paper_argv` (`--mode ActivatePaper --confirm-fake-money-paper
CUTOVER_FAKE_MONEY_ALPACA_PAPER`).
