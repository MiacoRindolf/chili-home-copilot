# CHILI Macro Long-Horizon Tournament

- Schema: chili.macro-long-horizon-tournament.v1
- Generated UTC: 2026-07-11T04:31:38.729213Z
- Status: passed
- Evidence mode: real_artifacts
- Run id: macro-chili-codex56-fable5-20260711
- Tasks: 3
- Phases per task: 3
- Scoring revision: phase-change-evidence-v2
- Regraded from raw artifacts: true
- Source kinds: local_model, codex, claude
- Winner counts: local_model=3, codex=0, claude=0, none=0
- Collection failures: 0
- Runtime measurements: measured=9, unmeasured=0
- Premium-independent local results: 3/3
- Winner rule: cumulative correctness and safety first; on an exact quality tie, zero-premium operational independence; then measured runtime.
- Safety: isolated temporary repositories only; Fable 5 and Codex 5.6 Sol are benchmark opponents; premium routes are fatal inside CHILI; no real source publication, deployment, runtime, broker, or live-trading action

| Task | Winner | Model | Local | Codex | Fable 5 | Evidence |
| --- | --- | --- | ---: | ---: | ---: | --- |
| progressive-rollout | local_model | qwen2.5-coder:7b | 100 | 100 | 100 | local_model:phases=3/3,quality=100,premium_calls=0,seconds=0.646; codex:phases=3/3,quality=100,premium_calls=None,seconds=127.72; claude:phases=3/3,quality=100,premium_calls=None,seconds=1533.882 |
| resumable-import | local_model | qwen2.5-coder:7b | 100 | 20 | 100 | local_model:phases=3/3,quality=100,premium_calls=0,seconds=0.543; codex:phases=0/3,quality=20,premium_calls=None,seconds=106.015; claude:phases=3/3,quality=100,premium_calls=None,seconds=1783.747 |
| dependency-deployment | local_model | qwen2.5-coder:7b | 100 | 100 | 100 | local_model:phases=3/3,quality=100,premium_calls=0,seconds=0.569; codex:phases=3/3,quality=100,premium_calls=None,seconds=142.944; claude:phases=3/3,quality=100,premium_calls=None,seconds=1786.405 |
