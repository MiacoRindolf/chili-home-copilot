# Eleventh Sealed Diagnosis-to-Fix Transfer Protocol

## Frozen implementation

- Branch: `codex/fable5-diagnostic-reasoning`
- Core implementation commit: `6cf5b7e0e7da6840da57dec678f8846796265091`
- Evidence baseline commit: `6bc9ebaa999b4b90bda851d1d10b3a348a645c4b`
- Reference family label: `claude-fable-5`
- Premium calls allowed: `0`

Frozen SHA-256 files:

```text
5637784a40907da5576d859e4d2182b8209757666154e348692ab605ab2211e8  scripts/autopilot_diagnosis_to_fix_benchmark.py
975eb82a1375da37d62608b4f865a5c9b659496bfe1c40a01c49afe750c15901  tests/test_diagnosis_to_fix_benchmark.py
f34cbf344da327d32f83a6e7b3bc31946c2cb2c433118ef4c3e5a3239ed75e75  app/services/project_autonomy/diagnostic_reasoning.py
cbc85a4c3e9c223e2e53fd10f16d2c09176937a822d1b13d683db89e2fb2b441  app/services/project_autonomy/orchestrator.py
c0e35a2c277d7e5629061e5f2ca9a3d55b57bc0882f89d789c8d753eb886384c  app/services/coding_task/validator_runner.py
d65b8890328bcf6a519370f670f50de41a97bb72cb92c9657f7b27be13d5052e  app/services/code_brain/agent.py
```

The focused evaluator regression suite passed 65 tests at this source state. The implementation, runner,
diagnostic prompts, repair policy, and model route are frozen before the controller can inspect any eleventh-suite
case or oracle body.

## Authorship isolation

Three independently spawned context-isolated authors create disjoint Python, Node/TypeScript, and Dart/SQL fixture
lanes in external directories. They may not read CHILI source, Git history, prior fixtures or reports, conversation
history, Claude/Fable material, or the internet. They receive only the neutral fixture contract and runner naming
rules.

A fourth context-isolated validator combines the lanes, checks schema and fairness, runs every baseline, verifies
multi-file necessity and oracle separation, and hashes the suite. It may correct fixture-only defects before freeze,
but must record each correction. The parent controller receives metadata and hashes only until the scored run ends.

## Suite contract

- Exactly 12 independently authored cases.
- Language mix: 4 Python/pytest, 4 TypeScript-compatible Node ESM, 2 Dart, and 2 SQL/pytest.
- Every case requires coordinated changes to 2-4 source owners.
- At least six cases use mechanism families intended to fall outside the disclosed deterministic operator library.
- Prompts describe noisy real-world symptoms, healthy controls, and safety constraints without naming the repair.
- Public tests pass on the defective seed.
- Repair-feedback tests fail on the defective seed.
- Sealed final tests independently fail on the defective seed.
- Feedback and final paths and payloads are disjoint.
- Candidate paths contain source only, never tests or oracles.
- Final oracle files are first loaded after every model call for that case.
- No network, credentials, runtime services, trading state, or destructive operations.

## Precommitted run policy

- Primary model: local `qwen2.5-coder:7b`
- Primary repair rounds: `2`
- Escalation model: local `qwen2.5-coder:14b`
- Escalation repair rounds: `1`
- Per-call timeout: `240` seconds
- Premium fallback: forbidden
- Fixture validation runs before the first contestant call.
- No source, runner, settings, prompts, model route, case, feedback, or final-oracle edits after fixture freeze.
- The first complete untouched result is authoritative even if it fails.

## Interpretation

This gate tests transfer beyond the eight disclosed mechanisms; it cannot by itself establish universal Fable 5
parity. Replacement readiness still requires at least 30 fresh tasks, at least 95% sealed-final success across three
reproductions, real repository and incident-shadow trials, and an authenticated same-task blind comparison against
Fable 5. No disclosed replay may overwrite this untouched result.
