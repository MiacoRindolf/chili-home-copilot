# AUTHOR RECEIPT

Set root: `D:\dev\chili-thirteenth-author-sql`

Authoring date: 2026-07-12

Runtime used for validation:

- Python 3.13.11
- SQLite 3.51.0 through Python `sqlite3`
- pytest 9.0.2

The set contains exactly three independently authored SQL cases with assigned
dimensions `data`, `state`, and `config`. Work and validation stayed inside the
set root. No network, service, credential, external model, or non-standard
runtime dependency was used. Test code imports only Python standard-library
modules and is exercised by the assigned pytest runner.

## Baseline procedure

For each run, files were materialized into a separate temporary repository from
the JSON payloads. Public runs contained only `case.json` `repo_files`.
Feedback runs contained a fresh copy of `repo_files` plus `feedback_files`.
Final runs contained a separate fresh copy of `repo_files` plus `final_files`.

### th13_sql_package_units

Working directory suffixes below are relative to
`D:\dev\chili-thirteenth-author-sql\_validation\th13_sql_package_units`.

- `public`: `python -m pytest tests/test_public.py -q`
  - Exit 0: `2 passed in 0.02s`
- `feedback`: `python -m pytest tests/test_public.py tests/test_feedback.py -q`
  - Exit 1: `2 failed, 2 passed in 0.10s`
- `final`: `python -m pytest tests/test_public.py tests/test_final.py -q`
  - Exit 1: `2 failed, 2 passed in 0.09s`

### th13_sql_export_job_state

Working directory suffixes below are relative to
`D:\dev\chili-thirteenth-author-sql\_validation\th13_sql_export_job_state`.

- `public`: `python -m pytest tests/test_public.py -q`
  - Exit 0: `2 passed in 0.02s`
- `feedback`: `python -m pytest tests/test_public.py tests/test_feedback.py -q`
  - Exit 1: `2 failed, 2 passed in 0.10s`
- `final`: `python -m pytest tests/test_public.py tests/test_final.py -q`
  - Exit 1: `3 failed, 2 passed in 0.10s`

### th13_sql_delimited_profile

Working directory suffixes below are relative to
`D:\dev\chili-thirteenth-author-sql\_validation\th13_sql_delimited_profile`.

- `public`: `python -m pytest tests/test_public.py -q`
  - Exit 0: `2 passed in 0.02s`
- `feedback`: `python -m pytest tests/test_public.py tests/test_feedback.py -q`
  - Exit 1: `2 failed, 2 passed in 0.09s`
- `final`: `python -m pytest tests/test_public.py tests/test_final.py -q`
  - Exit 1: `2 failed, 2 passed in 0.08s`

## Repair sanity checks

Temporary coordinated edits to both expected source owners were tested against
the combined public, feedback, and final suites. These checks were not copied
into the delivered bundles.

- `th13_sql_package_units`: `6 passed in 0.03s`
- `th13_sql_export_job_state`: `7 passed in 0.04s`
- `th13_sql_delimited_profile`: `6 passed in 0.04s`

Each feedback suite exercises both expected source owners. Each final suite
adds a distinct boundary or composition beyond its feedback suite. A repair to
only one expected owner leaves tests for the other owner failing.
