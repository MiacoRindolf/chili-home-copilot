# Dart Author Receipt

Date: 2026-07-13

Branch: `chili/momentum-concurrency-basis-independent`

Starting branch tip: `5539dd61f547e9ecc9e8e105879b35aff6e142a3`

Authoring scope: exactly three Dart case JSON files, three repair-feedback oracle JSON files, three sealed-final oracle JSON files, and this receipt under `tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth`. No manifest, index, application source, benchmark source, capability source, or pre-existing test was edited.

SDK:

```text
Dart SDK version: 3.11.1 (stable) (Tue Feb 24 00:03:07 2026 -0800) on "windows_x64"
```

## Delivered Cases

| Case | Expected dimension | Expected source owners | Plausible distractor |
|---|---|---|---|
| `th14_dart_keyset_pagination` | `data` | `lib/audit_order.dart`, `lib/audit_pager.dart` | `lib/audit_event.dart` |
| `th14_dart_semver_selection` | `dependency` | `lib/semantic_version.dart`, `lib/release_selector.dart` | `lib/package_release.dart` |
| `th14_dart_websocket_fragments` | `dependency` | `lib/websocket_decoder.dart`, `lib/websocket_messages.dart` | `lib/websocket_frame.dart` |

Every case has three plausible `lib/*.dart` candidates, exactly two unique expected owners, `max_files: 2`, one standalone public main, one separately disclosed feedback main, and one independently authored sealed-final main. Public APIs remain intact in the verified repairs.

## Mechanisms And Novelty

The novelty audit read the thirteenth manifest and all twelve thirteenth cases, plus `DIMENSIONS`, `CAUSAL_DIMENSION_RUBRIC`, `derive_contract_invariants`, every `_recognize_*`/`_repair_*` operator, `_CONTRACT_INVARIANT_DIMENSIONS`, and `contract_repair_dimension` in `app/services/project_autonomy/diagnostic_reasoning.py` at the local `codex/fable5-diagnostic-reasoning` ref.

- `th14_dart_keyset_pagination`: descending event-time plus stable record identity forms one total order, and an opaque keyset cursor must preserve that complete compound boundary. This is a `data` representation/identity defect, not the existing ordered-preference cache identity, monotonic materialized-head, vector-clock, event-time rollup, range, or clock-transition family.
- `th14_dart_semver_selection`: SemVer prerelease identifiers require stable-versus-prerelease, numeric-versus-text, and sequence-length precedence, while every clause in one compatibility range is conjunctive. This is package-version compatibility in the `dependency` dimension, distinct from report-schema/license evaluation and required factory-parameter binding.
- `th14_dart_websocket_fragments`: a stateful frame decoder must retain incomplete transport bytes and emit every coalesced frame, while the message assembler must preserve fragmented text across interleaved control frames. This is wire-protocol compatibility in the `dependency` dimension, distinct from subscription cancellation, async rejection eviction, abort propagation, byte ranges, and every thirteenth mechanism.

Searches for pagination/continuation-token, semantic-version/SemVer, and WebSocket fragmented-message families found no matching current repair operator or thirteenth case. The mechanisms, baseline source skeletons, feedback boundaries, and final compositions are mutually distinct.

## Validation Commands And Results

The JSON structural harness used PowerShell `ConvertFrom-Json`, exact property-set comparisons, path containment checks, case/oracle identity checks, candidate/owner cardinality checks, test-partition disjointness checks, hidden-message leak scans, ASCII/BOM checks, and SHA-256 inventory generation.

```text
Result: FINAL_STRUCTURE_OK
Cases: 3
JSON artifacts: 9
Case keys: case_id, language, test_runner, prompt, candidate_paths, max_files, repo_files
Oracle keys: case_id, expected_dimension, expected_files, feedback_files
Final-oracle keys: case_id, final_files
ASCII/BOM/path/leak errors: 0
```

For each JSON-decoded repository, the exact language commands were:

```text
dart format --output=none --set-exit-if-changed lib tests
dart analyze lib tests
dart tests/public_test.dart
dart tests/feedback_test.dart
dart tests/final_test.dart
```

Formatter result for each case: exit `0`, six files checked, zero changed. Analyzer result for each case: exit `0`, `No issues found!`.

### Packed Baseline

| Case | Public | Feedback | Final |
|---|---|---|---|
| `th14_dart_keyset_pagination` | `0`, `public tests passed` | `255`, equal instants retain record identity ordering | `255`, compound boundaries visit every tied record once in total order |
| `th14_dart_semver_selection` | `0`, `public tests passed` | `255`, a stable release outranks its prerelease | `255`, prerelease precedence and bounded selection compose correctly |
| `th14_dart_websocket_fragments` | `0`, `public tests passed` | `255`, retains transport bytes until the frame is complete | `255`, chunked and coalesced frames preserve message boundaries around control traffic |

### Causal-Owner Proof

Disposable decoded copies, outside the fixture tree, were used for repair verification. No repair text was added to a fixture.

- With both expected owners repaired, public, feedback, and final commands exited `0` for all three cases.
- Restoring either expected owner to its packed baseline left public at exit `0` and made both feedback and final exit `255` in all six owner-revert scenarios.
- Restoring each first owner exposed respectively total-order identity, SemVer precedence, and incremental frame buffering.
- Restoring each second owner exposed respectively compound cursor continuation, conjunctive range selection, and fragmented-message preservation across control traffic.

### Final-Test Distinction

- Keyset pagination feedback separates one tied-order comparison from a one-record continuation. Final traverses several pages containing a three-record tie, delimiter-bearing identifiers, newer and older neighbors, progress checks, and exactly-once total order.
- SemVer feedback covers stable-versus-`rc`, numeric `rc` ordering, and a stable bounded range. Final adds numeric-versus-text identifiers, prefix-length precedence, ignored build metadata, and a prerelease-only bounded selection that composes both owners.
- WebSocket feedback separately covers a split frame header and a ping between two text fragments. Final composes arbitrary transport cuts, partial and coalesced frames, an interleaved ping, continuation assembly, and a following complete message.

No CHILI benchmark, local coding model, hosted coding model, network service, package download, or external dependency was invoked.

## Authored JSON SHA-256

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `cases/th14_dart_keyset_pagination.json` | 3732 | `071e74f9c2aafe82c2ac1bf54617066a744c14681bc7bdbd863867a1c41b58bf` |
| `cases/th14_dart_semver_selection.json` | 5502 | `7012287493ce099f3f7102ed87956a3405e1dd88427fcf09beec68ea32c14695` |
| `cases/th14_dart_websocket_fragments.json` | 3949 | `7d5f463557768a3e1902197cb6ad81a10b7c4e6ab259a386b40ab37f118eb7f2` |
| `oracles/th14_dart_keyset_pagination.json` | 1366 | `c8da9865fdc600415e1bdf20957133f948572a42d200d4477b1077f05d1d8025` |
| `oracles/th14_dart_semver_selection.json` | 1301 | `187bfd6234dd34b30af34e457035f95288c32c93c4c5d83dc5cd139ec2fff382` |
| `oracles/th14_dart_websocket_fragments.json` | 1497 | `1b3d53b09f3fcfe3df71185c30905733786a37e3bb1ad29ca77bfc9f984d2a28` |
| `final_oracles/th14_dart_keyset_pagination.json` | 1618 | `3bf46fcb5970976de5c4c8fcecef0b66d430811b418fbcfe39df40aa6e694d30` |
| `final_oracles/th14_dart_semver_selection.json` | 1484 | `fcf9fa2133d7f0ee4b733b0d70901bdd15e092d1f4c0caca175e4822c48b4100` |
| `final_oracles/th14_dart_websocket_fragments.json` | 1439 | `b45de56cc14d497ce84a1c7bbbf1e825286b19800c4b4e5a33458a76425b1391` |
