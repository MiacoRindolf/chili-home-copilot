# Fable 5-Class Diagnosis-to-Fix Blinded Twelfth Freeze Receipt

Date: 2026-07-12

## Frozen Inputs

- Implementation commit: `9b69cd8db6d9a97717422202e751b762b7ff7dc7`.
- Implementation tree: `c9ab31d3f3a529bd50b8d326613c2a60fa475b52`.
- Pre-authored protocol commit: `5e85b353831c96c763134896dc3fd9c0412462a4`.
- Fixture commit: `fdfaba853feec1a0f6c158c2cfe10a7ad7a0d3c2`.
- Fixture Git tree: `2d797d0f972b47489979b5e2d3793886d9709d67`.
- Fixture file count: 48.
- Sorted fixture path/hash aggregate SHA-256: `a24a99f3fe16b84f5acf78c609ad6f33d96a81d3cbea0cc56c5ca44ebb397979`.
- Independent validator V3 receipt SHA-256: `2d17f2722e5f087ca91b2b83ec2d0ef49c86379ca0e5da47e7795412b53fe6f3`.
- Runner adapter receipt SHA-256: `877a7948e72813dd2559820719043d72c60dfc2776d41a6c7474046083620daa`.

`git diff 9b69cd8..fdfaba8` reports no changes to the implementation or benchmark runner paths. The
intervening commits contain evidence documents, the pre-authored protocol, and frozen fixture artifacts only.

## Independent Authoring And Validation

- Four context-isolated authors produced three cases each in external directories, one language per author.
- Authors were instructed not to inspect CHILI source, repositories, history, fixtures, results, conversation history, or the internet.
- Independent V1 validation rejected four cases for disclosed-family or cross-language duplication.
- Independent V2 validation rejected two replacement cases for patch-null replay and shared fixed-point semantics.
- Independent V3 validation passed all 12 cases with no remaining correction requests.
- V3 independently observed 12/12 public baselines passing, 12/12 repair-feedback baselines failing, and 12/12 final baselines failing.
- Every case has two or three objective candidate owners and a feasible `max_files` value.
- The V1 and V2 rejection receipts are preserved byte-for-byte beside the V3 pass receipt.

## Non-Semantic Runner Adapter

- Model calls before adapter: 0.
- Model calls before fixture freeze: 0.
- Added the public `case_id` to each final-oracle wrapper because the frozen runner requires identity parity.
- Renamed Node and Dart test-map keys from `test/` to `tests/` because the frozen runner requires test containment under `tests/`.
- Test bodies, assertions, candidate sources, expected dimensions, expected owner sets, and final semantics changed: 0.
- `RUNNER_ADAPTER_RECEIPT.json` binds every adapted file to its validator SHA-256 and post-adapter SHA-256.

## Preflight

`python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_twelfth --validate-fixtures`

Result: `fixtures_valid=True cases=12`.

## Run Lock

From this receipt through final scoring:

- no implementation, runner, prompt, routing, scoring, case, oracle, or final-oracle edits;
- no source-writing agents;
- zero premium calls;
- final oracles are loaded only after all model calls for their case;
- any pre-run incompatibility must fail closed and be recorded, not silently corrected after contestant access.
