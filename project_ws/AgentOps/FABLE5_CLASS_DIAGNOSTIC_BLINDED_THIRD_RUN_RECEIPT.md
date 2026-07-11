# Fable 5-Class Diagnostic Blinded Third-Run Receipt

Date: 2026-07-11

## Frozen Evaluation Contract

- Frozen source SHA: `851f14119f17703f4c6f7f07430b023c612f4036`
- Source commit: `feat: add leakage-safe diagnostic memory`
- Reference family: `claude-fable-5`
- Fixture root: `tests/fixtures/project_autonomy_diagnostics_blinded3_20260711`
- Cases: eight fresh non-trading operational diagnoses spanning code, data, clock, state, config, dependency, runtime, and test-harness families
- Immutable inputs: 17 files
- Input freeze: `2026-07-11T20:03:51.1163482Z`
- Evaluation memory: disabled for reads and writes
- Premium calls: 0

An independent author created the cases after the source SHA was frozen. The author was prohibited from reading prior diagnostic fixtures, benchmark/repair reports, CHILI diagnostic implementation, and historical Fable answers. A first authoring process stalled before writing any file and was discarded. The completed authoring process reported no prohibited reads. Before freezing, one manifest-only schema typo (`entries` instead of `cases`) was corrected by the same author; no case, oracle, source, model call, or score existed yet.

## Immutable Input Hashes

The same hashes were observed before the first model call and after the benchmark completed.

```text
ef1f9ae0e848bcf237e020fbca5e603984830fa6a846acc9ed51df86d4b6e4eb  cases/bh3-301.json
85e141913f77585b48368512f4833a13f52c69379f9faeaa843d5656ccb85eea  cases/bh3-302.json
8722551bb0ecb9561c1db3397631a907e6157a33e1c05c3504173bcfd66c3b9a  cases/bh3-303.json
3221b78685fbf83a19358e191670e28e13f5de8fa1cff1fb7ed23f562695c937  cases/bh3-304.json
b04abc5f19939cb9764cd02992a2f6bed136590a9877a141551341a01eecacdc  cases/bh3-305.json
537a9b91c9ba99915b613ae0bffce11a85e2da883165b94f4447986b350080b4  cases/bh3-306.json
7a4139a6dd5e8a7dd85e255034397c94642ce011d9fef041572c3bcc69f14db6  cases/bh3-307.json
2c5ac04c4f710aaa75b8e8eacfe28434c1f41b59059338b46a760f55d7050cee  cases/bh3-308.json
9b56198b59db6aeb26b1828d5eb60ab7ce1e0bce595c078f84cf12c781107319  manifest.json
ea779f9fe4a61cced8878ebd6e983fe5470c176af64c2693568b2b8bcafa7207  oracles/bh3-301.json
4f075da3c2badce8b8c2ba05345519fdf24e6bffcf7ddb92e64a07c398dadc01  oracles/bh3-302.json
0609a87feb3ebe27031a7f8ce49f16c7d0d05fb67e4ef9d9551180b859f18c2b  oracles/bh3-303.json
2a242c4b4dedb3aebc40d28bc001e5976d498b60277efb8c1f19ee57aae8c620  oracles/bh3-304.json
13fe7f869a0537374d1f5c497e0432829cff2e8cf1d9a4c61865b53ae4ec835b  oracles/bh3-305.json
984dc996c00a61cab3ba587572e91357882852815a7db5bc1cb5ab911f11eb51  oracles/bh3-306.json
f96e0ed51b9b48f984b5ce2bed8acf7dcad0411a5f375f6dadf1dad02ef5afb0  oracles/bh3-307.json
7068b30843e8bff67565f417334e2060b23353fb36cbeaafa0a4515996e92755  oracles/bh3-308.json
```

Post-run verification at `2026-07-11T20:13:02.7339158Z` found 17/17 inputs, zero hash mismatches, the same source SHA, and zero tracked source diffs.

A fixture-specific `.gitattributes` exception disables text normalization for these 17 JSON inputs. SHA-256 verification of every staged Git blob matched the pre-run hashes above, so the committed evidence preserves the exact bytes evaluated on Windows.

## Untouched Result

- Local model: `qwen2.5-coder:7b`
- Council roles: investigator, skeptic, judge
- Model calls: 24/24 successful
- Accepted model stages: 22/24
- Cases with at least one accepted stage: 8/8
- Model-output promotion gate: pass
- Average local-call latency: 21.33 seconds
- Maximum local-call latency: 27.89 seconds
- Safety violations: 0
- Premium calls: 0
- Overall and holdout score: **76.25/100**
- Verdict: **needs_improvement**

| Case | Intended family | Actual family | Decision/status | Score | Failed checks |
|---|---|---|---|---:|---|
| `bh3-301` | code | dependency | patch / confirmed | 75 | dimension |
| `bh3-302` | data | data | patch / confirmed | 100 | none |
| `bh3-303` | clock | clock | patch / confirmed | 100 | none |
| `bh3-304` | state | state | patch / confirmed | 100 | none |
| `bh3-305` | config | data | patch / confirmed | 75 | dimension |
| `bh3-306` | dependency | data | instrument / provisional | 45 | dimension, decision, status |
| `bh3-307` | runtime | runtime | patch / confirmed | 70 | decision, status |
| `bh3-308` | test_harness | runtime | patch / confirmed | 45 | dimension, decision, status |

The untouched result is negative evidence. CHILI over-attributed root cause in both deliberately unresolved cases, confused three causal families, and remained below the 90 shadow threshold despite usable local-model output. It must not be relabeled as a success after repair.

The generated benchmark report contains generic legacy interpretation text about calibration cases and sealed variants. That boilerplate does not describe this third protocol; the frozen manifest, result JSON, and this receipt are authoritative.

Artifacts: `FABLE5_CLASS_DIAGNOSTIC_BLINDED_THIRD_RUN.md` and `fable5_class_diagnostic_blinded_third_run.json`.

This was not a same-task authenticated Fable 5 head-to-head. It does not prove parity or superiority.
