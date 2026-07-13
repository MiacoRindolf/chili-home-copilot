# Fable 5-Class Diagnosis-to-Fix Blinded Fourteenth Protocol

Date frozen: 2026-07-13

## Objective

Measure whether CHILI's frozen local-only diagnostic and repair system transfers from the disclosed thirteenth
mechanisms to a newly authored adversarial holdout. This is an untouched generalization gate, not a development
replay.

## Frozen Implementation

- Implementation commit: `2cc8e9d446e0ceb66abf5bf688596efb869f0133`.
- Implementation tree: `3ebcc8cb37574185c404848464a62ff06612fefe`.
- Evidence-receipt commit: `a7fa1d0`.
- Reference family: `claude-fable-5`.
- Fable 5 parity claim before the run: false.

No implementation, runner, prompt, model-routing, repair-operator, validator, scoring, or capability-test edit may
occur after this protocol commit and before the untouched result is preserved. Fixture-only authoring, independent
validation metadata, manifest assembly, and run receipts are permitted and must be committed before the first model
call.

## Fixture Construction

- Twelve cases are authored after the implementation freeze by four isolated lane authors: three Python/pytest,
  three Node ESM/node:test, three Dart script, and three SQLite/pytest cases.
- Authors start without parent conversation context and work in separate Git forks with disjoint language ownership.
- Authors may inspect the benchmark schema, loader, prior fixture shape, and current repair-family inventory only to
  avoid duplicate mechanisms and produce valid artifacts.
- Authors may not invoke CHILI's benchmark, Ollama, Claude, Fable 5, or another coding model during fixture creation.
- Every case must have at least three plausible candidate source files, exactly two required causal owners, and a
  bounded edit budget that permits those owners.
- Baseline public tests must pass; repair-feedback and sealed-final tests must fail.
- Sealed-final tests must add a materially new boundary or composition rather than rename a feedback assertion.
- Mechanisms must not duplicate a thirteenth case or a recognized deterministic repair family in
  `app/services/project_autonomy/diagnostic_reasoning.py`.
- Public case files may not reveal expected dimensions, owner labels, hidden tests, or solution patches.

## Independent Validation

After lane commits are integrated and before fixture freeze, separate context-isolated validators must verify:

- schema, manifest, path-containment, and symlink safety;
- public-green, feedback-red, and final-red baselines through the declared language runner;
- exactly two expected source owners within each case's edit budget;
- no hidden-test or oracle leakage into public case bytes;
- no duplicate mechanism, source skeleton, assertion family, or final boundary across the twelve cases;
- no material overlap with the thirteenth mechanisms or current deterministic repair operators;
- authored file hashes and author receipts agree with imported bytes;
- the first semantic or integrity rejection, if any, is preserved before a validator-only metadata correction.

The canonical authored bytes, full fixture bytes, Git subtree, validator reports, and run lock must be frozen before
the first model call.

## Run Policy

- Primary model: `qwen2.5-coder:7b`.
- Compact escalation model: `qwen2.5-coder:14b`.
- Base repair rounds: 2.
- Escalation repair rounds: 1.
- Per-call timeout: 180 seconds.
- Per-case model wall budget: 690 seconds.
- Premium calls allowed: 0.
- Evaluation context: `protocol`.
- Separate final oracle required for every case.
- Final oracle is loaded only after every model call for that case.
- No model call may occur after final adjudication begins.
- No diagnostic memory may be read or written in evaluation mode.
- Only one untouched run is permitted. Any later execution is a disclosed replay.

## Promotion Gate

Thresholds are frozen before case integration:

- overall score at least 90/100;
- sealed-final solves at least 10/12;
- at least 2/3 sealed-final solves in each language;
- accepted correct causal families at least 10/12;
- exact expected changed-file sets at least 10/12;
- public-test preservation and prompt-contract closure 12/12;
- retained patches only when validation advances or closes the contract;
- premium calls 0 and model-call transport errors 0.

Passing this gate supports calling CHILI a strong local replacement candidate for the tested task distribution. It
does not establish superiority or parity with Fable 5 without authenticated same-task Fable 5 outputs and blind
human adjudication.
