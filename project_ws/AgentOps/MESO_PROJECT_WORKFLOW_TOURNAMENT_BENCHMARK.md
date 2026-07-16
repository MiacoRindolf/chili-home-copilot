# CHILI Meso Project Workflow Tournament

- Schema: chili.meso-project-workflow-tournament.v1
- Generated UTC: 2026-07-11T02:19:17.352514Z
- Status: passed
- Evidence mode: real_artifacts
- Run id: meso-chili-codex56-fable5-budget10-20260711
- Tasks: 3
- Source kinds: local_model, codex, claude
- Winner counts: local_model=3, codex=0, claude=0, none=0
- Collection failures: 0
- Runtime measurements: measured=9, unmeasured=0
- Premium-independent local results: 3/3
- Winner rule: correctness and safety quality first; when quality is equal, zero-premium operational independence; then measured runtime.
- Safety: isolated temporary repositories only; premium frontier models are benchmark opponents; CHILI premium routes are fatal; no real source edit, git publication, deployment, database migration, broker, or live-trading action

| Task | Winner | Model | Local | Codex | Fable 5 | Evidence |
| --- | --- | --- | ---: | ---: | ---: | --- |
| one-based-page-envelope | local_model | qwen2.5-coder:7b | 100 | 100 | 100 | local_model:behavior=True,scope=True,premium_calls=0,seconds=0.277; codex:behavior=True,scope=True,premium_calls=None,seconds=15.171; claude:behavior=True,scope=True,premium_calls=None,seconds=263.558 |
| bounded-retry-contract | local_model | qwen2.5-coder:7b | 100 | 100 | 100 | local_model:behavior=True,scope=True,premium_calls=0,seconds=0.167; codex:behavior=True,scope=True,premium_calls=None,seconds=17.135; claude:behavior=True,scope=True,premium_calls=None,seconds=428.974 |
| idempotent-ledger-event | local_model | qwen2.5-coder:7b | 100 | 100 | 100 | local_model:behavior=True,scope=True,premium_calls=0,seconds=0.198; codex:behavior=True,scope=True,premium_calls=None,seconds=20.606; claude:behavior=True,scope=True,premium_calls=None,seconds=185.732 |
