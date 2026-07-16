# CHILI Offline Project Autonomy Benchmark

- Schema: chili.offline-project-autonomy-benchmark.v1
- Generated UTC: 2026-07-10T23:24:41.992907Z
- Status: passed
- Target score: 100
- Checks: 2
- Average score: 100/100
- Required behavior: Project Autopilot must plan, patch, preserve behavior tests, validate, and review with premium credentials absent and every premium model route made fatal.
- Safety: isolated temporary repository and in-memory database only; local Ollama inference is allowed; no premium model, real source edit, git publication, deployment, broker, or live-trading action

| Check | Score | Evidence |
| --- | ---: | --- |
| local_dependency_policy | 100 | premium_models_required=false; internet_required=false; frontier_default=false; external models benchmark/opt-in only |
| offline_local_plan_edit_test_review | 100 | model=qwen2.5-coder:7b; duration=40.053s; plan_files=app/service.py; changed_files=app/service.py; premium_calls=0; semantic_review=True; tests=...                                                                      [100%] \| 3 passed in 0.02s |
