# Fourteenth Diagnosis-to-Fix Python Author Receipt

## Scope

- Authored exactly three Python 3 / pytest case triples under `autonomy_diagnosis_to_fix_blinded_fourteenth`.
- Authored only `th14_py_*.json` case, oracle, and final-oracle files plus this receipt.
- Did not edit a manifest, index, application file, benchmark loader, existing test, source capability file, or the earlier `project_ws/AgentOps` receipt path.
- Did not run the CHILI benchmark, a local coding model, a service, or a network-dependent command.
- Temporary repositories and representative repairs used for validation were created outside the fixture and removed automatically.

## Environment

- `python --version` -> `Python 3.13.11`
- `python -m pytest --version` -> `pytest 9.0.2`

## Schema And Catalog Study

The author studied the v3 sealed-fixture loader in `scripts/autopilot_diagnosis_to_fix_benchmark.py`, including path normalization, ownership-budget checks, discoverable-test checks, disjoint feedback/final partitions, public-only baseline execution, and fresh final adjudication. The finalized thirteenth manifest and all twelve thirteenth case/oracle/final triples were also reviewed.

The current repair catalog in `app/services/project_autonomy/diagnostic_reasoning.py` was reviewed through `derive_contract_invariants`, `contract_repair_proposals`, and every `_repair_*` family. That catalog covers canonical Base64URL, TLS client auth, replacement reload, request snapshots, file checkpoints, hierarchy resolution, generation lifecycle, trusted proxies, tri-state overrides, transition accounting, async single-flight rejection, cancellation, injected-clock expiry, subscriptions, partial uniqueness, sibling aggregation, Vary handling, temporal bounds, retry timing/budgets, vector clocks, byte ranges, repeated query pairs, ordered identities, key rotation/timestamp units, scoped idempotency, materialized heads/event time, and the complete thirteenth family set.

The thirteenth mechanisms reviewed were upgraded dependency-report/license interpretation, UTC offset schedule inversion, Windows export naming, null-versus-zero rollups, attempt fencing, compression policy matching, keyword-only factory injection, month-end local scheduling, teardown under process termination, configured SQL delimiters, guarded export transitions, and physical-unit normalization.

Running the three new prompts through the current deterministic `derive_contract_invariants` implementation returned `existing_family_matches=0` for every case.

## Case Inventory

| Case | Novel mechanism | Dimension and rubric fit | Expected owner files | Plausible distractor |
| --- | --- | --- | --- | --- |
| `th14_py_context_offload` | `ContextVar` token restoration for nested scopes plus submit-time context propagation into a reused executor thread | `state`: context ownership, nesting, and job isolation | `request_scope.py`, `work_dispatch.py` | `audit_event.py` is on the observed attribution path but formats the correct state it receives |
| `th14_py_link_pagination` | Grammar-aware HTTP Link field boundaries plus resolution of URI references against the response URL | `data`: wire representation boundaries and URI identity | `link_header.py`, `page_client.py` | `item_decoder.py` is on every page path but decoding remains correct |
| `th14_py_decorated_handlers` | Coroutine semantics preserved through tracing wrappers plus dispatch based on the invocation result's awaitability | `runtime`: Python callable and await execution semantics | `trace_hooks.py`, `handler_dispatch.py` | `handler_registry.py` participates in final composition but preserves handler identity correctly |

Each case has three unique candidate source paths, exactly two expected owners, `max_files: 2`, one non-owner distractor, unchanged public APIs, independently authored feedback and final tests, and no fixture-specific source labels.

## Validation Commands

1. JSON parsing used `python -m json.tool` over every owned `th14_py_*.json` file.
2. An inline Python shape validator loaded all triples, enforced IDs/types/path safety, three-or-more candidates, exactly two owners, owner-budget membership, supported dimensions, and disjoint seeded/feedback/final paths. The same validator called `compile(content, path, "exec")` for every embedded `.py` payload.
3. A temporary materialization harness ran `python -m pytest -q --tb=no` separately for the public baseline, public plus disclosed feedback, and a fresh public plus sealed-final repository.
4. A disposable solvability harness overlaid representative changes only in the two expected owners, ran all tiers together and the final tier in a fresh repository, then repeated with each owner change removed.
5. The current deterministic recognizer was imported from the post-thirteenth diagnostic worktree and `derive_contract_invariants(case["prompt"])` was evaluated for each prompt.
6. Artifact hashes used `Get-FileHash -Algorithm SHA256` after all JSON validation completed.

## Baseline Results

| Case | Public baseline | Public + feedback | Fresh public + final |
| --- | --- | --- | --- |
| `th14_py_context_offload` | exit `0`, 2 passed | exit `1`, 2 failed and 2 passed | exit `1`, 1 failed and 2 passed |
| `th14_py_link_pagination` | exit `0`, 2 passed | exit `1`, 2 failed and 2 passed | exit `1`, 1 failed and 2 passed |
| `th14_py_decorated_handlers` | exit `0`, 3 passed | exit `1`, 2 failed and 3 passed | exit `1`, 1 failed and 3 passed |

JSON validation passed for 9 files. Shape validation passed for all 3 triples, and syntax compilation passed for all 18 embedded Python files.

## Two-Owner Proof

| Case | Coordinated repair | Fresh final after repair | First owner only | Second owner only |
| --- | --- | --- | --- | --- |
| `th14_py_context_offload` | exit `0`, 5 passed | exit `0` | exit `1` | exit `1` |
| `th14_py_link_pagination` | exit `0`, 5 passed | exit `0` | exit `1` | exit `1` |
| `th14_py_decorated_handlers` | exit `0`, 6 passed | exit `0` | exit `1` | exit `1` |

The representative repairs existed only in disposable validation copies. No solution patch or solution text was added to the fixture.

## Artifact Hashes

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `cases/th14_py_context_offload.json` | 2523 | `7f4b2a4a342b3b44f98926a0977cfa3d8afc36d493a38d0b9c0da402f654ecd2` |
| `cases/th14_py_decorated_handlers.json` | 2924 | `7439991beded730861d030359306a74fb87ee4740ac5bdece2e2456a3009f9f2` |
| `cases/th14_py_link_pagination.json` | 4038 | `55541cb878c94231253a26e1dc272910d6ad92c024bbae315b78ab2ced542293` |
| `oracles/th14_py_context_offload.json` | 1142 | `9bbb8e6164bf14abd0876f91421078941aff2aab2e5e9632b7af970c239595ba` |
| `oracles/th14_py_decorated_handlers.json` | 1248 | `d0a28284525c0918babceda51e066144e67f56c9e9878e3f0381d3c4c504d6af` |
| `oracles/th14_py_link_pagination.json` | 1169 | `0c0bd0a9e6d6ea285a3b5158766181346087439a68d031447cfd5c3dfbb3535b` |
| `final_oracles/th14_py_context_offload.json` | 1153 | `dff3f83b1efb73407d717f3292401e9cb68b5f9af4e83e7d5ee9a222288f541d` |
| `final_oracles/th14_py_decorated_handlers.json` | 982 | `0a2b2925f9a563e04acf5378f3b86637ee801c68620a6fa02318b7ce5f28ab2c` |
| `final_oracles/th14_py_link_pagination.json` | 1011 | `3f283d2774b7d9e0463e56f0a52a7decdaffabe6ab2c262a22100722809dac8b` |

The receipt is intentionally excluded from its own hash table because embedding its SHA-256 would be self-referential.
