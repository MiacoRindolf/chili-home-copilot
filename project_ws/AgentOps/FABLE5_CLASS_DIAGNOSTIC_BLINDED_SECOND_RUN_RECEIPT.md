# Fable 5-Class Diagnostic Blinded Second Run Receipt

## Preflight

- Checkout: `D:\dev\chili-home-copilot-fable5-holdout2-shared-20260711`
- Baseline SHA: `e1bc5538c1cfac65ec992bed6be1d296b603acc4`
- Initial checkout gate: exact baseline and completely clean before authoring
- Evaluator model/chat calls before authoritative freeze: **0**
- Authoritative freeze UTC: `2026-07-11T17:35:48.7177290Z`
- Immutable input count: **17**
- Immutable aggregate SHA-256: `957604e6ac1474fa8bd6f01c862f9cdeca03fdb63708227712129a032d82faa6`
- Structured validation UTC: `2026-07-11T17:35:40.1419909Z`
- Structured validation: **pass** (17 files; 8 entries; 8/8 case-oracle ID matches; 64 observations; zero non-`unknown` observation labels; zero oracle-only fields in public cases)
- Benchmark command start UTC: `2026-07-11T17:37:06.7023608Z`
- Benchmark command completion UTC: `2026-07-11T18:01:42.1267334Z`
- Benchmark model calls: **24**

An externally loaded `qwen2.5-coder:7b` process existed before the authoritative freeze. The user identified that load, observed around `2026-07-11T17:28:38Z`, as originating from the parent branch's broad regression suite. This evaluator did not query, start, or call Ollama before the freeze and made zero pre-freeze model/chat calls. The external process is recorded as pre-existing local runtime context and is not counted as a benchmark call.

An earlier checksum candidate was computed at `2026-07-11T17:33:26.6070170Z` before a parent integrity check required public observation dimensions to be blinded. No model call had occurred. That candidate was discarded, the cases were corrected, separation validation was rerun, and only the authoritative freeze above is eligible for this run.

## Executed Invocation

```text
python scripts/autopilot_realworld_diagnostic_benchmark.py --fixture-root tests/fixtures/project_autonomy_diagnostics_blinded2_20260711 --model qwen2.5-coder:7b --stages investigator,skeptic,judge --timeout 300 --num-predict 900 --num-ctx 8192 --keep-alive 20m --report project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_SECOND_RUN.md --results-json project_ws/AgentOps/fable5_class_diagnostic_blinded_second_run.json --json
```

The command was launched exactly once after the authoritative freeze. It requested exactly three local roles for each of eight cases, for 24 model calls total. No retry, premium/provider substitution, or alternate model was used.

## Immutable SHA-256 Ledger

| Immutable input | Bytes | SHA-256 before |
|---|---:|---|
| `cases/bh2-201.json` | 4732 | `0e2c29772e685bb88a6707ffe976cd7d88f0d94148e8d0f4ddff13a11ab59b11` |
| `cases/bh2-202.json` | 4742 | `8103a5371139134dd61db19fe8b9c90af4478b2055ed268cc3e8514a07496bb5` |
| `cases/bh2-203.json` | 4532 | `b40aa690a429575befdc07519d07a489b68c848e9ccb50de06939387ae99fad0` |
| `cases/bh2-204.json` | 5104 | `2b9cfe1a08e01c31200a88de3b4a68c6f3760adaa3f5c04c4668c172a759f781` |
| `cases/bh2-205.json` | 4882 | `1e9237faf9aaaf37978c18ca435ad99cc3b838423066e5033c96c2b2d2425601` |
| `cases/bh2-206.json` | 4775 | `5a50ff77a8467a6aafa04720dc747e91bf8c6d13f1418611c8b31b72c2804ef4` |
| `cases/bh2-207.json` | 4559 | `53609208bdef4290602dab5e9c48f9cdcc557cdc2c5780b33159f1cb55efeae0` |
| `cases/bh2-208.json` | 5018 | `3a518f2e0439898f4617ac9f464f564a5cc17c5a9cb8ed4e358a21e00bfc2d62` |
| `manifest.json` | 1980 | `ab3b949cc13492c33cc7890a18aa0a18a9c17397501a0b4f6068c060d8a76117` |
| `oracles/bh2-201.json` | 372 | `969fa8e4112c9841dd05d2fae6ad61edc6cbaaca5b13d6948ecdd61325ee3f88` |
| `oracles/bh2-202.json` | 374 | `a4583789a0efe7790a1796c4f6d651e56ef943cdf391f34c9967dceabc6925f7` |
| `oracles/bh2-203.json` | 371 | `a745eee291e6dee72368f085ed11fa2750413589ba6a9f457dc24d85cbd3a725` |
| `oracles/bh2-204.json` | 372 | `42de8126877595dd771cb3507ef326cf809e9bcca98ae73e519d2a6341f21919` |
| `oracles/bh2-205.json` | 382 | `42470b7a05a5d0277ddfd29505fe082cf7016a7ea18ce228aa35fe6d6d76ba4d` |
| `oracles/bh2-206.json` | 429 | `70ab58f3fd748d13a9da5abb04e1abcb33ddfe54b81f869028aea07a2fd29d7d` |
| `oracles/bh2-207.json` | 440 | `7db35be343a03c3f63c1d0b208384ba88ac6e6eb9318f2148d018167ed87863a` |
| `oracles/bh2-208.json` | 426 | `061affa762faa657d1bd6425b721ac3506ebc728a4bdf4513127818bc815811c` |

## Post-run Evidence

- Post-run hash verification UTC: `2026-07-11T18:02:05.2573256Z`
- Immutable input count after: **17**
- Immutable aggregate SHA-256 after: `957604e6ac1474fa8bd6f01c862f9cdeca03fdb63708227712129a032d82faa6`
- Per-file hashes after: **17/17 identical to the complete before ledger above**
- Baseline after: `e1bc5538c1cfac65ec992bed6be1d296b603acc4`
- Command exit code: **0**
- Model: `qwen2.5-coder:7b`
- Route: **local Ollama only**
- Calls: **24/24 recorded, 24/24 `ok=true`, 0 transport errors**
- Role sequence: **investigator, skeptic, judge for 8/8 cases**
- Premium/provider calls: **0**
- Safety violations: **0**
- Average local call latency: **61,232.54 ms**
- Maximum local call latency: **65,950 ms**
- Output tokens: **21,600; every call reached the 900-token cap**
- Accepted model stage packets: **0/24**
- Rejected model stage packets: **24/24**, each with `Model response was not a usable diagnostic JSON object.`
- Unseen second-holdout score: **87.5/100**
- Verdict: **needs_improvement**
- Weighted check failures: **4**, all dimension checks on `bh2-201`, `bh2-202`, `bh2-206`, and `bh2-208`
- Integrity-eligible as negative holdout evidence: **yes**
- Eligible as positive capability, shadow, promotion, or parity evidence: **no**

The score came entirely from deterministic fallback behavior because no model stage packet was accepted. It is preserved as a valid negative result and must not be represented as successful three-role model reasoning.

The raw harness result was captured before provenance augmentation at 134,794 bytes with SHA-256 `cc6263d22aa11da39cba10aa97d69a534a7c3dedfccc1bde49129595b197ecc3`. The raw generated report was captured before expansion at 1,860 bytes with SHA-256 `0f0431056d98fadd9c55737998787fb73a0330ffa9eff648c44827abdea3b90b`.

## Validation Receipt

- Initial public-test attempt: blocked before collection by the repository's missing-`TEST_DATABASE_URL` guard; zero tests and zero model calls ran.
- Public benchmark tests with the documented dedicated-test URL: **2 passed, 0 failed, 2 warnings**.
- Deterministic evaluator score recomputation: **8/8 case details reproduced; 87.5/100**.
- Cross-artifact integrity check at `2026-07-11T18:06:37.5004269Z`: **pass**, zero errors.
- Git scope audit before staging: **pass**, zero unauthorized entries.
