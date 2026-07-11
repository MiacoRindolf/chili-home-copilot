# CHILI Sequential Diagnostic Probe Policy

Date: 2026-07-11

## Purpose

A fixed batch can spend the entire evidence budget on low-value checks and cannot react when a probe is blocked or disproves the leading hypothesis. CHILI now selects and executes one bounded probe at a time, re-evaluates the case, and then chooses the next probe from the updated evidence state.

## Selection Policy

`rank_probes_for_report` scores only validated typed probes. Ranking considers:

- relevance to the current conclusion
- unresolved hypothesis families
- recommended counterfactual dimensions
- the structured earliest-break dimension
- probe-kind and causal-family affinity
- baseline-drift isolation value
- read-only preference and bounded execution cost

Every selected probe records `selection_score` and `selection_reasons`.

## Sequential Loop

Project Autonomy now:

1. Runs the initial local diagnostic council.
2. Builds prompt-grounded and model-proposed typed probe candidates.
3. Selects the highest-value unattempted probe.
4. Executes exactly that probe inside the existing safety envelope.
5. Appends fresh evidence without dropping it at the observation cap.
6. Runs a post-probe local judge and updates hypotheses.
7. Adds any newly proposed typed probes and re-ranks.
8. Stops on a probe-grounded confirmed conclusion, a stable confirmed conclusion, no admissible probe, count limit, or time limit.

The default hard limits remain four attempted probes and 120 seconds total. Environment variables may reduce or raise them only within the compiled bounds of one to six probes and ten to 300 seconds.

## Safety

- The probe catalog still has no raw-command operation.
- Automatic execution remains limited to read-only or isolated kinds.
- Attempted probe IDs cannot repeat.
- A blocked, failed, or timed-out probe does not bypass validation; the selector moves to the next admissible candidate.
- The artifact records every round, result, evidence item, candidate, attempted ID, stop reason, duration, local model call, and `premium_calls=0`.

## Validation

- Dependency failures prioritize bounded log search over unrelated repo or database probes.
- Once attempted, the same probe is not selected again.
- Structured state earliest-break evidence prioritizes the state profile.
- The production planning path performs bounded sequential judge rounds and records selection rationale.
- Focused diagnostic, runtime-evidence, probe, and service suite: **166 passed**.
- Broad routing, evidence, repair, identity, and autonomy suite: **301 passed**.

This improves adaptive diagnosis but does not yet provide automatic trace correlation or prove broad Fable 5 parity.
