# CHILI DB Incident Replay Benchmark

- Schema: chili.trading-db-incident-replay-benchmark.v1
- Generated UTC: 2026-07-03T12:23:43.506841Z
- Status: passed
- Target score: 100
- Checks: 2
- Average score: 100/100
- Required behavior: DB-backed incident replays must preserve queue-pressure audit shedding and split candidate scope lanes without loosening placement audits.
- Safety: focused pytest replay only; no runtime restart, deployment, database migration, broker call, or live-trading action.

| Check | Score | Evidence |
| --- | ---: | --- |
| queue_pressure_nonplacement_audit_passes | 100 | ....                                                                     [100%] \| ============================== warnings summary =============================== \| tests\conftest.py:206 \| D:\dev\chili-home-copilot\tests\conftest.py:206: SAWarning: Cannot correctly sort tables; there are unresolvable cycles between tables "trading_decisions, trading_positions, trading_proposals, trading_trades", which is usually caused by mutually dependent foreign key constraints.  Foreign key constraints involving these tables will not be considered; this warning may raise an error in a future release. \| for table in Base.metadata.sorted_tables \| -- Docs: https://docs.pytest.org/en/stable/how-to/capture-war |
| candidate_selector_scope_lane_passes | 100 | .                                                                        [100%] \| ============================== warnings summary =============================== \| tests\conftest.py:206 \| D:\dev\chili-home-copilot\tests\conftest.py:206: SAWarning: Cannot correctly sort tables; there are unresolvable cycles between tables "trading_decisions, trading_positions, trading_proposals, trading_trades", which is usually caused by mutually dependent foreign key constraints.  Foreign key constraints involving these tables will not be considered; this warning may raise an error in a future release. \| for table in Base.metadata.sorted_tables \| tests/test_auto_trader_safety.py::test_candidate_selector_spl |
