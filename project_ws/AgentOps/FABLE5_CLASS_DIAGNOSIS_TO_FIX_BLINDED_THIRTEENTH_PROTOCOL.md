# Fable 5-Class Diagnosis-to-Fix Blinded Thirteenth Protocol

Date frozen: 2026-07-12

## Objective

Measure whether CHILI's local-only diagnostic and repair system transfers beyond the disclosed twelfth mechanisms.
This run is an untouched generalization gate, not a development replay.

## Frozen Implementation

- Implementation commit: `cd85207c5ccfe6bbf6a92564e4789bc17790ce07`.
- Implementation tree: `00cc662c57375e439663e8d05ca510e302b803e9`.
- Reference family: `claude-fable-5`.
- Fable 5 parity claim before the run: false.

No implementation, runner, prompt, model-routing, repair-operator, validator, or scoring edit may occur after fixture
freeze begins and before the result is preserved. Nonsemantic manifest adapters require an explicit receipt and an
independent validator approval before any model call.

## Fixture Construction

- Twelve cases authored outside the CHILI repository by four context-isolated agents.
- Three Python/pytest, three Node ESM/node:test, three Dart script, and three SQLite/pytest cases.
- Authors may not inspect CHILI source, Git history, earlier fixtures, reports, outputs, or model conversations.
- Authors may not invoke CHILI, Ollama, Claude, Fable 5, Codex, or another model while constructing or validating
  a case after their initial delegated authoring response begins.
- Every case must have two or three plausible candidate source files and require a coordinated repair.
- Baseline public tests must pass; repair-feedback and sealed-final tests must fail.
- Sealed-final tests must add a materially new boundary or composition, not rename a feedback assertion.
- Cases must avoid the disclosed twelfth families: fixed-point apportionment, release-reader retirement, trusted
  proxy CIDR chains, canonical base64url, request policy snapshots, TLS client authentication, replacement config
  reload, source-aware tail checkpoints, unordered category hierarchy, tri-state override SQL, composite tenant
  stock ownership, and ticket archive/move accounting.

## Independent Validation

Before fixture import, a separate context-isolated validator must verify:

- schema and manifest consistency;
- source/test path containment and no symlinks;
- public-green, feedback-red, final-red baselines;
- two or three expected source owners within each case's edit budget;
- no feedback/final overlap or trivial fixed-point equivalence;
- no duplicate mechanism, source skeleton, assertion family, or final boundary across the twelve cases;
- no material overlap with the twelve prohibited disclosed mechanisms;
- no oracle labels or hidden test contents in public case files.

The fixture bytes and aggregate SHA-256 are frozen before the first model call.

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

## Scoring And Promotion

The frozen runner weights remain: baseline-final failure 5, diagnosis family 15, exact changed-file set 10, retained
patch 5, public tests 10, sealed final 45, and premium independence 10. Prompt-contract closure is an additional
verdict gate. A case is solved only when its sealed final passes in a fresh repository.

This run can support further development only. It cannot establish Fable 5 parity without authenticated Fable 5
outputs on these exact frozen tasks and blind human adjudication. Replacement readiness still requires the full
promotion gate in `project_ws/AgentOps/CHILI_FABLE5_CAPABILITY_GAP_REPORT.md`.
