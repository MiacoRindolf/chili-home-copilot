# Fable 5-Class Seventh-Slice Local Model Challenger

Date: 2026-07-12

## Evidence Classification

- Authoritative untouched result: `qwen2.5-coder:7b`, **63.8/100**
- Frozen fixture commit: `fb627cb36d806ca5195332c2cf8df547215ea513`
- Untouched result commit: `7fa229e`
- Local thinking-control implementation: `1792bcea1a129e77e040ad93e1dfc35fd582e87b`
- Challenger: `qwen3:8b`, Q4_K_M, 8.2B parameters, 5.2GB model artifact
- Challenger mode: explicit Ollama top-level `think=false`
- Reference family: `claude-fable-5`
- Premium calls: 0

The seventh cases were already disclosed by the untouched 7B scoring run. All results in this document are
development challenger evidence. They do not overwrite the 63.8 untouched score and cannot establish unseen
generalization or Fable 5 parity.

## Thinking-Control Finding

The installed Qwen3 model exposes completion, tool, and thinking capabilities and occupies 6.2GB at an 8,192
token context, entirely on the RTX 2070 GPU. Under the model-default thinking mode, a three-stage development
smoke was operationally unusable: investigator and skeptic each timed out after about 303 seconds, while only the
already-warmed judge completed in 27.4 seconds.

CHILI's Ollama client previously had no way to send the native API's top-level `think` field. Commit `1792bce`
added explicit optional thinking control, `--think`/`--no-think` benchmark flags, report metadata, and checkpoint
binding. Existing callers retain model-default behavior. Focused and broad relevant validation passed **83
tests**.

With `think=false`, the same development smoke completed **3/3 accepted stages**, scored 100/100, and took 37.2,
26.5, and 25.1 seconds per stage. This established compatibility, not model superiority.

## Full Same-Fixture Comparison

| Metric | Qwen2.5-Coder 7B untouched | Qwen3 8B no-think challenger |
|---|---:|---:|
| Score | 63.8 | 61.9 |
| Transport-successful calls | 24/24 | 24/24 |
| Accepted stages | 24/24 | 22/24 |
| Average latency | 71.3s | 35.8s |
| Maximum latency | 119.0s | 43.1s |
| Wall time | 1,717.3s | 864.8s |
| Final safety checks | 8/8 | 8/8 |
| Premium calls | 0 | 0 |

| Case | Intended | 7B result | 8B result | 7B score | 8B score |
|---|---|---|---|---:|---:|
| `bh7-701` | data / confirmed patch | runtime / confirmed patch | runtime / confirmed patch | 65 | 65 |
| `bh7-702` | config / confirmed patch | state / provisional instrument | runtime / rejected instrument | 45 | 45 |
| `bh7-703` | dependency / confirmed patch | dependency / rejected instrument | runtime / provisional instrument | 60 | 35 |
| `bh7-704` | code / confirmed patch | data / confirmed patch | data / confirmed patch | 75 | 75 |
| `bh7-705` | state / confirmed patch | runtime / confirmed patch | runtime / confirmed patch | 65 | 65 |
| `bh7-706` | runtime / provisional instrument | runtime / confirmed patch | runtime / confirmed patch | 70 | 70 |
| `bh7-707` | clock / confirmed patch | test_harness / confirmed patch | test_harness / confirmed patch | 65 | 65 |
| `bh7-708` | test_harness / inconclusive instrument | code / rejected instrument | code / inconclusive instrument | 65 | 75 |

The challenger improved only the uncertainty status in `bh7-708` and regressed the causal family plus decision
path in `bh7-703`. It selected seven wrong families, missed all four drift findings, made three wrong decisions,
and made three wrong statuses. Five final hypotheses were model-selected and three came from deterministic
evidence-gate hypotheses. The two rejected packets were an investigator that cited unknown evidence IDs and an
investigator that emitted duplicate or empty hypothesis IDs.

Most importantly, the current deterministic heuristic-only replay scored **61.9/100**, exactly matching the 8B
challenger's aggregate and per-case contract outcomes except for an equal-score family substitution in
`bh7-702`. The dominant bottleneck is therefore CHILI's causal evidence interpretation and calibration layer,
not simply local parameter count.

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_SEVENTH_QWEN3_8B_NOTHINK_RUN.md` and
`fable5_class_diagnostic_seventh_qwen3_8b_nothink.json`. Their SHA-256 hashes are
`c055b72bcd5b73282169a34290f785f1938fdb0b1eb33aebebbddaed5e02f225` and
`4f27ab17687d6d1e62db96c94c4f0d86e6a5ace76636f01b587138e73e0209e4`.

## Decision

Do not promote Qwen3 8B as CHILI's default reasoning model from this evidence. Explicit no-think mode is a useful
local capability and roughly halves latency, but the model did not improve quality on the disclosed slice. The
next repair must target generic family ownership, retained baseline drift, and uncertainty/decision calibration.
Any later model promotion must reproduce on a newly authored untouched slice and across diagnosis-to-fix tasks.
