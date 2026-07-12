# Tenth Sealed Diagnosis-to-Fix Holdout Protocol

## Frozen implementation

- Branch: `codex/fable5-diagnostic-reasoning`
- Source HEAD: `3241f6743e8b8745c39542f4645290a4a8d6f44a`
- Production repair commit: `550b7e4`
- Evidence-only child commit: `3241f67`
- Reference family label: `claude-fable-5`
- Premium calls allowed: `0`

Frozen SHA-256 files:

```text
48c25c43cc9a2aa60ea967dff11111a4f2c5b3819b3439dce392128787350ad3  scripts/autopilot_diagnosis_to_fix_benchmark.py
4f74592e80b649773cb88002c4749c0c1922470a15a53e30f658ad6cc6512d15  tests/test_diagnosis_to_fix_benchmark.py
2ca872a4e0c05ce327614a8cbab51d6327ffffcbf82fab1530d5f75043106a1e  app/services/project_autonomy/diagnostic_reasoning.py
7a89537f7598ad299b7ed4e64bdd2817eb829a8d803c2b0742d1783300922844  app/services/project_autonomy/orchestrator.py
16aa1a325c8a7391418f8f967251bebfd9765d40ba75eb751bf5738f8dc8e419  app/services/coding_task/validator_runner.py
```

## Authorship isolation

An independently spawned context-isolated author must create the suite in a separate external directory. The author may not read CHILI source, Git history, prior fixtures, benchmark reports, Fable/Claude transcripts, user chat history, or the internet. The parent controller receives only filenames, counts, hashes, baseline pass/fail counts, and an authorship attestation before scoring; it must not inspect case or oracle bodies.

A second context-isolated validator must inspect the authored suite without reading CHILI history or prior fixture content. It must verify schema, fairness, language mix, multi-file necessity, path containment, public/feedback/final separation, and baseline behavior. It may repair fixture-only defects before the fixture freeze, but must report every change and final aggregate hash.

## Suite contract

- Exactly 8 independently authored cases.
- Language mix: 2 Python/pytest, 2 TypeScript/node_test, 2 Dart/dart, 2 SQL/pytest.
- Every case must require coordinated changes to 2-4 candidate source files.
- Every case must set explicit `max_files` equal to the intended maximum source ownership breadth.
- Mechanisms and incident wording must be independently invented and materially different from common toy bugs.
- Public tests must pass on the defective seed.
- Repair-feedback tests must fail on the defective seed.
- Sealed final tests must fail on the defective seed.
- Feedback and final test files and assertions must be disjoint.
- Candidate paths may contain source only, never tests or oracles.
- Final oracle files must never be loaded before all model calls for that case finish.
- No network, credentials, runtime services, trading state, or destructive operations.

## Precommitted run policy

- Primary model: `qwen2.5-coder:7b`
- Primary repair rounds: `2`
- Escalation model: `qwen2.5-coder:14b`
- Escalation repair rounds: `1`
- Per-call timeout: `240` seconds
- Premium fallback: forbidden
- Fixture validation runs before the first contestant call.
- No source, runner, settings, prompt, model, case, feedback, or final-oracle edits after the fixture freeze.
- The first complete untouched result is authoritative even if it fails.

## Promotion interpretation

This one suite cannot establish universal Fable 5 parity. It is a post-fix transfer test. The primary measures are sealed-final functional success, correct causal family, exact changed-file ownership, safety, premium calls, and latency. Development replays cannot overwrite this result.
