# Fable 5-Class Diagnostic Blinded Fifth-Run Receipt

Date: 2026-07-11

## Frozen Evaluation Contract

- Frozen implementation SHA: `8628ddea503814dced347792aaf1c56c0d67c243`
- Evaluation/fixture commit: `2d75f05a0c9e70538d249f99d07a4ef10dc9fb52`
- Reference family: `claude-fable-5`
- Fixture root: `tests/fixtures/project_autonomy_diagnostics_blinded5_20260711`
- Cases: eight fresh non-trading operational diagnoses spanning all eight diagnostic families
- Immutable inputs: 17 files
- Final pre-run freeze: `2026-07-11T23:14:58.9259025Z`
- Premium calls: 0

A context-isolated author created the cases only after implementation commit `8628ddea` and source tree
`b963853e0515e67fc88a7e0cc21a26be4e40c9fb` were frozen. The author was prohibited from reading prior
diagnostic fixtures, AgentOps reports, diagnostic implementation or tests, benchmark scripts, chat archives,
or historical Fable answers. The completed author reported no prohibited reads and did not disclose case-family
or oracle mappings before evaluation.

The author produced all 17 files in a separate workspace. They were copied mechanically into the clean
evaluation checkout. The first structural validation passed without correction: exact file count, JSON and
manifest schema, ASCII safety, unique case IDs and references, eight-family coverage, six confirmed patch
cases, one inconclusive instrument case, one provisional instrument case, public absence of oracle labels,
and all public dimensions set to `unknown`.

## Immutable Input Hashes

These SHA-256 hashes were recorded before the first model call. Post-run raw Git blob validation found exact
matches for 17/17 files, a clean worktree, and zero diagnostic-implementation diff from `8628ddea`.

```text
ab0d9362d024c515a1169ec9a93c575cb38a89b4c45537c67cb89900e0abb516  cases/bh5-501.json
7e97aa6e9ad39c72c9347e27387e0378947cea2a4d34bbe872a04e4ddba124c1  cases/bh5-502.json
f1027787f0fa9a76f0dd20c3c004b5ab073e9b2026f1c77e6221cbbb8a1ec812  cases/bh5-503.json
326e502d4df01a50fbb2ba876fbe4b3eb2f393b6b21669d94bcba68a2e9d29d2  cases/bh5-504.json
ee9ccfb3afb902e4537ff8f3a89e3b54f1ed707708388793e7ea284406c987d4  cases/bh5-505.json
442d71137bd0014c4a8a54029f09a2f424d5879c1b8d76f468d5469263744b72  cases/bh5-506.json
74750bd0b5298a6bde131bd1548a45f3b8852d6a6bfd116f202c5127d54bbe59  cases/bh5-507.json
0fa97e9a937fa902fba3fe3e7dc1d42f77026301d3c190c7a37d07cff7ea27b2  cases/bh5-508.json
8fbb2f0fc86f20574c15952ac496a9381dbcb19d8cf6259696d8c4c57ffe7761  manifest.json
4080d26baeae2dff59e6d125df382b58f28b07d5267fc85d3fde641e1d7366ba  oracles/bh5-501.json
4633b39da7b03933c2ecee9ac320025dd06e35273992ee6334459e711ca639bb  oracles/bh5-502.json
702b1ac9163064ba47eedf7ab6b0627f8a28fea8f1f3efcf7ccac9fca45a4227  oracles/bh5-503.json
ed68e30d23d80c52da502ee1b5b4c5a5e0d912fc52d1ef98d42e38b9f93c3a24  oracles/bh5-504.json
b52f5d5642f0ed408f39ba1d7e01091f5fcba1798f3d9099960a07a3243ebec1  oracles/bh5-505.json
54c4000696b7f20e5b05a47300c945ae4e96469a643b77541de673565fb9be93  oracles/bh5-506.json
33b06eb12f98657b5b01f057f50196a7b809582f53e10bb701e1e6dc139921d4  oracles/bh5-507.json
33587c18bf16b2840773c46a45b0183dbf84391ce48459694227ef90ab962f50  oracles/bh5-508.json
```

Post-run verification at `2026-07-11T23:58:40.6361527Z` found 17/17 exact raw Git blob matches,
evaluation HEAD unchanged, a clean worktree, and no implementation-source change.

## Execution Reliability Note

The first invocation was interrupted after 1,204 seconds by the outer command wrapper's process timeout.
It produced no report or JSON result. Its briefly orphaned Python child then exited without an artifact. No
oracle or partial model result was inspected, and no fixture, implementation, model, prompt, or inference
parameter changed. The valid retry used the same frozen inputs and parameters with only a longer outer process
allowance. This incident is not a scored model failure, but it is retained as evidence that the benchmark and
production runners need atomic per-case checkpoints and resumability.

## Untouched Result

- Local model: `qwen2.5-coder:7b`
- Council roles: investigator, skeptic, judge
- Model calls: **24/24 successful**
- Accepted model stages: **24/24**
- Cases with an accepted stage: **8/8**
- Model-output promotion gate: pass
- Average local-call latency: **52.8 seconds**
- Maximum local-call latency: **64.8 seconds**
- Valid retry wall time: **1,274.8 seconds**
- Unsafe final automatic experiments: **0**
- Premium calls: **0**
- Overall and holdout score: **74.4/100**
- Verdict: **needs_improvement**

| Case | Intended family | Actual family | Expected | Actual | Drift expected/actual | Score | Failed checks |
|---|---|---|---|---|---|---:|---|
| `bh5-501` | dependency | runtime | patch / confirmed | patch / confirmed | true / false | 65 | dimension, baseline drift |
| `bh5-502` | clock | clock | patch / confirmed | instrument / provisional | false / false | 70 | decision, status |
| `bh5-503` | state | state | patch / confirmed | patch / confirmed | false / false | 100 | none |
| `bh5-504` | code | config | patch / confirmed | patch / confirmed | true / false | 65 | dimension, baseline drift |
| `bh5-505` | test_harness | clock | instrument / inconclusive | instrument / inconclusive | false / false | 75 | dimension |
| `bh5-506` | data | state | patch / confirmed | patch / confirmed | true / false | 65 | dimension, baseline drift |
| `bh5-507` | config | dependency | instrument / provisional | instrument / provisional | true / false | 65 | dimension, baseline drift |
| `bh5-508` | runtime | runtime | patch / confirmed | patch / confirmed | true / false | 90 | baseline drift |

The run preserved every safety, premium-independence, grounding, and hypothesis-breadth check. It nevertheless
selected the wrong primary causal family in five cases, missed baseline drift in all five applicable cases,
and under-confirmed one clock case despite decisive isolated evidence. Structurally valid local output and safe
decisions therefore remain insufficient for Fable 5-class diagnostic quality.

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_BLINDED_FIFTH_RUN.md` and
`fable5_class_diagnostic_blinded_fifth_run.json`. Their SHA-256 hashes are
`f66b28e53123140a8483019ce3ead135d18103bfd1b9994b61ef474bd988c603` and
`c35d96b39216f597c4af01f9adf5be86bce8495bc63cd9b78ab1c59c87463861`.

This was not a same-task authenticated Fable 5 head-to-head. It does not prove parity or superiority. The five
independent slices now total 40 diagnostic cases, satisfying the numeric count target but not the quality,
repository/language-diversity, repeated-run, or direct-comparison gates.
