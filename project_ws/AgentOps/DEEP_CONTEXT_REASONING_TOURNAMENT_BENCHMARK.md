# CHILI Deep-Context Reasoning Tournament

- Schema: chili.deep-context-reasoning-tournament.v1
- Generated UTC: 2026-07-11T05:09:10.497317Z
- Status: passed
- Evidence mode: real_artifacts
- Run id: deep-context-chili-codex56-fable5-20260711
- Tasks: 3
- Context files per task: 24
- Distractor files per task: 20
- Source kinds: local_model, codex, claude
- Winner counts: local_model=3, codex=0, claude=0, none=0
- Collection failures: 0
- Runtime measurements: measured=9, unmeasured=0
- Premium-independent local results: 3/3
- Winner rule: behavior and minimal-scope quality first; on an exact quality tie, zero-premium operational independence; then measured runtime.
- Safety: isolated temporary repositories only; exact Fable 5 and Codex 5.6 Sol are benchmark opponents; CHILI premium routes are fatal; no real source publication, deployment, runtime, broker, or live-trading action

| Task | Winner | Model | Local | Codex | Fable 5 | Evidence |
| --- | --- | --- | ---: | ---: | ---: | --- |
| tenant-authorization-trace | local_model | qwen2.5-coder:7b | 100 | 100 | 100 | local_model:behavior=True,scope=True,premium_calls=0,seconds=0.179; codex:behavior=True,scope=True,premium_calls=None,seconds=51.918; claude:behavior=True,scope=True,premium_calls=None,seconds=547.788 |
| revision-cache-trace | local_model | qwen2.5-coder:7b | 100 | 100 | 100 | local_model:behavior=True,scope=True,premium_calls=0,seconds=0.182; codex:behavior=True,scope=True,premium_calls=None,seconds=112.942; claude:behavior=True,scope=True,premium_calls=None,seconds=339.642 |
| decimal-billing-trace | local_model | qwen2.5-coder:7b | 100 | 100 | 100 | local_model:behavior=True,scope=True,premium_calls=0,seconds=0.187; codex:behavior=True,scope=True,premium_calls=None,seconds=36.958; claude:behavior=True,scope=True,premium_calls=None,seconds=603.397 |
