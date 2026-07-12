# Tenth-Suite Development Replay Index

Date: 2026-07-12

Every run in this index uses cases from the already disclosed tenth diagnosis-to-fix suite. These artifacts are
causal development evidence only. They are not untouched holdouts, do not replace the original 68.75/100 tenth
result, and cannot support a Fable 5 parity or superiority claim.

| Replay | Cases | Score | Final solves | Diagnosis | Exact owners | Average | Calls | Source status |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `tenth_development_replays_post_30bc032` | 8 | 33.75 | 0 | 3 | 0 | 537.7s | 156 | Frozen commit `30bc032`; first post-hardening full replay |
| `tenth_development_replays_post_optional_smoke` | 2 | 50.0 | 0 | 2 | 1 | 495.2s | 41 | Base `30bc032` plus evolving uncommitted optional-owner changes; not exactly reproducible |
| `tenth_development_replays_contract_guided_smoke` | 2 | 62.5 | 1 | 1 | 1 | 370.6s | 37 | Base `30bc032` plus evolving uncommitted contract-guidance changes; not exactly reproducible |
| `tenth_development_replays_sql_identity_smoke` | 1 | 100.0 | 1 | 1 | 1 | 217.8s | 16 | Base `30bc032` plus evolving uncommitted scoped-identity changes; not exactly reproducible |
| `tenth_development_replays_contract_guided_full_5905f63` | 8 | 58.12 | 2 | 6 | 5 | 791.8s | 168 | Frozen commit `5905f636ae6128f445fe88057188f40aa246fd32` |
| `tenth_development_replays_mechanical_contracts_full_2675d4e` | 8 | 100.0 | 8 | 8 | 8 | 180.7s | 58 | Frozen commit `2675d4ef43c27d2e50697514f76a3ca5e0ee5ab1` |

The three evolving-source smoke runs are retained because they causally localized useful mechanisms, but their
unfrozen intermediate source means they must never be cited as reproducibility evidence. The final full replay has
an exact source/tree/policy receipt in its own directory.

## Artifact Hashes

| Replay | Markdown SHA-256 | JSON SHA-256 |
|---|---|---|
| `post_30bc032` | `ce159c29987f5463547a85a9fb8add4609b94426c222bd376a2e1efa09a29997` | `0349951ab3733c9ae784a901d34418ab680d8fe03d125259b4f6eab004c8c09c` |
| `post_optional_smoke` | `2a6877d36ff15b1df06b334051871020358ec0eee94858b96e7a4e211959dba0` | `93941d623f6992493cc4dd3dd742371666cf8d226741071646e341a0fd69d01e` |
| `contract_guided_smoke` | `09eff358e09831a325547bfa7b4b6e53898eb9b9df44f3d375fd4b071ed28ad9` | `a1ec00f68d74199609b5e84ea723a401e6825fbacb4482421afd739e16bbdbe4` |
| `sql_identity_smoke` | `5d4aadfa01ecf747c4ea5d874fd1d1eac9b2a391eaaf580bb17b7210aeb94269` | `dc712b0d08eaf7e7c2be77d0e29123e69eb31774ebf07e812ca7651df3d36d53` |
| `contract_guided_full_5905f63` | `89a0b42b38608e5e5d2135b22574de332e9d2c76457099789ae590882a87b93e` | `8b089c3ee6b870574a8c944cb0736fbf97d65e1f047eee3abee658a1b821d4b1` |
| `mechanical_contracts_full_2675d4e` | `cdc0c0d8ff19c6400cd6e544a316211673cbdd025cec44bd80b9167c71f3db6e` | `fc6e30357e55ac55c1b30aa3a90477cb0990d61fb3642c94152e4e53bf8bdefa` |

## Development Conclusion

Contract guidance first moved causal-family accuracy from 3/8 to 6/8 but left final success at 2/8. Frozen
mechanical operators plus production-aligned routing later reached 8/8 final success, 8/8 diagnoses, and 8/8 exact
owners in 24.1 minutes with nine 14B calls. This is a complete disclosed regression result, not unseen
generalization. Another replay can verify reproducibility only; a separately authored untouched suite is required.
