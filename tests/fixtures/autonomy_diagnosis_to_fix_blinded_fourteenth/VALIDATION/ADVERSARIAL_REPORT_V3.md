# Fourteenth Holdout V3 Adversarial Validation

**Verdict: PASS**

Target commit: `a249993262ce5c2f621ed17ce67b7cccf8e74fef`

Validation branch: `codex/fourteenth-holdout-adversarial-v3`

Validator: Codex independent `adversarial_v3` validator

The V1 compound-position collision remains absent, and the V2 one-file redirect
loophole is closed. Independent placement analysis found no plausible one-file or
alternate-owner repair for the hardened redirect case. A repeated audit of all 12
active triples found no duplicate mechanism, deterministic operator solution,
trivial end-to-end repair, weak final, or public leak.

No CHILI evaluation mode, Ollama, Claude, Fable 5, hosted model, local coding
model, or other coding model was run. The only benchmark execution was the
explicitly permitted `--validate-fixtures` preflight. The deterministic operator
calls were direct, model-free API calls.

## Target And Preflight

The branch was created directly from the full requested target. Before these two
outputs were authored, `HEAD` and the merge base both equaled:

```text
a249993262ce5c2f621ed17ce67b7cccf8e74fef
```

Validation-only command:

```text
python -B scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures --json
```

It exited `0` with schema
`chili.diagnosis-to-fix-fixture-validation.v3` and `valid=true`. All 12 public
baselines passed. All 12 feedback baselines and all 12 external sealed finals
failed as required, and all 12 reported sealed-final adjudication.

The active shape is balanced at three Dart, three TypeScript, three Python, and
three SQL cases. Every case has three candidate paths, exactly two expected
owners, `max_files=2`, and disjoint public, feedback, and final paths.

## V1 Collision

The V1 rejection compared `th14_dart_keyset_pagination` with the ninth holdout's
`dart-equal-time-event-order`. Both required a compound `(time, secondary
identity)` position for tied ordering and continuation.

That collision is absent at the target:

- active manifest references to `th14_dart_keyset_pagination`: `0`;
- active case/oracle/final paths for that ID: `0`;
- active payload mentions of that ID: `0`;
- active replacement entries for `th14_dart_redirect_handoffs`: `1`;
- timestamp, cursor, secondary-position, and once-only traversal contracts in the
  replacement: none.

Historical mentions occur only in author receipts and preserved V1/V2 validation
artifacts. Those files are not packed into a synthetic case repository. The
replacement's HTTP status derivation and post-policy origin handoff do not
reintroduce the compound-position mechanism or assertion family.

## V2 Loophole

V2 was correctly rejected because `RedirectPolicy.derive` already received the
current request, redirect status, and target URI. A repair confined to
`lib/redirect_request.dart` could therefore implement both 303 derivation and
cross-origin credential removal while leaving the follower unchanged.

V3 adds an independent post-dispatch observation surface. Feedback and final
inject subclasses that override `derive` and return fresh complete requests with
mixed-case credentials. Dynamic dispatch bypasses the base policy repair. The
follower must inspect the returned request and enforce the origin boundary after
each policy call.

This conclusion does not rely on the author's chosen repair or ablations. The
plausible placements were reviewed independently:

| Placement | Adversarial result |
| --- | --- |
| Request file only | It can repair direct base-policy 303 behavior, but an override bypasses that logic. A fresh returned request has the target URI and headers but no source-origin input, so request construction cannot distinguish required same-origin preservation from cross-origin removal. |
| Follower file only | Feedback and final call `RedirectPolicy().derive` directly. Those calls never reach the follower, so 303 method, body, and entity metadata remain wrong. |
| Response file only | It validates status and resolves `Location` before dispatch. It sees neither a direct policy call nor the request returned by an overriding policy. |
| Request plus response, omitting follower | There is still no normal post-dispatch hook over the overriding policy's returned request. Making this work requires a public API change, wire-visible provenance marker, hidden global state, or host-specific behavior, none of which is a plausible contract-preserving patch. |
| Follower plus response, omitting request | Direct policy assertions remain unreachable, so status derivation remains wrong. |
| Request plus follower | Each file owns one independently observed primitive and the pair composes through all hops. This is the declared owner set. |

A global constructor-level credential scrub is also excluded by healthy public
behavior: an injected policy may return same-origin credentials. The final adds
implicit-versus-explicit HTTPS port equivalence and requires those credentials to
survive. Conversely, an end-of-chain scrub is insufficient because the final
records whether credentials from the first cross-origin handoff reach the second
policy decision.

The information boundary is therefore real under plausible alternatives:
`lib/redirect_request.dart` is required for direct status derivation, and
`lib/redirect_follower.dart` is required for enforcement over replaceable policy
output.

## All-Prior Audit

The repeat scan parsed all 17 fixture manifests at the target, all active V3
triples, and every prior manifest-listed case.

| Scope | Count |
| --- | ---: |
| Manifest-listed cases | 151 |
| Active V3 cases | 12 |
| All prior cases | 139 |
| Source-bearing diagnosis-to-fix cases | 77 total / 65 prior |
| Active intraset pairs | 66 |
| Active-to-all-prior comparisons | 1,668 |
| Active-to-source-bearing-prior comparisons | 780 |
| Embedded payloads | 81 active / 359 prior |
| Candidate sources | 36 active / 169 prior |

Exact normalized prompt matches, exact cross-fixture embedded payload matches,
canonical candidate-source matches against prior cases, and candidate-source
matches between active cases each produced zero results. Semantic review then
covered incident mechanisms, assertion families, owner call graphs, and source
shapes rather than treating hashes as sufficient.

No material duplicate was found. The closest surfaces remain distinct:

- the fourth holdout login redirect is browser-test state contamination, not HTTP
  request derivation;
- the trusted-proxy case derives an inbound external origin, not an outbound
  post-policy credential boundary;
- prior UTF-8 stream cases do not own WebSocket frame/control semantics;
- Vary, Link, and retry cases do not own entity-tag or redirect semantics;
- equal-time ordering, reconnect fencing, and attempt fencing do not own
  partition completion watermarks;
- retained-payment deletion policy does not own non-destructive registry refresh;
- tri-state inheritance does not own nullable anti-join behavior.

## Operator Audit

The target registry source blob is
`0b4b1366a471dc099a4866bd5870bafc2f5524ff`, unchanged since V1. Its frozen
inventory remains 44 invariant templates and 36 repair operators.

The updated prompt and candidate source set for every active case were passed to:

```text
derive_contract_invariants
contract_repair_dimension
contract_repair_proposals
contract_invariant_warnings
```

For all 12 cases: `invariants=0`, `dimension=unknown`, `proposals=0`, and
`warnings=0`. No current deterministic operator directly solves an active case.

## Case Review

| Case | Owner and triviality result | Final-strength result |
| --- | --- | --- |
| `th14_dart_redirect_handoffs` | Direct base-policy 303 behavior and post-override origin enforcement require request and follower owners. The V2 request-only placement is bypassed. | Adds per-hop injected credentials, two cross-origin hops, pre-second-hop observation, normal 303/308 composition, port equivalence, and HEAD. |
| `th14_dart_semver_selection` | Direct precedence assertions require the version owner; conjunctive range selection requires the selector. Comparator distortion cannot plausibly make `any` enforce both bounds. | Adds numeric/text prerelease precedence, prefix ordering, build neutrality, and bounded prerelease composition. |
| `th14_dart_websocket_fragments` | Decoder state is tested directly across transport chunks; assembler state is tested directly across a ping. Neither owner can mask the other. | Coalesces four frames, arbitrary chunks, continuation, control traffic, and a following message. |
| `th14_node_esm_plugin_loading` | Conditional export resolution is directly asserted; a literal `#` directory independently requires encoded file-URL loading. | Nests node/import conditions and activates the selected module through the registry from a scoped path. |
| `th14_node_http_preconditions` | Quote-aware tag parsing and weak-read/strong-write matching are independently asserted. | Composes comma-bearing opaque tags with read and write response behavior. |
| `th14_node_partition_commits` | The tracker directly holds out-of-order completion; a tracker spy directly exposes hard-coded partition attribution in the consumer. | Uses concurrent completion across two partitions and verifies the restart-safe report. |
| `th14_py_context_offload` | Nested token restoration is direct; submit-time context propagation into a reused thread requires dispatch wrapping. | Reuses one worker across two nested scopes and verifies restoration and non-leakage. |
| `th14_py_decorated_handlers` | Trace completion ordering and awaitable-result dispatch are independently observed, including an unwrapped callable object. | Composes registry lookup, a traced callable object, and one complete asynchronous timeline. |
| `th14_py_link_pagination` | Rich Link grammar is directly parsed; relative resolution requires the current page URL in the client. | Adds quoted comma/semicolon metadata, multi-token `rel`, multiple links, and parent-relative navigation. |
| `th14_sql_partner_search` | Each fixed query owner is executed independently. No single file or literal constant repairs both literal wildcard searches. | Composes percent, underscore, punctuation, case folding, ordering, and verbatim audit preservation. |
| `th14_sql_registry_refresh` | Supplier and depot refresh SQL are independently executed; the cleanup query is a healthy distractor. | Adds repeated refresh, multiple retained bindings, and explicit cleanup composition. |
| `th14_sql_suppression_batches` | Independent email and webhook anti-joins must each tolerate nullable advisory rows. | Adds duplicate suppressions, cross-scope identities, ordering, and review-feed preservation. |

All 12 cases require both expected owners. No plausible distractor, alternate
owner pair, one-file patch, or end-to-end literal substitution closes a case.
Some local SQL or selector edits are mechanically small, but another directly
tested owner remains independently necessary.

## Finals And Leakage

Public, feedback, and final paths are disjoint in all 12 triples. No weak payload
is byte-identical to a final payload. Every final changes values and adds
protocol composition, concurrency, repetition, cross-component integration, or
another adversarial boundary; none is a renamed feedback test.

The public leak scan found:

- forbidden oracle keys: `0`;
- feedback/final path hits: `0`;
- hidden feedback test/check labels scanned: `33`, hits: `0`;
- hidden final test/check labels scanned: `40`, hits: `0`;
- final assertion payloads or repair recipes in public cases: `0`.

The redirect public subclass is healthy contract evidence, not a hidden recipe.
It establishes that replaceable policies may return complete same-origin requests;
it does not disclose the cross-origin hidden values, per-hop observation, or the
required implementation.

## Input Integrity

The authored inventory outside `VALIDATION/` contains 42 files and 149,337 bytes.
Its aggregate SHA-256 is:

```text
0fc6adfdc16f2e0662948333c4b431c3d9d7a1830d86a08603acd19daa0679c9
```

The digest is SHA-256 over sorted UTF-8 records of:

```text
fixture-relative-path + NUL + lowercase(file SHA-256) + LF
```

The hardened redirect triple's independently reproduced 322-byte aggregate is:

```text
3e57947e1ef6d50514eba858a862d75e4e1ab9e70d5d958b7b783ea918adfaa9
```

It matches the corrected replacement receipt. No authored input, manifest,
source, script, `project_ws` file, or prior V1/V2 validation byte was changed.
Only this report and `adversarial_result_v3.json` are owned by this validator.
