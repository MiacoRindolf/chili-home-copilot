# Runtime Evidence Diagnostic Benchmark

- Run: 2026-07-11T11:59:41.910316+00:00
- Local model: `qwen2.5-coder:7b`
- Reference family: `claude-fable-5`
- Overall score: **61.7/100**
- Verdict: **needs_improvement**
- Premium calls: **0**
- Average wall time: **121.4s/case**
- Fable 5 parity claim: **No**. This is a typed runtime-evidence holdout, not a frontier head-to-head.

| Case | Score | Final dimension | Status | Probes | Retractions |
|---|---:|---|---|---|---:|
| log-dependency-401 | 65 | code | confirmed | log_inventory, log_search, repo_state, git_history | 0 |
| db-queue-402 | 65 | data | confirmed | log_inventory, log_search, db_schema, db_profile | 0 |
| runtime-retraction-403 | 55 | runtime | confirmed | log_inventory, log_search, db_schema, db_profile | 0 |

## Interpretation

Cases begin without access to log or database contents. CHILI must request typed probes, execute bounded log-tail or aggregate-only PostgreSQL reads, ingest their provenance as evidence, and re-evaluate the conclusion. Database fixtures are created only in a `_test` database; probe transactions independently enforce PostgreSQL read-only mode.
