# Fable 5-Class Diagnostic Blinded Fourth-Run Receipt

Date: 2026-07-11

## Frozen Evaluation Contract

- Frozen implementation SHA: `b8616f6273480ca892c229d74021a6ef0c3c411f`
- Evaluation/fixture commit: `d58e880806a1d62ff3df54b6baa162e78dc789b5`
- Reference family: `claude-fable-5`
- Fixture root: `tests/fixtures/project_autonomy_diagnostics_blinded4_20260711`
- Cases: eight fresh non-trading operational diagnoses spanning all eight diagnostic families
- Immutable inputs: 17 files
- Final input freeze: `2026-07-11T22:33:47.8110819Z`
- Premium calls: 0

An independent context-isolated author created the cases after source commit `b8616f6` was frozen. The
author was prohibited from reading prior diagnostic fixtures, benchmark/repair reports, CHILI diagnostic
implementation/tests, chat archives, or historical Fable answers. The first author process hit a platform
usage limit before producing any file and was discarded. The completed author confirmed no prohibited read
and did not disclose case-family or oracle mappings before evaluation.

Pre-model structural validation found three schema-shape issues in author-owned metadata: manifest key
`entries` instead of `cases`, entry keys `case_path`/`oracle_path` instead of `case`/`oracle`, and noncanonical
`expected_dimensions` shape. The same author corrected only those shapes while preserving labels and values.
No model call, score, public case display, or oracle mapping had occurred. Final validation then passed exact
file count, JSON syntax, ASCII safety, IDs, references, required observation fields, eight-family coverage,
six confirmed cases, two uncertainty cases, public absence of oracle fields, and all public dimensions set to
`unknown`.

## Immutable Input Hashes

The same hashes were observed immediately before the first model call and after the run. All 17 staged Git
blobs also matched the evaluated worktree bytes.

```text
91c1ea081531af2f27910d372d061868c9a1ff604f0d14b3f59282f4603807ca  cases/bh4-401.json
dd30cfe17c03f801fc45483b0ff6a403fbc01fbe2325198e3719c8a042f793d0  cases/bh4-402.json
40f3c1a4051ca86ca4ee44df10607c36031f4d2dd57d0bc1e86578eadf7dfe9f  cases/bh4-403.json
27250a5aa8227289f890b3656d83b575cf44576137c71eed14d69907858e2a23  cases/bh4-404.json
16e3ac8fe145cd45dfd16418e82a9b5a54e8738d46d9a9050e07546de60a38bb  cases/bh4-405.json
e5f08b0e3e29484cdda3b6a66e86285cd1a4a0972b8a7fd1135037865482ed0f  cases/bh4-406.json
21dba8af9001756a3fe5596a639011143d2501a41e5cea3ff0141a3f5738caad  cases/bh4-407.json
f7ca22e81d07a5ce88f2e3a3f44c0a170b9efda19ad2c28b8241a3614cfe1105  cases/bh4-408.json
f5a9d5e7d2de0c07d14457983b48535dcb82e8139d033e42dec423844dcaac7c  manifest.json
b4f4f5e056c61787b6fc7c9d05b41dc1c03205e78ea9d75ed79038f7c56d3671  oracles/bh4-401.json
f176316f68dbc97258458ab0a7dec28f8366a2f56882b501f17ce5b4e23606e0  oracles/bh4-402.json
573ca418bea3f8fe60fcf0c6f20f0da5b717484a9c75d0fdd1c0624f867ad525  oracles/bh4-403.json
98946aacf7f8eda2dc8ae32157a7845a7962ca9b78ba6f28d4ff38a4be1f0180  oracles/bh4-404.json
0181885b463b764b1a6f22d4d2b57d190011606b6cff4e9450db1b78581699f7  oracles/bh4-405.json
39d3436496e724edcfc70c4f70f0fa393f4ba252d37b13b2a22f6865576b54dc  oracles/bh4-406.json
76166a0990efa40780248ee7f3952d9e035159c0f7ec4d0ae55bf750576aefc1  oracles/bh4-407.json
4d003da2611df840fdb53ae896fdcf43f4829ab7137110ddd7fb9ff49f64005d  oracles/bh4-408.json
```

Post-run verification at `2026-07-11T22:43:13.9629998Z` found 17/17 byte matches, evaluation HEAD
unchanged, a clean worktree, and zero implementation-source diff from `b8616f6`.

## Untouched Result

- Local model: `qwen2.5-coder:7b`
- Council roles: investigator, skeptic, judge
- Model calls: **24/24 successful**
- Accepted model stages: **24/24**
- Cases with an accepted stage: **8/8**
- Model-output promotion gate: pass
- Average local-call latency: **22.65 seconds**
- Maximum local-call latency: **31.68 seconds**
- Unsafe final automatic experiments: **0**
- Premium calls: **0**
- Overall and holdout score: **69.38/100**
- Verdict: **needs_improvement**

| Case | Intended family | Actual family | Expected | Actual | Score | Failed checks |
|---|---|---|---|---|---:|---|
| `bh4-401` | runtime | runtime | patch / confirmed | instrument / provisional | 70 | decision, status |
| `bh4-402` | dependency | data | patch / confirmed | patch / confirmed | 75 | dimension |
| `bh4-403` | state | state | patch / confirmed | instrument / provisional | 70 | decision, status |
| `bh4-404` | code | dependency | patch / confirmed | instrument / provisional | 45 | dimension, decision, status |
| `bh4-405` | test_harness | data | instrument / inconclusive | instrument / provisional | 65 | dimension, status |
| `bh4-406` | data | data | patch / confirmed | patch / confirmed | 100 | none |
| `bh4-407` | config | data | patch / confirmed | instrument / provisional | 40 | dimension, decision, status, breadth |
| `bh4-408` | clock | clock | instrument / provisional | instrument / provisional | 90 | baseline drift |

The untouched result is strong negative evidence. Only one case scored 100. CHILI selected three wrong causal
families, under-confirmed three otherwise correctly identified confirmed cases, missed the intended uncertainty
status in one case, and missed one baseline-drift flag. Structurally usable local output and zero premium calls
did not translate into Fable 5-class diagnostic quality on this slice.

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_BLINDED_FOURTH_RUN.md` and
`fable5_class_diagnostic_blinded_fourth_run.json`.

This was not a same-task authenticated Fable 5 head-to-head. It does not prove parity or superiority. The
four independent slices now total 32 cases, satisfying the numeric diagnostic-count target but not the quality,
repository/language-diversity, repeated-run, or direct-comparison gates.
