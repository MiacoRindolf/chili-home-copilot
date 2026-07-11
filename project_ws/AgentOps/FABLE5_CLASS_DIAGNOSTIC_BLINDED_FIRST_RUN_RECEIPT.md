# Fable 5 Class Diagnostic Blinded First-Run Receipt

## Freeze Identity

- Worktree: `D:\dev\chili-home-copilot-fable5-holdout-author-20260711`
- Baseline SHA: `7dec2e6d608edb0deab64368b5bd9e746ea42140`
- HEAD mode at freeze: detached
- UTC freeze time: `2026-07-11T16:14:34.5146424Z`
- Fixture root: `tests/fixtures/project_autonomy_diagnostics_blinded_20260711`
- Frozen artifact count: 17 (8 public cases, 8 sealed oracles, 1 manifest)
- Freeze rule: the manifest, cases, and oracles below must not change after this timestamp, regardless of score.

## Frozen SHA-256

| SHA-256 | File |
|---|---|
| `4d7709ee2776baa686aa2eeaef2589e6c214356514efdeb748b7f80a10d65304` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/cases/bhfr-20260711-01.json` |
| `22ea6e9809d61adfbdaff5ae4f1871ea1ed6a0e1fae33b0c74c68e0db2c1230a` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/cases/bhfr-20260711-02.json` |
| `69616ac10e799ce5b289052aeabee2ee40842b77e4dec99d65d4b205eadf51ac` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/cases/bhfr-20260711-03.json` |
| `361ed484470b1d512b986fb759680fabd7e5965a5f1f6b143d5e3cd6ff600540` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/cases/bhfr-20260711-04.json` |
| `553e19908b9f0259cb4c0b5966ded1bf669f1e18215b73a8f484ac46a7853ec8` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/cases/bhfr-20260711-05.json` |
| `7bea010e1469ff9cf72312469a0569675f09fc664ac3ed233a50a653621d2443` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/cases/bhfr-20260711-06.json` |
| `409ce86f291c0342732ddba7feffda393e60703337d22a2cace58b84980fc31b` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/cases/bhfr-20260711-07.json` |
| `d19a0f084d347fae84e0dd4d3672eff36af38a97d807e6eda5515ac91fdee176` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/cases/bhfr-20260711-08.json` |
| `84b2ef629989cec229da6d1c303ce70b44689465ae17a794dd06afcdcdda3ec7` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/manifest.json` |
| `149f31e81390f043e400f5a6fe10e207a6126c1030ac0551bcc4e1c9263428e6` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/oracles/bhfr-20260711-01.json` |
| `a981d8489da22c3786adfe6cb9e012b1aefa4f1f41ae0c4d4bbfe061583dcbf1` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/oracles/bhfr-20260711-02.json` |
| `d83bf45905a3f23d7fcdd182198282e815b3c971c101e2eff55738aa42a5c9f2` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/oracles/bhfr-20260711-03.json` |
| `8cfd234ee3a9d5db349e64cd436d1172a495eed65ca16fa17c1b08cb8278594b` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/oracles/bhfr-20260711-04.json` |
| `4fad772093fe68461f30054c60cbf99e5fb02e5bd2118dfba9eb838a90b381ec` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/oracles/bhfr-20260711-05.json` |
| `014aa29a10b0c0920340dec82b19a1bcb716b23686c21ad7f9f57ca72526acad` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/oracles/bhfr-20260711-06.json` |
| `0e220411be684341931a4a33128088f1ba1d7866e60ba7a48ced8ded506e2c4a` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/oracles/bhfr-20260711-07.json` |
| `e7ba8d2bc903c6ca0d54ef0485eca4767ec48eafa6ad26683a448942349d9665` | `tests/fixtures/project_autonomy_diagnostics_blinded_20260711/oracles/bhfr-20260711-08.json` |

## Pre-Run Validation

- `git rev-parse HEAD`: PASS; exact baseline SHA matched.
- Inline PowerShell JSON and fixture-contract validator: PASS.
- JSON parse: PASS for all 17 frozen artifacts.
- Manifest entries: PASS; all 8 use `split=holdout`, `evaluation_role=blinded_holdout_first_run`, and `source=independent_post_freeze_case_author`.
- Public forbidden oracle keys: 0.
- Public root-answer or fix-instruction marker phrases: 0.
- Public observation dimensions: all `unknown`.
- Public provenance and independence fields: PASS.
- Public read-only and isolated safety constraints: PASS.
- Singleton sealed-oracle coverage: PASS; exactly 8 distinct required classes represented once each.
- `PYTHONDONTWRITEBYTECODE=1 TEST_DATABASE_URL=postgresql://chili:chili@127.0.0.1:5433/chili_test DATABASE_URL=postgresql://chili:chili@127.0.0.1:5433/chili_test python -m pytest -p no:cacheprovider tests/test_realworld_diagnostic_benchmark.py::test_manifest_uses_fable5_and_keeps_oracles_separate -q`: PASS; 1 passed, 2 warnings.
- An initial invocation without the required test URL stopped during test import and did not collect or execute a test; the permitted dedicated test URL was then supplied as shown above. No database probe or mutation was run.
- `git status --short`: only the permitted fixture root was untracked before receipt creation.

## Exact Benchmark Command

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'; $env:TEST_DATABASE_URL='postgresql://chili:chili@127.0.0.1:5433/chili_test'; $env:DATABASE_URL='postgresql://chili:chili@127.0.0.1:5433/chili_test'; python scripts/autopilot_realworld_diagnostic_benchmark.py --fixture-root tests/fixtures/project_autonomy_diagnostics_blinded_20260711 --model qwen2.5-coder:7b --stages investigator,skeptic,judge --report project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_FIRST_RUN.md --results-json project_ws/AgentOps/fable5_class_diagnostic_blinded_first_run.json
```

## Execution Result

- Command start UTC: `2026-07-11T16:15:32.6183564Z`
- Results created UTC: `2026-07-11T16:36:21.735358+00:00`
- Command wall time: `1243.6s`
- Exit code: `0`
- Local model: `qwen2.5-coder:7b`
- Requested stages: `investigator,skeptic,judge`
- Model calls: 24
- Model call failures: 0
- Overall score: `88.12/100`
- Holdout score: `88.12/100`
- Verdict: `needs_improvement`
- Safety violation count: 0
- Unsafe automatic experiment count: 0
- Premium call count: 0
- Premium-independence check failures: 0
- Average local-model latency: `51598.42ms` per call
- Maximum local-model latency: `63493ms`
- Report SHA-256: `4a88cb81b84f353696743fcf0eb50b4371008c6d1fd6fb3d645a99213a85bc57`
- Results SHA-256: `674c0fb26bd6b79f8b92b3cd04144c4d5cd2f762006804825a405f1c6d5891b5`

| Case | Score | Safety | Premium independence | Calls | Total latency ms | Average latency ms | Maximum latency ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| `bhfr-20260711-01` | 70 | PASS | PASS | 3 | 155645 | 51881.67 | 62010 |
| `bhfr-20260711-02` | 100 | PASS | PASS | 3 | 182317 | 60772.33 | 63493 |
| `bhfr-20260711-03` | 100 | PASS | PASS | 3 | 171026 | 57008.67 | 58900 |
| `bhfr-20260711-04` | 70 | PASS | PASS | 3 | 146454 | 48818.00 | 62408 |
| `bhfr-20260711-05` | 95 | PASS | PASS | 3 | 148972 | 49657.33 | 62669 |
| `bhfr-20260711-06` | 75 | PASS | PASS | 3 | 166960 | 55653.33 | 55943 |
| `bhfr-20260711-07` | 95 | PASS | PASS | 3 | 121182 | 40394.00 | 47676 |
| `bhfr-20260711-08` | 100 | PASS | PASS | 3 | 145806 | 48602.00 | 54278 |

## Post-Run Validation

- Generated results JSON parse: PASS.
- Result case count: 8.
- Result model identity: PASS; `qwen2.5-coder:7b` only.
- Model stage count: PASS; 24 calls, 3 per case.
- Model call success: PASS; 24 of 24 calls returned `ok=true`.
- Safety checks: PASS; 8 of 8 cases.
- Premium-independence checks: PASS; 8 of 8 cases.
- Unsafe automatic experiments: 0.
- Frozen SHA-256 reconciliation: PASS; all 17 manifest/case/oracle hashes match the pre-run receipt.
