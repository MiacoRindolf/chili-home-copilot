# Fable 5 Local Quality Resource Profile Receipt

Date: 2026-08-01
Worktree: `D:\dev\chili-home-copilot-fable5-seventeenth-contestant-558088`
Branch: `codex/fable5-diagnostic-reasoning`
Implementation commit: `c69e6c28` (`feat: allocate quality-first local reasoning`)

## Verdict

CHILI now has a quality-first local reasoning profile sized for this workstation instead of
assuming an 8 GB system-memory ceiling. The workstation has 63.93 GB of physical RAM, a
32-thread Ryzen 9 5950X, and an RTX 2070 with 8 GB of fixed VRAM. Ollama can and does offload
larger models into system RAM.

This change improves the production autonomy path's available context, generation budget,
timeout budget, local fallback behavior, and bounded 14B repair opportunities. It does not
prove Fable 5 parity. A disclosed stress replay with a 14B primary editor regressed under host
pressure, so indiscriminate maximum allocation is explicitly rejected as a quality strategy.

## Production Allocation

The default `CHILI_PROJECT_AUTOPILOT_RESOURCE_PROFILE` is now `quality`. `balanced` remains an
operator-selectable escape hatch.

| Resource | Previous balanced default | Quality default |
|---|---:|---:|
| Plan timeout | 90 seconds | 600 seconds |
| Plan output budget | 700 tokens | 1,200 tokens |
| Plan context | 8,192 tokens | 12,288 tokens |
| Diagnostic timeout | 150 seconds | 600 seconds |
| Diagnostic output budget | 900 tokens | 2,400 tokens |
| Diagnostic context | 8,192 tokens | 16,384 tokens |
| Recovery timeout | 150 seconds | 480 seconds |
| Recovery output budget | 700 tokens | 1,000 tokens |
| Edit timeout | 150 seconds | 600 seconds |
| Edit output budget | 350 tokens | 1,200 tokens |
| Edit context | 4,096 tokens | 12,288 tokens |
| Local 14B repair rounds | 1 | 2 |
| Ollama keep-alive | 15 minutes | 30 minutes |

All values retain environment overrides. No premium model is introduced.

## Reliability And Semantic Guards

- The local reasoner response must contain usable visible JSON. A timeout, empty response, or
  malformed response triggers one bounded retry on the local coder model with thinking off.
- After that fallback, later diagnostic roles and planning stay on the warm coder for the same
  run instead of repeatedly loading a failing reasoner.
- Model-selection receipts include the coder model, resource profile, timeout, context, and
  fallback reason.
- The split candidate-scope contract remains attached after a partial model rewrite. A patch
  that merely removes the mixed-scope OR but omits complete merge, timestamp-normalization,
  ordering, or cap semantics now fails closed.

## Host Allocation

The host WSL/Docker ceiling was also found to be real but separate from Ollama's native Windows
process. `C:\Users\rindo\.wslconfig` was changed from 24 GB RAM, 6 processors, and 8 GB swap to:

```ini
[wsl2]
memory=40GB
processors=20
swap=16GB
```

This leaves roughly 24 GB of physical-memory headroom for Windows and native Ollama. The change
will take effect on the next normal WSL/Docker restart. No Docker, database, broker, scheduler,
or trading service was restarted for this receipt.

## Validation

- Python compilation passed for the touched production and test modules.
- `git diff --check` passed.
- Focused new behavior tests passed: 10/10.
- Broad affected regression suite passed: **519 tests** with pre-existing warnings.
- Pytest emitted a Windows temporary-directory cleanup `PermissionError` after the passing test
  summary; it did not represent a test failure.
- `ruff` was unavailable in the environment and therefore was not run.

## Disclosed Replay Evidence

Both replays used the historical scope-lane fixture with deterministic contracts disabled,
`qwen3:8b` as reasoner, `qwen2.5-coder:14b` as primary editor, 600-second call bounds, and a
3,600-second case model budget. They are development replays, not unseen evidence.

| Replay | Score | Correct diagnosis | Exact owner | Retained patch | Sealed final |
|---|---:|---:|---:|---:|---:|
| Pre-guard generous baseline | 55/100 | yes | yes | yes | 0/1 (4/6 assertions) |
| Post-guard generous stress replay | 25/100 | no | no | no | 0/1 |

The baseline produced a partially correct patch after a 14B plan and edit, but it omitted exact
timestamp and tie-order semantics. The post-guard replay produced no patch: all five local model
calls failed, four by timeout and one by a Windows/Ollama socket-buffer transport error. The GPU
was later observed at 7,700/8,192 MiB and 100 percent utilization while the 14B model was loaded
with 41 percent CPU / 59 percent GPU placement.

Because no patch was produced after the guard change, the semantic guard was not exercised by
that replay. Its behavior is established by tests, not by a functional score improvement.

## Honest Conclusion

The workstation was under-allocated for Docker workloads, and CHILI's production reasoning
defaults were too conservative for available system RAM. Both are corrected. However, 64 GB of
RAM does not turn an 8 GB VRAM card into a fast 14B inference device, and longer deadlines alone
do not guarantee stronger reasoning. CHILI remains premium-independent and more generously
provisioned, but unseen complex-task parity with Fable 5 remains unproven.
