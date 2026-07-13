# Dart Replacement Author Receipt

Date: 2026-07-13

Target branch: codex/fable5-diagnostic-reasoning

Starting branch tip: 83906c4cde8bf4a1e0f99223f6ed3347baa29b04

Integrated V1 fixture commit: 64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3

Frozen implementation commit: 2cc8e9d446e0ceb66abf5bf688596efb869f0133

Frozen implementation tree: 3ebcc8cb37574185c404848464a62ff06612fefe

Frozen diagnostic_reasoning.py Git blob: 0b4b1366a471dc099a4866bd5870bafc2f5524ff

## Scope

This replacement adds exactly these four files:

1. cases/th14_dart_redirect_handoffs.json
2. oracles/th14_dart_redirect_handoffs.json
3. final_oracles/th14_dart_redirect_handoffs.json
4. AUTHORS/dart_replacement_AUTHOR_RECEIPT.md

No rejected file, original Dart receipt, manifest, validation artifact, application
source, script, test outside the four paths, or project_ws file was edited or
deleted.

The branch started four validator-only commits after V1. The tracked diff from
64b32b8 to the starting tip contains only the eight existing files under
VALIDATION/. The case, oracle, final-oracle, author, and manifest bytes from V1 were
unchanged.

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

The dependency dimension is supported by CAUSAL_DIMENSION_RUBRIC: this is HTTP
wire-protocol compatibility. One owner derives the redirected method, entity, and
entity headers from the response status. The other applies the outbound credential
boundary when a redirect changes origin. The response/location model is on every
redirect path and is therefore plausible, but relative resolution and status
validation are already correct.

The public API remains unchanged in the disposable repair. Same-origin 307
behavior, relative Location resolution, body preservation, credential preservation,
and ordinary header forwarding remain green.

## Prior-Corpus Audit

The novelty review parsed every manifest and read every problem statement or prompt
in the first through thirteenth fixture corpus. For source-bearing diagnosis-to-fix
cases it also read candidate sources, feedback oracles, and sealed finals.

The comparison inventory has 14 roots, 129 prior cases, and 324
manifest/case/oracle files. Its aggregate SHA-256 is:

    5deda0f1888a2e37cc44c9cccd334c3121c3c27cd34b1316bc6027583dc816a0

The aggregate is SHA-256 over sorted UTF-8 records:

    relative-path + NUL + lowercase(file SHA-256) + LF

The inventory comprises:

- first through eighth real-world diagnostic holdouts: 64 cases;
- disclosed autonomy_diagnosis_to_fix development fixture: 13 cases;
- ninth and tenth diagnosis-to-fix holdouts: 8 cases each;
- eleventh, twelfth, and thirteenth diagnosis-to-fix holdouts: 12 cases each.

### Generation Comparisons

| Corpus | Mechanism families reviewed |
|---|---|
| First | Early-return routing, leading-zero coercion, host clock skew, stale lease ownership, settings overlay precedence, expired certificate chain, inode exhaustion, and leaked mock callbacks. |
| Second | Normalize-before-validation control flow, trusted-proxy hop count, incomplete publisher manifests, cross-snapshot cursors, platform package skew, overlay MTU, check-then-send races, and stale deployment membership. |
| Third | Metadata disposal order, Unicode identifier punctuation, producer-time versus broker order, mismatched recovery snapshots, whitespace-bearing principals, transitive package defaults, loaded-module uncertainty, and unpinned visual baselines. |
| Fourth | Container memory ceilings, folded calendar parsing, orphan workflow transitions, cursor selection after filtering, leaked browser state, leading-zero stop IDs, topic-filter normalization, and offset-free wall-time latency. |
| Fifth | WebVTT note-state parsing, repeated local-hour arithmetic, non-atomic marker/intent state, half-open intervals, harness readiness, reset producer IDs, SNI ambiguity, and descriptor ceilings. |
| Sixth | Duplicate facility identity, bad time sources, ownerless reservations, zero-valued overrides, missing native libraries, interval overlap, unclassified handle growth, and capture-rig attribution. |
| Seventh | Reused trip identity, relay deadlines, decoder channel order, stale debounce snapshots, restored suppression fences, host-pressure evidence gaps, resumed-host wall skew, and observer-context contamination. |
| Eighth | Unit metadata, prefix configuration, GIS axis order, natural sorting, epoch reconciliation, swap contention, unmeasured gateway clock state, and unretained printer protocol bytes. |
| Development diagnosis-to-fix | Injected clocks and TTL refresh, flag precedence, source/sink contracts, reservation dedupe, async single-flight rejection, abort propagation, subscription cancellation, partial uniqueness, and sibling aggregation. |
| Ninth | Explicit-value precedence, matrix attribution, workspace ownership, UTF-8 stream boundaries, omitted versus explicit null, equal-time compound event position, retained history, and exact identifier representation. |
| Tenth | Canonical query/key windows, scoped idempotency, Vary isolation, retry budgets, vector-clock joins, inclusive chunk ranges, temporal grant bounds, and out-of-order telemetry correction. |
| Eleventh | Lifecycle epochs, offline rebase barriers, ordered cache identity, JSON sequence/channel assembly, pre-abort generations, policy algebra, atomic transfer, reconnect fencing, client rotation, UTF-8 envelopes, monotonic document heads, and settlement transitions. |
| Twelfth | Fixed-point apportionment, release-reader retirement, trusted inbound proxy chains, canonical Base64URL, policy snapshots, TLS client auth, replacement reload, tail checkpoints, unordered hierarchies, tri-state overrides, tenant ownership, and archive transitions. |
| Thirteenth | Report-schema/license adaptation, offset-transition schedules, portable export names, null/zero rollups, attempt fencing, compression configuration, keyword-only factory binding, month-end scheduling, teardown precedence, configured delimiters, guarded job state, and unit-normalized volume. |

The V1 fourteenth peers were also reviewed. They cover compound keyset traversal,
SemVer, WebSocket fragmentation, ESM export resolution, HTTP preconditions,
partition watermarks, context propagation, decorated-handler awaitability, Link
grammar, literal SQL search, non-destructive registry refresh, and nullable
anti-joins.

### Collision Checks

The rejected th14_dart_keyset_pagination and ninth
dart-equal-time-event-order both require a complete compound event position
(time, secondary identity) for ordering and continuation. Their assertions use
ordered event identities and gap-free traversal across a cursor or batch boundary.

This replacement has no timestamp order, tie-break identity, cursor, page,
monotonic sequence, or once-only traversal assertion. It derives a new outbound
request from an HTTP redirect status and origin, and asserts method/body/header
behavior across a response chain. It is not semantically adjacent to either
compound-position case or the fourth holdout's cursor-after-filtering case.

The closest lexical and protocol surfaces were checked separately:

- Fourth bh4-405 mentions a browser login redirect, but its mechanism is
  test-harness state contamination and insufficient attribution, not HTTP redirect
  request derivation.
- Twelfth trusted-proxy handling derives an inbound client origin from forwarded
  hops; this case governs outbound credentials and status-specific request changes.
- Fourteenth HTTP preconditions parses entity-tag lists and weak/strong comparison.
- Fourteenth Link pagination parses Link header grammar and relative references.
  Relative Location resolution is deliberately healthy here and remains in the
  distractor.

A targeted case scan found zero prior matches for HTTP redirect status semantics,
See Other, cross-origin/cross-authority redirect credentials, or 303/307/308
redirect behavior. A generic redirect scan found only the seven references inside
bh4-405 described above.

An exact comparison against all 141 manifest-listed reference cases, including V1
fourteenth, inspected 421 embedded source/test payloads:

    reference_roots=15
    reference_cases=141
    reference_payloads=421
    fresh_payloads=6
    case_id_matches=0
    normalized_prompt_matches=0
    exact_payload_matches=0

## Deterministic Registry Audit

The current frozen registry contains 44 invariant templates and 36 repair
operators. Its validator-recorded canonical inventory SHA-256 is:

    06aed5350e2e5e26c2fea9f389172d534750b3d531a82d54460339d19a603954

derive_contract_invariants, every _recognize_* and _repair_* family,
contract_repair_dimension, contract_repair_proposals, and
contract_invariant_warnings were reviewed.

Direct calls over the new prompt and its three candidate files returned:

    invariants= []
    dimension= unknown
    proposals= {}
    warnings= []

The command set PYTHONDONTWRITEBYTECODE=1 and a non-routable validator
DATABASE_URL. It imported the frozen pure registry APIs directly. It did not run the
diagnosis benchmark or any model.

## Fixture Structure

A PowerShell ConvertFrom-Json check produced:

    STRUCTURE_OK
    case keys: case_id, language, test_runner, prompt, candidate_paths, max_files, repo_files
    oracle keys: case_id, expected_dimension, expected_files, feedback_files
    final keys: case_id, final_files
    candidates=3
    expected owners=2
    max_files=2
    public/feedback/final path overlap=0
    ASCII/BOM errors=0

The public case contains only tests/public_test.dart. It contains no feedback or
final path, owner label, expected dimension, hidden value, TODO, solution comment,
or patch recipe. Feedback and final use different paths and different bytes.

## Direct Dart Validation

SDK:

    Dart SDK version: 3.11.1 (stable) (Tue Feb 24 00:03:07 2026 -0800) on "windows_x64"

Each variant was materialized outside the repository. The direct commands were:

    dart format --output=none --set-exit-if-changed lib tests
    dart analyze lib tests
    dart run tests/public_test.dart
    dart run tests/feedback_test.dart
    dart run tests/final_test.dart

Output SHA-256 values below hash UTF-8 stdout/stderr lines joined with LF after
trailing whitespace removal.

### Packed Baseline

| Command | Exit | Result | Output SHA-256 |
|---|---:|---|---|
| Format | 0 | 6 files, 0 changed | 1ed94a7a6ac9347c5810d8452cab72199620c875876c040fb7a05e3ca43a1715 |
| Analyze | 0 | No issues found | 96963b00e092d9d0715a3b6434315da8979863bb157a7c4f8eb4198c454310af |
| Public | 0 | public tests passed | 92d793eda68815ff3282f4175bb8f235aa1201faacdab63cd2aecd04d8b54843 |
| Feedback | 255 | see-other changes a write into a read | 5a935a2cb494a137f0ddb544e83aff69dab451d11d1b650181413bcb076b3990 |
| Final | 255 | derived method survives a later redirect | bb34a1b33783487f27261ed4cf05f60bc0eef0d0233741c8885f5bb9fd729f1e |

### Disposable Coordinated Repair

The repair changed only the two expected owners and preserved every class,
constructor, method signature, and return type.

| Command | Exit | Result | Output SHA-256 |
|---|---:|---|---|
| Format | 0 | 6 files, 0 changed | 1ed94a7a6ac9347c5810d8452cab72199620c875876c040fb7a05e3ca43a1715 |
| Analyze | 0 | No issues found | 96963b00e092d9d0715a3b6434315da8979863bb157a7c4f8eb4198c454310af |
| Public | 0 | public tests passed | 92d793eda68815ff3282f4175bb8f235aa1201faacdab63cd2aecd04d8b54843 |
| Feedback | 0 | feedback tests passed | fd5c36f88c2ec8344d93fab0106eb99aabf8cf4031f502919225b1990bee8a46 |
| Final | 0 | final tests passed | d4c064829013f480abe7489b3e21866ed4946a576f626ff9c4b164f010489899 |

### One-Owner Ablations

Both ablations also passed format and analyze with exit 0 and kept public at exit
0.

| Repair retained | Feedback | Final | Exposed missing owner |
|---|---|---|---|
| redirect_request.dart only | exit 255, credentials do not cross authority | exit 255, case-insensitive credentials remain outside the new authority | redirect_follower.dart |
| redirect_follower.dart only | exit 255, see-other changes a write into a read | exit 255, derived method survives a later redirect | redirect_request.dart |

The baseline and coordinated source hashes make the ownership proof explicit:

| Source | Packed SHA-256 | Coordinated SHA-256 |
|---|---|---|
| lib/redirect_request.dart | ba0a6796be512551a6bcdab9dad663eb986e810bb5f95b018202e0ff27a6f3ba | f9a8d241786771157811350def019ac534350038cf17238fa8b33a89000d0ae9 |
| lib/redirect_follower.dart | 43385e4f4736ebb113a4be38aee6e88af37d840700cd7d1284e25bbd00a06e72 | af0ad45f5c787a24126dcd1152108201b4b7c4f8331b491ce341fe5573151fe6 |
| lib/redirect_response.dart | 0d6fde577c303b9167f9cb63c90ab897f65b7c90ff681fbd1171c80560e2deff | 0d6fde577c303b9167f9cb63c90ab897f65b7c90ff681fbd1171c80560e2deff |

The distractor is byte-identical in baseline and solution. The case cannot be
closed by a literal constant or syntax edit: it needs status-conditional entity
derivation, case-insensitive header classification, origin comparison with default
ports, and cross-hop state preservation in two files.

## Final-Oracle Strength

Feedback discloses two independent one-hop boundaries: POST under 303 and
credentials under a cross-authority 307. The separate final adds:

- a 303 followed by a relative 308;
- PATCH-to-GET derivation retained across the second hop;
- entity absence that cannot be restored by a later preserving status;
- mixed-case authorization, cookie, proxy authorization, and entity headers;
- an explicit default HTTPS port that must compare as the same origin;
- HEAD behavior under 303;
- ordinary metadata retained through the complete chain.

The decoded test payloads are distinct:

| Test | Bytes | SHA-256 |
|---|---:|---|
| Public | 1251 | 57eb86b8e2faeaba7c7a61c0aadc5e9e368f0eb1f4ff8a9e1a7a2dec45a9fba5 |
| Feedback | 2395 | 3416bc64600b408dc8012280a75c1c4093c83dc647143fa5d228eca8e5e4c218 |
| Final | 3109 | b2a9852b50930072177d0fbf308b839cc70959ef3f2de95b396c234ed47c2460 |

## Authored Artifact Hashes

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| cases/th14_dart_redirect_handoffs.json | 4955 | 341e60251684055963e94b350f4d08a387dbbafb310196921322183b95011418 |
| oracles/th14_dart_redirect_handoffs.json | 2716 | 895add9c4ee93bb1ffe914b47ee9a9ec092815c80d97c5e40fe92bd86ce1e48a |
| final_oracles/th14_dart_redirect_handoffs.json | 3313 | 08df7e56fe05393631b6927e81ccd7bbf40912f44265a1244bb79780e95a7e84 |

Triple aggregate SHA-256:

    66aec0bb2a036a271590f710c6bbfa227fe5e02412dc660ab100cb315957c0fd

It uses the same sorted path + NUL + hash + LF definition as the prior-corpus
inventory.

No CHILI diagnosis benchmark, Ollama, Claude, Fable 5, other coding model, network
service, or package download was invoked. All coordinated-repair and ablation work
was confined to disposable decoded copies outside the repository.
