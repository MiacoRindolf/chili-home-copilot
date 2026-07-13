# Python Validation Lane Report

## Verdict

**PASS**

All assigned gates were actually verified for exactly these three cases:

- `th13_py_factory_binding` (`dependency`)
- `th13_py_monthly_settlement` (`clock`)
- `th13_py_task_teardown` (`runtime`)

The author root was read-only throughout. Its before/after sorted inventory SHA-256 is identical:

`56a265fe9935c5e71fadace1e3307fa439920c0e70cb40346f4a62bf724a8f45`

## Commands And Runtime

- `python --version` -> exit `0`; `Python 3.13.11`
- `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest --version` -> exit `0`; `pytest 9.0.2`
- `python .\tmp\validate_lane.py` from `D:\dev\chili-thirteenth-validation-lane-python` -> exit `0`; `cases=3 checks=62 failed=0`, `result=PASS`

Every pytest run used a fresh materialization below the output root, a 20-second timeout, disabled plugin autoload, disabled bytecode/cache output, and this exact command form, where `R` is the absolute run directory recorded below:

`C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=no -p no:cacheprovider --rootdir R -c R\pytest.ini`

The fully expanded command, working directory, timeout, exit code, concise output, and full-output SHA-256 for every run are recorded under each case's `runs` array in `lane_result.json`.

## Execution Matrix

| Case | Fresh run directory suffix | Exit | Concise output |
| --- | --- | ---: | --- |
| factory binding | `public_baseline` | 0 | 2 passed |
| factory binding | `feedback_baseline` | 1 | 2 failed, 2 passed |
| factory binding | `final_baseline` | 1 | 1 failed, 2 passed |
| factory binding | `repaired_all` | 0 | 5 passed |
| factory binding | `ablation_revert_dependency_plan_py` | 1 | 3 failed, 2 passed |
| factory binding | `ablation_revert_service_container_py` | 1 | 2 failed, 3 passed |
| monthly settlement | `public_baseline` | 0 | 3 passed |
| monthly settlement | `feedback_baseline` | 1 | 2 failed, 3 passed |
| monthly settlement | `final_baseline` | 1 | 1 failed, 3 passed |
| monthly settlement | `repaired_all` | 0 | 6 passed |
| monthly settlement | `ablation_revert_billing_clock_py` | 1 | 2 failed, 4 passed |
| monthly settlement | `ablation_revert_settlement_runner_py` | 1 | 2 failed, 4 passed |
| task teardown | `public_baseline` | 0 | 2 passed |
| task teardown | `feedback_baseline` | 1 | 2 failed, 2 passed |
| task teardown | `final_baseline` | 1 | 1 failed, 2 passed |
| task teardown | `repaired_all` | 0 | 5 passed |
| task teardown | `ablation_revert_teardown_stack_py` | 1 | 2 failed, 3 passed |
| task teardown | `ablation_revert_task_runtime_py` | 1 | 2 failed, 3 passed |

The failing baseline and ablation outputs named the required feedback/final test functions; there were no collection, import, timeout, or infrastructure failures.

## Case Findings

### `th13_py_factory_binding`

- Strict JSON parsing and identity/path/test-map consistency: pass.
- Language/runner: `python` / `pytest`.
- Candidates and expected owners: `dependency_plan.py`, `service_container.py`; 2 plausible sources, 2 expected owners, `max_files=2`.
- Repair mechanism: discover required keyword-only parameters and invoke factories with separate positional and keyword bindings.
- Owner necessity: reverting either owner from the passing repair caused required feedback/final failures.
- Final novelty: feedback isolates keyword-only discovery and invocation; final composes positional-only, keyword-only, and mixed signatures across a nested provider graph.
- Baseline -> temporary repaired source SHA-256:
  - `dependency_plan.py`: `c318b9c74a0ed11c4482929031675d7abbe60ff2057341c85030018604b2756d` -> `1c050b5f8147a0a7bf211215c7efa4aa9de1b8572eb8cc17258c32ddad971d17`
  - `service_container.py`: `75d81aad6ca081b492541484a9b0d56a9c4c3db75f6de021e4662ca577a72f90` -> `b73aeb9bc93f26cb0d6295dd0f7a8599d23c014faed22790b3693f0845cb51ad`

### `th13_py_monthly_settlement`

- Strict JSON parsing and identity/path/test-map consistency: pass.
- Language/runner: `python` / `pytest`.
- Candidates and expected owners: `billing_clock.py`, `settlement_runner.py`; 2 plausible sources, 2 expected owners, `max_files=2`.
- Repair mechanism: clamp billing days to the real month end and convert the completion instant with `astimezone` before scheduling.
- Owner necessity: reverting either owner from the passing repair caused required feedback/final failures.
- Final novelty: feedback separately covers non-leap February clamping and timezone conversion; final composes UTC month rollback, leap day, month-end clamping, and a same-local-day future cutoff.
- Baseline -> temporary repaired source SHA-256:
  - `billing_clock.py`: `5a1e5d88f13fdae79115deb0a930d882d23897f67dc3ba0f80a70e9f87e888b0` -> `c0b0abe35ed7985992e7aa53d276327f23337e2eb91f05eeb26905db052cd68b`
  - `settlement_runner.py`: `7d929ee8cf9756d5f6cecb36a6eb7be470cc9a8e59a3b9e04908415d2ce0e2ba` -> `89e35af8f329abc7fc644b7bdc3041fe418da2a87116fa711f0f4a593254df7a`

### `th13_py_task_teardown`

- Strict JSON parsing and identity/path/test-map consistency: pass.
- Language/runner: `python` / `pytest`.
- Candidates and expected owners: `teardown_stack.py`, `task_runtime.py`; 2 plausible sources, 2 expected owners, `max_files=2`.
- Repair mechanism: continue all callbacks after `BaseException`, then preserve a task's original process-level termination over cleanup failure.
- Owner necessity: reverting either owner from the passing repair caused required feedback/final failures.
- Final novelty: feedback separately covers callback continuation and clean `KeyboardInterrupt` cleanup; final composes `SystemExit`, a failed middle hook, complete LIFO execution, and original-exception preservation.
- Baseline -> temporary repaired source SHA-256:
  - `teardown_stack.py`: `5826c34d0bc7c3e61dff941f6586fadbc0aeb447cd31961f56d77f6e6c5bf940` -> `cfe0a319cf9887cd865e39e59164b201a1eba16b001052bebfbe3191d072b8e1`
  - `task_runtime.py`: `0f8a04f6c12f6588223a9d81bd933fdf8f0ee7122fdd931caf433e4049fc7e23` -> `d1065d8a33139aba8d39ec7458797eed248a7222fae596572c6bee1a95f97ec6`

## Static And Cross-Case Gates

- All nine JSON files parsed strictly without duplicate keys; directory and three-file identities matched.
- Every declared source/test path was normalized, relative, contained, ASCII, and `.py`; neither the physical author inventory nor materialized files contained symlinks.
- Public metadata contained no oracle fields. Hidden filenames, test names, complete files, test bodies, inputs, and boundaries were absent from public metadata/source/tests. The teardown tests share only the generic consequence assertion `events == ["closed"]` under materially different exception contexts; this is not disclosure of the hidden input or boundary.
- All mapped Python compiled before execution.
- Dimensions, mechanisms, assertion families, combined source skeletons, all six normalized individual source skeletons, and final boundaries were unique. There was no exact cross-case hidden assertion overlap.
- Manual prohibited-mechanism review found no material overlap. Monthly settlement is calendar scheduling, not fixed-point apportionment or ownership accounting; task teardown is a generic callback stack, not release-reader retirement; factory binding is callable dependency injection. None use any of the other listed proxy, encoding, policy, TLS, reload, checkpoint, hierarchy, SQL, stock, or ticket mechanisms.

## Frozen Author Hashes

Each physical author file had the same SHA-256 before and after validation:

| Relative path | Before and after SHA-256 |
| --- | --- |
| `AUTHOR_RECEIPT.md` | `cc580e69e5a43cd09a16d3189d2f11186d2fd6de65d1bb1dbcaebc95a42fbb1c` |
| `th13_py_factory_binding/case.json` | `30906cb6b6364bcfe6a166d8bb8b6e8b1d338d28d644c1fa28939687f981f3f6` |
| `th13_py_factory_binding/final_oracle.json` | `35dbeb6fee30c8868ad06fe84a2a8a55438a5ea51c72d4458e25fc0dc95648f1` |
| `th13_py_factory_binding/oracle.json` | `421cf8c120c5d1e4e35d84c78892c5dd0d4e853b734fc5e0f0f150394e812672` |
| `th13_py_monthly_settlement/case.json` | `08e975278eba8f2b739ba48b0a85d91e4f484b4049b1e841a9b2b28e730692e2` |
| `th13_py_monthly_settlement/final_oracle.json` | `87a4db172687698d435a144fd3756a98b9c304aa2800cf2817865b2aa14c0d54` |
| `th13_py_monthly_settlement/oracle.json` | `2c87cfe4075b2892a90aad230210405a14212074b1d98d864990ea0c70dbc4cd` |
| `th13_py_task_teardown/case.json` | `9bc9aa757d6059aa24531860982b57f8e83b294c6deafd54ea43f7bb7e525fc6` |
| `th13_py_task_teardown/final_oracle.json` | `cf7a7133c70679e074c6990cfbb3b71be75fa7f18cdf46b982f7de70c209c3d4` |
| `th13_py_task_teardown/oracle.json` | `46e85aa47c1fd2fac3f4bac592574163cb52e575708074ef74273067e81c5845` |

The machine-readable result contains the sorted inventory lines, aggregate inventory hash, all 62 gate records, all exact commands and outputs, per-source baseline/repaired hashes, and cross-case findings.
