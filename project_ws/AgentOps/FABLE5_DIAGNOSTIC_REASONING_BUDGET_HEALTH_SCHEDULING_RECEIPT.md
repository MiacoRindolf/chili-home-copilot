# Fable 5 Diagnostic Reasoning — Budget/Health-Aware Model Scheduling Receipt

Date: 2026-07-19
Worktree: `D:\dev\chili-home-copilot-fable5-seventeenth-contestant-558088`
Branch: `codex/fable5-diagnostic-reasoning`
Base commit at start: `9eed3fd0` (feat: freeze authenticated Fable 5 comparison pack)

## 1. Summary

This session hardened the **novel (contract-disabled) reasoning path** of the
diagnosis-to-fix runner so it reliably **completes** on a VRAM-constrained host, instead of
collapsing to an all-timeout no-op. Two generic, adaptive improvements were implemented,
unit-tested, and committed. The full focused suite remains green (**388 passed**), and
production autonomy still makes **zero premium calls**.

The work also isolated the true present bottleneck with fresh replay evidence: on the
measurement host the dual-model configuration (`qwen3:8b` reasoner + `qwen2.5-coder:7b`
editor) is unreliable, and the residual score is bounded by stochastic local 7B editor
synthesis. **No Fable 5 parity or sealed-final solve is claimed.** Current evidence is
**disclosed development replay** only.

## 2. Exact files and commits changed

- `542d9535` — feat: schedule repair planning within the local model budget
- `0f9e8d67` — feat: fall back to the warm editor model when the reasoner times out

Files (both commits):
- `scripts/autopilot_diagnosis_to_fix_benchmark.py`
- `tests/test_diagnosis_to_fix_benchmark.py`

Pushed to `origin/codex/fable5-diagnostic-reasoning` (`9eed3fd0..0f9e8d67`).

No other file was touched. No runtime service, database, broker, Docker container, live
trading state, deployment, or credential was touched. All edits are confined to the
isolated worktree; the dirty runtime checkout at `D:\dev\chili-home-copilot` was not
modified.

## 3. Root causes found in the reasoning pipeline

1. **Wasted repair budget on doomed reasoner re-plans (fixed by `542d9535`).** The per-case
   model budget is shared across diagnosis, the initial patch, and every repair round. A slow
   local reasoner drains most of the budget before repair runs, so the repair re-plan clamps
   to the remainder and dies on a timeout without ever reaching the editor. This pattern is
   recorded on every historical Fable-5 trading pilot (each has 1–3 case-budget timeouts).

2. **No fallback when the reasoner times out on a VRAM-constrained host (fixed by `0f9e8d67`).**
   The measurement GPU (RTX 2070, **8 GB VRAM**) cannot hold `qwen3:8b` (6.2 GB) and
   `qwen2.5-coder:7b` (~5 GB) at once, so every diagnosis↔edit switch forces an ~18 s cold
   model reload. `qwen3:8b` generates at ~30 tok/s, and with thinking the investigator lands at
   ~135 s — exactly the clamped `180 − 45 = 135 s` primary timeout. Any load or cold-load tips
   it over. Without a fallback, once the reasoner starts timing out, **every** reasoner call
   times out and no plan/edit/repair stage runs at all (a hard collapse to the score floor).

3. **Score is coupled to a validated editor patch (observed, not fixed).** The retained
   diagnosis dimension is upgraded to the proven family only when a repair passes feedback;
   otherwise it stays the deterministic boundary-ownership fallback (`state`). So the whole
   score (25 vs 55) hinges on whether the stochastic 7B editor produces a validated patch in
   that run.

4. **Sealed-only requirements exceed 7B synthesis (observed, open).** For the scope-lane case
   the best run retained the correct owner and passed all repair-feedback tests but still failed
   2/6 sealed-final tests (per-lane capacity, id-first merge), which are visible only in the
   prompt/invariant, not in feedback. This is a local-model synthesis ceiling.

## 4. What the improvements do (generic, adaptive, no gate weakened)

- `_observed_model_latency_sec` — conservative (max) observed successful wall time per model,
  read from the call ledger (no fixed magic latency).
- `_model_timed_out` — the reasoning model has a recorded timeout / budget-exhaustion failure
  (slow-but-successful calls do not disqualify it).
- `_effective_reasoning_model` — keep a distinct reasoner while it is healthy and affordable;
  otherwise route the plan to the warm editor model. Applied to the initial plan.
- `_budget_aware_repair_plan_schedule` — for the repair re-plan: prefer the reasoner when it
  fits with an edit reserve; otherwise route to the warm/faster editor model; if neither a plan
  nor an edit fits, **stop deterministically** rather than clamping a doomed call.

All four are no-ops when the reasoner is healthy and budget is ample, so the common/warm path
is unchanged. Only genuine timeouts and observed budget pressure trigger a fallback.

## 5. Before/after benchmark results (contract-disabled disclosed replays)

Config: `--disable-deterministic-contracts --evaluation-context disclosed_replay`,
`--timeout 180 --case-model-time-budget 480`. Score weights: baseline 5, diagnosis 15,
file 10, patch 5, public 10, final 45, premium 10.

| Run | Config | Reasoner (qwen3) | Pipeline completed | Score | Diag | Files | Sealed-final |
|---|---|---|---|---|---|---|---|
| scope-lane baseline `9eed3fd0` (warm) | dual | healthy 132 s | partial (repair re-plan timed out) | 40 | ✓ | ✗ | 0/1 |
| scope-lane isolated rerun `9eed3fd0` (cold) | dual | **all timed out** | **NO — 0 stages ran** | 25 | ✗ | ✗ | 0/1 |
| scope-lane + ch1+2 (run 1) | dual | all timed out | **YES** (plan→editor, edit, repair) | 25 | ✗ | ✗ | 0/1 |
| scope-lane + ch1+2 (run 2) | dual | all timed out | **YES** | 25 | ✗ | ✗ | 0/1 |
| queue-priority + ch1+2 | dual | all 4 timed out | **YES** | 40 | ✓ 100% | ✗ | 0/1 |
| scope-lane single-model | `qwen2.5-coder:7b` | n/a (swap-free) | **YES** | **55** | ✓ | ✓ `auto_trader.py` | 0/1 (4/6 final) |

Interpretation:
- **Reliability improvement is deterministic and reproduced across two mechanism families:**
  under a reasoner timeout, the un-changed pipeline completes zero stages (score 25, hard
  collapse); with Change 1+2 the plan/edit/repair stages reliably run on the warm editor model
  with deterministic budget stops. Verified on scope-lane (×2) and queue-priority (×1); the
  call ledgers show `plan` routed to `qwen2.5-coder:7b` with `reasoning_model_fallback=reasoner_timed_out`.
- **Score improvement is not reproducible.** It is dominated by stochastic 7B editor synthesis
  (observed 25–55) and by whether a validated patch emerges. The single-model run reached 55
  with a retained correct-owner patch and all feedback green, but this did not reproduce.
- **Sealed-final remains 0/1** in every run. No novel-reasoning repair survived fresh sealed
  final adjudication.

## 6. Tests run and exact pass counts

- Focused suite (`tests/test_project_autonomy_diagnostic_reasoning.py`,
  `tests/test_diagnosis_to_fix_benchmark.py`, `tests/test_fable5_diagnostic_headtohead.py`,
  `tests/test_frontier_model_identity_attestation.py`,
  `tests/test_realworld_diagnostic_benchmark.py`): **388 passed, 1 warning** post-change
  (375 baseline + 13 new unit tests). The one warning is the pre-existing SAWarning about
  mutually dependent trading FK cycles.
- 13 new unit tests cover `_observed_model_latency_sec`, `_model_timed_out`,
  `_effective_reasoning_model`, `_budget_aware_repair_plan_schedule` (reasoner / editor-fallback
  / budget-stop / reasoner-timed-out routes), and the `_generate_patch` and `_repair_after_failure`
  routing/stop paths.

## 7. Premium calls made

**Zero.** All model calls in this session were local (`qwen3:8b`, `qwen2.5-coder:7b`). The
authenticated Fable 5 collector was **not** run (it remains pending explicit user approval).

## 8. Remaining blockers to Fable 5 parity

1. **Local 7B editor synthesis quality/variance** is the dominant residual bottleneck.
   Contract-disabled sealed-final success is still 0/1; the score swings 25–55 run to run.
2. **Latency/hardware.** On 8 GB VRAM the dual-model configuration thrashes (18 s cold-loads +
   ~30 tok/s reasoner). The reliable path on this host is swap-free single-model, but that
   trades away the reasoner's depth. Prompt compression for the diagnosis stage (to fit the
   investigator within the per-call timeout) is an unaddressed lever.
3. **No authenticated same-task Fable 5 head-to-head** exists (collector unapproved).
4. Parity still requires ≥30 independently authored blinded cases, broad language/repository
   coverage, ≥95% sealed-final success, and blind human adjudication — none met.

## 9. Evidence classification

All new numbers here are **disclosed development replay** evidence on already-disclosed
fixtures with recognized-contract repair disabled. They are **not** an untouched holdout and
**not** an authenticated same-task Fable 5 comparison. They do not change the latest fully
interpretable unseen composite (the fifteenth suite, 41.88/100). The accurate current
statement remains:

> CHILI is a premium-independent, evidence-gated autonomous coding system with strong bounded
> shadow results. It is not a Fable 5 wrapper, and broad superiority remains an open empirical
> claim.
