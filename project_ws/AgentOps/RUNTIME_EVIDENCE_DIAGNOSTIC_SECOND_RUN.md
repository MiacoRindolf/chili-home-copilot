# Runtime Evidence Diagnostic Benchmark

- Run: 2026-07-11T12:07:59.146976+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **88.3/100**
- Verdict: **needs_improvement**
- Premium calls: **0**
- Average wall time: **101.0s/case**
- Fable 5 parity claim: **No**. This is a typed runtime-evidence holdout, not a frontier head-to-head.

| Case | Score | Final dimension | Status | Probes | Retractions |
|---|---:|---|---|---|---:|
| log-dependency-401 | 65 | clock | confirmed | log_inventory, log_search, repo_state, git_history | 1 |
| db-queue-402 | 100 | state | confirmed | log_inventory, log_search, db_schema, db_profile | 0 |
| runtime-retraction-403 | 100 | state | confirmed | log_inventory, log_search, db_schema, db_profile | 1 |

## Interpretation

Cases begin without access to log or database contents. CHILI must request typed probes, execute bounded log-tail or aggregate-only PostgreSQL reads, ingest their provenance as evidence, and re-evaluate the conclusion. Database fixtures are created only in a `_test` database; probe transactions independently enforce PostgreSQL read-only mode.
