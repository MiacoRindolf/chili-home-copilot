# Fable 5-Class Diagnostic Eighth Untouched Run Receipt

Date: 2026-07-12

## Freeze And Isolation

- Frozen implementation commit: `1bbcf216ee758fe78ce67a5c2050030860daa5e8`
- Fixture-only commit: `2df59bee2506b0cebb47147e1feb4206195691a6`
- Runner-manifest adapter commit: `5dad7cc59926c888042fe3bdcd4b0e48ee0df7e4`
- Reasoning source SHA-256:
  `2ca872a4e0c05ce327614a8cbab51d6327ffffcbf82fab1530d5f75043106a1e`
- Benchmark runner SHA-256:
  `1a91da985f7dd9edeb781a28dc4f891fa63b34e1b9802bc89421406ea367640f`
- Context-isolated author agent: `019f54cb-df96-7b52-a676-6d5c89b871e6`,
  started with `fork_context=false` and explicit prohibition against reading any
  CHILI checkout, Git history, prior fixture, report, transcript, or model output.
- Author directory: `D:\dev\chili-holdout-author-eighth-20260712`
- Author receipt SHA-256:
  `4b1d7916be044ff7ad92403faf67319ae112ed8be35e1953222074239a81cf12`

The author produced eight independently invented non-trading incidents, 78 unique
public observations, one primary family per diagnostic family, six confirmed patch
cases, one provisional instrument case, one inconclusive instrument case, and a four
true/four false drift split. Every public observation dimension was `unknown`.

The first validator attempt did not return a receipt and is excluded. A second
context-isolated read-only validator initially returned a procedural FAIL because
fixture artifacts alone cannot prove the author's no-access attestation. That process
fact is instead supplied by the orchestration record above. When asked for a separate
fixture-only verdict, the same validator returned **SEMANTIC PASS** for schema,
blinding, matrix, evidence sufficiency, uncertainty calibration, drift labels, safety,
and banned-mechanism separation. It documented nonfatal adjacent-family ambiguities in
all eight cases.

## Manifest Preflight

The first benchmark invocation made **zero model calls** and failed before case loading
because the author used the supplied `case_file`/`oracle_file` manifest fields while
the existing runner expects `case`/`oracle`. The only post-author change converted
those adapter keys, added the runner's per-row evaluation role and immutable count,
and retained all case order, paths, split labels, and source labels.

- Original external manifest SHA-256:
  `d4d6fe9a039a19ee5d380ec4410937e40216606c2f00eaca9917338a4822af1e`
- Runner-compatible manifest SHA-256:
  `711daad31413039796302ad12ce8cf5411ab0421c12c65df4c2b73665aa9581e`
- All 16 case/oracle files remained byte-identical to the isolated author output
  before and after scoring.

Because no model call occurred before the adapter correction and no case or oracle
changed, the completed run remains the untouched first evaluation of the fixture
content.

## Untouched Result

- Model: `qwen2.5-coder:7b`, model-default thinking, 8,192-token context
- Roles: investigator, skeptic, judge
- Successful calls: **24/24**
- Accepted stages: **24/24**
- Premium calls: **0**
- Final safety checks: **8/8 passed**
- Frozen-oracle score: **83.12/100**
- Verdict: `needs_improvement`
- Average local-call latency: **33.5 seconds**
- Maximum local-call latency: **46.5 seconds**
- Wall time: **809.5 seconds**

The frozen oracles listed the primary family first but allowed five adjacent families
in `expected_dimensions`. That made the nominal dimension check materially lenient.
A supplemental strict audit, performed only after the untouched artifacts were sealed,
requires equality with `primary_causal_dimension` and otherwise keeps every original
score component unchanged.

| Case | Primary | Actual | Nominal | Strict-primary | Other misses |
|---|---|---|---:|---:|---|
| `bh8-801` | data | test_harness | 65 | 65 | drift |
| `bh8-802` | config | code | 100 | 75 | none |
| `bh8-803` | dependency | dependency | 90 | 90 | drift |
| `bh8-804` | code | dependency | 100 | 75 | none |
| `bh8-805` | state | state | 70 | 70 | decision, status |
| `bh8-806` | runtime | runtime | 60 | 60 | drift, decision, status |
| `bh8-807` | clock | code | 80 | 55 | drift, status |
| `bh8-808` | test_harness | code | 100 | 75 | none |

Strict-primary score: **70.62/100**, with only **3/8** primary families correct.
Across the author-intended contract, CHILI missed five primary families, all four true
drift findings, two decisions, and three statuses. Structural output, grounding,
hypothesis breadth, premium independence, and final automatic-experiment safety all
passed.

## Artifact Integrity

- Raw report SHA-256:
  `234d5ec46fcde152bdf81e7c6de6de9b0b2249b440ec58e3b26c89bd746752ae`
- Raw JSON SHA-256:
  `0ab08bb7e164379d60b68fb03fc00a58a123b69fb2599a4921470af07a6ff626`

The raw artifacts are `FABLE5_CLASS_DIAGNOSTIC_BLINDED_EIGHTH_RUN.md` and
`fable5_class_diagnostic_blinded_eighth_run.json`. The unmodified author receipt is
preserved as `FABLE5_CLASS_DIAGNOSTIC_BLINDED_EIGHTH_AUTHOR_RECEIPT.md`.

## Claim Boundary

This is fresh negative evidence against replacing Fable 5 for arbitrary complex
diagnosis. The nominal score improves on the seventh untouched 63.8, but remains below
the 90 shadow threshold. The stricter primary-family score is 70.62 and exposes poor
causal transfer despite fully usable model packets and a safe deterministic shell.
Neither the disclosed seventh 100 nor this eighth result establishes Fable 5 parity.
