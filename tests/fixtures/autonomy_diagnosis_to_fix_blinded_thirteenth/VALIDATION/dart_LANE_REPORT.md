# Dart Validation Lane Report

## Verdict

**PASS**

Exactly three Dart cases were validated. Every assigned gate was exercised and passed. The author root was read only. All decoded repositories, repair copies, owner-revert copies, scripts, and evidence were written beneath `D:\dev\chili-thirteenth-validation-lane-dart\tmp`.

| Case ID | Dimension | Candidate / expected owners | `max_files` |
|---|---|---:|---:|
| `th13_dart_dependency_report` | `dependency` | 2 / 2 | 2 |
| `th13_dart_offset_schedule` | `clock` | 2 / 2 | 2 |
| `th13_dart_portable_exports` | `code` | 2 / 2 | 2 |

## Structural Gates

For every case, `case.json`, `oracle.json`, and `final_oracle.json` parsed successfully. Directory identity and both oracle identities equal `case_id`; the exact test maps are `tests/public_test.dart`, `tests/feedback_test.dart`, and `tests/final_test.dart`. Language and runner are `dart`.

All artifact bytes, embedded source/test contents, and paths are ASCII. Every embedded path is slash-normalized, relative, contains no `.` or `..` segment, and remains contained when materialized. The physical author inventory contains no symbolic links, and no materialized source/test path is a link.

Each case has exactly two plausible `lib/*.dart` candidates. Its two expected owners are unique, equal the candidate set, present in `repo_files`, and fit `max_files: 2`.

Public metadata, source, and public tests contain none of the oracle field labels, hidden test paths, complete hidden tests, or hidden assertion messages. A diagnostic initially matched `Summary`, but inspection showed it is a public input reused by a hidden test, not a hidden assertion label or hidden-test leak. The actual final argument of every hidden `check(...)` call was checked and is absent from the public bundle.

## Execution Evidence

The completed harness command was:

```text
cwd D:\dev\chili-thirteenth-validation-lane-dart
dart tmp\validate_lane.dart
exit 0
wrote D:\dev\chili-thirteenth-validation-lane-dart\tmp\validation_evidence.json
```

Every test process had a 10-second timeout; none timed out. In each scenario directory the exact commands were:

```text
dart tests/public_test.dart
dart tests/feedback_test.dart
dart tests/final_test.dart
```

Exit `0` outputs were respectively `public tests passed`, `feedback tests passed`, and `final tests passed`. Exit `255` outputs began `Unhandled exception:` followed by the `Bad state:` text shown below.

### `th13_dart_dependency_report`

| Fresh scenario | Public | Feedback | Final |
|---|---|---|---|
| baseline | `0` pass | `255` reads artifacts from upgraded reports | `255` keeps all upgraded dependency records |
| both owners repaired | `0` pass | `0` pass | `0` pass |
| revert `lib/scan_report_adapter.dart` | `0` pass | `255` reads artifacts from upgraded reports | `255` keeps all upgraded dependency records |
| revert `lib/license_gate.dart` | `0` pass | `255` an allowed alternative satisfies a compound expression | `255` evaluates alternatives, conjunctions, and scope |

The repair adds upgraded `document.artifacts` decoding while retaining legacy `components`, plus precedence-aware parenthesized `AND`/`OR` evaluation. Each declared owner is independently necessary.

Final novelty is material: feedback covers one upgraded record and a simple `OR`; final composes three upgraded records, nested `AND` inside `OR`, parentheses, a failing runtime record, and development-scope exclusion.

### `th13_dart_offset_schedule`

| Fresh scenario | Public | Feedback | Final |
|---|---|---|---|
| baseline | `0` pass | `255` the new offset starts at the transition instant | `255` a wall-clock target created by the jump resolves to the transition instant |
| both owners repaired | `0` pass | `0` pass | `0` pass |
| revert `lib/offset_schedule.dart` | `0` pass | `255` the new offset starts at the transition instant | `255` a wall-clock target created by the jump resolves to the transition instant |
| revert `lib/daily_window.dart` | `0` pass | `255` the next wall-clock run uses the offset in effect at that run | `255` a wall-clock target created by the jump resolves to the transition instant |

The repair makes a change effective at its exact UTC instant and resolves a local target against the offset effective at the target. Each owner is independently necessary.

Final novelty is material: feedback separately checks transition inclusion and a later `09:00` target; final composes both owners at the discontinuity by resolving the newly created `03:00` wall time to the exact transition instant.

### `th13_dart_portable_exports`

| Fresh scenario | Public | Feedback | Final |
|---|---|---|---|
| baseline | `0` pass | `255` device names must be made portable | `255` a device basename remains reserved when it has an extension |
| both owners repaired | `0` pass | `0` pass | `0` pass |
| revert `lib/export_name.dart` | `0` pass | `255` device names must be made portable | `255` a device basename remains reserved when it has an extension |
| revert `lib/report_bundle.dart` | `0` pass | `255` case-only variants share a collision domain | `255` collisions introduced by normalization and casing are suffixed |

The repair strips terminal dots/spaces, protects reserved Windows device basenames including extension-bearing names, and allocates in a case-insensitive collision domain. Each owner is independently necessary.

Final novelty is material: feedback uses bare `CON`, terminal punctuation, and a case-only duplicate; final adds an extension-bearing reserved basename and a collision produced jointly by segment normalization and case folding.

## Cross-Case Review

No pair duplicates a mechanism, source skeleton, assertion family, or final boundary:

| Case | Mechanism | Assertion family | Final boundary |
|---|---|---|---|
| dependency report | nested schema adaptation plus Boolean expression parser | decoded records, logical satisfaction, runtime filtering | nested `AND`/`OR` composition across scopes |
| offset schedule | inclusive temporal transition plus wall-time inversion | `DateTime`/`Duration` equality | target resolves exactly to offset discontinuity |
| portable exports | Windows segment normalization plus collision allocation | string/path equality and sequential allocation | reserved basename with extension plus normalized case collision |

No case materially overlaps fixed-point apportionment, release-reader retirement, trusted proxy CIDR chains, canonical base64url, request policy snapshots, TLS client authentication, replacement config reload, source-aware tail checkpoints, unordered category hierarchy, tri-state override SQL, composite tenant stock ownership, or ticket archive/move accounting.

## Author Integrity

Canonical inventory format: slash-normalized relative path, TAB, byte length, TAB, lowercase file SHA-256, LF; lines sorted ascending by path. Before and after inventories are identical.

**Sorted inventory SHA-256 before:** `e27a71a46e092e1033b295bee05500d9d188c518dfb35866519ff16bb538ce49`  
**Sorted inventory SHA-256 after:** `e27a71a46e092e1033b295bee05500d9d188c518dfb35866519ff16bb538ce49`

| Author file | Bytes | SHA-256 before | SHA-256 after |
|---|---:|---|---|
| `AUTHOR_RECEIPT.md` | 3602 | `cb4df78902ca5cc7613687ec3e486b1ea6589e44c7eedf9b7a53387429924ce9` | same |
| `th13_dart_dependency_report/case.json` | 3621 | `c843b3a4c89fbd76a130941f7497bd577c0d222a3028919849945d4804eb83e1` | same |
| `th13_dart_dependency_report/final_oracle.json` | 1568 | `50631c7489a3c74621a002d978f1181fe298158e0e54abd20b248c813aa7e142` | same |
| `th13_dart_dependency_report/oracle.json` | 1532 | `ef029d99b0be51028bec790edb6c32c109d5a9a7d24a9b000e2c7ccdf3ed6efd` | same |
| `th13_dart_offset_schedule/case.json` | 4141 | `d499a3740aceafe6fcc571cb6472d396a6312b7ae254a1bb02a4ab3976407500` | same |
| `th13_dart_offset_schedule/final_oracle.json` | 770 | `9b772a97de211b81fabe7cc72c704d162260728673b05c6d9a9119646a4ed679` | same |
| `th13_dart_offset_schedule/oracle.json` | 1055 | `57724ffe83f033b9bad01c0e4488436e459214893a7fda14c42d4670cd2c6506` | same |
| `th13_dart_portable_exports/case.json` | 2348 | `1d5101622bf012f0fdd1c60bf834de667a04bee28ed65c5bbc1c5690c7488a9a` | same |
| `th13_dart_portable_exports/final_oracle.json` | 802 | `d397532681ee5b69d938da4831e0244d0de162563996197b54b81049d3275c46` | same |
| `th13_dart_portable_exports/oracle.json` | 956 | `aa7d390219813b95306fc61f7df2c5463d5843019f832f74f2ec87a81533d5be` | same |

The post-snapshot command exited `0` and reported `file_count_before=10; file_count_after=10; identical=True`.

## Non-Gate Events

An optional formatter check on the dependency repair exited `1` solely because `dart format --output=none --set-exit-if-changed lib tests` preferred a ternary line wrap; all three required repaired tests exited `0`. Formatting was not an assigned gate.

A second full harness invocation was interrupted by the user after 5.8 seconds while regenerating temporary copies. It was discarded and contributes no evidence. The completed 14.8-second invocation above is the recorded validation run.

## Source Skeleton Supplement

This supplement preserves the PASS verdict and prior test evidence. Skeletons were computed only from each candidate's baseline source text embedded in `case.json`.

### Deterministic normalization

Normalization version: `dart-lexical-skeleton-v1`.

1. Scan source left to right and remove `//` and `/* ... */` comments outside literals.
2. Replace each complete raw or non-raw single-, double-, or triple-quoted string literal, including its payload, with token `STR`.
3. Replace each decimal/hexadecimal-style numeric token, including underscore, decimal, and exponent payload characters, with token `NUM`.
4. Preserve Dart control/declaration/context tokens from this fixed set: `abstract as assert async await break case catch class const continue covariant default deferred do dynamic else enum export extends extension external factory false final finally for get hide if implements import in interface is late library mixin new null of on operator part required rethrow return sealed set show static super switch sync this throw true try typedef var void when while with yield`.
5. Map every other leading-uppercase identifier to `TYPE`, every underscore-leading identifier to `PRIVATE`, and every other identifier to `ID`.
6. Preserve operators and punctuation as lexical tokens, then join all tokens with one ASCII space and no trailing newline. Hash the normalized UTF-8 bytes with SHA-256.

For a combined case skeleton, sort candidate paths ascending and concatenate, for each candidate, `@@PATH <path><LF><normalized skeleton><LF>`. Hash that complete UTF-8 byte sequence with SHA-256. Per-candidate hashes do not include path framing.

### Case fingerprints

#### `th13_dart_dependency_report`

- **dimension:** `dependency`
- **mechanism:** dual-schema scan-report adaptation plus precedence-aware Boolean license-expression evaluation
- **assertion_family:** decoded record count/fields, expression satisfaction and violation membership, and runtime-scope filtering
- **feedback_boundary:** one upgraded `document.artifacts` record with nested coordinates/license and runtime scope, plus a simple `OR` satisfied by one allowed license
- **final_boundary:** three upgraded records with parenthesized `AND` nested beneath `OR`, exactly one unsatisfied runtime expression, and development-scope exclusion
- **final_novelty:** composes upgraded nested decoding with grouped `AND`/`OR` evaluation and scope filtering; it is not a renamed or trivial equivalent of feedback
- **combined_source_skeleton_sha256:** `a247b5e8afcece29db174cfee8ca72bd15b058fb1974c7823990023c9bb0d839`

| Candidate baseline source | Tokens | `source_skeleton_sha256` |
|---|---:|---|
| `lib/license_gate.dart` | 96 | `14f8975f34f2f46a9f485c0d68ad91adfd3078fdd7267f0a9f3f4a603530c7a5` |
| `lib/scan_report_adapter.dart` | 236 | `84a9b2d730b39c05ae090b28a869923242499d119775d760a38d5450b57ffe5f` |

#### `th13_dart_offset_schedule`

- **dimension:** `clock`
- **mechanism:** inclusive offset-transition lookup plus target-time local-wall-clock to UTC resolution
- **assertion_family:** exact `Duration` offset equality and exact `DateTime` next-run equality around a discontinuity
- **feedback_boundary:** the new offset at the exact transition and a later `09:00` wall-clock target resolved with its effective offset
- **final_boundary:** the newly created `03:00` wall-clock target resolves to the exact UTC transition instant
- **final_novelty:** composes transition inclusion and target-time resolution at the discontinuity itself; it is not a renamed or trivial equivalent of feedback
- **combined_source_skeleton_sha256:** `621e4119b0b4ea4eab1b10657f61c969c8442cc67c2fb73bf3260f95023a2cf4`

| Candidate baseline source | Tokens | `source_skeleton_sha256` |
|---|---:|---|
| `lib/daily_window.dart` | 210 | `8f416afa561c084da8838224498d844f53e06cfc2b6c1de9ade65a9164cf4533` |
| `lib/offset_schedule.dart` | 201 | `a313daedc79fe50f6c46d70b65ce267e3c31b017c0db60c95a880fb8f0246b25` |

#### `th13_dart_portable_exports`

- **dimension:** `code`
- **mechanism:** Windows segment normalization plus case-insensitive deterministic entry allocation
- **assertion_family:** exact normalized-string equality and sequential allocated-path equality
- **feedback_boundary:** a bare reserved device name, terminal dots/spaces, and a case-only duplicate
- **final_boundary:** an extension-bearing reserved basename plus a collision created jointly by normalization and case folding
- **final_novelty:** composes both source owners through normalization-induced collision behavior and extends reservation to basename-with-extension; it is not a renamed or trivial equivalent of feedback
- **combined_source_skeleton_sha256:** `a9f6207937b8a207b44c9e89b82aa481a63a8d72ca54181fddccf7e8dcfe754d`

| Candidate baseline source | Tokens | `source_skeleton_sha256` |
|---|---:|---|
| `lib/export_name.dart` | 41 | `fc97b080a3cbe97e7a504ef6756504909572c465f7feddf6f96fbcbfcf5575b4` |
| `lib/report_bundle.dart` | 91 | `0c38a811cc8e519ab7283061438e3b48d6c6df0f60612a633560b714e1b5ec28` |

The six per-candidate skeleton hashes and three combined hashes are all distinct.

### Current author inventory recheck

The current complete author inventory was recomputed after the skeleton pass. It has 10 files and canonical inventory SHA-256 `e27a71a46e092e1033b295bee05500d9d188c518dfb35866519ff16bb538ce49`, exactly matching both prior before and prior after inventories.

| Current author file | Bytes | Current SHA-256 | Matches prior before/after |
|---|---:|---|---|
| `AUTHOR_RECEIPT.md` | 3602 | `cb4df78902ca5cc7613687ec3e486b1ea6589e44c7eedf9b7a53387429924ce9` | yes / yes |
| `th13_dart_dependency_report/case.json` | 3621 | `c843b3a4c89fbd76a130941f7497bd577c0d222a3028919849945d4804eb83e1` | yes / yes |
| `th13_dart_dependency_report/final_oracle.json` | 1568 | `50631c7489a3c74621a002d978f1181fe298158e0e54abd20b248c813aa7e142` | yes / yes |
| `th13_dart_dependency_report/oracle.json` | 1532 | `ef029d99b0be51028bec790edb6c32c109d5a9a7d24a9b000e2c7ccdf3ed6efd` | yes / yes |
| `th13_dart_offset_schedule/case.json` | 4141 | `d499a3740aceafe6fcc571cb6472d396a6312b7ae254a1bb02a4ab3976407500` | yes / yes |
| `th13_dart_offset_schedule/final_oracle.json` | 770 | `9b772a97de211b81fabe7cc72c704d162260728673b05c6d9a9119646a4ed679` | yes / yes |
| `th13_dart_offset_schedule/oracle.json` | 1055 | `57724ffe83f033b9bad01c0e4488436e459214893a7fda14c42d4670cd2c6506` | yes / yes |
| `th13_dart_portable_exports/case.json` | 2348 | `1d5101622bf012f0fdd1c60bf834de667a04bee28ed65c5bbc1c5690c7488a9a` | yes / yes |
| `th13_dart_portable_exports/final_oracle.json` | 802 | `d397532681ee5b69d938da4831e0244d0de162563996197b54b81049d3275c46` | yes / yes |
| `th13_dart_portable_exports/oracle.json` | 956 | `aa7d390219813b95306fc61f7df2c5463d5843019f832f74f2ec87a81533d5be` | yes / yes |
