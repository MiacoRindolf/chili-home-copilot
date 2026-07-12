# Fable 5-Class Diagnostic Blinded Sixth-Run Receipt

Date: 2026-07-12

## Frozen Evaluation Contract

- Frozen implementation SHA: `aa2821db9c67444bb6d3ce5cc63c71bdbfe1756c`
- Evaluation/fixture commit: `90e88dbd1eeca7612ba9e9db2949e19d5a18bae8`
- Pre-author evaluation HEAD: `39ab1636f5de35c6c53aee301151f19d212e70a4`
- Pre-author source tree: `741469d5df3710d83f2edfc00acf46a53010bec8`
- Reference family: `claude-fable-5`
- Fixture root: `tests/fixtures/project_autonomy_diagnostics_blinded6_20260712`
- Cases: eight fresh non-trading operational diagnoses spanning all eight diagnostic families
- Immutable inputs: 17 files
- Final pre-run freeze: `2026-07-12T00:56:58.7228043Z`
- Premium calls: 0

A context-isolated author created the cases only after implementation commit `aa2821db` was frozen. The author
was prohibited from reading prior diagnostic fixtures, AgentOps reports, diagnostic implementation or tests,
benchmark scripts, Git history, chat/task history, Claude/Fable archives or answers, or any prior benchmark or
repair result. The author reported no prohibited read, external or premium model call, benchmark run, Git action,
CHILI output inspection, or edit outside the assigned isolated directory.

The author produced all 17 files in `D:\dev\chili-holdout-author-sixth-20260712`. Independent pre-model
validation passed on the first completed artifact set with no correction: exact file count, parseable ASCII JSON,
matching IDs and references, eight unique families, six confirmed patch cases, one provisional instrument case,
one inconclusive instrument case, both baseline-drift values, 7-10 observations per case, public absence of oracle
fields, and every public observation dimension set to `unknown`. The directory was then copied mechanically into
the clean evaluation checkout.

## Immutable Input Hashes

These SHA-256 hashes matched the isolated author output, copied checkout, staged Git blobs, committed Git blobs,
and post-run worktree.

```text
4fbae52770465278eeb5c61f8d6d8b0430f7fc7aaff0630eded3c2f611620f3c  cases/bh6-601.json
ce36e86c3e43d5595b5ed902c8200471572393f36cd2394b4b0390f5aad1b173  cases/bh6-602.json
db79aeec0d9c2110a27fbe9c3e74771e88f43164893705241ede786f5dbc14de  cases/bh6-603.json
9c27bc20ccd973e1c89be574070070f687ee3159868f8c636695c62bd0fe2f3d  cases/bh6-604.json
cc700255368166d5e5d9fa738f2fce7f6bb1aae73a70e42c08e5fa2a95108e91  cases/bh6-605.json
0e370fa281fa93bec42d5f4d095d33cc92a2fe1f689ab8a912de582e1ffea4b1  cases/bh6-606.json
cf5222cc7fbf1cea1d5b3bc00ec3be3cb2b07b537950d70252375a62298d80f6  cases/bh6-607.json
b4bb7ea8e170b824fe2eeb48467063f64d8b989fdbd1f49ee5ebef2f53a5b8f4  cases/bh6-608.json
470f09d427d4935660287814f36cea3d058aab137f9694168241e603503646ee  manifest.json
724fc90930ad239713af795de8bd169c2e308283d0c6fe7bb3c7d355968c7566  oracles/bh6-601.json
9af7459f45dec4f844ea2c665eb44d5ff8cf08eafb649d75f92582ef3375926c  oracles/bh6-602.json
87f4b0e43e3690d5df811e27768caecd9b45ab66d64f61690cc191b748697c0d  oracles/bh6-603.json
fb36c2d301251159d9e0eaf2ba44614029a03f7365bb709fab8416e2606bbc2d  oracles/bh6-604.json
bd05c9b6215842303ae17873355c77068b1cedd87b9add8533ac77401d6dbfac  oracles/bh6-605.json
91b37068f608d9d6cc17cfc3619c17c15805386c34f4a90a5a68aea1555ac205  oracles/bh6-606.json
7746617b39bacce30a71a0118cfff5d26804b51e5e707cbeef4c66e212ab293c  oracles/bh6-607.json
f94527ee2144abbe8baca39cba3e659e46c5984c90880891ca1c39e1a01ad4ce  oracles/bh6-608.json
```

Post-run verification at `2026-07-12T01:17:50.9554426Z` found 17/17 exact raw Git blob matches,
evaluation HEAD unchanged, a clean worktree, zero diagnostic/runner source diff from `aa2821db`, and successful
automatic checkpoint cleanup.

## Untouched Result

- Local model: `qwen2.5-coder:7b`
- Council roles: investigator, skeptic, judge
- Model calls: **24/24 successful**
- Accepted model stages: **24/24**
- Cases with an accepted stage: **8/8**
- Model-output promotion gate: pass
- Average local-call latency: **50.9 seconds**
- Maximum local-call latency: **66.5 seconds**
- Full-run wall time: **1,228.1 seconds**
- Unsafe final automatic experiments: **0**
- Premium calls: **0**
- Overall and holdout score: **67.5/100**
- Verdict: **needs_improvement**

| Case | Intended family | Actual family | Expected | Actual | Drift expected/actual | Score | Failed checks |
|---|---|---|---|---|---|---:|---|
| `bh6-601` | data | state | patch / confirmed | patch / confirmed | true / false | 65 | dimension, baseline drift |
| `bh6-602` | clock | clock | patch / confirmed | patch / confirmed | true / false | 90 | baseline drift |
| `bh6-603` | state | state | patch / confirmed | patch / confirmed | false / false | 100 | none |
| `bh6-604` | config | state | patch / confirmed | instrument / provisional | false / false | 45 | dimension, decision, status |
| `bh6-605` | dependency | runtime | patch / confirmed | instrument / provisional | true / false | 35 | dimension, decision, status, baseline drift |
| `bh6-606` | code | test_harness | patch / confirmed | patch / confirmed | false / false | 75 | dimension |
| `bh6-607` | runtime | runtime | instrument / provisional | patch / confirmed | false / false | 70 | decision, status |
| `bh6-608` | test_harness | test_harness | instrument / inconclusive | patch / confirmed | true / false | 60 | decision, status, baseline drift |

Only one case scored 100. CHILI selected four wrong causal families, missed every one of the four expected
baseline-drift findings, over-confirmed both uncertainty cases, and under-confirmed two confirmed causes. Every
model stage was structurally accepted and every final safety check passed, demonstrating again that valid local
JSON, broad hypotheses, and zero premium calls do not imply Fable 5-class diagnostic judgment.

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_BLINDED_SIXTH_RUN.md` and
`fable5_class_diagnostic_blinded_sixth_run.json`. Their SHA-256 hashes are
`100ebb581041d84938550f194c8b9725d1b1679864d3ef2f5ac469881097be91` and
`a80437f269ed5e713945412d53a1798d8158b2b5a925cbb198d23a3c1e22d92f`.

This was not a same-task authenticated Fable 5 head-to-head. It does not prove parity or superiority. The six
independent slices now total 48 diagnostic cases, but every untouched slice remains below 90 and the newest result
is the lowest score so far.
