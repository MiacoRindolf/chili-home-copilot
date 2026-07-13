# Fourteenth Holdout Independent Semantic V2 Report

**Target commit:** `86d328b7f136ebfbc6f3ace508dd53b401a04939`
**Validation branch:** `codex/fourteenth-v2-semantic-validation`
**Validator:** `codex-independent-semantic-validator-v2`
**Verdict:** **REJECT**

The active V2 fixture passes dimensions, novelty, distractor, final-strength, and leakage review. It fails the mandatory exactly-two-causal-owner gate for `th14_dart_redirect_handoffs`. The defect is preserved; no fixture or implementation file was repaired.

## Scope And Method

- The validation branch was created directly from the requested commit. The shared dirty checkout was not switched or modified.
- All 12 active case/oracle/final triples and the manifest were read.
- Every active case was compared with all 116 cases in generations 1 through 13: 1,392 active-to-prior comparisons.
- The 66 unordered pairs among the 12 active V2 cases were reviewed.
- The separate 13-case disclosed development fixture was also reviewed, adding 156 comparisons.
- The current deterministic registry's 44 invariant templates and 36 concrete repair operators were reviewed. Pure AST extraction of `derive_contract_invariants` returned no invariant for any active prompt.
- No CHILI evaluation mode, Ollama, Claude, Fable 5, other coding model, or network service was invoked. Only `--validate-fixtures --json` preflight was run.

Validation-only preflight passed all 12 cases: public tests passed, feedback failed on the packed defects, sealed finals failed on the packed defects, and all finals were external.

## Corpus Evidence

| Corpus | Cases | Comparisons | Result |
| --- | ---: | ---: | --- |
| Generations 1-8 | 64 | 768 | No material match |
| Generations 9-10 | 16 | 192 | No material match |
| Generations 11-13 | 36 | 432 | No material match |
| Other active V2 cases | 11 per case | 66 unordered | No internal material match |
| Development diagnosis-to-fix fixture | 13 | 156 | No material match |
| Deterministic registry | 44 invariants / 36 operators | All active cases | No material match |

The mandatory first-through-thirteenth referenced inventory contains 297 manifest/case/oracle/final files and hashes to `e6f092655378299436257b85ffd038609f52b211b00324a444e80c9b74f4805b`. Exact normalized prompt and candidate-bundle scans also found zero matches. Those exact checks supplement, but do not replace, the semantic comparison.

## Gate Results

| Gate | Result | Evidence |
| --- | --- | --- |
| Target provenance | PASS | Branch base and pre-artifact `HEAD` equal the full requested SHA. |
| Fixture structure | PASS | 12 unique cases; three candidates, two declared owners, one declared distractor, and `max_files=2` each. |
| Dimensions | PASS | `data=3`, `dependency=5`, `runtime=1`, `state=3`; every mechanism fits the frozen rubric. |
| Prior-generation novelty | PASS | 1,392 comparisons against all 116 generation 1-13 cases; no material match. |
| Internal V2 novelty | PASS | 66 pairwise comparisons; no material match. |
| Deterministic novelty | PASS | No active prompt-derived invariant and no manual collision with the 44 families or 36 operators. |
| Exactly two causal owners | **REJECT** | 11 cases pass; `th14_dart_redirect_handoffs` is solvable through one expected file. |
| Distractor non-necessity | PASS | All 12 distractors are plausible path participants but own no required failed primitive. |
| Materially stronger final | PASS | All 12 finals add adversarial values, protocol composition, concurrency, repetition, or cross-component integration. |
| Leakage | PASS | No hidden path/name, oracle key, solution comment, or repair recipe appears publicly. |

## Per-Case Audit

| Case | Dimension | Owner gate | Final / novelty | Verdict |
| --- | --- | --- | --- | --- |
| `th14_dart_redirect_handoffs` | dependency | **FAIL: follower is not necessary** | Stronger and novel | **REJECT** |
| `th14_dart_semver_selection` | dependency | Two necessary owners | Stronger; distinct from report/license adaptation and package-skew diagnostics | PASS |
| `th14_dart_websocket_fragments` | dependency | Two necessary owners | Stronger; distinct from WebSocket-origin config and UTF-8/JSON-sequence streaming | PASS |
| `th14_node_esm_plugin_loading` | dependency | Two necessary owners | Stronger; distinct from package-version, architecture, and report-schema cases | PASS |
| `th14_node_http_preconditions` | dependency | Two necessary owners | Stronger; distinct from Vary, compression, Link, proxy, and retry semantics | PASS |
| `th14_node_partition_commits` | state | Two necessary owners | Stronger; distinct from compound event positions, file checkpoints, and attempt fencing | PASS |
| `th14_py_context_offload` | state | Two necessary owners | Stronger; distinct from immutable request-policy snapshots and workspace ownership | PASS |
| `th14_py_decorated_handlers` | runtime | Two necessary owners | Stronger; distinct from task teardown and failed single-flight cleanup | PASS |
| `th14_py_link_pagination` | data | Two necessary owners | Stronger; distinct from repeated-query canonicalization, Vary, and proxy-origin resolution | PASS |
| `th14_sql_partner_search` | data | Two necessary owners | Stronger; distinct from fixed-width identity and configured output delimiters | PASS |
| `th14_sql_registry_refresh` | state | Two necessary owners | Stronger; distinct from replacement config, guarded transitions, and archive accounting | PASS |
| `th14_sql_suppression_batches` | data | Two necessary owners | Stronger; distinct from tri-state inheritance, null/zero rollups, and sibling aggregation | PASS |

For the 11 passing ownership cases, disclosed feedback directly isolates a contract in each expected file: neither owner can satisfy the other owner's direct assertion. The redirect case lacks that separation.

## Reject Finding

### `SEM-V2-001`: one expected redirect owner is unnecessary

`RedirectPolicy.derive` is defined in `lib/redirect_request.dart`. Its inputs are the current `RedirectRequest`, `statusCode`, and target `Uri`, and its output is the complete next `RedirectRequest`. It therefore already has every value needed for both failed contracts:

1. Derive method, body, and entity headers from `303` while preserving `HEAD`.
2. Compare the current and target origins and remove credential headers when authority changes.

`RedirectFollower.follow` already resolves each response location and invokes `policy.derive` for every hop. A policy-only change is consequently observed by the cross-authority `307`, the `303` followed by relative `308`, mixed-case credential headers, explicit default port equivalence, and ordinary-header preservation. No feedback or final assertion introduces a follower-only input or observes a follower-specific action.

The direct `RedirectPolicy().derive(...)` feedback proves `lib/redirect_request.dart` is necessary. It does not prove `lib/redirect_follower.dart` is necessary. The two-file ablation of one particular proposed implementation would not establish necessity against this alternate one-file placement.

This is a semantic fixture defect, not a requested implementation repair. Under the validation instructions it requires `REJECT` and is left unchanged.

## Redirect Novelty

The redirect mechanism itself is novel. The closest HTTP, proxy, link, and precondition surfaces were checked explicitly:

| Reference | Different causal contract |
| --- | --- |
| Generation 4 `bh4-405` | A browser login redirect is an observed symptom of leaked test profile/proxy state; it does not test 3xx request derivation. |
| Generation 10 `ts_http_vary_isolation` | Normalizes `Vary`, request-header lookup, cache keys, and wildcard non-cacheability. |
| Generation 10 `py_relay_rotation_window` | Preserves repeated query pairs and verifies signed key/timestamp windows. |
| Generation 10 `ts_retry_budget_clock` | Parses `Retry-After` and allocates retry time budgets. |
| Generation 12 `dart_trusted_proxy_chain` | Resolves inbound forwarded hops and external origin from configured proxy trust. |
| Generation 12 `node_tls_client_auth_config` | Maps startup TLS client-auth modes and validates trust material. |
| Generation 13 `th13_node_response_compression` | Matches configured media types and parameterized response content types. |
| V2 `th14_node_http_preconditions` | Parses entity-tag lists and applies weak read versus strong write validators. |
| V2 `th14_py_link_pagination` | Parses protected Link delimiters and resolves response-relative page navigation. |

No prior case combines HTTP redirect status semantics with cross-origin credential forwarding. Relative `Location` resolution is intentionally healthy in `lib/redirect_response.dart`, so it does not duplicate the Link case's failed parser/client owners. The retired keyset case's compound event position is also absent.

The current deterministic header, Vary, repeated-query, trusted-proxy, TLS, request-snapshot, and retry families remain distinct for the same reasons. Case-insensitive classification of sensitive outbound headers is not the registry's generic case-insensitive map lookup contract, and no deterministic operator owns redirect status derivation or redirect-origin handoff.

## Frozen Inputs

The fourteenth authored inventory, excluding `VALIDATION`, contains 42 files and 142,866 bytes. Its sorted path/hash/size inventory SHA-256 is `42e5876fab439824af5951ecc6ede33df9e003a01866929a9474a218a0ad2920`; the active manifest SHA-256 is `899570e965437b3b028c73a5ddf5f30f52850e7556f3d00d27c418ce6dcab12f`.

Only this report and `semantic_result_v2.json` were authored by this validator.
