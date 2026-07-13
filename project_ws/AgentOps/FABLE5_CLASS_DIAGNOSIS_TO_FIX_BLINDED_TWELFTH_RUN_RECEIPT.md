# Fable 5-Class Diagnosis-to-Fix Blinded Twelfth Run Receipt

Date: 2026-07-12

## Verdict

- Score: **49.17/100**.
- Evaluation verdict: `blinded_evaluation_failed`.
- Sealed-final solves: **2/12 (16.67%)**.
- Correct causal families: **7/12 (58.33%)**.
- Causally accepted diagnoses: **6/12 (50.0%)**.
- Exact changed-file owner sets: **6/12 (50.0%)**.
- Public regression preservation: **12/12**.
- Repair-feedback pass: **5/12**.
- Retained patches: **7/12**.
- Premium calls: **0**.
- Fable 5 parity claim: **No**.

This improves on the eleventh untouched result of 32.92/100, 0/12 finals, 4/12 diagnosis families, and 1/12
owner sets. It remains far below replacement readiness.

## Frozen Provenance

- Implementation commit: `9b69cd8db6d9a97717422202e751b762b7ff7dc7`.
- Fixture commit: `fdfaba853feec1a0f6c158c2cfe10a7ad7a0d3c2`.
- Freeze receipt commit: `bf35538`.
- Fixture tree: `2d797d0f972b47489979b5e2d3793886d9709d67`.
- Pre-run and post-run fixture aggregate SHA-256: `a24a99f3fe16b84f5acf78c609ad6f33d96a81d3cbea0cc56c5ca44ebb397979`.
- Source/runner diff from implementation commit through run completion: empty.
- Independent validator V3 verdict: PASS.
- Fixture preflight: `fixtures_valid=True cases=12`.

## Run Policy

- Primary model: `qwen2.5-coder:7b`.
- Compact escalation model: `qwen2.5-coder:14b`.
- Base repair rounds: 2.
- Escalation repair rounds: 1.
- Per-call timeout ceiling: 180 seconds.
- Per-case total model wall budget: 690 seconds.
- Deterministic repair operators recognized: **0/12**.
- Final oracles loaded after all model calls for each case: **12/12**.
- Model calls after final adjudication began: **0**.

## Runtime Evidence

- Total local calls: **141**.
- 7B calls: **126/126 successful**.
- 14B calls: **14/15 successful**.
- Failed calls: **1**, a pinned-host 14B timeout.
- Budget-exhausted synthetic calls: **0**.
- Summed model-call wall time: **3,384.5 seconds**.
- Process wall time: **3,592.2 seconds (59.9 minutes)**.
- Average case time: **298.6 seconds (5.0 minutes)**.
- Previous eleventh process wall time: **14,596.7 seconds (243.3 minutes)**.

The single 14B timeout did not trigger fallback to another Ollama host. Compact escalation prevented generative
review, adapter retry, and model compiler-recovery fan-out.

## Solved Cases

- `node_tls_client_auth_config`: diagnosis, exact two-file ownership, public, feedback, and isolated final all passed.
- `sql_tenant_stock_ownership`: diagnosis, exact two-file ownership, public, feedback, and isolated final all passed.

`node_base64url_blob_ids` passed repair feedback but failed its materially new final alias boundary. `py_config_reload`
passed feedback with only one of two required owners retained and failed final. `sql_ticket_archive_transitions`
passed feedback with exact owners but failed the composed final transition.

## Artifact Hashes

- Markdown report SHA-256: `ce7e39228f2c046f50a003bc11791d9b0ca6eed56c4c923423edbbdbb048ee7b`.
- Result JSON SHA-256: `52fa5019dcf10691938bc6102d7f3fbdc5436ac99cddb576ec191f8f08195274`.
- Stdout log SHA-256: `bc026ecfb5d88f0622a1d110ce0445f8682e9d0ed1a53ff7bc48c53e9d102edc`.
- Stderr log SHA-256: `1701eae9c2ecd35504d2f2f0133e5699489403594d695014435b90b628290a24`.

## Interpretation

The batch proves a large operational improvement: unseen 12-case wall time fell from 243.3 to 59.9 minutes,
local-call failures fell from 22 to 1, diagnosis accuracy rose from 4/12 to 7/12, exact ownership rose from 1/12
to 6/12, and sealed solves rose from 0/12 to 2/12. It also proves that boundedness alone is insufficient. Ten of
twelve unseen complex repairs still failed sealed final adjudication. CHILI remains a premium-independent shadow
system, not a validated replacement for Fable 5 on complex real-world diagnosis.
