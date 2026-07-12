# Fable 5-Class Diagnosis-to-Fix Blinded Ninth Run Receipt

Date: 2026-07-12 UTC

## Verdict

The first completed schema-v3 sealed diagnosis-to-fix holdout scored **53.75/100** with verdict
`needs_improvement` and comparison verdict `blinded_evaluation_failed`. CHILI is not yet a
Fable 5-class replacement for unseen complex diagnosis-to-fix work.

This result is authoritative. Later development reruns cannot replace it.

## Frozen Protocol

- Frozen implementation commit: `70c22ded4ec715c680192bf199f185dfa20bcb52`
- Fixture-only commit used for execution: `59709db31acd220d676c0cbd7eb7cb94ba0c3403`
- Runner SHA-256: `b1b0c24df74503330941c093a782081d8604c9dec74fba59625ff2ef90f6c910`
- Runner-test SHA-256: `a562a84e3081b995bea764c3efa637f99538fa2b827cfa3baa8ef8f0f1de8a1d`
- Diagnostic-reasoning SHA-256: `2ca872a4e0c05ce327614a8cbab51d6327ffffcbf82fab1530d5f75043106a1e`
- Local model: `qwen2.5-coder:7b`, digest prefix `dae161e27b0e`, Q4_K_M, 7.6B parameters
- Timeout: 180 seconds per model call
- Bounded repair rounds: 3
- Premium calls: 0
- Model calls after final adjudication began for each case: 0

The protocol was written before fixture visibility. The benchmark and reasoning hashes were
unchanged after the run.

## Independent Fixture Evidence

- Context-isolated author agent: `019f551a-2a07-7830-b570-0f1e96195776`, `fork_context=false`
- Author directory: `D:\dev\chili-holdout-author-ninth-20260711`
- Author receipt SHA-256: `f73143c5e4a82498f4b9a400bd338b8db864bcb09ce2518f75731994764d1e43`
- Composition: 8 cases, all 8 multi-file; Python 2, TypeScript 2, Dart 2, SQL 2
- Author validation: public baseline 8/8 pass, feedback baseline 8/8 fail, fresh final baseline 8/8 fail
- Context-isolated semantic validator: `019f5527-cf25-73d1-a7b3-772368a9f58f`
- Semantic validator: PASS on schema, language, multi-file ownership, baseline behavior,
  partition separation, payload independence, leakage safety, and execution safety, all 8/8
- Validator pre/post aggregate SHA-256:
  `aff3fbe2417220016405e79a19a09a15b0ddfd39a532ba3f920f5d2b5a75c292`

Author isolation is an attestation; fixture artifacts cannot independently prove which external
sources the author did not access.

## Untouched Result

| Case | Expected | Actual | Score | Exact files | Patch | Feedback | Final | Seconds |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `py-config-explicit-values` | config | runtime | 45 | false | true | false | false | 176.0 |
| `py-matrix-result-attribution` | test_harness | runtime | 80 | true | true | true | true | 186.2 |
| `ts-workspace-state-ownership` | state | state | 65 | false | true | false | false | 223.5 |
| `ts-utf8-stream-boundaries` | runtime | code | 60 | true | true | false | false | 254.5 |
| `dart-profile-patch-null` | data | code | 30 | false | false | false | false | 201.6 |
| `dart-equal-time-event-order` | clock | state | 45 | false | true | false | false | 216.8 |
| `sql-retained-payment-history` | data | config | 60 | true | true | false | false | 215.9 |
| `sql-fixed-width-identifiers` | runtime | data | 45 | false | true | false | false | 224.9 |

- Correct diagnosis family: **1/8**
- Exact changed-file ownership: **3/8**
- Accepted non-empty patch: **7/8**
- Public regression tests passed: **8/8**
- Repair-feedback tests passed: **1/8**
- Sealed final tests passed: **1/8**
- All eight cases remained `provisional`
- Local calls: **106/106 successful**, 22,310 output tokens
- Average model-call wall time: **15.5 seconds**; maximum: **58.0 seconds**
- Average repository wall time: **212.4 seconds**
- End-to-end wall time: **1,704.2 seconds**

The sole sealed-final success had the correct patch and exact files but the wrong diagnostic
family. This is evidence of occasional repair success, not reliable causal diagnosis.

## Protocol Deviation

The fixture-only Git commit initially normalized CRLF bytes. Before any CHILI model call, it was
amended with a fixture-local no-normalization rule and all 27 authored blobs matched the external
originals exactly.

During that correction, a staged whitespace check printed portions of repair-oracle text to the
parent controller. The local Qwen contestant did not receive this output, the implementation and
settings were already frozen, and no manual intervention occurred. Therefore this slice is
independently authored and **CHILI-model-blind**, but it is not fully controller-blind. This
limitation is preserved in the raw deviations artifact.

## Artifact Hashes

- Markdown report: `ea9ca7d45270c3192bbb0640976ec274ba8ae17acc2bd6438bf3eb64a4cff7ec`
- Raw result JSON: `e47a28a79a332c1660949b19ec4af5429ff70d2f043ae24b1901dd4168c7e338`
- Frozen protocol: `ef9350ee5ff93928b176bb8a10592e8e077d3672d3d79fc7657d7d6e23526e31`
- Environment receipt: `1f59db5b31548df6c300457fcc37889466642a4a7f9b6abac637ccdefbf5422d`
- Deviations receipt: `09ce5b09bcc05aca48abe6af0c211a6406fc853e049481455fbad05228b03b2c`

No authenticated same-task Fable 5 output was collected, so this run supports no direct parity or
superiority claim.
