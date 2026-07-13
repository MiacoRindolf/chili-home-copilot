# Fourteenth Holdout V2 Integrity Report

- Validator: `codex-independent-integrity-validator/v2`
- Target: `86d328b7f136ebfbc6f3ace508dd53b401a04939`
- Validation branch: `codex/fourteenth-holdout-v2-integrity`
- Verdict: **REJECT**
- Authored files unchanged: **true**
- Findings: one blocking receipt-attestation defect

The active V2 fixture is structurally coherent, its payload bytes match the
individual author claims, and the replacement history is tightly scoped.
However, the replacement Dart receipt records a triple aggregate that cannot be
reproduced by the formula written in that receipt. The instruction for any defect
is to reject and preserve it, so no authored case, oracle, final oracle, receipt,
manifest, V1 report, source, script, test, or `project_ws` file was changed.

## Blocking Finding

### INT-V2-001: Replacement receipt has an incorrect triple aggregate

`AUTHORS/dart_replacement_AUTHOR_RECEIPT.md` lines 279-283 declare:

```text
66aec0bb2a036a271590f710c6bbfa227fe5e02412dc660ab100cb315957c0fd
```

The receipt defines the aggregate record stream at lines 70-72 as sorted:

```text
relative-path + NUL + lowercase(file SHA-256) + LF
```

All three individual receipt claims match the target bytes:

| Replacement payload | Bytes | SHA-256 |
|---|---:|---|
| `cases/th14_dart_redirect_handoffs.json` | 4,955 | `341e60251684055963e94b350f4d08a387dbbafb310196921322183b95011418` |
| `final_oracles/th14_dart_redirect_handoffs.json` | 3,313 | `08df7e56fe05393631b6927e81ccd7bbf40912f44265a1244bb79780e95a7e84` |
| `oracles/th14_dart_redirect_handoffs.json` | 2,716 | `895add9c4ee93bb1ffe914b47ee9a9ec092815c80d97c5e40fe92bd86ce1e48a` |

Applying the stated formula in ordinal ASCII path order produces:

```text
769c2ede2a2c29d35bb83f5badc2602820e4b0d5eb50c123a1d3d976eaf20caa
```

Independent Python and PowerShell/.NET implementations agree on this result.
The declared value also matches none of the six possible path orderings under
the same record formula. This is a defect in the authored receipt attestation,
not a mismatch in any of the three payload hashes. It was not repaired.

## Gate Results

| Gate | Result | Evidence |
|---|---|---|
| Target anchor | PASS | The validation branch was created directly from `86d328b7f136ebfbc6f3ace508dd53b401a04939`, whose parent is `3de05be7f077de78d26034ae8e82563f3097a232`. |
| Active V2 inventory | PASS | Exactly 36 active JSON payloads: 12 cases, 12 repair oracles, and 12 final oracles. The target also has five receipts, one manifest, and eight preserved V1 reports. |
| Manifest | PASS | Twelve ASCII-sorted entries cover all and only the active triplets once, with `blinded_holdout` and language-correct sealed splits. |
| JSON schema and identity | PASS | All 37 active JSON inputs parse without duplicate keys and use exact ordered v3 role keys; filenames, IDs, languages, runners, and splits agree. |
| Paths and partitions | PASS | All paths are normalized, relative, ASCII, and contained. Seeded, feedback, and final paths are pairwise disjoint under case folding. |
| Collisions and links | PASS | No case-fold path collision exists. All 50 target Git entries are mode `100644`; no filesystem reparse point or Git symlink exists. |
| ASCII/BOM/NUL | PASS | All 50 target-input files are 7-bit ASCII with no BOM or embedded NUL. |
| Hidden leakage | PASS | No hidden path, complete hidden payload, oracle/final field label, or hidden test label appears on an earlier visible surface. |
| Individual receipt claims | PASS | All 39 stated payload hashes match; all 30 stated byte counts match. Active receipt coverage is exactly 36/36. |
| Replacement receipt aggregate | **FAIL** | Declared `66aec0bb...`; reproducible result under the declared formula is `769c2ede...`. |
| Authored-byte preservation | PASS | Thirty-seven retained V1 authored files and all four replacement-author files are byte-identical to their authoritative commits. |
| V1 report preservation | PASS | All eight V1 validation files are byte-identical to completed V1 validation tip `83906c4cde8bf4a1e0f99223f6ed3347baa29b04`. |
| Rejected triple handling | PASS | The old triple is absent from active paths and the manifest, but all three blobs remain reachable and byte-identical in Git history. |
| Commit provenance | PASS | Original lanes, replacement authoring, and activation commits have exact expected scopes and byte identity. |
| Canonical aggregate computation | PASS | Independent Python and PowerShell implementations agree on every V2 target-input aggregate below. |
| Validation-only preflight | PASS | Fixture validation exited `0`: `fixtures_valid=True cases=12`. No evaluation or coding model ran. |

Language balance is `dart=3`, `typescript=3`, `python=3`, and `sql=3`.
Every case has three candidates, exactly two expected owners, `max_files=2`, a
non-owner distractor, and a discoverable test in each of the public, feedback,
and final partitions. The hidden-label scan covered 33 feedback labels and 29
final labels without an earlier-surface match.

## Receipt Authority

| Receipt | Role | Claims verified |
|---|---|---:|
| `dart_AUTHOR_RECEIPT.md` | Historical V1 receipt; six retained active claims plus three rejected-triple claims verified from Git history | 9 |
| `dart_replacement_AUTHOR_RECEIPT.md` | Authoritative for the active redirect replacement triple | 3 |
| `node_AUTHOR_RECEIPT.md` | Authoritative for the active Node lane | 9 |
| `python_AUTHOR_RECEIPT.md` | Authoritative for the active Python lane | 9 |
| `sql_AUTHOR_RECEIPT.md` | Authoritative for the active SQL lane | 9 |

Thus every active payload has exactly one applicable receipt claim. The 36 active
claims and three historical rejected-triple claims all match raw bytes. The SQL
receipt states hashes but not byte counts; the other receipts state 30 byte counts,
all of which match.

## Replacement Provenance

The integrated V1 and validator chain is:

```text
096cb1ea9480f106fb7636c5cabed5542ea2a48a  imported Dart lane
  -> 64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3  V1 manifest only
  -> 2e2ba957fa0cdeaca95fb9b6ddccd2a09e01c924  V1 integrity files only
  -> 5e8089180bf104c5eab3a5fe00619531665af1e0  V1 adversarial files only
  -> 1dae5eea6a90abf19d674ebdcf3fb278f358d120  V1 runtime files only
  -> 83906c4cde8bf4a1e0f99223f6ed3347baa29b04  V1 semantic files only
  -> 3de05be7f077de78d26034ae8e82563f3097a232  replacement triple and receipt only
  -> 86d328b7f136ebfbc6f3ace508dd53b401a04939  activation
```

Commit `3de05be7` adds exactly four files: the redirect case, repair oracle,
final oracle, and replacement receipt. Target `86d328b7` deletes exactly the
three `th14_dart_keyset_pagination` payload paths and changes `manifest.json`.
The manifest semantic delta is exactly the case, oracle, and final-oracle path
in entry zero; no other manifest value changed.

The 33 retained V1 payloads and four original receipts are byte-identical from
integrated V1 commit `64b32b8` to the target. The four replacement-author files
are byte-identical from `3de05be7` to the target. All eight V1 reports are
byte-identical from `83906c4` to the target.

The rejected triple remains available at the original Dart source commit
`e96d620b`, Dart import commit `096cb1ea`, integrated V1 commit `64b32b8`, and
pre-activation commit `3de05be7`:

| Historical payload | SHA-256 |
|---|---|
| `cases/th14_dart_keyset_pagination.json` | `071e74f9c2aafe82c2ac1bf54617066a744c14681bc7bdbd863867a1c41b58bf` |
| `oracles/th14_dart_keyset_pagination.json` | `c8da9865fdc600415e1bdf20957133f948572a42d200d4477b1077f05d1d8025` |
| `final_oracles/th14_dart_keyset_pagination.json` | `3bf46fcb5970976de5c4c8fcecef0b66d430811b418fbcfe39df40aa6e694d30` |

## Canonical Aggregates

These aggregates describe immutable target-commit inputs and therefore exclude
the two V2 report outputs. For each selected file in ascending ordinal ASCII
order by fixture-relative path, one SHA-256 state receives:

```text
UTF-8(relative/path) || 0x00 || raw file bytes || 0x00
```

| Candidate set | Files | Bytes | SHA-256 |
|---|---:|---:|---|
| Active case, repair-oracle, and final-oracle payloads | 36 | 95,745 | `c8f15eec805346a2e5dcbd07bd0e5bf74a83e7797bb6119d5e238876150eb9cc` |
| Active payloads plus all five receipts | 41 | 139,264 | `13782a442fd36bad49f80be4d00f55b231975740162adc57543a4e8cf302b9cf` |
| V2 core, including manifest | 42 | 142,866 | `b81e37ebe4ee5fa7c619ad771770d34aa7c36aa5a0302ffa8bb1996090715f6d` |
| Preserved V1 reports | 8 | 141,129 | `138e35069458d8ede63de606d5e08a05584f00946b59f2abc8f5adc89baf0579` |
| Full target fixture | 50 | 283,995 | `8e90ff21662f0bbf3c1138136eccf73ce199322e4d2cd6139489b5a67181adb6` |

The 50-record target inventory uses:

```text
relative/path<TAB>decimal byte count<TAB>lowercase SHA-256<LF>
```

It is 5,431 bytes with SHA-256
`5c4c7ebc1b046cc66e7cb028a5e87074fab9e60c01356d96b6573e6152694a97`.
The target fixture Git tree OID is
`9423b9188a1c3ea0b1fd53b3fe45475d808b700e` (Git SHA-1 object ID).

## Validation-Only Preflight

The permitted fixture-only loader check ran with bytecode writes disabled:

```text
python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures
```

Output and exit status:

```text
fixtures_valid=True cases=12
exit code 0
```

No CHILI evaluation mode, Ollama, Claude, Fable 5, other coding model, network
service, or package download was invoked.

## Conclusion

The active payload set, manifest activation, historical preservation, individual
receipt hashes, paths, schemas, and provenance all pass. The incorrect aggregate
attestation in the authoritative replacement receipt is nevertheless an integrity
defect. Per the required defect policy, the V2 verdict is **REJECT** and the
authored bytes remain unchanged.
