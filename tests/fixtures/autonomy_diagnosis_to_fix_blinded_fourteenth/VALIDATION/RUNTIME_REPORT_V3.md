# Fourteenth Holdout V3 Runtime Validation

**Verdict: PASS**

Independent runtime V3 validation targeted exact commit
`a249993262ce5c2f621ed17ce67b7cccf8e74fef` on branch
`codex/fourteenth-holdout-v3-runtime-validation`. Only validation preflight and
direct language commands were run. CHILI evaluation mode, Ollama, Claude,
Fable 5, and other coding models were not invoked.

## Environment

| Tool | Version |
|---|---|
| Python | 3.13.11 |
| pytest | 9.0.2 |
| Node | 24.15.0 |
| Dart | 3.11.1 stable |
| Git | 2.53.0.windows.1 |

## Model-Free Preflight

The validation-only command exited `0`:

```text
python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures --json
```

It returned schema `chili.diagnosis-to-fix-fixture-validation.v3`, `valid=true`,
and all 12 active manifest cases. Counts were 12 public passes, 12 expected
feedback failures, 12 expected final failures, 12 sealed final adjudications,
and 12 external final oracles. Stderr was empty. Stdout SHA-256 was
`ceca58ef0e7be0a3b223e1861c83f2db60814a1d69ef0662de0712ef98ffc014`.

## Direct Results

Each case was materialized into distinct public, feedback, and final Git
repositories. Syntax ran before behavior. Feedback and final runs were followed
by direct public rechecks. In the table, feedback and final exits are shown as
`tier,public-recheck`.

| Case | Runner | Syntax P/F/Fn | Public | Feedback | Final | Fresh final | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|
| `th14_dart_redirect_handoffs` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | yes | PASS |
| `th14_dart_semver_selection` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | yes | PASS |
| `th14_dart_websocket_fragments` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | yes | PASS |
| `th14_node_esm_plugin_loading` | Node | `5x0/7x0/10x0` | `0` | `1,0` | `1,0` | yes | PASS |
| `th14_node_http_preconditions` | Node | `4x0/5x0/5x0` | `0` | `1,0` | `1,0` | yes | PASS |
| `th14_node_partition_commits` | Node | `4x0/5x0/5x0` | `0` | `1,0` | `1,0` | yes | PASS |
| `th14_py_context_offload` | pytest | `0/0/0` | `0` | `1,0` | `1,0` | yes | PASS |
| `th14_py_decorated_handlers` | pytest | `0/0/0` | `0` | `1,0` | `1,0` | yes | PASS |
| `th14_py_link_pagination` | pytest | `0/0/0` | `0` | `1,0` | `1,0` | yes | PASS |
| `th14_sql_partner_search` | pytest + SQLite | `0/0/0` | `0` | `1,0` | `1,0` | yes | PASS |
| `th14_sql_registry_refresh` | pytest + SQLite | `0/0/0` | `0` | `1,0` | `1,0` | yes | PASS |
| `th14_sql_suppression_batches` | pytest + SQLite | `0/0/0` | `0` | `1,0` | `1,0` | yes | PASS |

Dart used `dart analyze lib tests` and `dart run`. Node used `node --check`
for every discovered JavaScript module and `node --test --test-reporter=spec`.
Python used `compileall` and pytest. SQL used `compileall`; pytest executed the
queries through Python `sqlite3`. All syntax commands exited zero.

## V3 Redirect Baseline

The changed redirect artifacts matched the author receipt byte-for-byte:

| Artifact | SHA-256 |
|---|---|
| Public case | `3774b7d6688bf6d8f2dafa0c67a4500a18f468e4b45e838bfb9eac179da7477a` |
| Feedback oracle | `d350364a517ef17b648c0afd3fac810c33637db032ffb02f5c3e5d76584ced8f` |
| Final oracle | `36d8f782b7f79b76b86d2b61edf2206ac6ad89914d50f6174e4805407b25f971` |

The V3 packed baseline was confirmed after the hidden/final changes. Public
exited `0` with `public tests passed`; feedback exited `255` at
`see-other changes a write into a read`; final exited `255` at
`see-other changes PATCH into GET`. Public rechecks in both repositories exited
`0`. This matches the receipt's V3 baseline and differs from the preserved V2
final failure location as intended.

## Isolation And Authorship

Every final repository was freshly initialized from `repo_files`, had one seed
commit, then received `final_files` only. Public, feedback, and final paths were
distinct; no feedback path appeared in a final repository; all final paths were
present; and every seed tree and seed file matched the case input. Each oracle
declared exactly two distinct owners within candidate paths and every case had
`max_files=2`.

The direct process ledger contained only Git, Python, Node, and Dart. Outbound
proxies were pinned to `127.0.0.1:9`; model calls were zero. The 42 authored
fixture files outside `VALIDATION/` retained aggregate SHA-256
`0fc6adfdc16f2e0662948333c4b431c3d9d7a1830d86a08603acd19daa0679c9`
before and after execution. Manifest, source, script, `project_ws`, and all 16
prior V1/V2 validation files were unchanged. No defects were found.
