# Fourteenth Holdout Integrity Report

- Validator: `codex-independent-integrity-validator/v1`
- Target: `64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3`
- Validation branch: `codex/fourteenth-integrity-validation`
- Verdict: **PASS**
- Authored files unchanged: **true**
- Findings: none

The audit used the integrated target tree as immutable input. No authored case,
oracle, final oracle, receipt, manifest, application file, script, existing
test, or `project_ws` file was changed. No diagnosis benchmark evaluation mode,
Ollama, Claude, Fable 5, or other coding model was invoked.

## Gate Results

| Gate | Result | Evidence |
|---|---|---|
| Target anchor | PASS | `HEAD` was `64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3` before validation work. |
| Inventory and containment | PASS | Exactly 41 input files: 36 JSON payloads, 4 receipts, and 1 manifest; every filesystem and embedded path is normalized, relative, ASCII, and contained. |
| Symlinks and special files | PASS | All 41 Git entries are mode `100644`; no symlink, junction, or reparse point exists in the checked-out fixture. |
| BOM and ASCII policy | PASS | All 41 files and paths are 7-bit ASCII; no UTF BOM or embedded NUL was found. |
| JSON shape | PASS | All 37 JSON files parse as objects without duplicate keys and use the exact ordered v3 key sets for their roles. |
| Triplet identity | PASS | All 12 filenames, case IDs, repair-oracle IDs, final-oracle IDs, and manifest IDs agree and are unique. |
| Partition disjointness | PASS | Seeded, feedback, and final paths are pairwise disjoint under case folding; feedback and final whole-file payloads are distinct. |
| Receipt hashes | PASS | `36/36` author-claimed payload SHA-256 values match imported raw bytes; every recorded byte count also matches. |
| Author commit scopes | PASS | Each source and imported lane commit adds only its 9 JSON files and receipt; target commit `64b32b8` adds only `manifest.json`. |
| Source/import identity | PASS | Exact lane diffs from source author commits to target are empty; source/import stable patch IDs match. |
| Source/test paths | PASS | Every candidate and expected owner is in `repo_files`; hidden files stay under `tests/`; each case has 3 candidates, 2 owners, `max_files=2`, and a distractor. |
| Manifest completeness/order | PASS | The 12 ASCII-sorted entries cover every triplet exactly once with `blinded_holdout` and the correct sealed language split. |
| Hidden leakage | PASS | No oracle labels, hidden test paths, test names/titles, assertion labels, or complete hidden payloads occur on an earlier visible surface. |
| Aggregate candidates | PASS | Independent Python and PowerShell implementations agree on all three canonical aggregate digests. |
| Validation-only preflight | PASS | CHILI `--validate-fixtures` exited `0`: `fixtures_valid=True cases=12`. |

Language balance is `dart=3`, `typescript=3`, `python=3`, and `sql=3`.
Every case has one discoverable public test partition and non-empty,
discoverable feedback and final partitions. The Node ESM plugin case also has
supporting hidden package-data files; generic names such as `package.json` and
`node.mjs` were inspected as data, not misclassified as hidden test labels.

## Commit Provenance

| Lane | Source commit | Imported commit | Files added | Stable patch ID |
|---|---|---|---:|---|
| Python | `ad882553038a0515517856d5005cea46db4456b3` | `f396496cfd7cf62f0f386bbfdcacabce409ccc90` | 10 | `e065db9da103f061eaad1b331afc4644a9bc0ff7` |
| SQL | `5539dd61f547e9ecc9e8e105879b35aff6e142a3` | `07a79b78042dac6543a7ea36b98e0854fd1b18e2` | 10 | `2c7fc62621f25909ec83798dc92d284b7fb26abd` |
| Node | `b3558518aae27bad283d4925494b07ca145fcb93` | `fcf907b86c1c63e7ca5cda2713675d123ea81a4f` | 10 | `62b2737adf9a05d7c9c8b984725282435a230d60` |
| Dart | `e96d620b4980ded143d2a53f3f6878a294337ca4` | `096cb1ea9480f106fb7636c5cabed5542ea2a48a` | 10 | `748569b2975e42f643bdf051ea7ab29b3c634809` |

The exact parent chain for the source commits is:

```text
395f4ef0348a2a3908e3f6946e06612b4b1652c8
  -> ad882553038a0515517856d5005cea46db4456b3  Python
  -> 5539dd61f547e9ecc9e8e105879b35aff6e142a3  SQL
  -> b3558518aae27bad283d4925494b07ca145fcb93  Node
  -> e96d620b4980ded143d2a53f3f6878a294337ca4  Dart
```

The imported target chain is:

```text
5764a0f9d2919c19cdd2f85b62ee20b8ee553169
  -> f396496cfd7cf62f0f386bbfdcacabce409ccc90  Python
  -> 07a79b78042dac6543a7ea36b98e0854fd1b18e2  SQL
  -> fcf907b86c1c63e7ca5cda2713675d123ea81a4f  Node
  -> 096cb1ea9480f106fb7636c5cabed5542ea2a48a  Dart
  -> 64b32b8e81f6fa3cbd3c5c509aa65940e2d18be3  manifest only
```

`git diff --exit-code` produced no lane diff between each source commit and
the target for its 10 authored files, including the receipt. Thus the stable
patch-ID agreement is backed by exact blob equality, not used as a substitute
for it.

## Hash Evidence

The canonical aggregate algorithm uses one SHA-256 state. For each selected
file in ascending ASCII order by fixture-relative path, feed exactly:

```text
UTF-8(relative/path) || 0x00 || raw file bytes || 0x00
```

| Candidate set | Files | Bytes | SHA-256 |
|---|---:|---:|---|
| Case, repair-oracle, and final-oracle payloads | 36 | 91,477 | `32629d6d402d5b879dcda6379d9b310d5c6245900ffec5359f168d85931b8913` |
| Authored files, including receipts | 40 | 119,821 | `e731294c5d64d8677e2a7608c3ae9db4456592fc221fc5acee0742099e310c8b` |
| Integrated target fixture, including manifest | 41 | 123,423 | `4fd13aa188c344a562f22db44a7d0392e838c36b8903a2d194f830d0e43bcfd0` |

The 41-line report-input inventory uses:

```text
relative/path<TAB>decimal byte count<TAB>lowercase SHA-256<LF>
```

Its SHA-256 is
`c4496a8f353b68ca81b867cebe377914fc5a2906e900320ffcc30b2c394643f7`.
The target fixture Git tree OID is
`7c4fdbbaa776f809ff104e8433cef79e4a00ea26` (Git SHA-1 object ID).

| Input | SHA-256 |
|---|---|
| `manifest.json` | `f196fe7ca334ab3e88af2aa9c3d075a550ff67f1e253c3fa0aec4f5f6fec900c` |
| `AUTHORS/dart_AUTHOR_RECEIPT.md` | `1bc87fa434b822a2cae468510296e926e52ada671b76f2f1cc9de541bd42bd5d` |
| `AUTHORS/node_AUTHOR_RECEIPT.md` | `5bb963e94908df8c2632c9ff626ccafe5a5dd539a1ee396bb2bdecb35c448558` |
| `AUTHORS/python_AUTHOR_RECEIPT.md` | `14b85e4c433f81e02c290e3f94db03aa9d78c35f115b40ac0ba67f587c288267` |
| `AUTHORS/sql_AUTHOR_RECEIPT.md` | `c1fe5d079f17ea455ec71e0fd790c5c414b518f58d3eb218061361913bff6c68` |

## Command Evidence

Core Git evidence was collected in the clean validation worktree:

```powershell
git rev-parse HEAD
git ls-tree -r -l HEAD -- tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth
git show --no-ext-diff --no-renames --format='COMMIT %H%nPARENT %P%nAUTHOR %an <%ae>%nAUTHOR_DATE %aI%nCOMMITTER %cn <%ce>%nCOMMIT_DATE %cI%nSUBJECT %s' --name-status <commit>
git diff --exit-code --no-ext-diff <source-commit> HEAD -- '<lane JSON pathspec>' '<lane receipt path>'
git diff --no-ext-diff --no-renames <parent> <commit> | git patch-id --stable
git rev-parse HEAD:tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth
```

Commit-scope inspection was run for all four source commits, all four imported
commits, and target `64b32b8`. The exact-diff command was run independently for
Python, SQL, Node, and Dart. The aggregate was independently streamed through
Python `hashlib.sha256` and PowerShell/.NET
`System.Security.Cryptography.SHA256`; both implementations returned the three
digests above.

Filesystem link evidence used:

```powershell
Get-ChildItem -Force -Recurse -LiteralPath 'tests\fixtures\autonomy_diagnosis_to_fix_blinded_fourteenth' |
  Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
  Select-Object FullName,LinkType,Target
```

Output: empty.

The allowed validation-only fixture command was run from the clean target
worktree with bytecode writes disabled:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures
```

Output and exit status:

```text
fixtures_valid=True cases=12
exit code 0
```

## Finding

No mismatch or integrity defect was found. Verdict: **PASS**.
