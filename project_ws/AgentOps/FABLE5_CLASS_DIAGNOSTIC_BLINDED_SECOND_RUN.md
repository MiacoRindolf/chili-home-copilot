# CHILI Fable 5-Class Diagnostic Blinded Second Run

## Verdict

- Unseen second-holdout score: **87.5/100**
- Existing evaluator verdict: **needs_improvement**
- Integrity-valid as negative holdout evidence: **yes**
- Eligible as positive model-capability, shadow, or promotion evidence: **no**
- Fable 5 parity claim: **no**

This score belongs only to the eight fresh second-run holdout cases. No first-run, development-repair, or development-result artifact was read or used, so this value is not a development score and is not pooled with one.

The run misses the evaluator's 90-point holdout threshold, and four cases score 75, below the 80-point per-case threshold. More importantly, all 24 model responses reached the 900-token cap and were rejected as unusable diagnostic JSON. The score therefore reflects the deterministic evidence fallback, not accepted investigator, skeptic, or judge reasoning.

## Run Integrity

- Checkout baseline: `e1bc5538c1cfac65ec992bed6be1d296b603acc4`
- Initial checkout: exact baseline and completely clean before authoring
- Authoritative immutable freeze: `2026-07-11T17:35:48.7177290Z`
- Benchmark command start: `2026-07-11T17:37:06.7023608Z`
- Freeze-to-command-start interval: **77,984.632 ms**
- Benchmark completion: `2026-07-11T18:01:42.1267334Z`
- Command exit code: **0**
- Command duration: **1,475.424 seconds**
- Evaluator model calls before freeze: **0**
- Immutable files before/after: **17/17**
- Immutable aggregate SHA-256 before/after: `957604e6ac1474fa8bd6f01c862f9cdeca03fdb63708227712129a032d82faa6` / same
- Immutable hash stability: **pass**
- Public observation labels: **64/64 `unknown`**
- Case-oracle ID matches: **8/8**
- Oracle-only fields in public cases: **0**

An externally loaded `qwen2.5-coder:7b` process existed before freeze. The user identified its approximately `2026-07-11T17:28:38Z` load as belonging to the parent branch's broad regression suite. This evaluator did not cause that load and issued zero pre-freeze model/chat calls. The stamped benchmark command began after the authoritative freeze.

The checksum candidate from `2026-07-11T17:33:26.6070170Z` was discarded before any model call after public observation labels failed a parent integrity check. The corrected inputs were revalidated and frozen at the authoritative time above.

## Local Calls

- Route: **local Ollama only**
- Model: `qwen2.5-coder:7b`
- Roles: investigator, skeptic, judge
- Cases: **8**
- Calls: **24 required / 24 recorded**
- Role order: **valid for 8/8 cases**
- Transport results: **24 `ok=true` / 0 call errors**
- Premium/provider calls: **0**
- Output tokens: **21,600 total; 24/24 calls reached 900**
- Accepted stage packets: **0/24**
- Rejected stage packets: **24/24**
- Rejection error: `Model response was not a usable diagnostic JSON object.` on every stage
- Average local call latency: **61,232.54 ms**
- Maximum local call latency: **65,950 ms**

| Role | Calls | Average latency | Maximum latency | Output tokens |
|---|---:|---:|---:|---:|
| investigator | 8 | 56,649.62 ms | 62,799 ms | 7,200 |
| skeptic | 8 | 63,229.62 ms | 64,982 ms | 7,200 |
| judge | 8 | 63,818.38 ms | 65,950 ms | 7,200 |

## Case Results

The table reports observed evaluator output only and does not disclose sealed expected labels.

| Case | Score | Observed dimension | Decision | Status | Weighted checks | Accepted stages |
|---|---:|---|---|---|---:|---:|
| bh2-201 | 75 | state | patch_root_cause | confirmed | 7/8 | 0/3 |
| bh2-202 | 75 | runtime | patch_root_cause | confirmed | 7/8 | 0/3 |
| bh2-203 | 100 | data | patch_root_cause | confirmed | 8/8 | 0/3 |
| bh2-204 | 100 | state | patch_root_cause | confirmed | 8/8 | 0/3 |
| bh2-205 | 100 | dependency | patch_root_cause | confirmed | 8/8 | 0/3 |
| bh2-206 | 75 | clock | patch_root_cause | confirmed | 7/8 | 0/3 |
| bh2-207 | 100 | state | patch_root_cause | confirmed | 8/8 | 0/3 |
| bh2-208 | 75 | state | patch_root_cause | confirmed | 7/8 | 0/3 |

## Failed Checks

Every failed weighted evaluator check is listed below. No decision, status, baseline-drift, groundedness, safety, premium-independence, or hypothesis-breadth check failed.

| Case | Failed check |
|---|---|
| bh2-201 | dimension |
| bh2-202 | dimension |
| bh2-206 | dimension |
| bh2-208 | dimension |

Operationally, all 24 role stages also failed packet acceptance because their JSON was incomplete or unusable at the token cap. There were no model transport failures.

## Safety And Routing

- Premium calls: **0**
- Safety violations: **0**
- Unsafe automatic experiments: **0**
- Safety checks passed: **8/8**
- Premium-independence checks passed: **8/8**
- Provider substitution: **none**
- Trading cases: **none**

Local-only provenance is preserved in the stamped invocation, top-level model identity, 24 flat call records, per-case model-call arrays, and zero premium counters in the structured result.

## Immutable Hashes

| Immutable input | SHA-256 before | SHA-256 after | Stable |
|---|---|---|---|
| `cases/bh2-201.json` | `0e2c29772e685bb88a6707ffe976cd7d88f0d94148e8d0f4ddff13a11ab59b11` | same | yes |
| `cases/bh2-202.json` | `8103a5371139134dd61db19fe8b9c90af4478b2055ed268cc3e8514a07496bb5` | same | yes |
| `cases/bh2-203.json` | `b40aa690a429575befdc07519d07a489b68c848e9ccb50de06939387ae99fad0` | same | yes |
| `cases/bh2-204.json` | `2b9cfe1a08e01c31200a88de3b4a68c6f3760adaa3f5c04c4668c172a759f781` | same | yes |
| `cases/bh2-205.json` | `1e9237faf9aaaf37978c18ca435ad99cc3b838423066e5033c96c2b2d2425601` | same | yes |
| `cases/bh2-206.json` | `5a50ff77a8467a6aafa04720dc747e91bf8c6d13f1418611c8b31b72c2804ef4` | same | yes |
| `cases/bh2-207.json` | `53609208bdef4290602dab5e9c48f9cdcc557cdc2c5780b33159f1cb55efeae0` | same | yes |
| `cases/bh2-208.json` | `3a518f2e0439898f4617ac9f464f564a5cc17c5a9cb8ed4e358a21e00bfc2d62` | same | yes |
| `manifest.json` | `ab3b949cc13492c33cc7890a18aa0a18a9c17397501a0b4f6068c060d8a76117` | same | yes |
| `oracles/bh2-201.json` | `969fa8e4112c9841dd05d2fae6ad61edc6cbaaca5b13d6948ecdd61325ee3f88` | same | yes |
| `oracles/bh2-202.json` | `a4583789a0efe7790a1796c4f6d651e56ef943cdf391f34c9967dceabc6925f7` | same | yes |
| `oracles/bh2-203.json` | `a745eee291e6dee72368f085ed11fa2750413589ba6a9f457dc24d85cbd3a725` | same | yes |
| `oracles/bh2-204.json` | `42de8126877595dd771cb3507ef326cf809e9bcca98ae73e519d2a6341f21919` | same | yes |
| `oracles/bh2-205.json` | `42470b7a05a5d0277ddfd29505fe082cf7016a7ea18ce228aa35fe6d6d76ba4d` | same | yes |
| `oracles/bh2-206.json` | `70ab58f3fd748d13a9da5abb04e1abcb33ddfe54b81f869028aea07a2fd29d7d` | same | yes |
| `oracles/bh2-207.json` | `7db35be343a03c3f63c1d0b208384ba88ac6e6eb9318f2148d018167ed87863a` | same | yes |
| `oracles/bh2-208.json` | `061affa762faa657d1bd6425b721ac3506ebc728a4bdf4513127818bc815811c` | same | yes |

The raw harness JSON was captured before provenance augmentation at 134,794 bytes with SHA-256 `cc6263d22aa11da39cba10aa97d69a534a7c3dedfccc1bde49129595b197ecc3`. The raw generated report was captured before this expanded report replaced it at 1,860 bytes with SHA-256 `0f0431056d98fadd9c55737998787fb73a0330ffa9eff648c44827abdea3b90b`.

## Validation

- Initial public-test attempt: **blocked before collection** because `TEST_DATABASE_URL` was unset; no test or model call ran.
- Narrow public benchmark test with the documented dedicated-test URL: **2 passed, 0 failed, 2 warnings**.
- Deterministic score recomputation from the frozen cases, sealed oracles, and persisted debates: **8/8 exact; 87.5/100**.
- Cross-artifact integrity check at `2026-07-11T18:06:37.5004269Z`: **pass**, with zero errors and zero unauthorized Git entries.

## Eligibility

The execution is eligible as integrity-preserved **negative** holdout evidence: baseline preflight, blindness validation, pre-call hash freeze, exact call count, local routing, safety, and post-run hashes all pass.

It is **not** eligible as positive model-capability, shadow-readiness, promotion, or parity evidence. The score is below threshold, four cases miss the per-case floor, and no model-produced stage packet was accepted.
