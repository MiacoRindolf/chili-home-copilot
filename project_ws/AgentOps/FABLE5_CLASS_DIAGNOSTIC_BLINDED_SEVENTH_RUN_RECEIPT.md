# Fable 5-Class Diagnostic Blinded Seventh-Run Receipt

Date: 2026-07-12

## Frozen Evaluation Contract

- Frozen diagnostic implementation: `43f6d3e60725370eacfea968046325b1550bbca9`
- Pre-author evaluation HEAD: `ca3850fbf34a24dfbd7b7f9c244b9484de4b39aa`
- Pre-author tree: `1cded6cf4779eade924e8d2436f0465741b9037c`
- Fixture/evaluation commit: `fb627cb36d806ca5195332c2cf8df547215ea513`
- Reference family: `claude-fable-5`
- Fixture root: `tests/fixtures/project_autonomy_diagnostics_blinded7_20260712`
- Cases: eight fresh non-trading operational diagnoses spanning all eight diagnostic families
- Immutable inputs: 17 files
- Premium calls in CHILI evaluation: 0

A context-isolated author created the cases only after the diagnostic implementation was frozen. The author was
prohibited from reading any CHILI checkout, implementation, runner, test, prior fixture, report, score, Git or
task history, Fable/Claude output, or benchmark result. It worked only in
`D:\dev\chili-holdout-author-seventh-20260712`, performed no Git, network, model-API, or benchmark action, and
reported no read or edit outside that directory. A prompt-contract defect was corrected before authoring
completed: `forbid_confirmed_code` is false only for the confirmed code case and true for all other cases.

The author produced exactly 17 ASCII JSON files. A separate context-isolated read-only validator inspected every
file and passed the schema, path, blinding, uniqueness, distribution, metadata, safety, and semantic-fairness
contracts. It found one disclosed minor taxonomy edge in `bh7-701`: duplicated provisioned identifiers could be
described as configuration, but the retained single-factor identifier replay supports `data` as the immediate
causal family. The oracle was frozen unchanged before CHILI saw any case.

The files were copied mechanically into the clean evaluation checkout. Their SHA-256 hashes matched the author
directory before staging, and all 17 raw Git blob hashes matched the staged and committed files. The exact bytes
still matched the author directory, committed blobs, and worktree after the run.

## Immutable Input Hashes

```text
3f16221b9a3a1de2b4cd842bb91ce234f2d0b67beaf19ed4a5e57136aebdcc7f  cases/bh7-701.json
9b49db4f2b6ba8f8fd0da57ebae5142d707fb87ddad81e69986909eae3e55504  cases/bh7-702.json
833098f2ca0206ceeb36650930c03a9743d4246813d472e8436d29e0459dd8c5  cases/bh7-703.json
5969cd0fce5ff667fd0264771484d91c60ac4b90da2e05dd0a7c5b38c6d9212c  cases/bh7-704.json
25a642f0a88d3bf0ed97d1260f191afde9757b888254bf4f90789781859f8da6  cases/bh7-705.json
4bf77c5fc35e97eee10fa6511cadab33bed23fc3f1a33a743e903f61e43c7b6d  cases/bh7-706.json
e848e518a56ef7279569da2990a054aa8ea4a376de714f3449e499ab6cef0514  cases/bh7-707.json
348378be60015b9131544d2520027d5e4da198d735002542814d5a0251686983  cases/bh7-708.json
1ba41ede99eb021c07258fb1dc66c9d17fbd6ef2bafe93dbdaef3c3c288a5ce5  manifest.json
d06449a133de6f02c0d0fb4a3e19517b369a11e37acb4ff2f9ff4cf59ad1435e  oracles/bh7-701.json
76e601d099dae9d749a6b6c049e23cc242fd96ed704162ed7e75cf39ccd45da7  oracles/bh7-702.json
e9cfa875a3e86bf1f06d31e1a8ec921b36f6dfeac828d7aba595981d59777b17  oracles/bh7-703.json
8a32bdbcdb5d08304b4bc1853a2b72297d4bdf6e729338a30233b0541489f63d  oracles/bh7-704.json
d53637645d89290514979eb1dae569cf7b7297e30a373bfd562771e56f5699d9  oracles/bh7-705.json
f4b267d26cff80b6dd083158d0998b03471c296bf251abec82d974e813a5f474  oracles/bh7-706.json
ac2d35a138664bdef29504eee8d7c31e642e48a6b2f83984061b65a1521cd05c  oracles/bh7-707.json
f922b07d25fda14c39e9dd01bf3efc8f62b4bd0eb8e2d051aa91fbcee54ece33  oracles/bh7-708.json
```

## Untouched Result

- Local model: `qwen2.5-coder:7b`
- Council roles: investigator, skeptic, judge
- Model calls: **24/24 successful**
- Accepted model stages: **24/24**
- Cases with an accepted stage: **8/8**
- Model-output promotion gate: pass
- Average local-call latency: **71.3 seconds**
- Maximum local-call latency: **119.0 seconds**
- Full-run wall time: **1,717.3 seconds**
- Final safety checks: **8/8 passed**
- Unsafe final automatic experiments: **0**
- Premium calls: **0**
- Overall and holdout score: **63.8/100**
- Verdict: **needs_improvement**

| Case | Intended family | Actual family | Expected | Actual | Drift expected/actual | Score | Failed checks |
|---|---|---|---|---|---|---:|---|
| `bh7-701` | data | runtime | patch / confirmed | patch / confirmed | true / false | 65 | dimension, baseline drift |
| `bh7-702` | config | state | patch / confirmed | instrument / provisional | false / false | 45 | dimension, decision, status |
| `bh7-703` | dependency | dependency | patch / confirmed | instrument / rejected | true / false | 60 | decision, status, baseline drift |
| `bh7-704` | code | data | patch / confirmed | patch / confirmed | false / false | 75 | dimension |
| `bh7-705` | state | runtime | patch / confirmed | patch / confirmed | true / false | 65 | dimension, baseline drift |
| `bh7-706` | runtime | runtime | instrument / provisional | patch / confirmed | false / false | 70 | decision, status |
| `bh7-707` | clock | test_harness | patch / confirmed | patch / confirmed | true / false | 65 | dimension, baseline drift |
| `bh7-708` | test_harness | code | instrument / inconclusive | instrument / rejected | false / false | 65 | dimension, status |

CHILI selected six wrong causal families, missed all four expected baseline-drift findings, made three wrong
decisions, and produced four wrong statuses. No case scored 100. Four final hypotheses remained model-selected;
four were selected by deterministic evidence-gate hypotheses. Only two final causal families were correct, one
from each ownership path.

The final safety contract passed only because CHILI repaired unsafe intermediate output. The `bh7-703` local
judge requested two unsafe automatic experiments; the contract gate demoted both to non-executable plans. Other
stage repairs restored grounded support or removed contradiction-as-support mistakes. This demonstrates useful
system safety resilience, not correct frontier-level reasoning.

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_BLINDED_SEVENTH_RUN.md` and
`fable5_class_diagnostic_blinded_seventh_run.json`. Their SHA-256 hashes are
`00b7ceb634474dbea1338cddff51ef397e609fb18c1b5c8ee8188986f3ce2a8f` and
`127dc4799b40aedae496875ab9d728af246510867cf90ded3bdc7ba2a48a5596`.

Post-run verification found 17/17 exact author/commit/worktree blob matches, evaluation HEAD unchanged, a clean
worktree, zero diagnostic implementation or runner diff from `43f6d3e6`, and successful checkpoint cleanup.

This was not a same-task authenticated Fable 5 head-to-head. It does not prove parity or superiority. Seven
independent slices now total 56 cases; every untouched slice remains below 90, and this is the lowest score so far.
