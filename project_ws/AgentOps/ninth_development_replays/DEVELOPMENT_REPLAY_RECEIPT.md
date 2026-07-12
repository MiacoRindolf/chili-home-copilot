# Ninth Holdout Development Replay Receipt

## Evidence status

These files preserve post-result development replays against selected cases from the ninth sealed holdout. They are useful causal repair evidence, but they are not a fresh holdout and must not replace the untouched ninth result.

The authoritative untouched result remains `FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_NINTH_RUN.md`: 53.75/100 overall, 12.5% functional sealed-final solve rate, 12.5% causal-diagnosis accuracy, and zero premium calls.

Although the replay reports retain their runner-generated `blinded_holdout` labels, the fixtures were already known to the development process. Therefore:

- do not use these replay scores as unseen-generalization evidence;
- do not claim Fable 5 parity from them;
- use them only to compare causal effects of generic runner and routing changes;
- require a separately authored, frozen post-fix holdout for promotion.

## Causal sequence

| Replay | Scope | Functional final | Diagnosis | Exact files | Wall time | Interpretation |
|---|---:|---:|---:|---:|---:|---|
| 7B generic repair | 4 cases | 25.0% | 75.0% | 25.0% | 228.3s/case | Multi-file default, fuller validation, and rollback controls helped but did not generalize across the known slice. |
| Qwen3 8B | 2 cases | 0.0% | 50.0% | 0.0% | 640.1s/case | Slower and worse than the 7B baseline; rejected as default or specialist. |
| 7B source-owner map | 3 cases | 33.3% | 100.0% | 33.3% | 236.2s/case | Solved the TypeScript ownership case and supports import-grounded file expansion. |
| Standalone 14B Dart | 1 case | 100.0% | 0.0% | 100.0% | 1062.3s | Demonstrated synthesis ability but was too slow and unreliable for default routing. |
| First staged 7B/14B | 1 case | 0.0% | 0.0% | 100.0% | 614.3s | Escalation alone was insufficient. |
| Diff-ledger staged 7B/14B | 1 case | 0.0% | 0.0% | 0.0% | 710.3s | Correctly rejected duplicate/no-progress edits but still did not solve the case. |
| Regression-ledger staged 7B/14B | 1 case | 100.0% | 0.0% | 100.0% | 385.0s | Preserving transient validation evidence enabled one successful bounded local escalation. This is stochastic single-case evidence, not parity. |

## Frozen file hashes

```text
88ab375d45946c1f34805f29a0b46e8285b864997405ce446ebc9fa5ce8b44fb  ninth_development_14b_dart.json
dbbfc6748c56564959ec88b615fa53fde89e62afa777e031c4789c19344fa2c5  NINTH_DEVELOPMENT_14B_DART.md
33ac9fdeb78436218db5889be018f99c083b9d464c0beec7f70d193939dd6250  ninth_development_local_escalation_dart.json
eaf7321ed8ea6b078210073379db605a9d5f292e5314b39afee6613b8c621d6c  NINTH_DEVELOPMENT_LOCAL_ESCALATION_DART.md
f4158b5255e2bc03ae941e7ec3816846c26c139c3861ac4da51195c56b35e57d  ninth_development_local_escalation_dart_ledger.json
c17575944242fb9a811a93272028cdf0c88c523cbab5170e7c4a7e80873e6466  NINTH_DEVELOPMENT_LOCAL_ESCALATION_DART_LEDGER.md
e381998f0924f04f77ee7eaa909ed7ffa98664ff77938570c8c21d72fabfb3cd  ninth_development_local_escalation_dart_regression_ledger.json
fb0d3bf3eacccb7b63d622cf2758c2bc9d0a322c07404ce4dcd3822bc151fb6a  NINTH_DEVELOPMENT_LOCAL_ESCALATION_DART_REGRESSION_LEDGER.md
18b07cbf5ac6f8f30df9ab0d3d263b8acebeab46391a63903afedee20a9d6f96  ninth_development_owner_map_targeted.json
8c223836433984c785279368723970f4d4ee7ae24d2ed3ac0b0a3ff54c4cad8d  NINTH_DEVELOPMENT_OWNER_MAP_TARGETED.md
164c71da99afc15b87a65f4dd8a8d67381e193d8b1b7c45bb3b68cffc0231967  ninth_development_qwen3_targeted.json
8e12af3275d67078451ef4bbc936107a18a78448604c1939ec6d06c73ff5a7b6  NINTH_DEVELOPMENT_QWEN3_TARGETED.md
13a80d609970d59445a5bfc80411ab4b4b91dd0f0cc6ed678df7e472dad6f6d3  ninth_development_repair_targeted.json
55cbbf99af4ef5526e670c886052bf369aef0bccd06afe27b9917b55dbfb900e  NINTH_DEVELOPMENT_REPAIR_TARGETED.md
```

All 14 copied files were byte-identical to the external development evidence directory at preservation time.
