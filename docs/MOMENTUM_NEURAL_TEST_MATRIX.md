# Momentum neural — test matrix (Phases 1–12)

Use this to pick **pytest slices**. **Postgres** is required for most rows below (`TEST_DATABASE_URL`).

| Phase | Focus | Suggested tests |
|-------|--------|------------------|
| 1 | Neural mesh + momentum tick / graph seed | `tests/test_momentum_neural.py`, `tests/test_brain_neural_mesh.py` |
| 2 | Persistence (variants, viability, sessions) | `tests/test_momentum_neural_persistence.py` |
| 3 | Coinbase adapter / readiness (with mocks) | Covered inside operator/viable paths; adapter unit tests in repo as applicable |
| 4 | Operator API (viable, refresh, paper draft, live arm) | `tests/test_momentum_operator_api.py` |
| 5 | Automation monitor APIs | `tests/test_momentum_automation_api.py` |
| 6 | Risk policy / evaluator | `tests/test_momentum_risk_phase6.py` |
| 7 | Paper runner FSM | `tests/test_momentum_paper_runner.py` |
| 8 | Live runner FSM | `tests/test_momentum_live_runner.py` |
| 9 | Outcomes + feedback | `tests/test_momentum_feedback_phase9.py` |
| 10 | Brain desk / projection | `tests/test_brain_momentum_desk_phase10.py` |
| 11 | Execution-family registry | `tests/test_execution_family_registry_phase11.py` |
| 12 | Settings closeout (no DB) | `tests/test_momentum_neural_settings_closeout.py` |

## Recommended commands

**Fast unit-only:**

```powershell
python -m pytest tests/test_execution_family_registry_phase11.py tests/test_momentum_neural_settings_closeout.py -v --tb=short
```

**Full momentum slice (DB):**

```powershell
python -m pytest tests/test_momentum_neural.py tests/test_momentum_neural_persistence.py tests/test_momentum_operator_api.py tests/test_momentum_automation_api.py tests/test_momentum_risk_phase6.py tests/test_momentum_paper_runner.py tests/test_momentum_live_runner.py tests/test_momentum_feedback_phase9.py tests/test_brain_momentum_desk_phase10.py tests/test_execution_family_registry_phase11.py tests/test_momentum_neural_settings_closeout.py -v --tb=short
```

## Notes

- If Postgres is unreachable, DB-marked tests **error at setup** — start Docker Postgres or point `TEST_DATABASE_URL` at a test instance.
- Prefer **targeted** runs during development; full trading suite may include unrelated tests.
