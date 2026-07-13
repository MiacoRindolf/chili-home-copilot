# Fourteenth Holdout Independent Semantic V3 Report

**Target commit:** `a249993262ce5c2f621ed17ce67b7cccf8e74fef`
**Validation branch:** `codex/fourteenth-v3-semantic-validation`
**Validator:** `codex-independent-semantic-validator-v3`
**Verdict:** **PASS**

The V3 hardening closes the V2 single-owner redirect defect. All 12 cases fit the
frozen dimensions, remain novel against the complete prior corpus, require exactly
their two declared owners, retain plausible unnecessary distractors, and use
materially stronger sealed finals. No authored fixture byte was changed.

## Scope And Safety

- The branch was created directly from the full requested target SHA in an isolated
  worktree. The occupied shared checkout was not switched or modified.
- All 12 active case/oracle/final triples, the prior V1/V2 semantic and ownership
  findings, all 17 diagnostic manifests, and every manifest-listed prior case were
  read.
- No CHILI evaluation mode, Ollama, Claude, Fable 5, other coding model, network
  service, or package download was invoked.
- The only benchmark execution was the explicitly allowed fixture-validation
  preflight. Deterministic registry calls were pure local functions.
- Redirect repair variants were decoded into disposable directories outside the
  repository. Every scratch directory was removed.

## Gate Results

| Gate | Result | Evidence |
| --- | --- | --- |
| Target provenance | PASS | Pre-output `HEAD` and merge base equal the requested full SHA. |
| Validation-only preflight | PASS | 12 public passes, 12 expected feedback failures, 12 expected sealed-final failures, all finals external. |
| Structure and dimensions | PASS | 12 unique cases; `dart/node/python/sql=3` each; `data=3`, `dependency=5`, `runtime=1`, `state=3`. |
| Full prior-corpus novelty | PASS | 1,668 active-to-prior comparisons over 139 prior cases plus 66 active internal pairs; no material match. |
| Deterministic novelty | PASS | All 12 produce zero invariants, repair dimensions, proposals, and warnings against 44 templates and 36 operators. |
| Exactly two causal owners | PASS | Direct owner-isolating assertions cover both owners in every case. |
| Redirect single-owner attack | PASS | Coordinated repair passes; request-only and follower-only variants each fail the opposite direct boundary. |
| Distractor non-necessity | PASS | All 12 distractors are plausible integration participants but own no failed primitive. |
| Materially stronger finals | PASS | All 12 add composition, repetition, concurrency, protocol nesting, or adversarial value classes. |
| Leakage | PASS | No hidden path/name/value, solution comment, or final payload is exposed publicly. |
| Authored-byte freeze | PASS | The 42 non-VALIDATION inputs retain their frozen 149,337-byte aggregate. |

## Full Corpus Novelty

The audit repeated the complete prior-corpus review rather than carrying forward
the V2 conclusion.

| Corpus | Cases | Active comparisons |
| --- | ---: | ---: |
| First through eighth blinded diagnostic generations | 64 | 768 |
| Ninth and tenth diagnosis-to-fix generations | 16 | 192 |
| Eleventh through thirteenth generations | 36 | 432 |
| Disclosed development diagnosis-to-fix fixture | 13 | 156 |
| Base and runtime calibration fixtures | 10 | 120 |
| **All prior cases** | **139** | **1,668** |
| Other active V3 cases | 66 unordered pairs | 66 |

The 129 reference cases account for 1,548 comparisons. The additional 10
calibration cases were also included. Exact normalized prompts, cross-fixture
embedded payloads, candidate-source bundles, active prompts, and active bundles
produced zero matches. Manual review then compared mechanism, assertion family,
owner call graph, and source shape for every pair.

Closest surfaces remain materially different:

| Active case | Closest earlier surfaces | Distinction |
| --- | --- | --- |
| Redirect handoffs | browser redirect symptom, trusted proxy chain, Link pagination | Outbound 3xx derivation plus post-policy cross-origin credential enforcement. |
| SemVer selection | package skew and dependency-report adaptation | SemVer identifier precedence plus conjunctive range selection. |
| WebSocket fragments | UTF-8 stream boundaries and JSON sequence assembly | WebSocket frame buffering and fragmented-message continuity around control frames. |
| ESM plugin loading | loaded-module uncertainty and dependency report schemas | Conditional exports plus correctly encoded dynamic file imports. |
| HTTP preconditions | Vary isolation and response compression | Quote-aware entity-tag lists and asymmetric weak-read/strong-write matching. |
| Partition commits | event positions, tail checkpoints, and attempt fencing | Per-partition attribution plus contiguous completion watermarks. |
| Context offload | request policy snapshots and workspace ownership | ContextVar token restoration plus submit-time thread context capture. |
| Decorated handlers | factory binding and task teardown | Awaitability through decorators and callable objects. |
| Link pagination | repeated query preservation and redirects | Protected Link grammar plus response-relative next-page resolution. |
| Partner search | fixed-width exact identifiers | Literal wildcard escaping in two independent substring-search statements. |
| Registry refresh | config reload and guarded state transitions | SQLite replacement-cascade avoidance while preserving immutable onboarding state. |
| Suppression batches | tri-state overrides, null/zero rollups, sibling aggregation | NULL-safe anti-joins for advisory suppression rows. |

The retired `th14_dart_keyset_pagination` collision remains absent. Redirect
handoffs contain no timestamp order, secondary identity, cursor, page position, or
exactly-once traversal mechanism.

## Causal Owners

| Case | Necessary owner 1 | Necessary owner 2 | Isolation |
| --- | --- | --- | --- |
| `th14_dart_redirect_handoffs` | `redirect_request.dart` | `redirect_follower.dart` | Direct policy 303 behavior versus post-override credential enforcement. |
| `th14_dart_semver_selection` | `semantic_version.dart` | `release_selector.dart` | Direct precedence versus conjunctive selection. |
| `th14_dart_websocket_fragments` | `websocket_decoder.dart` | `websocket_messages.dart` | Partial/coalesced bytes versus control-frame-safe assembly. |
| `th14_node_esm_plugin_loading` | `package-exports.mjs` | `plugin-loader.mjs` | Direct condition resolution versus hash-safe file URL import. |
| `th14_node_http_preconditions` | `entity-tag-list.mjs` | `request-preconditions.mjs` | Quoted-list parsing versus weak/strong decision semantics. |
| `th14_node_partition_commits` | `offset-tracker.mjs` | `batch-consumer.mjs` | Contiguous watermark versus actual partition forwarding. |
| `th14_py_context_offload` | `request_scope.py` | `work_dispatch.py` | Nested token restoration versus submit-time context capture. |
| `th14_py_decorated_handlers` | `trace_hooks.py` | `handler_dispatch.py` | Wrapper completion order versus callable-object awaitability. |
| `th14_py_link_pagination` | `link_header.py` | `page_client.py` | Protected grammar versus base-URL resolution. |
| `th14_sql_partner_search` | `search_inventory.sql` | `search_delivery_lanes.sql` | Each query is loaded and asserted independently. |
| `th14_sql_registry_refresh` | `refresh_supplier.sql` | `refresh_depot.sql` | Each object-kind refresh is loaded and asserted independently. |
| `th14_sql_suppression_batches` | `select_email_batch.sql` | `select_webhook_batch.sql` | Each channel query is loaded and asserted independently. |

No distractor can satisfy either direct owner assertion. Distractors remain
plausible because they participate in the public or final integration path, but
their packed contracts are already correct.

## Redirect Architecture Attack

The V2 request-only alternative was reproduced, not merely ablated from one chosen
repair. Every variant preserved the public constructors, methods, signatures, and
subclass API and passed `dart analyze`.

| Variant | Public | Feedback | Final |
| --- | ---: | ---: | ---: |
| Coordinated request-policy plus follower repair | 0 | 0 | 0 |
| Request-only centralization; packed follower unchanged | 0 | 255 | 255 |
| Follower-only repair; packed request/policy unchanged | 0 | 255 | 255 |

The request-only centralization implemented 303 method/body/entity derivation,
case-insensitive credential filtering, and scheme/host/effective-port comparison
inside `RedirectPolicy.derive`. It fails feedback at `credentials from a redirect
decision stay with their origin` and final at `credentials from one handoff do not
enter the next decision`. The injected override returns a fresh complete request
after base policy dispatch, so only the follower can enforce that boundary.

The maximal follower-only variant implements both status derivation and credential
filtering after policy dispatch. It fails feedback at `see-other changes a write
into a read` and final at `see-other changes PATCH into GET` because those direct
`RedirectPolicy` calls never enter the follower.

Changing `RedirectRequest` construction cannot close the request-only gap while
preserving APIs: construction sees the resulting URI and headers but not the prior
origin. It therefore cannot distinguish the required public same-origin
credential-bearing policy result from an equivalent result returned during a
cross-origin handoff. The two direct observation surfaces force both owners.

## Final Strength

Every feedback path is disjoint from and byte-distinct from its final path. The
redirect final adds direct PATCH 303 behavior, two cross-origin preserving hops,
credentials reintroduced on each hop, observation before the next policy decision,
mixed-case authorization/cookie/proxy-authorization, a normal 303-to-relative-308
chain, default HTTPS port equivalence, and direct HEAD behavior.

The other finals likewise compose both owners: advanced SemVer ordering with a
bounded selection, chunked/coalesced WebSocket frames around control traffic,
registry activation through nested conditions and a literal-hash directory,
mixed entity-tag lists through the catalog response, concurrent mixed-partition
completion, reused-thread context isolation, registered traced callable objects,
rich Link metadata across a relative archive boundary, composed SQL punctuation,
repeated registry refresh plus explicit expiry, and duplicate/advisory/cross-scope
suppression rows.

## Validation Evidence

Allowed preflight:

```text
$env:PYTHONDONTWRITEBYTECODE='1'
python -B scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures --json
```

Observed exit `0`, schema `chili.diagnosis-to-fix-fixture-validation.v3`, and
`valid=true` for all 12 cases.

The non-VALIDATION authored inventory contains 42 files and 149,337 bytes. Its
aggregate is SHA-256 over ordinally sorted UTF-8 records of
`fixture-relative-path + NUL + lowercase file SHA-256 + LF`:

```text
0fc6adfdc16f2e0662948333c4b431c3d9d7a1830d86a08603acd19daa0679c9
```

The active manifest SHA-256 is
`899570e965437b3b028c73a5ddf5f30f52850e7556f3d00d27c418ce6dcab12f`.
The V3 redirect triple aggregate independently reproduces as
`3e57947e1ef6d50514eba858a862d75e4e1ab9e70d5d958b7b783ea918adfaa9`.

`authored_files_unchanged=true`. Only this report and
`semantic_result_v3.json` are authored by this validator.
