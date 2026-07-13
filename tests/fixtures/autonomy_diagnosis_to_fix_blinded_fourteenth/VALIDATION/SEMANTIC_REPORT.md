# Fourteenth Holdout Independent Semantic Report

**Target commit:** `64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3`
**Validator:** `codex-independent-semantic-validator-v1`
**Verdict:** **PASS**

All 12 fourteenth cases are semantically admissible. Their expected dimensions fit the causal rubric, every case needs exactly the two named source owners, every third candidate is plausible but unnecessary, every sealed final adds a material boundary beyond feedback, and no prompt or candidate source leaks a solution. The 12 new mechanisms have no material match in any of the 12 thirteenth cases or in the target commit's complete deterministic invariant/repair registry.

## Scope And Independence

- Validation was performed on branch `codex/fourteenth-semantic-validation`, created directly from the requested target because the shared checkout was on another commit with unrelated work.
- The audit read all 36 fourteenth case/oracle/final JSON files, the manifest, all four author receipts, all 36 thirteenth case/oracle/final JSON files, the thirteenth semantic V2 artifacts, the benchmark loader, and the target diagnostic invariant/repair source.
- Author receipts were treated as provenance inputs, not as proof. The verdict comes from the packed source, disclosed feedback, sealed final, target rubric, target deterministic registry, and independent pairwise review.
- No evaluation run, Ollama call, Claude call, Fable 5 call, hosted model call, or local coding-model call occurred. CHILI's `--validate-fixtures` path was used only in validation mode.
- No authored fixture, application source, script, test, manifest, receipt, case, oracle, or final-oracle byte was edited.

## Reproducible Evidence

Key commands and observed evidence:

```powershell
git rev-parse HEAD
# 64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3
```

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -B scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures
# exit 0
# fixtures_valid=True cases=12
```

```powershell
git show 64b32b8:app/services/project_autonomy/diagnostic_reasoning.py | rg -n 'CAUSAL_DIMENSION_RUBRIC|derive_contract_invariants|contract_repair_proposals|def _repair_'
```

An AST-only inventory of that exact Git object found 44 unique invariant templates and 36 repair operators. Executing only the extracted pure `derive_contract_invariants` function against each of the 12 fourteenth prompts returned `[]` for every prompt. The canonical JSON inventory of those 44 templates and 36 operator names has SHA-256 `06aed5350e2e5e26c2fea9f389172d534750b3d531a82d54460339d19a603954`.

The structure check independently confirmed this identical shape for all 12 cases:

```text
candidates=3 owners=2 distractors=1 max_files=2 owners_in_candidates=true
```

The leak scan reported, for every case:

```text
forbidden_public_keys=[] hidden_name_hits=[]
suspicious_prompt_or_candidate_source_lines=[] duplicate_hidden_payload_count=0
```

## Frozen Inputs

| Input | Scope | SHA-256 |
| --- | --- | --- |
| Fourteenth authored inventory | 41 files excluding `VALIDATION`; 123,423 bytes | `3352f0916908ce0aac893230177be1a299c3c6ae1c3df5f2462d027d5020428e` |
| Thirteenth reference triples | manifest plus 12 case/oracle/final triples, 37 files | `48864439434cbb8c55aab2343e79f6bfe65608b5ebf727149666fd0379c2a25c` |
| Thirteenth semantic result V2 | `semantic_result_v2.json` | `963e995bdbd63752bf650c287e12254f7b57667521bd990b3e49af0f51a842d3` |
| Thirteenth semantic report V2 | `SEMANTIC_REPORT_V2.md` | `ace570a4fe2903aa86ff23e924bdd3cde8e249a97ad4c281ca707e716673dd3f` |
| Deterministic registry source | `diagnostic_reasoning.py` | `6a081ca2d5d8b4775f299c3c54743c22d83d2bb77f8177c62f2e899069e0d8a7` |
| Validation loader | `autopilot_diagnosis_to_fix_benchmark.py` | `8a1ffe4b7ea68824892330e218154e1601f18619b4cba1af8f3181087254b361` |
| Fourteenth manifest | `manifest.json` | `f196fe7ca334ab3e88af2aa9c3d075a550ff67f1e253c3fa0aec4f5f6fec900c` |

The inventory hash is SHA-256 over sorted lines of `relative_path<TAB>file_sha256<TAB>byte_count`, joined with LF and terminated by LF.

## Global Gates

| Gate | Result | Evidence |
| --- | --- | --- |
| Target identity | PASS | Full HEAD equals the requested integrated fixture commit. |
| Fixture validity | PASS | Validation-only preflight: `fixtures_valid=True cases=12`. |
| Dimension rubric | PASS | `data=4`, `dependency=4`, `runtime=1`, `state=3`; each mechanism matches the cited rubric definition. |
| Thirteenth novelty | PASS | 12 x 12 = 144 semantic comparisons; zero material matches. |
| Deterministic novelty | PASS | 44 invariant families and 36 repair operators reviewed; zero prompt recognizer matches and zero manual mechanism collisions. |
| Internal novelty | PASS | Twelve distinct semantic fingerprints and twelve distinct candidate/owner source-bundle hashes. |
| Two-owner necessity | PASS | Every feedback partition isolates one contract per expected owner; each final suite requires both contracts. |
| Distractors | PASS | Every third candidate lies on the observed/integration path but already satisfies its contract. |
| Final strength | PASS | Twelve finals add protocol nesting, adversarial values, concurrency, repeated transitions, or cross-component composition. |
| Prompt/source leak | PASS | No oracle key, hidden name/body, solution API, repair recipe, or solution comment appears publicly. |

## Thirteenth Reference Set

Every new case was compared against all of these mechanisms, not only against a same-language or same-dimension subset:

1. `th13_dart_dependency_report`: report-schema adaptation plus license-expression evaluation.
2. `th13_dart_offset_schedule`: inclusive offset transition plus wall-time inversion.
3. `th13_dart_portable_exports`: Windows filename normalization plus case-folded allocation.
4. `th13_node_facility_rollup`: null-versus-zero preservation and aggregation.
5. `th13_node_job_recovery`: attempt-token fencing for late settlement.
6. `th13_node_response_compression`: config token normalization plus media-type matching.
7. `th13_py_factory_binding`: callable parameter-kind discovery and invocation binding.
8. `th13_py_monthly_settlement`: month-end clamp plus instant-preserving timezone conversion.
9. `th13_py_task_teardown`: BaseException-safe LIFO teardown and exception precedence.
10. `th13_sql_delimited_profile`: configured separators plus delimiter-sensitive quoting.
11. `th13_sql_export_job_state`: guarded lifecycle transitions plus worker ownership.
12. `th13_sql_package_units`: lookup-based physical-unit normalization and threshold filtering.

No fourteenth mechanism is a domain rename of any item above. The closest shared concepts are explicitly distinguished per case below.

## Case Results

| Case | Mechanism fingerprint | Dimension | Two owners | Distractor | Verdict |
| --- | --- | --- | --- | --- | --- |
| `th14_dart_keyset_pagination` | `data.compound_keyset_cursor.total_order_timestamp_desc_id_asc` | `data` | `audit_order` + `audit_pager` | `audit_event` | PASS |
| `th14_dart_semver_selection` | `dependency.semver_precedence.conjunctive_range` | `dependency` | `semantic_version` + `release_selector` | `package_release` | PASS |
| `th14_dart_websocket_fragments` | `dependency.websocket.incremental_frame_buffer.fragment_control_preservation` | `dependency` | `websocket_decoder` + `websocket_messages` | `websocket_frame` | PASS |
| `th14_node_esm_plugin_loading` | `dependency.node_exports.conditional_resolution.file_url_encoding` | `dependency` | `package-exports` + `plugin-loader` | `plugin-registry` | PASS |
| `th14_node_http_preconditions` | `dependency.http_etag.quote_aware_list.weak_strong_preconditions` | `dependency` | `entity-tag-list` + `request-preconditions` | `catalog-response` | PASS |
| `th14_node_partition_commits` | `state.partition_checkpoint.contiguous_watermark.actual_partition` | `state` | `offset-tracker` + `batch-consumer` | `commit-report` | PASS |
| `th14_py_context_offload` | `state.contextvars.token_restore.submit_context_propagation` | `state` | `request_scope` + `work_dispatch` | `audit_event` | PASS |
| `th14_py_decorated_handlers` | `runtime.python_awaitability.wrapper_completion.dispatch_result` | `runtime` | `trace_hooks` + `handler_dispatch` | `handler_registry` | PASS |
| `th14_py_link_pagination` | `data.http_link.quoted_delimiters.relative_resolution` | `data` | `link_header` + `page_client` | `item_decoder` | PASS |
| `th14_sql_partner_search` | `data.sql_like.literal_metachar_escape` | `data` | two search queries | search audit | PASS |
| `th14_sql_registry_refresh` | `state.sqlite_upsert.preserve_identity_children_immutable_fields` | `state` | supplier + depot refresh | binding expiry | PASS |
| `th14_sql_suppression_batches` | `data.sql_antijoin.null_safe_scoped_suppression` | `data` | email + webhook batches | suppression review | PASS |

### `th14_dart_keyset_pagination` - PASS

- **Rubric:** `data`; timestamp-only ordering/cursor state omits stable record identity.
- **Owners:** `audit_order.dart` must define a total tie order. `audit_pager.dart` must carry that same compound key in its continuation boundary. Either contract absent still loses tied records.
- **Distractor:** `audit_event.dart` correctly provides id and UTC time.
- **Final:** feedback has one tie and a one-record continuation. Final traverses three tied records over multiple pages, uses delimiter-bearing ids, surrounds the tie with newer/older rows, and proves progress, exactly-once order, and termination.
- **Nearest collision:** ordered-preference identity, monotonic materialized heads, and source-aware file checkpoints do not implement a tied-row keyset cursor. Thirteenth portable exports allocates filenames rather than traversal boundaries.

### `th14_dart_semver_selection` - PASS

- **Rubric:** `dependency`; package/version compatibility is explicit in the rubric.
- **Owners:** `semantic_version.dart` owns prerelease precedence; `release_selector.dart` owns conjunction across bounds. Direct feedback assertions make neither substitutable.
- **Distractor:** `package_release.dart` correctly carries package, version, and withdrawal state.
- **Final:** adds numeric-versus-text identifiers, prefix length, ignored build metadata, and prerelease-only bounded selection.
- **Nearest collision:** thirteenth license-expression evaluation is a Boolean expression parser over licenses; it does not compare SemVer identifiers or select releases across version bounds.

### `th14_dart_websocket_fragments` - PASS

- **Rubric:** `dependency`; framing and message assembly are wire-protocol compatibility.
- **Owners:** `websocket_decoder.dart` must retain partial bytes and drain coalesced frames. `websocket_messages.dart` must keep an active text fragment across control traffic.
- **Distractor:** `websocket_frame.dart` already enforces valid control-frame shape.
- **Final:** arbitrary transport cuts and coalescing feed a fragmented text message, interleaved ping, continuation, and following complete message.
- **Nearest collision:** inclusive byte ranges, subscription cancellation, and abort propagation do not provide incremental WebSocket framing or message-fragment state.

### `th14_node_esm_plugin_loading` - PASS

- **Rubric:** `dependency`; Node package exports and ESM activation are package compatibility.
- **Owners:** `package-exports.mjs` owns recursive active-condition selection. `plugin-loader.mjs` owns conversion of filesystem paths into encoded file URLs.
- **Distractor:** `plugin-registry.mjs` correctly delegates and stores the loaded object.
- **Final:** combines a literal `#` package directory, nested node/import/default conditions, and registry activation.
- **Nearest collision:** canonical base64url validates encoded text, while unordered hierarchy resolves parent graphs. Neither performs Node conditional exports or file-URL serialization.

### `th14_node_http_preconditions` - PASS

- **Rubric:** `dependency`; ETag and precondition semantics are HTTP wire-protocol rules.
- **Owners:** `entity-tag-list.mjs` owns quote-aware list boundaries. `request-preconditions.mjs` owns weak read comparison and strong write comparison.
- **Distractor:** `catalog-response.mjs` correctly maps a decision to the stable response shape.
- **Final:** composes a comma-bearing current tag with a mixed weak read list and a weak write guard through the response service.
- **Nearest collision:** the thirteenth compression case and deterministic Vary family also touch HTTP headers, but they handle media policy/cacheability rather than ETag grammar and asymmetric validators.

### `th14_node_partition_commits` - PASS

- **Rubric:** `state`; partition-owned checkpoint advancement is queue/lifecycle state.
- **Owners:** `offset-tracker.mjs` owns a contiguous watermark with later completions retained behind gaps. `batch-consumer.mjs` must forward the actual partition.
- **Distractor:** `commit-report.mjs` only formats the supplied snapshot.
- **Final:** controlled concurrent completion spans two partitions, checks the unsafe intermediate gap boundary, closes the gap, and verifies the restart report.
- **Nearest collision:** job-attempt fencing rejects a stale worker; tail checkpoints reset replaced files; monotonic heads choose a latest tuple. None aggregate out-of-order completions into a per-partition contiguous watermark.

### `th14_py_context_offload` - PASS

- **Rubric:** `state`; execution-context ownership, nested restoration, and worker isolation are state semantics.
- **Owners:** `request_scope.py` must restore the replaced ContextVar token. `work_dispatch.py` must capture submit-time execution context for the executor thread.
- **Distractor:** `audit_event.py` faithfully reports the current request value.
- **Final:** two distinct nested submissions reuse one worker while the parent is restored after each and no identity leaks after shutdown.
- **Nearest collision:** the existing request-policy snapshot family is the closest. It deep-copies mutable policy plus generation so authorization, response, and audit agree across a reload. This case transports and restores language execution context; it has no policy reload, generation, or shared decision snapshot.

### `th14_py_decorated_handlers` - PASS

- **Rubric:** `runtime`; the failure is Python invocation-result and await execution semantics.
- **Owners:** `trace_hooks.py` must defer finish instrumentation until an awaitable settles. `handler_dispatch.py` must inspect the invocation result, including async callable objects.
- **Distractor:** `handler_registry.py` preserves exact handler identity.
- **Final:** a registered, traced async callable object requires both contracts in one result/timeline.
- **Nearest collision:** BaseException teardown controls cleanup order and exception precedence, not decorator-preserved awaitability or result-driven dispatch.

### `th14_py_link_pagination` - PASS

- **Rubric:** `data`; Link grammar boundaries and resolved URI identity are representation concerns.
- **Owners:** `link_header.py` owns protected comma/semicolon parsing and relation grouping. `page_client.py` owns response-relative URI resolution.
- **Distractor:** `item_decoder.py` correctly handles unchanged page bodies.
- **Final:** combines parent-relative navigation, comma and semicolon inside protected contexts, multiple links, multi-token `rel`, and collection across an archive boundary.
- **Nearest collision:** repeated query canonicalization preserves `(key, value)` pairs; Vary normalizes header names. Neither parses Link field grammar or resolves page-relative navigation.

### `th14_sql_partner_search` - PASS

- **Rubric:** `data`; literal text is being misrepresented as a pattern language.
- **Owners:** both SQL search files execute independently and each must preserve literal substring semantics.
- **Distractor:** search audit must keep the unescaped operator value verbatim.
- **Final:** composes percent, underscore, the escape punctuation itself, and mixed case in both domains, then verifies audit preservation.
- **Nearest collision:** configured delimiter quoting renders output fields. It does not escape SQL LIKE metacharacters for literal search.

### `th14_sql_registry_refresh` - PASS

- **Rubric:** `state`; parent identity, child ownership, and immutable establishment metadata are lifecycle state.
- **Owners:** supplier and depot refresh statements independently use destructive replacement and each is directly exercised.
- **Distractor:** binding expiry correctly deletes only due children and is required unchanged in the final composition.
- **Final:** repeated supplier refreshes preserve multiple links and immutable metadata; depot refresh composes with explicit expiry so only the due route disappears.
- **Nearest collision:** replacement configuration reload intentionally discards omitted overrides, while guarded transitions and monotonic heads enforce predecessor/newer predicates. This case avoids SQLite delete/reinsert and preserves parent identity plus foreign-key children.

### `th14_sql_suppression_batches` - PASS

- **Rubric:** `data`; nullable representation and SQL three-valued anti-join behavior own the failure.
- **Owners:** the email and webhook selection queries independently use NULL-poisonable `NOT IN` subqueries.
- **Distractor:** the review query must continue displaying advisory NULL rows, so filtering it would be incorrect.
- **Final:** adds duplicate targeted rows, reused ids in another scope, advisory NULL rows, deterministic ordering, and review-feed preservation for both channels.
- **Nearest collision:** thirteenth null-versus-zero rollup concerns missing measurements and aggregation; tri-state overrides concern NULL inheritance. Neither is a scoped NULL-safe anti-semijoin.

## Final Decision

The fixture passes the semantic gate with no reservations requiring fixture changes. All twelve per-case verdicts are `PASS`; there are no defects to preserve as `REJECT`.

The machine-readable companion, `semantic_result.json`, records the complete reference fingerprints, source-bundle hashes, owner necessity, nearest-family distinctions, leak findings, and input hashes.
