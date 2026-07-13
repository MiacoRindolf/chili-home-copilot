# Dart Replacement Author Receipt

Date: 2026-07-13

Target branch: codex/fable5-diagnostic-reasoning

V3 starting branch tip: 5762ca76a005202de6111d706c5218260e317070

V3 starting tree: 60d3f1be570e3e7102c4a3bcfb134f3d11e86000

Integrated V1 fixture commit: 64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3

Replacement authoring commit: 3de05be7f077de78d26034ae8e82563f3097a232

Replacement activation commit: 86d328b7f136ebfbc6f3ace508dd53b401a04939

Frozen implementation commit: 2cc8e9d446e0ceb66abf5bf688596efb869f0133

Frozen implementation tree: 3ebcc8cb37574185c404848464a62ff06612fefe

Frozen diagnostic_reasoning.py Git blob:
0b4b1366a471dc099a4866bd5870bafc2f5524ff

## V3 Scope

This hardening modifies exactly these four existing replacement-author files:

1. cases/th14_dart_redirect_handoffs.json
2. oracles/th14_dart_redirect_handoffs.json
3. final_oracles/th14_dart_redirect_handoffs.json
4. AUTHORS/dart_replacement_AUTHOR_RECEIPT.md

No manifest, VALIDATION artifact, rejected fixture, original Dart receipt,
application source, source capability, script, test outside the four paths, or
project_ws file was edited or deleted.

Commit 3de05be introduced these four replacement-author files. Commit 86d328b
activated the replacement and removed the rejected keyset triple. From 86d328b
through the V3 starting tip, commits ea58015, 3072970, 6b4c8f6, and 5762ca7 add
only the eight preserved V2 VALIDATION report/result files. The V3 fixture baseline
continues to use the same three embedded library source payloads as 3de05be. V3
changes only the prompt, public test, feedback test, final test, and this receipt
inside the four owned files.

## V2 Findings Addressed

The preserved reports were read before authoring:

- VALIDATION/SEMANTIC_REPORT_V2.md
- VALIDATION/ADVERSARIAL_REPORT_V2.md
- VALIDATION/INTEGRITY_REPORT_V2.md

SEM-V2-001 and ADV2-001 showed that the V2 hidden tests did not require
lib/redirect_follower.dart. RedirectPolicy.derive already receives the current
request, status, and target, so one plausible change in lib/redirect_request.dart
could perform both status derivation and origin filtering.

V3 closes that alternative with two independent observation surfaces:

1. Direct RedirectPolicy().derive calls require 303 method, body, and entity-header
   derivation. A follower-only repair cannot affect these calls.
2. Injected RedirectPolicy subclasses override derive and return fresh complete
   requests containing mixed-case credentials. RedirectFollower must inspect the
   returned request after dispatch and enforce the origin boundary. A base
   RedirectPolicy.derive change is bypassed and cannot affect these requests.

The public tier establishes the compatible ordinary behavior: an injected policy
may return a credential-bearing complete request on a same-origin redirect. The
final tier also treats an omitted HTTPS port and explicit port 443 as the same
origin. Consequently, globally deleting credentials in RedirectRequest construction
or copyWith is not a valid centralization.

INT-V2-001 found that the prior receipt's declared triple aggregate
66aec0bb2a036a271590f710c6bbfa227fe5e02412dc660ab100cb315957c0fd
did not follow its documented formula. Applied to the V2 artifact hashes, that
formula produces:

    769c2ede2a2c29d35bb83f5badc2602820e4b0d5eb50c123a1d3d976eaf20caa

The V3 artifacts have new hashes and a new independently reproduced aggregate,
recorded below.

## Delivered Case

| Field | Value |
|---|---|
| Case ID | th14_dart_redirect_handoffs |
| Language / runner | Dart / dart |
| Expected dimension | dependency |
| Expected owner 1 | lib/redirect_request.dart |
| Expected owner 2 | lib/redirect_follower.dart |
| Plausible unnecessary distractor | lib/redirect_response.dart |
| Candidate count | 3 |
| max_files | 2 |

The dependency dimension is supported by CAUSAL_DIMENSION_RUBRIC as an HTTP
wire-protocol compatibility boundary. RedirectPolicy owns status-dependent method
and entity derivation. RedirectFollower owns enforcement over the request returned
by a replaceable policy dependency. RedirectResponse is plausible because every
hop passes through its status and Location resolution, but that behavior is already
correct and the file is byte-identical in baseline and repair.

Public classes, constructors, method signatures, and return types stay unchanged.
Same-origin 307 behavior, relative Location resolution, method/body preservation,
same-origin credentials, injected policy output, and ordinary headers remain green.

## Prior-Corpus Novelty

The replacement audit parsed every manifest and read every prompt, candidate
source, feedback oracle, and final oracle available in the first through thirteenth
diagnosis-to-fix corpora. Those corpus bytes are unchanged at the V3 starting tip.
The inventory covers 14 roots, 129 prior cases, and 324 manifest/case/oracle files.
Its aggregate SHA-256 is:

    5deda0f1888a2e37cc44c9cccd334c3121c3c27cd34b1316bc6027583dc816a0

That aggregate uses sorted UTF-8 records:

    relative-path + NUL + lowercase(file SHA-256) + LF

The mechanism comparison was:

| Corpus | Mechanism families reviewed |
|---|---|
| First | Early-return routing, leading-zero coercion, host clock skew, stale lease ownership, settings precedence, certificate chains, inode exhaustion, and leaked mock callbacks. |
| Second | Normalize-before-validation flow, proxy hop counts, publisher manifests, snapshot cursors, package skew, overlay MTU, check-then-send races, and deployment membership. |
| Third | Metadata disposal, Unicode identifiers, producer versus broker order, recovery snapshots, whitespace principals, package defaults, loaded-module uncertainty, and visual baselines. |
| Fourth | Memory ceilings, calendar folding, workflow transitions, cursor-after-filtering, browser-state leakage, stop IDs, topic filters, and wall-time latency. |
| Fifth | WebVTT state, repeated local hours, marker/intent atomicity, half-open intervals, readiness, producer IDs, SNI, and descriptor ceilings. |
| Sixth | Facility identity, time sources, reservations, zero overrides, native libraries, interval overlap, handle growth, and capture attribution. |
| Seventh | Trip identity, relay deadlines, channel order, debounce snapshots, suppression fences, host pressure, resumed-host skew, and observer context. |
| Eighth | Unit metadata, prefix configuration, GIS axes, natural sorting, epoch reconciliation, swap contention, gateway clock evidence, and printer bytes. |
| Development | Injected clocks, flag precedence, source/sink contracts, reservation dedupe, async single-flight rejection, abort propagation, cancellation, partial uniqueness, and sibling aggregation. |
| Ninth | Explicit-value precedence, matrix attribution, workspace ownership, UTF-8 boundaries, omitted versus null, equal-time compound positions, retained history, and exact identifiers. |
| Tenth | Canonical windows, idempotency scope, Vary isolation, retry budgets, vector clocks, chunk ranges, grant bounds, and telemetry correction. |
| Eleventh | Lifecycle epochs, rebase barriers, cache identity, sequence assembly, abort generations, policy algebra, atomic transfer, reconnect fencing, client rotation, UTF-8 envelopes, document heads, and settlement transitions. |
| Twelfth | Fixed-point allocation, reader retirement, proxy chains, Base64URL, policy snapshots, TLS auth, replacement reload, tail checkpoints, unordered hierarchies, tri-state overrides, tenant ownership, and archive transitions. |
| Thirteenth | Schema/license adaptation, offset schedules, export names, null/zero rollups, attempt fencing, compression, factory binding, month-end schedules, teardown, delimiters, guarded jobs, and unit-normalized volume. |

The rejected th14_dart_keyset_pagination and ninth equal-time event case both need
a complete compound event position for ordering and continuation. This case has no
timestamp order, secondary identity, cursor, page, sequence, or once-only traversal
contract. Its mechanism is HTTP redirect request derivation plus enforcement after
a replaceable policy returns.

The closest surfaces remain distinct:

- Fourth bh4-405 mentions a login redirect, but diagnoses browser harness state
  contamination and attribution.
- Twelfth trusted-proxy handling derives an inbound client origin from forwarded
  hops; this case governs outbound redirected requests.
- Fourteenth HTTP preconditions parses entity tags.
- Fourteenth Link pagination parses Link grammar; relative Location resolution is
  deliberately healthy here.

The V3 injected subclasses are an isolation method, not a reused mechanism. Prior
injected-clock and cancellation fixtures do not enforce a post-dependency HTTP
credential boundary. No prior case combines status-specific 303 entity derivation,
cross-origin credential handling, and enforcement over complete injected policy
output.

## Deterministic Registry Audit

The frozen registry inventory remains 44 invariant templates and 36 repair
operators, with canonical SHA-256:

    06aed5350e2e5e26c2fea9f389172d534750b3d531a82d54460339d19a603954

The updated V3 prompt and its three candidate sources were passed directly to
derive_contract_invariants, contract_repair_dimension,
contract_repair_proposals, and contract_invariant_warnings with
PYTHONDONTWRITEBYTECODE=1 and a non-routable validator DATABASE_URL. Results:

    invariants= []
    dimension= unknown
    proposals= {}
    warnings= []

This imported the deterministic pure registry functions only. It did not run the
CHILI diagnosis benchmark or any model.

## Fixture Structure

Structured JSON checks produce:

    STRUCTURE_OK
    case keys: case_id, language, test_runner, prompt, candidate_paths, max_files, repo_files
    oracle keys: case_id, expected_dimension, expected_files, feedback_files
    final keys: case_id, final_files
    candidates=3
    expected owners=2
    max_files=2
    public/feedback/final path overlap=0
    ASCII/BOM/NUL errors=0

The public case contains only tests/public_test.dart. It contains no hidden path,
owner label, expected dimension, hidden value, TODO, solution comment, or repair
recipe. Feedback and final use separate paths and distinct bytes.

## Direct Dart Validation

SDK:

    Dart SDK version: 3.11.1 (stable) (Tue Feb 24 00:03:07 2026 -0800) on "windows_x64"

Every variant was decoded or copied into a disposable directory outside the
repository. Direct commands were:

    dart format --output=none --set-exit-if-changed lib tests
    dart analyze lib tests
    dart tests/public_test.dart
    dart tests/feedback_test.dart
    dart tests/final_test.dart

### Packed Baseline

| Check | Exit | Result |
|---|---:|---|
| Format | 0 | 6 files, 0 changed |
| Analyze | 0 | No issues found |
| Public | 0 | public tests passed |
| Feedback | 255 | see-other changes a write into a read |
| Final | 255 | see-other changes PATCH into GET |

### Disposable Coordinated Repair

The repair changes only the two expected owners. RedirectPolicy handles 303
method/body/entity metadata. RedirectFollower compares effective origins after
policy dispatch and removes authorization, cookie, and proxy authorization from
the returned request on every cross-origin hop.

| Check | Exit | Result |
|---|---:|---|
| Format | 0 | 6 files, 0 changed |
| Analyze | 0 | No issues found |
| Public | 0 | public tests passed |
| Feedback | 0 | feedback tests passed |
| Final | 0 | final tests passed |

### One-Owner Ablations

Both ablations pass format and analyze and retain a green public tier.

| Repair retained | Feedback | Final | Missing owner proved |
|---|---|---|---|
| redirect_request.dart only | 255: credentials from a redirect decision stay with their origin | 255: credentials from one handoff do not enter the next decision | redirect_follower.dart |
| redirect_follower.dart only | 255: see-other changes a write into a read | 255: see-other changes PATCH into GET | redirect_request.dart |

### Explicit Request-Only Centralization

A separate adversarial implementation changed only lib/redirect_request.dart. It
implemented the complete plausible V2 alternative: 303 method/entity derivation,
case-insensitive credential filtering, and scheme/host/effective-port comparison
inside RedirectPolicy.derive. The follower stayed byte-identical to baseline.

| Check | Exit | Result |
|---|---:|---|
| Format | 0 | 6 files, 0 changed |
| Analyze | 0 | No issues found |
| Public | 0 | public tests passed |
| Feedback | 255 | credentials from a redirect decision stay with their origin |
| Final | 255 | credentials from one handoff do not enter the next decision |

The direct policy assertions pass before those failures. The injected overrides
then bypass base derive and return fresh credential-bearing requests, exposing the
unchanged follower.

## Ownership Hashes

| Source | Packed bytes / SHA-256 | Coordinated bytes / SHA-256 | Centralized bytes / SHA-256 |
|---|---|---|---|
| lib/redirect_request.dart | 1476 / ba0a6796be512551a6bcdab9dad663eb986e810bb5f95b018202e0ff27a6f3ba | 2070 / 375ded6d621149ab668b826a747cbc0a4a56b97a77d795dd36840d8efd694eb0 | 2894 / ea11cbd7a818c2a7316ba519854cbd64843e48989a3beec17053706dbb7ac598 |
| lib/redirect_follower.dart | 592 / 43385e4f4736ebb113a4be38aee6e88af37d840700cd7d1284e25bbd00a06e72 | 1606 / 6538cb63683e362212b9df30c7b3ebf840c6a87b5a63fb15ab8d823842b7f3e3 | 592 / 43385e4f4736ebb113a4be38aee6e88af37d840700cd7d1284e25bbd00a06e72 |
| lib/redirect_response.dart | 541 / 0d6fde577c303b9167f9cb63c90ab897f65b7c90ff681fbd1171c80560e2deff | 541 / 0d6fde577c303b9167f9cb63c90ab897f65b7c90ff681fbd1171c80560e2deff | 541 / 0d6fde577c303b9167f9cb63c90ab897f65b7c90ff681fbd1171c80560e2deff |

The centralization follower hash exactly equals packed baseline, proving that the
attack changed one source file. The distractor is byte-identical in all variants.

## Final-Oracle Strength

Feedback independently checks a direct POST 303 derivation and one cross-origin
307 whose injected policy returns mixed-case authorization and cookie headers.

The materially stronger separate final adds:

- direct PATCH 303 policy derivation with mixed entity metadata;
- two cross-origin preserving hops;
- fresh credentials reintroduced by the injected policy on every hop;
- direct observation that first-hop credentials are absent before the second
  redirect decision, defeating an end-of-chain-only scrub;
- authorization, cookie, and proxy-authorization case variants;
- method, body, and ordinary metadata preservation through those hops;
- a normal 303 followed by a relative 308 integration chain;
- consumed entity state that cannot be restored by the preserving redirect;
- explicit default HTTPS port equivalence with same-origin injected credentials;
- direct HEAD behavior under 303.

Decoded test payloads:

| Test | Bytes | SHA-256 |
|---|---:|---|
| Public | 2285 | fefb90c5bef91f3bb3a2739d18ffa36e1985b59b6dc56716a693feab0a76cf56 |
| Feedback | 2804 | cbcd6f9675a981ef2f2526c5d4539ba698a6cb73202904c715ffdecaf1a1d33f |
| Final | 6690 | a85691b490208ea6e9147f0089f3d298dcb2aaa0cd58c90ab949649f3b4495fc |

## Authored Artifact Hashes

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| cases/th14_dart_redirect_handoffs.json | 6114 | 3774b7d6688bf6d8f2dafa0c67a4500a18f468e4b45e838bfb9eac179da7477a |
| final_oracles/th14_dart_redirect_handoffs.json | 7001 | 36d8f782b7f79b76b86d2b61edf2206ac6ad89914d50f6174e4805407b25f971 |
| oracles/th14_dart_redirect_handoffs.json | 3141 | d350364a517ef17b648c0afd3fac810c33637db032ffb02f5c3e5d76584ced8f |

The triple aggregate algorithm is exact:

1. Sort the three fixture-relative paths by ordinal ASCII bytes.
2. For each path, append UTF-8 path bytes, one NUL byte, the 64 lowercase ASCII
   SHA-256 hex bytes for that artifact, and one LF byte.
3. Concatenate the three records and SHA-256 the resulting 322 bytes.

The sorted order is cases, final_oracles, oracles. Independent Node.js and
PowerShell/.NET implementations both produced:

    3e57947e1ef6d50514eba858a862d75e4e1ab9e70d5d958b7b783ea918adfaa9

The receipt is excluded from this non-circular triple aggregate.

No CHILI diagnosis benchmark, Ollama, Claude, Fable 5, other coding model, network
service, or package download was invoked. All coordinated repair, ablation, and
adversarial work was confined to disposable decoded copies outside the repository.
