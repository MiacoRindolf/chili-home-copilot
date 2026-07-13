# Fourteenth Holdout V2 Runtime Validation

**Verdict: PASS**

Independent runtime V2 validation targeted commit `86d328b7f136ebfbc6f3ace508dd53b401a04939`. Branch `codex/fourteenth-holdout-v2-runtime-validation` was created directly from that commit. Only validation preflight and direct language commands were run; evaluation mode, Ollama, Claude, Fable 5, and other coding models were not invoked.

## Environment

| Tool | Version |
|---|---|
| Python | 3.13.11 |
| pytest | 9.0.2 |
| Node | 24.15.0 |
| Dart | 3.11.1 stable |
| Git | 2.53.0.windows.1 |

## Model-Free Preflight

Command (exit `0`):

```text
python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures --json
```

The result used schema `chili.diagnosis-to-fix-fixture-validation.v3`, reported `valid=true`, and returned all 12 active manifest cases. Every case had `public_passed=true`, `feedback_failed=true`, `final_failed=true`, `sealed_final_adjudication=true`, and `external_final_oracle=true`. Stdout SHA-256 was `ceca58ef0e7be0a3b223e1861c83f2db60814a1d69ef0662de0712ef98ffc014`; stderr was empty with SHA-256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.

## Direct Results

Each case was independently materialized into distinct public, feedback, and final Git repositories. The final repository was initialized from `repo_files`, then received `final_files` only. It had one seed commit, no feedback path, every final path, and seed content matching the case input. Syntax ran before tests in all three repositories.

| Case | Runner | Syntax exits P/F/Fn | Public | Feedback (hidden, public) | Final (hidden, public) | Owners / max | Fresh final | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `th14_dart_redirect_handoffs` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | `2/2` | yes | PASS |
| `th14_dart_semver_selection` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | `2/2` | yes | PASS |
| `th14_dart_websocket_fragments` | Dart | `0/0/0` | `0` | `255,0` | `255,0` | `2/2` | yes | PASS |
| `th14_node_esm_plugin_loading` | Node test | `5x0/7x0/10x0` | `0` | `1,0` | `1,0` | `2/2` | yes | PASS |
| `th14_node_http_preconditions` | Node test | `4x0/5x0/5x0` | `0` | `1,0` | `1,0` | `2/2` | yes | PASS |
| `th14_node_partition_commits` | Node test | `4x0/5x0/5x0` | `0` | `1,0` | `1,0` | `2/2` | yes | PASS |
| `th14_py_context_offload` | pytest | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_py_decorated_handlers` | pytest | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_py_link_pagination` | pytest | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_sql_partner_search` | pytest + SQLite | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_sql_registry_refresh` | pytest + SQLite | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |
| `th14_sql_suppression_batches` | pytest + SQLite | `0/0/0` | `0` | `1` | `1` | `2/2` | yes | PASS |

The active replacement case, `th14_dart_redirect_handoffs`, passed public syntax and behavior. Its feedback command failed at the expected 303 method-rewrite contract, and its final command failed at the expected multi-hop derived-method contract. The public command remained green in both repositories.

Direct commands were the declared language operations: Dart used `dart analyze lib tests` and `dart run`; Node used `node --check` on every discovered JavaScript module and `node --test --test-reporter=spec`; Python used `compileall` and pytest; SQL used `compileall` for its Python harness and pytest execution of every query through `sqlite3`. Exact per-case commands, exits, expected owners, fixture hashes, and failure summaries are recorded in `runtime_result_v2.json`. The direct evidence bundle SHA-256 was `a70a0ee4157ad73b74fa82ad057d7e255966268cd33288cb34c5783a808facb4`.

## Owner And Freshness Audit

All 12 cases declare `max_files=2`. Every repair oracle declares exactly two distinct expected owners, and every owner is an approved candidate path. All 12 final repositories were separate from their public and feedback repositories, contained no feedback test path, had no missing final path, retained exactly one seed commit, and had no seed-content mismatch.

## Model And Authorship Audit

The benchmark's validation branch returns at line 3291 before `ollama_client.list_models()` at line 3298. The direct process ledger contained only Git, Python, Node, and Dart, with model calls `0`; direct command proxies were pinned to `127.0.0.1:9`.

The 42 authored input files outside `VALIDATION/` had aggregate SHA-256 `899c1702cdfec8be3d5cf5f3313442a55075b5588274268e74b89904d1ff220e` before and after execution. Protected fixture content and all V1 validation artifacts were unchanged. No defects were found.
