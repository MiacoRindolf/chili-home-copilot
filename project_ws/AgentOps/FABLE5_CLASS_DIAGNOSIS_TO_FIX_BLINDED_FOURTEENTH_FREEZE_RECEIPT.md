# Fable 5-Class Diagnosis-to-Fix Blinded Fourteenth Freeze Receipt

Date: 2026-07-13

## Frozen Inputs

- Frozen implementation commit: `2cc8e9d446e0ceb66abf5bf688596efb869f0133`.
- Frozen implementation tree: `3ebcc8cb37574185c404848464a62ff06612fefe`.
- Pre-authored protocol commit: `5764a0f9d2919c19cdd2f85b62ee20b8ee553169`.
- Active V3 authored target commit: `a249993262ce5c2f621ed17ce67b7cccf8e74fef`.
- Validation-complete tip: `5edae0f7a461bcba162a461ba390bcd6ae8ad15f`.
- Validation-complete Git tree: `b7049ed7209d6f9c3bf042c29b8a36b506146049`.
- Fixture Git subtree: `31bcca632bd5d467841757b07a152d0cc5556fb6`.
- Reference family: `claude-fable-5`.

Authoritative V3 aggregates use ordinal ASCII fixture-relative paths and the framing recorded by integrity V3:

- Active payloads, 36 files: `32aa8a8e73d1506d253a29736adf0fcbcc1aca422af6cc307a327b48c3fc0b2d`.
- Author receipts, 5 files: `c83140ecd4e4e99e054e2f7c95d48f8b09a01e71dbd45029bd61f83e32518c41`.
- Authored payloads plus receipts, 41 files: `d82e0c6bf0057d82e33ec223721211ee168ebe4cf9c3a80fe4da474c5fad3524`.
- Active V3 core including manifest, 42 files: `eba357467b11829cfb552a40230804a78e586c5cddd91c25a514e0a61923960d`.
- Full post-validation fixture, 66 files and 476338 bytes: `4f49380bcd78c3670384bffb3e1bb87c1cf73eb94f076b3510221f7596401424`.

## Authorship And Correction History

- Four isolated lane authors produced three cases each after the implementation freeze.
- The integrated V1 fixture passed runtime and integrity validation.
- Adversarial V1 rejected `th14_dart_keyset_pagination` because it duplicated the ninth holdout's equal-time compound
  event-position mechanism. That rejection remains preserved in `ADVERSARIAL_REPORT.md` and Git history.
- A new isolated author replaced the rejected triple with `th14_dart_redirect_handoffs`; the old triple was removed
  from the active manifest but remains preserved by commits and V1 hashes.
- Runtime V2 passed. Integrity V2 rejected an incorrect replacement-receipt aggregate. Semantic and adversarial V2
  rejected a one-file centralization path that made `redirect_follower.dart` unnecessary. All three rejections remain
  preserved in the V2 reports.
- V3 changed only the redirect case/oracle/final triple and its replacement receipt. It added direct follower
  isolation with injected policy output, direct request-policy isolation, a stronger final composition, an explicit
  request-only centralization attack, and corrected aggregate metadata.
- No implementation, runner, prompt, routing, validator, scoring, or capability-test file changed during V1-V3.

## Independent V3 Validation

All four V3 validators started from `a249993262ce5c2f621ed17ce67b7cccf8e74fef`, wrote only their report pair,
and returned PASS with `authored_files_unchanged=true`.

- Runtime result: `2d1bffb84296d2f0e5a95416acf76f214f8cf8f941b399dc10d748517434c112`.
- Runtime report: `9e2d775a273e54d5cae15d7e550a6a4296b1c04b85cd62a8ecd05a70e3cfaf4b`.
- Integrity result: `96e17b91a28b739ae14515845723ba94389e027a280a723de52426df2cba83c9`.
- Integrity report: `6df90b6e43833cba3a996a0a6c02dbf9f696fd4470b1b036db7fe729b108e24a`.
- Semantic result: `41a0b173de5c463fc17335fa83e7aaa1f7945efc5908d26b81778e19569e6041`.
- Semantic report: `04eca957341f4461469a7a0158b0402ecac903cfcfbcd23f4c3a67caccbf029c`.
- Adversarial result: `1daac349e4ea6328d87ce736132999f5b961c0bf1354d7490037149b07abd93a`.
- Adversarial report: `389479f4ef4b34a534b2c65ed83e3302b18363cd81f6ac0e2b6a8f9333d12801`.

V3 evidence includes a 139-case prior-corpus novelty audit, direct owner-necessity review, failure of the plausible
request-only centralization attack, no current deterministic repair proposal, exact payload/receipt hash reproduction,
real language syntax and test runners, fresh final repositories, and zero model calls.

## Final Preflight

Command:

`python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --validate-fixtures`

Result: `fixtures_valid=True cases=12`.

Model calls before freeze: 0. Premium calls before freeze: 0.

## Run Lock

Exactly one untouched execution is authorized after this receipt is committed and pushed:

`python scripts/autopilot_diagnosis_to_fix_benchmark.py --fixture-root tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth --model qwen2.5-coder:7b --escalation-model qwen2.5-coder:14b --max-repairs 2 --max-escalation-repairs 1 --timeout 180 --case-model-time-budget 690 --evaluation-context protocol --report project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_FOURTEENTH_UNTOUCHED.md --results-json project_ws/AgentOps/fable5_class_diagnosis_to_fix_blinded_fourteenth_untouched.json`

From this receipt through preserved final scoring:

- no implementation, runner, prompt, routing, scoring, case, oracle, final-oracle, receipt, or validator edit;
- no source-writing or fixture-writing agent;
- zero premium calls;
- primary model `qwen2.5-coder:7b`, compact escalation `qwen2.5-coder:14b`;
- two base repair rounds and one escalation repair round;
- 180-second per-call timeout and 690-second per-case model wall budget;
- final oracle loaded only after all model calls for its case;
- no diagnostic-memory read or write in evaluation mode;
- any incompatibility or failed gate is preserved without post-access correction.

## Frozen Promotion Gate

- Overall score at least 90/100.
- Sealed-final solves at least 10/12 and at least 2/3 in every language.
- Correct accepted causal families at least 10/12.
- Exact expected changed-file sets at least 10/12.
- Public-test preservation and prompt-contract closure 12/12.
- Premium calls 0 and model-call transport errors 0.

Passing supports a strong local replacement-candidate claim for this tested distribution. It does not establish Fable
5 parity or superiority without authenticated same-task Fable 5 outputs and blind human adjudication.
