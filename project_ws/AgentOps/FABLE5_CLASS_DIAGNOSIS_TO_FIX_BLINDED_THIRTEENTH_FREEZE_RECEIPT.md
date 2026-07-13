# Fable 5-Class Diagnosis-to-Fix Blinded Thirteenth Freeze Receipt

Date: 2026-07-13

## Frozen Inputs

- Frozen implementation commit: `cd85207c5ccfe6bbf6a92564e4789bc17790ce07`.
- Frozen implementation tree: `00cc662c57375e439663e8d05ca510e302b803e9`.
- Pre-authored protocol commit: `358678a7b0f13a5409807872478571c88f7a18dc`.
- Fixture commit: `d627a8e4a1c3611644280ae6928e5aa733c1a45a`.
- Fixture Git subtree: `8f1b502ffdde4a0312d21e1a72132f04faa39dd3`.
- Fixture file count: 57.
- Imported authored-file count: 40.
- Canonical authored-byte aggregate SHA-256: `e05b205217ab8e618d6946b7811df744c836db4b71ddbd81adf09cd4f4761803`.
- Full fixture path/raw-byte aggregate SHA-256: `8cc56e5e106b27b1b80bb2fd1ebd8731e16bbc07e197c45b5cebd2bc24e7f6f7`.
- Reference family: `claude-fable-5`.

The canonical author aggregate uses root labels in the exact order `dart,node,python,sql`, then normalized relative
paths. For each authored file it hashes UTF-8 `<label>/<relative-path>\0`, the raw file bytes, and one NUL byte.
The full fixture aggregate uses the same framing with fixture-relative paths sorted ordinally.

## Independent Validation

- Four context-isolated authors produced three cases each outside the CHILI repository.
- Four independent language-lane validators passed Python, Node ESM/TypeScript, Dart, and SQLite SQL.
- Every lane independently observed public-green, feedback-red, final-red, coordinated-repair-green, and required
  owner-ablation failure for all three assigned cases.
- The first aggregate semantic audit rejected incomplete validation metadata: nine missing source-skeleton records
  and missing Dart assertion-family records.
- The first aggregate integrity audit rejected incomplete SQL receipt coverage because `AUTHOR_RECEIPT.md` was not
  represented in that lane's before/after inventory.
- Only validator reports were supplemented. The 40 authored files remained byte-identical.
- Aggregate semantic V2 passed all 12 cases with no failed or incomplete gate.
- Aggregate integrity V2 passed all 40 authored hashes and reproduced the canonical aggregate above.

Preserved receipt hashes:

- Semantic V1 reject JSON: `6ae75ab5dc28c598dcbcc79622c3d715e9b40ee5a3eef992f64a07528d34b349`.
- Semantic V1 reject report: `e59a35da94872157f5312dfa831d9f4a14f79c377d51eb519b9582a42c3223e6`.
- Semantic V2 pass JSON: `963e995bdbd63752bf650c287e12254f7b57667521bd990b3e49af0f51a842d3`.
- Semantic V2 pass report: `ace570a4fe2903aa86ff23e924bdd3cde8e249a97ad4c281ca707e716673dd3f`.
- Integrity V1 reject JSON: `8d672b69c97b06aacce7b098358171d96baaa73e52c2d4bca0c64dfd4a06e0dc`.
- Integrity V1 reject report: `c36a57f7ee08140752389db81b14a5fc82c7f3ca7bd8e89e968bb9b41ae4d551`.
- Integrity V2 pass JSON: `39a977728c94fb32b805bf52c3893b9a580b56b4a1f2e4f26fddc33cc2390b6c`.
- Integrity V2 pass report: `3ac43e0b563e6e714b6f6d1e277a77c9658fc20ce0b117b5326e54ca2285eab8`.

## Freeze Boundary

`git diff cd85207c5ccfe6bbf6a92564e4789bc17790ce07..d627a8e4a1c3611644280ae6928e5aa733c1a45a`
contains only the pre-authored protocol and the frozen fixture artifacts. It contains no implementation, benchmark
runner, prompt, routing, validation, scoring, or repair-operator change.

The copied validation Markdown intentionally preserves one Markdown hard-break and one terminal blank line. These
produce two `git diff --check` whitespace notices but were not rewritten because their byte hashes are evidence.
All fixture JSON parsed successfully through the frozen runner preflight.

## Preflight

Command:

`python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_thirteenth --validate-fixtures`

Result: `fixtures_valid=True cases=12`.

Model calls before fixture freeze: 0. Premium calls before fixture freeze: 0.

## Run Lock

From this receipt through preserved final scoring:

- no implementation, runner, prompt, routing, scoring, case, oracle, final-oracle, or validator edit;
- no source-writing agent;
- zero premium calls;
- primary model `qwen2.5-coder:7b`, compact escalation `qwen2.5-coder:14b`;
- two base repair rounds and one escalation repair round;
- 180-second per-call timeout and 690-second per-case model wall budget;
- final oracle loaded only after all model calls for its case;
- no diagnostic-memory read or write in evaluation mode;
- any runtime incompatibility fails closed and is preserved without post-access fixture correction.

This run can measure transfer after disclosed development. It cannot establish Fable 5 parity without authenticated
Fable 5 outputs on the exact frozen tasks and blind human adjudication.
