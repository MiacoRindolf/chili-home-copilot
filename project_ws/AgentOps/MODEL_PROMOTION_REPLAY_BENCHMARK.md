# CHILI Model Promotion Replay Benchmark

- Schema: chili.model-promotion-replay-benchmark.v1
- Generated UTC: 2026-07-03T07:18:30.394617Z
- Status: passed
- Target score: 100
- Checks: 3
- Average score: 100/100
- Required behavior: model/tool promotion must be gated by all-up coding score, source stability, and real shadow/tournament/hosted PR evidence.
- Safety: temporary scorecard replay only; no model calls, git action, runtime restart, deployment, database, broker, or live-trading action.

| Check | Score | Evidence |
| --- | ---: | --- |
| promotion_ready_signal_passes | 100 | promotion_status=passed; score=100; scope=full |
| missing_promotion_scorecard_blocks | 100 | Coding benchmark is not promotion-ready: 100/90 across 12 scenario(s), pass rate 12/12. Scorecard contract: missing project_ws/AgentOps/MODEL_PROMOTION_REPLAY_BENCHMARK.md. |
| self_test_frontier_evidence_blocks | 100 | real PR repair inventory |
