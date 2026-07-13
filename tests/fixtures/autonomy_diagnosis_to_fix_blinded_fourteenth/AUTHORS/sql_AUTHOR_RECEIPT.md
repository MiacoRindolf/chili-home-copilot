# SQLite SQL Author Receipt

Authoring date: 2026-07-13

Workspace: `D:\dev\chili-home-copilot`

Branch: `chili/momentum-concurrency-basis-independent`

Base tip: `395f4ef0348a2a3908e3f6946e06612b4b1652c8`

Runtime used for validation:

- Python 3.13.11
- SQLite 3.51.0 through Python `sqlite3`
- pytest 9.0.2

This lane contains exactly three independently authored SQLite SQL cases. Each
case has three source candidates, a two-file edit budget, exactly two causal
source owners, a passing public baseline, a failing disclosed feedback
partition, and a separately authored failing final partition. No CHILI
benchmark or local coding model was run. Temporary repair sanity checks were
used only in disposable directories and were not copied into the fixtures.

## Mechanisms And Ownership

### `th14_sql_partner_search`

- Mechanism: literal operator input is passed directly into SQLite `LIKE`, so
  pattern metacharacters broaden two independent substring searches.
- Expected dimension: `data` (query representation and matching semantics).
- Expected owners: `sql/search_inventory.sql`,
  `sql/search_delivery_lanes.sql`.
- Distractor: `sql/record_search_audit.sql`; it is on the reported path and
  handles the same input, but correctly preserves the submitted value.
- Final strengthening: composed punctuation plus case-insensitive matching,
  beyond the individual percent and underscore feedback boundaries.

### `th14_sql_registry_refresh`

- Mechanism: SQLite `INSERT OR REPLACE` refreshes delete and reinsert an
  existing parent, cascading away attached rows and replacing immutable
  establishment metadata.
- Expected dimension: `state` (entity lifecycle and relationship ownership).
- Expected owners: `sql/refresh_supplier.sql`, `sql/refresh_depot.sql`.
- Distractor: `sql/expire_registry_bindings.sql`; it legitimately removes only
  due bindings and composes correctly with a refresh.
- Final strengthening: repeated refreshes, multiple retained links, mutable
  metadata updates, and explicit cleanup composition.

### `th14_sql_suppression_batches`

- Mechanism: a nullable target in a scoped suppression subquery makes SQLite
  `NOT IN` evaluate to unknown for otherwise eligible rows, emptying two
  independent outbound batches.
- Expected dimension: `data` (nullable representation and anti-join semantics).
- Expected owners: `sql/select_email_batch.sql`,
  `sql/select_webhook_batch.sql`.
- Distractor: `sql/list_suppression_feed.sql`; it correctly exposes both
  advisory and targeted rows and plausibly sits on the incident path.
- Final strengthening: duplicate targeted records, target reuse across scopes,
  advisory records, deterministic ordering, and review-query composition.

These mechanisms were compared with every current contract invariant and
repair operator in `app/services/project_autonomy/diagnostic_reasoning.py` and
with all twelve thirteenth case prompts. None duplicates the existing families.
An isolated AST execution of `derive_contract_invariants` returned zero known
invariants for each new prompt.

## Baseline Validation

Each mode was materialized from JSON into a fresh temporary repository. Public
runs contained only `repo_files`; feedback runs added only `feedback_files`;
final runs used another fresh copy and added only `final_files`.

| Case | Public | Feedback | Final |
| --- | --- | --- | --- |
| `th14_sql_partner_search` | exit 0, 3 passed | exit 1, 2 failed / 3 passed | exit 1, 2 failed / 4 passed |
| `th14_sql_registry_refresh` | exit 0, 3 passed | exit 1, 2 failed / 3 passed | exit 1, 2 failed / 3 passed |
| `th14_sql_suppression_batches` | exit 0, 3 passed | exit 1, 2 failed / 3 passed | exit 1, 2 failed / 4 passed |

Commands used inside each materialized repository:

```text
python -m py_compile <materialized test files>
python -m pytest tests/test_public.py -q --disable-warnings
python -m pytest tests/test_public.py tests/test_feedback.py -q --disable-warnings
python -m pytest tests/test_public.py tests/test_final.py -q --disable-warnings
```

The PowerShell shape auditor also parsed every JSON object with
`ConvertFrom-Json`, checked case-id agreement, `language=sql`,
`test_runner=pytest`, three candidates, `max_files=2`, exactly two expected
owners contained in candidates and `repo_files`, non-empty disjoint feedback
and final paths, and the absence of `final_files` from repair oracles. All
checks passed. Every embedded SQL source was prepared and exercised by at
least one pytest path against a real in-memory SQLite schema; result values,
selected-row counts, or mutation `rowcount` were asserted.

## Causal Sanity

Disposable coordinated repairs were applied only after materialization, then
public, feedback, and final tests were run together with:

```text
python -m pytest tests -q --disable-warnings
```

| Case | Both expected owners | First owner only | Second owner only |
| --- | --- | --- | --- |
| `th14_sql_partner_search` | exit 0, 8 passed | exit 1, 2 failed / 6 passed | exit 1, 2 failed / 6 passed |
| `th14_sql_registry_refresh` | exit 0, 7 passed | exit 1, 2 failed / 5 passed | exit 1, 2 failed / 5 passed |
| `th14_sql_suppression_batches` | exit 0, 8 passed | exit 1, 2 failed / 6 passed | exit 1, 2 failed / 6 passed |

Thus both expected owners are independently necessary, both together are
sufficient, and the distractor needs no change.

## Authored Payload Hashes

SHA-256, lowercase:

```text
444d075c6ad611528691fd25e32fa36aba2bed852a991c955647f338d8af20e3  cases/th14_sql_partner_search.json
a7710bdb10af50e8adc1647ceb62f9e8381ddb5d1f9204df5a83c017605807d4  cases/th14_sql_registry_refresh.json
c7a04cf8931bc6085507105f3e8007519d6f6509f4436f3ae48ae7d187af8538  cases/th14_sql_suppression_batches.json
33b99190352bc5370d0959ab42ac1a5236e05e2c1dd18442aff73678e20dab8c  final_oracles/th14_sql_partner_search.json
2e3c0821b4a973e727b2fab547f892a2ad790f0233fbbcc636cad2f4ae427f4e  final_oracles/th14_sql_registry_refresh.json
c7361ae323a1d1cc954f2acd3103e24bb5175739ce7fcefeecacc04b45675c75  final_oracles/th14_sql_suppression_batches.json
4e1359a8e49b93f22227d2b669e9968fca64e5ba899488b43120f0e673af861d  oracles/th14_sql_partner_search.json
4497003459235977286dc0cdcf3d44923dce512e84a960ee78927b22ddf145c4  oracles/th14_sql_registry_refresh.json
adf88f29a08449a4878a504ce52ffb6670c1276cfa8daef5c598a1925ed55416  oracles/th14_sql_suppression_batches.json
```

The receipt is intentionally excluded from its own embedded hash table because
embedding its digest would change that digest.
