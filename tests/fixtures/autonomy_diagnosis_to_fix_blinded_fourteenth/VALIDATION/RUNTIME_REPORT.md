# Fourteenth Holdout Runtime Validation

**Verdict: PASS**

Independent validation targeted commit `64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3` on branch `codex/fourteenth-runtime-validation-sparse`. The benchmark was run only in `--validate-fixtures` mode. No evaluation mode, Ollama, Claude, Fable 5, or coding-model process was invoked.

## Runtime

| Tool | Version |
|---|---|
| Python | 3.13.11 |
| pytest | 9.0.2 |
| Node | 24.15.0 |
| Dart | 3.11.1 stable |
| Git | 2.53.0.windows.1 |

## Real Preflight

Command (exit `0`):

```text
python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures --json
```

The result used schema `chili.diagnosis-to-fix-fixture-validation.v3`, reported `valid=true`, and returned 12/12 cases with `public_passed=true`, `feedback_failed=true`, `final_failed=true`, `sealed_final_adjudication=true`, and `external_final_oracle=true`. Stdout SHA-256: `b751acf46a7311bc9baae7cb718bd3edaeb2aca58933b4d2121ead65d39a0ac5`; stderr was empty (SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).

## Direct Results

Each case was materialized independently into distinct public, feedback, and final directories. Final directories were built from `repo_files + final_files` only; no feedback path was present, all seed hashes matched, and all final paths were present. Syntax was checked in all three directories before direct declared-runner execution.

| Case | Runner | Syntax exits P/F/Fn | Public | Feedback (hidden, public) | Final (hidden, public) | Owners / max | Fresh final | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `th14_dart_keyset_pagination` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | `2/2` | yes | PASS |
| `th14_dart_semver_selection` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | `2/2` | yes | PASS |
| `th14_dart_websocket_fragments` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | `2/2` | yes | PASS |
| `th14_node_esm_plugin_loading` | Node test | all `0` | `0` | `1,0` | `1,0` | `2/2` | yes | PASS |
| `th14_node_http_preconditions` | Node test | all `0` | `0` | `1,0` | `1,0` | `2/2` | yes | PASS |
| `th14_node_partition_commits` | Node test | all `0` | `0` | `1,0` | `1,0` | `2/2` | yes | PASS |
| `th14_py_context_offload` | pytest | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_py_decorated_handlers` | pytest | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_py_link_pagination` | pytest | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_sql_partner_search` | pytest + SQLite | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_sql_registry_refresh` | pytest + SQLite | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_sql_suppression_batches` | pytest + SQLite | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |

Exact per-case syntax and test commands, individual exit codes, expected owners, fixture hashes, failure summaries, and output SHA-256 values are recorded in `runtime_result.json`. The corrected direct-evidence bundle SHA-256 is `c26a1356d00122cad5775013e93fa00c2151302c8fe750255898ed75117a933d`.

## Runner And Model Audit

Runner declarations matched language/runtime behavior: Dart used `dart analyze` plus `dart run`; TypeScript/MJS used `node --check` plus `node --test --test-reporter=spec`; Python used `compileall` plus pytest; SQL tests compiled their Python harnesses and parsed/executed every query through `sqlite3` under pytest.

No model call was reachable in preflight. `run()` returns validation JSON at benchmark lines 3291-3297, before `ollama_client.list_models()` at line 3298. Direct commands operated only on isolated synthetic case directories and invoked Python, pytest, Node, or Dart.

## Integrity

All 12 cases declare `max_files=2`; every oracle declares exactly two expected owners, both within candidate paths. The 41 authored input files outside `VALIDATION/` have aggregate SHA-256 `e5d14329ffd1544532e3ebad1610ee201edaf7ce15cb2cc0511d22e851599853`, and `git diff` against the target commit was empty for `AUTHORS/`, `cases/`, `oracles/`, `final_oracles/`, and `manifest.json`. Authored fixture bytes are unchanged.

No defects were found.
