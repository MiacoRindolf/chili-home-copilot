# Fable 5-Class Diagnosis-to-Fix Blinded Twelfth Protocol

Date: 2026-07-12

## Frozen System

- Implementation commit: `9b69cd8db6d9a97717422202e751b762b7ff7dc7`.
- Implementation tree: `c9ab31d3f3a529bd50b8d326613c2a60fa475b52`.
- Evidence-only parent commit before fixture authoring: `f7fb1b873e7f106285bb096fb2df50f2e5ed695f`.
- No implementation, runner, prompt, model-routing, or scoring edits are allowed after authoring begins and before scoring completes.

## Independent Authoring

- Twelve new cases: three Python, three Node ESM, three Dart, and three SQLite SQL.
- Authors receive no CHILI repository access, prior fixtures, prior results, source history, model outputs, or internet research.
- Authors work in external directories and provide case, repair-feedback oracle, final oracle, and process attestation artifacts.
- Each case must contain a real defect, pass public tests at baseline, fail repair-feedback and final tests at baseline, and require two to four coordinated source owners.
- Cases must be plausible real-world mechanisms, not trivia, syntax-only defects, renamed prior fixtures, or tasks keyed to disclosed operator phrases.

## Independent Validation

- A separate context-isolated validator receives only the authored bundle and this protocol.
- Validation covers schema, path containment, baseline behavior, source ownership feasibility, public/feedback/final separation, semantic fairness, language balance, mechanism diversity, and author attestations.
- The validator may reject cases or request author-side corrections before freeze. No CHILI model call may occur before validation passes.

## Contestant Visibility

- CHILI sees the operator prompt, candidate source files, and public tests.
- Repair-feedback tests are written only after the initial patch and may guide bounded repair.
- Final-oracle files are loaded only after all model calls for the case have ended.
- Final adjudication runs once in a fresh repository that contains public plus final tests and never repair-feedback tests.

## Frozen Run Policy

- Primary local model: `qwen2.5-coder:7b`.
- Local escalation model: `qwen2.5-coder:14b`.
- Base repair rounds: 2.
- Compact escalation rounds: 1.
- Per-call timeout ceiling: 180 seconds.
- Per-case total model wall budget: 690 seconds.
- Premium calls: 0.
- Deterministic contract operators remain enabled exactly as frozen.
- Per-case checkpoints and final no-more-model-calls guard remain enabled.

## Adjudication

- Authoritative metrics: sealed-final solve rate, exact changed-file ownership, causal-family accuracy, public preservation, premium calls, local-call reliability, and wall time.
- Development replay results cannot replace this score.
- No Fable 5 parity or superiority claim is permitted without an authenticated same-task Fable 5 run and blinded human adjudication.
