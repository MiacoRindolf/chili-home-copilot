# Fourteenth Holdout V2 Adversarial Validation

**Verdict: REJECT**

Target commit: `86d328b7f136ebfbc6f3ace508dd53b401a04939`
Validation branch: `codex/fourteenth-holdout-v2-adversarial`
Validator: Codex independent `adversarial_v2` validator

The V1 keyset collision is removed. The active manifest no longer references
`th14_dart_keyset_pagination`, its case/oracle/final triple is absent, and the
replacement `th14_dart_redirect_handoffs` does not materially collide with the
prior corpus. The V2 fixture is nevertheless rejected because the replacement is
solvable end to end in one source file while its oracle declares two required
owners.

No CHILI evaluation mode, Ollama, Claude, Fable 5, hosted model, local coding
model, or other coding model was run. The only benchmark execution was the
explicitly allowed `--validate-fixtures` preflight.

## Blocking Finding

### ADV2-001: `redirect_handoffs` has a one-file repair

`th14_dart_redirect_handoffs` declares these expected owners:

1. `lib/redirect_request.dart`
2. `lib/redirect_follower.dart`

Only the first is behaviorally necessary.

`RedirectPolicy.derive`, defined in `lib/redirect_request.dart`, already receives
the complete decision tuple: the current request, redirect status code, and target
URI. The same file owns `RedirectRequest.copyWith`. A repair confined to that file
can therefore:

1. For 303, preserve `HEAD`; otherwise derive `GET`, clear the body, and remove
   entity headers case-insensitively.
2. Compare the current and target scheme, host, and effective port, including
   implicit versus explicit default ports.
3. On an origin change, remove `Authorization`, `Cookie`, and
   `Proxy-Authorization` case-insensitively while retaining ordinary headers.
4. Return `current.copyWith` with the target and all derived state.

The packed `RedirectFollower.follow` already resolves each `Location` from the
request that received it and delegates every hop to exactly that policy method:

```text
current = policy.derive(
  current,
  statusCode: response.statusCode,
  target: target,
)
```

Consequently, `lib/redirect_follower.dart` can remain byte-identical. The weak
direct-policy 303 assertions pass through the repaired policy. The weak
cross-authority 307 assertions also pass through it because the policy has both
URIs. In the sealed 303-to-308 chain, the unchanged follower passes the first
derived request back into the same policy, so removed entity and credential state
stays removed. The default-port and `HEAD` checks are likewise policy-local.

The author receipt's one-owner ablation only retains one half of its chosen
coordinated implementation. It shows that that particular split needs both files;
it does not rule out moving the origin-sensitive logic into the already capable
policy owner. This alternative-ownership counterexample requires no API change,
new dependency, constant special case, or distractor edit.

This is a benchmark-quality defect. Scoring requires the changed-file set to equal
the oracle's two expected files, so a minimal behaviorally complete repair loses
the file-selection check while a no-op second edit can satisfy it.

Evidence hashes:

| Artifact | SHA-256 |
| --- | --- |
| `cases/th14_dart_redirect_handoffs.json` | `341e60251684055963e94b350f4d08a387dbbafb310196921322183b95011418` |
| Embedded `lib/redirect_request.dart` | `ba0a6796be512551a6bcdab9dad663eb986e810bb5f95b018202e0ff27a6f3ba` |
| Embedded `lib/redirect_follower.dart` | `43385e4f4736ebb113a4be38aee6e88af37d840700cd7d1284e25bbd00a06e72` |
| `oracles/th14_dart_redirect_handoffs.json` | `895add9c4ee93bb1ffe914b47ee9a9ec092815c80d97c5e40fe92bd86ce1e48a` |
| `final_oracles/th14_dart_redirect_handoffs.json` | `08df7e56fe05393631b6927e81ccd7bbf40912f44265a1244bb79780e95a7e84` |

The defect was preserved. No disposable repair was applied to the repository or
fixture.

## V1 Collision Status

The V1 finding was `th14_dart_keyset_pagination` versus the ninth holdout's
`dart-equal-time-event-order`: both used a compound `(time, secondary identity)`
position for ordering and continuation.

At the target commit:

- active manifest references to `th14_dart_keyset_pagination`: `0`;
- active case/oracle/final files for that ID: `0`;
- active replacement entries for `th14_dart_redirect_handoffs`: `1`;
- timestamp/cursor/compound-position behavior in the replacement: none.

Historical mentions remain only in the frozen V1 validation artifacts and author
receipts, which the validation brief explicitly forbids changing. They are not
packed into a synthetic case repository. The V1 collision is removed and is not
reintroduced by the replacement.

## Full Corpus Audit

The audit parsed every manifest at the target commit, every active V2 triple, and
the complete prior manifest corpus.

| Scope | Count |
| --- | ---: |
| Manifest roots | 17 |
| Manifest-listed cases | 151 |
| Active V2 cases | 12 |
| All prior cases | 139 |
| Prior reference cases | 129 |
| Prior base/runtime calibration cases | 10 |
| Source-bearing diagnosis-to-fix cases | 77 total, 65 prior |
| Active intraset case pairs | 66 |
| Active-to-all-prior comparisons | 1,668 |
| Active-to-source-bearing-prior comparisons | 780 |
| Embedded source/test payloads | 81 active, 359 prior |

Exact normalized prompts, exact cross-fixture embedded payloads, exact generic
candidate-source skeletons against prior cases, and exact candidate-source
skeletons between active cases each produced zero matches. Semantic review then
covered mechanisms, assertion families, owner call graphs, and source shapes
beyond those hashes.

The replacement's closest surfaces are not collisions:

- `bh4-405` contains the word redirect, but diagnoses contaminated browser-test
  state and insufficient attribution.
- `dart_trusted_proxy_chain` validates an inbound trusted-proxy chain and derives
  an external origin. The replacement governs outbound status-specific request
  derivation and sensitive-header forwarding.
- `ts_http_vary_isolation` normalizes cache variant metadata. It does not compare
  redirect origins or derive a follow-up request.
- Active `th14_node_http_preconditions` parses entity-tag lists and applies
  weak/strong validators.
- Active `th14_py_link_pagination` parses Link grammar and resolves a next-page
  reference. Its relative-URI surface does not share redirect status or credential
  semantics.

No material mechanism, assertion-family, or source-skeleton duplicate was found
for `redirect_handoffs`, and no second duplicate was found among the other eleven
active cases.

## Case Review

| Case | Adversarial result | Result |
| --- | --- | --- |
| `th14_dart_redirect_handoffs` | Corpus-novel and strongly finalized, but `RedirectPolicy.derive` can own both repairs; the follower is unnecessary. | **REJECT** |
| `th14_dart_semver_selection` | Direct comparator assertions require SemVer repair, while the tautological multi-clause selection requires the selector owner. | PASS |
| `th14_dart_websocket_fragments` | Decoder buffering and fragmented-message continuity around control frames are independently exercised. Prior UTF-8 stream cases do not share WebSocket frame/control semantics. | PASS |
| `th14_node_esm_plugin_loading` | Conditional-export recursion and encoded file-URL construction require separate owners; prior dependency cases do not share this source shape. | PASS |
| `th14_node_http_preconditions` | Quote-aware entity-tag parsing and asymmetric weak/strong matching are separately asserted and distinct from Vary handling. | PASS |
| `th14_node_partition_commits` | Actual partition attribution and contiguous completion watermarks are independently tested; prior sequence and fencing cases have different state ownership. | PASS |
| `th14_py_context_offload` | ContextVar token restoration and submit-time context capture into a reused thread require both owners. | PASS |
| `th14_py_decorated_handlers` | Trace completion ordering and result awaitability for callable objects require both wrapper and dispatcher behavior. | PASS |
| `th14_py_link_pagination` | Structured Link parsing and base-URL resolution are separately asserted and compose in the final. | PASS |
| `th14_sql_partner_search` | Literal wildcard escaping is required in both independent query owners; audit preservation remains a healthy control. | PASS |
| `th14_sql_registry_refresh` | Supplier and depot refresh queries are independently executed; each must avoid destructive replacement while retaining immutable metadata. | PASS |
| `th14_sql_suppression_batches` | Both independent anti-joins must tolerate nullable advisory rows. The familiar SQL repair is not a one-file or constant end-to-end solution. | PASS |

## Oracle Quality And Leakage

- All 12 cases have three candidates, `max_files: 2`, and two expected files
  inside the candidate set.
- Public, feedback, and final test paths are disjoint. No weak payload is
  byte-identical to a final payload.
- Every final changes values and adds a material boundary or composition. The
  replacement final adds a multi-hop chain, state persistence, mixed-case header
  names, default-port origin equivalence, and `HEAD` behavior.
- Public cases contain no oracle-only keys, hidden test paths, feedback/final test
  names, final assertions, or patch recipes.
- Eleven cases require both expected owners. No other distractor, single owner, or
  literal constant closes an active case end to end.

## Deterministic Operators

The target APIs were called over every active prompt and candidate source set:

`derive_contract_invariants`, `contract_repair_dimension`,
`contract_repair_proposals`, and `contract_invariant_warnings`.

For all 12 cases: `invariants=0`, `dimension=unknown`, `proposals=0`, and
`warnings=0`. No current CHILI deterministic repair operator directly solves an
active case. This does not cure ADV2-001, which is an independently demonstrated
one-owner solution in the candidate design.

## Validation Preflight

Validation-only command:

```text
python -B scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures --json
```

Observed exit `0`, schema `chili.diagnosis-to-fix-fixture-validation.v3`, and
`valid=true`. All 12 public baselines passed; all 12 feedback baselines and all 12
external sealed finals failed as required. The validation branch reached this
point with no changed files.

## Input Integrity

The 42 authored inputs outside `VALIDATION/` contain 142,866 bytes. Their aggregate
SHA-256 is:

`3d9fb68d966221c108811605f41b170fbebf0189492c491590ed076a8eb1f23b`

The digest is SHA-256 over sorted UTF-8 records of:

`relative-path + NUL + lowercase(file SHA-256) + LF`

No authored input has non-ASCII bytes or a BOM. `authored_files_unchanged=true`:
the validation branch changes only `VALIDATION/adversarial_result_v2.json` and
`VALIDATION/ADVERSARIAL_REPORT_V2.md` relative to the target commit.
