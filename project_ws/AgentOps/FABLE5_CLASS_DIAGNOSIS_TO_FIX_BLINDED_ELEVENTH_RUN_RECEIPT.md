# Eleventh Sealed Diagnosis-to-Fix Run Receipt

Run completed: 2026-07-12

## Classification

This is the authoritative **untouched transfer result** for the eleventh suite. Three isolated authors created the
cases after the core implementation freeze, an isolated validator assembled and checked them, and the controller
did not inspect case or oracle bodies before scoring. The first complete run is retained even though it failed.

## Frozen Inputs And Policy

- Core implementation commit: `6cf5b7e0e7da6840da57dec678f8846796265091`
- Policy commit: `c67eacca02e77aed51a6360729af28a60d6f08ff`
- Fixture commit: `5445302654e31c56e652756817be2415f7208cf0`
- Fixture tree: `667c5986ba0cd91542d08b882d7b24b4456e1d10`
- Freeze receipt commit: `23dfa014ff995d3c90468b3c383b6a03ede5ec97`
- Post-adapter fixture aggregate SHA-256: `afc0821baec0a5b386b79e00433b777c126431973df600054a1ed283ac05261d`
- Primary model: local `qwen2.5-coder:7b`
- Repair policy: two primary rounds
- Escalation model: local `qwen2.5-coder:14b`
- Escalation policy: one final repair round
- Configured per-call timeout: 240 seconds
- Premium calls allowed: 0

One pre-freeze fixture-only adapter renamed the manifest collection key from `entries` to the frozen runner's
`cases` key. It occurred before any contestant call and changed no prompt, source, oracle, assertion, behavior, or
difficulty. CHILI's own preflight then confirmed 12/12 public passes, 12/12 feedback failures, 12/12 final failures,
and separate external final oracles.

## Result

- Overall: **32.92/100**, `blinded_evaluation_failed`
- Sealed-final functional success: **0/12 (0%)**
- Correct causal families: **4/12 (33.33%)**
- Exact changed-file sets: **1/12 (8.33%)**
- Public regressions preserved: **12/12**
- Repair-feedback success: **1/12**
- Cases retaining any patch: **5/12**
- Recognized deterministic repairs: **0/12**
- Local model calls: **226**
- Successful local calls: **204**
- 7B calls: **170/170 successful**
- 14B calls: **34/56 successful; 22 timed out**
- Premium calls: **0**
- Calls after final adjudication began: **0**
- Case-time average: **20.27 minutes**
- Process wall time: **14,596.7 seconds (243.3 minutes)**

## Integrity

- Generated report SHA-256: `3bbefc39c9ae7cd57e5733ead5380214faf776f07942e62d0abb65e2c5c47f6c`
- Results JSON SHA-256: `3af56d639c6d060a3238e7342cef83c685a6a1697e68c29c2294904f9b01421e`
- Run-state JSON SHA-256: `765346b78795bf0e1ed331e097260d4faaf128327f778cbc45b541b6d20e9fa0`
- Runner stdout SHA-256: `810a1a009a664ad9adf001feb931541ae187154c4292c8ce292d64b677f47d77`
- Runner stderr SHA-256: `a93f7c11d648cca9766e67d8b5558f218c5ebb297197371b38380e4ea351969f`
- Implementation tree changed during execution: no
- Scored-run fixture, policy, model-route, or timeout changes: none

## Interpretation

The run proves that CHILI is not a Fable 5 or Sol wrapper: all 226 calls were local, public regressions stayed
green, final adjudication remained sealed, and no premium route existed. It also proves that CHILI is **not yet a
credible Fable 5 replacement for unseen complex diagnosis-to-fix work**. The disclosed 100/100 replay measured
regression mastery, while this fresh suite exposed weak causal-family calibration, weak source ownership, failure
to recognize abstract transfer variants, low patch retention, and an unreliable high-fan-out 14B fallback.

This result must never be overwritten by development replays. Repairs may use the now-disclosed suite for causal
development, but replacement readiness requires another independently authored untouched suite after the next
source freeze.
