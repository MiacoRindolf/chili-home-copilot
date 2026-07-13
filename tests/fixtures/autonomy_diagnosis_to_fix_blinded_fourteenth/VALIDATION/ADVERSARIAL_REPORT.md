# Fourteenth Holdout Adversarial Validation

## Verdict

**REJECT**

Target: `64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3`
Validation branch: `codex/fourteenth-adversarial-validation`
Scope: all 12 case/oracle/final-oracle triples in `autonomy_diagnosis_to_fix_blinded_fourteenth`

The fixture is structurally valid and executable, all 12 public baselines pass, all 12 feedback and sealed-final baselines fail as intended, and no current deterministic contract operator proposes a repair. It is rejected because `th14_dart_keyset_pagination` materially duplicates the ninth holdout's equal-time compound event-position mechanism, source shape, and assertion family.

## Blocking Finding

### ADV-001: prior holdout mechanism reuse

`th14_dart_keyset_pagination` is a domain-renamed and slightly strengthened form of `dart-equal-time-event-order` from the ninth holdout.

| Axis | Fourteenth case | Ninth case |
| --- | --- | --- |
| Incident | Records sharing `occurredAt` disappear after continuation; tied export order is unstable. | Events sharing one recorded millisecond disappear after reconnect; only one tied event remains. |
| Secondary identity | `AuditEvent.id` | `LedgerEvent.sequence` |
| Baseline order defect | Comparator uses only `occurredAt`. | Equal-time comparator orders the secondary key incorrectly. |
| Baseline cursor defect | Cursor serializes only `occurredAt`, so it cannot identify a peer within the tie. | Cursor stores only `_lastTime`, so it cannot advance within the tie. |
| Feedback assertion family | Exact secondary identity order at one instant, then retention of the next peer at the continuation boundary. | Exact secondary sequence order at one instant. |
| Final assertion family | Exact, gap-free, once-only traversal of tied IDs over several pages. | Exact, gap-free, once-only cross-batch acceptance of later tied sequences while rejecting a duplicate. |

The distinction between an ID tie-breaker and a sequence tie-breaker does not create a new causal mechanism. Both cases require the same invariant: event order and continuation must use the complete compound position `(time, stable secondary identity)`, not time alone. The fourteenth final adds multi-page traversal and delimiter-bearing IDs, but that strengthens a previously tested mechanism rather than creating a novel one.

Evidence hashes:

| Artifact | SHA-256 |
| --- | --- |
| `cases/th14_dart_keyset_pagination.json` | `071e74f9c2aafe82c2ac1bf54617066a744c14681bc7bdbd863867a1c41b58bf` |
| `oracles/th14_dart_keyset_pagination.json` | `c8da9865fdc600415e1bdf20957133f948572a42d200d4477b1077f05d1d8025` |
| `final_oracles/th14_dart_keyset_pagination.json` | `3bf46fcb5970976de5c4c8fcecef0b66d430811b418fbcfe39df40aa6e694d30` |
| Ninth `cases/dart-equal-time-event-order.json` | `f3e57232a64ca3195c1b8dbc6cedf37e72db735b4dd848294d2982e53b72b983` |
| Ninth `oracles/dart-equal-time-event-order.json` | `ddde1ba320c1de80c4f95d45e983aebc89d2ffda60ec74a9e87cd380f53e8597` |
| Ninth `final_oracles/dart-equal-time-event-order.json` | `0b7e4b61f166f1017dfb24fb1d6c025df30e868ef3614c0d536f2de605f4c8ef` |

The Dart author receipt records comparison against the thirteenth holdout and the current operator catalog, but not the ninth holdout where this collision resides. Exact-byte searches do not catch it because identifiers, file layout, direction, and test values differ.

## Case Review

| Case | Mechanism and adversarial result | Result |
| --- | --- | --- |
| `th14_dart_keyset_pagination` | Two owners and a stronger final are real, but the compound equal-time cursor/order contract and exact-ID-sequence assertion family materially reuse the ninth holdout. | **REJECT** |
| `th14_dart_semver_selection` | SemVer prerelease precedence plus conjunctive range selection. The `.any` edit is locally easy, but comparator repair is independently required; final adds numeric/text, prefix, build, and prerelease-range composition. | PASS |
| `th14_dart_websocket_fragments` | Stateful frame-byte buffering/coalescing plus fragmented-message continuity across control frames. Prior UTF-8/JSON-sequence cases were reviewed; this case exercises WebSocket frame boundaries and control semantics rather than scalar decoding or channel sequence ownership. | PASS |
| `th14_node_esm_plugin_loading` | Recursive conditional exports plus encoded file-URL construction. Nested conditions and a literal `#` path jointly require both owners. | PASS |
| `th14_node_http_preconditions` | Quote-aware entity-tag lists plus asymmetric weak/strong comparison. Final composes comma-bearing opaque tags with read and write response behavior. | PASS |
| `th14_node_partition_commits` | Actual partition attribution plus contiguous watermark advancement behind out-of-order completions. The earlier channel-sequence case rejects input gaps; this one retains completed work behind an execution gap and advances only when the gap closes. | PASS |
| `th14_py_context_offload` | `ContextVar` token restoration plus submit-time context propagation into a reused thread. Deep-copy/workspace ownership cases were reviewed and use a different mechanism. | PASS |
| `th14_py_decorated_handlers` | Result awaitability through a synchronous-preserving trace wrapper and dispatch. Callable objects and registry composition prevent a one-function shortcut. | PASS |
| `th14_py_link_pagination` | HTTP Link grammar boundaries plus relative-reference resolution. Final adds quoted comma/semicolon metadata, multi-token `rel`, a second link, and parent-relative resolution. | PASS |
| `th14_sql_partner_search` | Literal escaping for `%`, `_`, and the escape character in two substring searches. Final changes values and composes punctuation with case-insensitive behavior and audit preservation. | PASS |
| `th14_sql_registry_refresh` | Non-destructive upsert semantics in two registry paths. The earlier retained-payment case changes deletion retention policy; this case prevents `REPLACE` from performing a delete during metadata refresh. | PASS |
| `th14_sql_suppression_batches` | Nullable anti-join semantics in two batch queries. It is mechanically simple, but not a constant substitution or a one-file repair; final adds duplicate targets, cross-scope reuse, and review composition. | PASS |

## Global Checks

### Structure and execution

- 12 unique triples: 3 Dart, 3 TypeScript/Node, 3 Python, and 3 SQLite SQL.
- Every case has three candidates, `max_files: 2`, and exactly two expected owners.
- Seeded, feedback, and final test paths are disjoint. All 41 authored inputs are ASCII without a BOM.
- Official validation-only result: `valid=true`; 12/12 public passed, 12/12 feedback failed as required, 12/12 external sealed finals failed as required.
- No CHILI evaluation run, Ollama, Claude, Fable 5, or additional coding model was invoked.

### Duplicate and leakage scan

The scan covered 141 case documents, including 77 diagnosis-to-fix cases, and 421 embedded source/test payloads.

- Exact normalized prompt duplicates: `0`.
- Exact embedded payload duplicates between the fourteenth fixture and prior fixtures: `0`.
- Exact generic canonical-source duplicates against prior diagnosis-to-fix cases: `0`.
- Material duplicate found despite those exact checks: `th14_dart_keyset_pagination` versus `dart-equal-time-event-order`.
- No public case contains a feedback/final path or a final assertion. Public test names describe only the healthy baseline. Feedback names are descriptive after the intended oracle-access boundary.

### Repair scope and oracle quality

- All 12 feedback partitions independently exercise both expected owners, or compose one owner while separately isolating the other.
- Static owner-revert review found no distractor or single expected file sufficient for a final pass.
- No case has an end-to-end literal-constant repair. Some local edits are obvious (`any`/conjunction, partition `0`, weak/strong flags), but another expected owner remains necessary.
- Weak and final files are path-disjoint and byte-distinct in all 12 cases. Every final changes values and adds composition or a stronger boundary; none is a renamed weak test.

### Deterministic operators

The exact current APIs were called over each prompt and its three candidate files:

`derive_contract_invariants`, `contract_repair_dimension`, `contract_repair_proposals`, and `contract_invariant_warnings`.

For every case: `invariants=0`, `dimension=unknown`, `proposals=[]`, and `warnings=0`. No current deterministic operator solves any case directly.

## Input Integrity

Aggregate digests are SHA-256 over sorted UTF-8 records of:

`relative-path + NUL + lowercase(file SHA-256) + LF`

`VALIDATION/` is excluded.

| Group | Files | Aggregate SHA-256 |
| --- | ---: | --- |
| Cases | 12 | `da24a7d31fd8996468cf72b65b2629b28518b58f4bc9c5bb3ee3fa25f6d270ab` |
| Weak oracles | 12 | `b48ad5c5fc72bd85daaa42e698d11929d06dc1d54d2712f348bb72337c510d6b` |
| Final oracles | 12 | `8973887ddf648d52a4b7a5e071b3afe079b0922dbb51872be4eacda2da5f2334` |
| Author receipts | 4 | `c92c8ea298210614e6ebf56800641dcd1f464a410647a61a99b3eb77e155d742` |
| All report inputs, including manifest | 41 | `2782dcb63fe1dabfb5a9fee510d4fe878abc408e9b2873f6f410e8955b9286d5` |

`authored_files_unchanged=true`: the branch differs from `64b32b8` only at the two owned `VALIDATION/` paths.

## Commands

Target and branch provenance:

```text
git show --no-patch --format=fuller 64b32b8
git rev-parse HEAD
git merge-base HEAD 64b32b8
```

Allowed official preflight:

```text
$env:PYTHONDONTWRITEBYTECODE='1'
$env:DATABASE_URL='postgresql://validator:validator@127.0.0.1:1/validator'
python -B scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root D:\dev\c14v\tests\fixtures\autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures --json
```

Collision evidence:

```text
git grep -l -F 'dart-equal-time-event-order' 64b32b8 -- tests/fixtures
git show 64b32b8:tests/fixtures/autonomy_diagnosis_to_fix_blinded_ninth/cases/dart-equal-time-event-order.json
git show 64b32b8:tests/fixtures/autonomy_diagnosis_to_fix_blinded_ninth/oracles/dart-equal-time-event-order.json
git show 64b32b8:tests/fixtures/autonomy_diagnosis_to_fix_blinded_ninth/final_oracles/dart-equal-time-event-order.json
```

Final ownership verification:

```text
git diff --exit-code 64b32b8 -- tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth/AUTHORS tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth/cases tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth/oracles tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth/final_oracles tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth/manifest.json
git diff --name-only 64b32b8
```
