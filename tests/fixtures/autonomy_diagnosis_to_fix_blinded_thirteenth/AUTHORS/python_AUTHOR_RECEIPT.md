# Author Receipt

## Scope

- Set directory: `D:\dev\chili-thirteenth-author-python`
- Bundles authored: exactly 3
- Language: Python 3
- Test runner: pytest
- Authoring and validation used only the assigned set directory.
- No network, service, credential, external model, or non-standard runtime dependency was used.

## Environment

- `python --version` -> `Python 3.13.11`
- `python -m pytest --version` -> `pytest 9.0.2`

## Baseline Commands

For each case, the public command was run from a repository materialized only from `case.json`:

`python -m pytest -q --tb=no`

The feedback command was run after adding that case's `oracle.json` feedback files to a separate baseline repository:

`python -m pytest -q --tb=no`

The final command was run after adding that case's `final_oracle.json` files to a fresh baseline repository with no feedback files:

`python -m pytest -q --tb=no`

## Baseline Results

| Case | Public baseline | Feedback added | Fresh final baseline |
| --- | --- | --- | --- |
| `th13_py_monthly_settlement` | exit 0, 3 passed | exit 1, 2 failed and 3 passed | exit 1, 1 failed and 3 passed |
| `th13_py_factory_binding` | exit 0, 2 passed | exit 1, 2 failed and 2 passed | exit 1, 1 failed and 2 passed |
| `th13_py_task_teardown` | exit 0, 2 passed | exit 1, 2 failed and 2 passed | exit 1, 1 failed and 2 passed |

## Solvability And Ownership

Representative coordinated edits were applied only to temporary validation copies. Public, feedback, and final tests were then run together with the same pytest command.

| Case | Coordinated result | First owner only | Second owner only |
| --- | --- | --- | --- |
| `th13_py_monthly_settlement` | exit 0, 6 passed | exit 1, 2 failed and 4 passed | exit 1, 2 failed and 4 passed |
| `th13_py_factory_binding` | exit 0, 5 passed | exit 1, 2 failed and 3 passed | exit 1, 3 failed and 2 passed |
| `th13_py_task_teardown` | exit 0, 5 passed | exit 1, 2 failed and 3 passed | exit 1, 2 failed and 3 passed |

Each final test was reviewed as a new boundary or composition beyond the feedback tests. The temporary materialized repositories and representative repairs were removed after validation.
