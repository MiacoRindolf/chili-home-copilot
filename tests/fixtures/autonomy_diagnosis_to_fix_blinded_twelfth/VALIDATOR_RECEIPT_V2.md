# Independent Validator Receipt V2

**Verdict: REJECT**

Validation date: 2026-07-12  
Read scope: only the complete current `python`, `node`, `dart`, and `sql` author directories  
Write scope: only `D:\dev\chili-twelfth-validation`  

No author artifact was edited. No repository, git history, prior fixture/result/history, internet resource, or CHILI model was inspected or called. Fresh V2 test directories were removed before this receipt was written.

## Original Receipts

The original REJECT receipts are unchanged:

| File | Bytes | SHA-256 before and after |
|---|---:|---|
| `VALIDATOR_RECEIPT.json` | 24594 | `70c22aa1564baa035a379ab0539bbb19fdf7c39656be545572135ade4643a598` |
| `VALIDATOR_RECEIPT.md` | 13377 | `bb2027e4cf7e137ddc9a16664ad26f73cfadf0f4166b79c255b2e2b64faffbbf` |

## Protocol Summary

| Check | Result | Finding |
|---|---:|---|
| Exactly 12 cases, 3 per language | PASS | Python 3; Node ESM 3; Dart 3; SQLite SQL 3. |
| Authored case/oracle/final schemas | PASS | Exact schemas and matching identifiers for all current triples. |
| Baseline public/feedback/final pattern | PASS | 12/12 public passed; 12/12 feedback failed; 12/12 final failed. |
| Test-file disjointness and final boundaries | PASS | One pairwise-disjoint file per tier; every final adds a material boundary. |
| Candidate ownership and `max_files` | PASS | Every case has 2-3 owners; `expected_files` matches and fits `max_files`. |
| Safety and dependencies | PASS | Offline/runtime-only production mechanisms; no unsafe, premium, or network dependency. |
| Author attestations | PASS | All required isolation and no-access declarations are present. |
| Retained hash verification | PASS | 25 current files match the original receipt byte-for-byte. |
| Attestation accounting | PASS | Python, Node, and Dart attestations changed only to account for replacements/current validation. |
| Disclosed-family novelty | **REJECT** | Python sparse catalog updates replay patch-null/omission semantics. |
| Cross-suite diversity | **REJECT** | Node mass precision and Dart decimal apportionment share exact scaled-decimal parsing and the same large-precision final boundary. |

## Rejection Findings

1. `py_sparse_catalog_updates` is a direct semantic replay of the previously disclosed patch-null family. Feedback preserves explicit falsy values; final distinguishes an omitted key from explicit `None` and uses `None` to clear stored state.
2. `node_manifest_mass_precision` and `dart_decimal_apportionment` both require exact decimal-string-to-scaled-integer parsing in place of floating point and both make beyond-binary-precision exactness a final boundary. Aggregation and apportionment differ downstream, but the shared parser/precision mechanism is too close under the strict cross-suite rule. Retain Dart's apportionment-focused case and replace the Node mass case.

`sql_notification_override_tristate` is not classified as patch-null: its `NULL` is retained relational provenance meaning inherit at a hierarchy level, not an omitted-versus-null sparse update payload.

## Per-Case Findings

| Case | Verdict | Dimension | Mechanism / final boundary | Novelty classification |
|---|---:|---|---|---|
| `py_config_reload` | PASS | config | Live replacement and derived rebinding; final resets removed overrides. | Novel. |
| `py_sparse_catalog_updates` | **REJECT** | data | Sparse patch presence and explicit-null clearing; final distinguishes omission from `None`. | Disclosed patch-null replay. |
| `py_tail_checkpoint` | PASS | state | File-identity checkpoint provenance; final covers same-identity truncation. | Novel. |
| `node_manifest_mass_precision` | **REJECT** | data | Exact scaled-decimal parsing/aggregation; final crosses safe-integer precision. | Near-duplicate of Dart fixed-point parsing. |
| `node_policy_reload_consistency` | PASS | state | Request-scoped policy generation; final covers absent-route insertion. | Novel. |
| `node_tls_client_auth_config` | PASS | config | TLS client-auth policy validation/option mapping; final covers optional mode. | Novel. |
| `dart_decimal_apportionment` | PASS | data | Exact signed minor units and stable largest-remainder allocation; final covers large values, credits, and zero weights. | Retained canonical case in fixed-point pair. |
| `dart_release_reader_lifecycle` | PASS | state | Reader accounting and deferred release reclamation; final covers multiple generations/readers. | Novel. |
| `dart_trusted_proxy_chain` | PASS | config | CIDR trust and aligned forwarded-hop resolution; final covers IPv6. | Novel. |
| `sql_notification_override_tristate` | PASS | config | Relational tri-state hierarchy; final covers workspace-to-system fallback. | Novel; not patch-null. |
| `sql_tenant_stock_ownership` | PASS | data | Composite tenant ownership; final covers parent mutation/deletion. | Novel. |
| `sql_ticket_archive_transitions` | PASS | state | Stored active-count transitions; final composes move and restore. | Novel. |

Every case independently passes ownership review: 2-3 candidate source owners, all candidates present, `expected_files` equal to the candidate set, and a feasible `max_files` value.

## Independent Baselines

All 36 combinations were run in separate fresh V2 directories; only the selected tier was invoked.

| Case | Public | Feedback | Final |
|---|---|---|---|
| `py_config_reload` | exit 0, 3 passed | exit 1, 2 failed | exit 1, 3 failed |
| `py_sparse_catalog_updates` | exit 0, 3 passed | exit 1, 3 failed | exit 1, 2 failed/1 passed |
| `py_tail_checkpoint` | exit 0, 2 passed | exit 1, 3 failed | exit 1, 2 failed |
| `node_manifest_mass_precision` | exit 0, pass | exit 1, fail | exit 1, fail |
| `node_policy_reload_consistency` | exit 0, pass | exit 1, fail | exit 1, fail |
| `node_tls_client_auth_config` | exit 0, pass | exit 1, fail | exit 1, fail |
| `dart_decimal_apportionment` | exit 0, pass | exit 255, fail | exit 255, fail |
| `dart_release_reader_lifecycle` | exit 0, pass | exit 255, fail | exit 255, fail |
| `dart_trusted_proxy_chain` | exit 0, pass | exit 255, fail | exit 255, fail |
| `sql_notification_override_tristate` | exit 0, 3 passed | exit 1, 2 failed | exit 1, 2 failed |
| `sql_tenant_stock_ownership` | exit 0, 2 passed | exit 1, 2 failed | exit 1, 2 failed |
| `sql_ticket_archive_transitions` | exit 0, 2 passed | exit 1, 2 failed | exit 1, 2 failed |

## Hash Reconciliation

Compared with the original receipt: 25 files are retained unchanged, 3 attestations are updated, 12 replacement artifacts are added, and 12 previously rejected artifacts are removed.

| Updated attestation | Previous SHA-256 | Current SHA-256 | Accounted change |
|---|---|---|---|
| `python/AUTHOR_ATTESTATION.json` | `8d151cefc3d33940820d9fdbaddce62205196d821b6fe5fae3ab59989f2107a5` | `5d42da827101982f9c3224157d7e007f192fa3af48df8d2c8783c8eab1be654b` | Current case IDs/files, validation, and Python replacement note. |
| `node/AUTHOR_ATTESTATION.json` | `bda29b8eff543b1658a08ad471ffafbaf2d8f2190dae95ac910461741c18a45a` | `8774499a77d4fc90f177b4f0909f6d45e2bb34848fba506b84b497c814f9179e` | Current files plus two Node replacements and validation. |
| `dart/AUTHOR_ATTESTATION.json` | `fa983ecd1e07a0adbd697f5789c179ab1a3598f1321f64e7bbd4bf5dcc65662f` | `db190f7248cf2ab6b49ec74a220fa96d80765a20ae80d9021d29666fb39f45a1` | Current files and Dart replacement validation. |

The removed triples are the prior `py_stream_records`, `node_ndjson_chunk_ingest`, `node_worker_env_reconfigure`, and `dart_stream_record_framing` artifacts. Their twelve current replacements are fully reflected in the hash manifest below.

## Exact Correction Requests

1. **Python / `py_sparse_catalog_updates`:** Replace the complete triple with a Python data mechanism unrelated to sparse/partial patch application, key-presence sentinels, falsy-value preservation, explicit-null clearing, omission-based retention, merge-patch semantics, or tri-state update payloads. Preserve exact schemas, 2-4 owners, feasible `expected_files`/`max_files`, disjoint tiers, required baseline behavior, and a materially new final boundary. Update the Python attestation.
2. **Node / `node_manifest_mass_precision`:** Replace the complete triple with a Node ESM data mechanism unrelated to decimal-string parsing, scaled fixed-point integers, floating-point precision loss, exact unit/currency conversion, fixed-point aggregation, or proportional apportionment. Preserve all structural and baseline requirements and update the Node attestation.

## Current SHA-256 Manifest

| Path | Class | Bytes | SHA-256 |
|---|---|---:|---|
| `dart/AUTHOR_ATTESTATION.json` | updated attestation | 1864 | `db190f7248cf2ab6b49ec74a220fa96d80765a20ae80d9021d29666fb39f45a1` |
| `dart/cases/dart_decimal_apportionment.json` | replacement | 2774 | `266cc76ef29808a6783f0880decc1af65618ea5362bad45db2fb4d09c28dc8c9` |
| `dart/cases/dart_release_reader_lifecycle.json` | retained | 3681 | `e67aaecc0249f1a7cb0fb61b74d47dc10d2a5bc946a3478066d08d784b123277` |
| `dart/cases/dart_trusted_proxy_chain.json` | retained | 4439 | `fe905d47bbd43b8a1bb86cd298cae29c217267e9b40e63ecc13910fcdd8090de` |
| `dart/final_oracles/dart_decimal_apportionment.json` | replacement | 1065 | `3095f2742096a1f960ee41e1cc36c9da7044b4921ec5d64cf865423fbdc3f4c6` |
| `dart/final_oracles/dart_release_reader_lifecycle.json` | retained | 1555 | `1e7f665ecbee9b80b1dbc5b062da685a8679825dc6e581b0a5c863123212cd1d` |
| `dart/final_oracles/dart_trusted_proxy_chain.json` | retained | 1130 | `54dadd8e29c9eb36ec26043a74a5f31f5c76f359559fc4b577fd2afc3fa84dc2` |
| `dart/oracles/dart_decimal_apportionment.json` | replacement | 1163 | `8d34e4cf11e1082490feb38e163dde2187c241c46fa492c88ab300c61a10018a` |
| `dart/oracles/dart_release_reader_lifecycle.json` | retained | 1057 | `74892d7b2d951c4691b5a7374c310f1d8495d33f38de5c7e01b7173ef68d6960` |
| `dart/oracles/dart_trusted_proxy_chain.json` | retained | 1375 | `672feb1ce5b04633688021d4da251a77f6e5fb84ec8ae33525e14d307483ae01` |
| `node/AUTHOR_ATTESTATION.json` | updated attestation | 1798 | `8774499a77d4fc90f177b4f0909f6d45e2bb34848fba506b84b497c814f9179e` |
| `node/cases/node_manifest_mass_precision.json` | replacement | 2772 | `cbf5f1d3ebff3a8647bd351456cd771824b676b7a362cc7239f7251ed792eefd` |
| `node/cases/node_policy_reload_consistency.json` | retained | 3520 | `0a2552a5bc0ace443af09c431c48371795da2d5017cb55b31a35dcb71a769ff2` |
| `node/cases/node_tls_client_auth_config.json` | replacement | 2685 | `87f681e5f4c46ea4833caf72ae4e9cf50bf2e77df0879d868b4b4deac4487b64` |
| `node/final_oracles/node_manifest_mass_precision.json` | replacement | 604 | `c88b84371be90677349223bcef9f86d5eb22d239c388a429d19e74f026410cad` |
| `node/final_oracles/node_policy_reload_consistency.json` | retained | 1239 | `c65bb4eefa54b59377daa8170993c665bef70a041758e094cb2ac0deb88e460f` |
| `node/final_oracles/node_tls_client_auth_config.json` | replacement | 533 | `7a44c88e445a225fc467653b5d5fadd68773971df051bb35fdf0dd544d7ca843` |
| `node/oracles/node_manifest_mass_precision.json` | replacement | 786 | `cda02f414b3b7d23cdc156c70be087137f306626b6ae15ebc21bf3571acb57cc` |
| `node/oracles/node_policy_reload_consistency.json` | retained | 1359 | `581c2dc23144047cba3667b4016c48f3a7738baf466b072463fac3a70252ea10` |
| `node/oracles/node_tls_client_auth_config.json` | replacement | 991 | `494566ca5c2506dd3258c884a527cbf55a38d9fe73d113001f4b93160241eff6` |
| `python/AUTHOR_ATTESTATION.json` | updated attestation | 2102 | `5d42da827101982f9c3224157d7e007f192fa3af48df8d2c8783c8eab1be654b` |
| `python/cases/py_config_reload.json` | retained | 3661 | `581a889150674816b9e6148e3a98298835374f5db6760eea4e0e3b7c0cd2fbd4` |
| `python/cases/py_sparse_catalog_updates.json` | replacement | 3306 | `51776715d99fac74f6493868810ffa93d0980ddf46c0681829bca239c9c30386` |
| `python/cases/py_tail_checkpoint.json` | retained | 2692 | `aa87a5f6f5054bc82cb78352af380baf9f5372a2ce430063e5ce1fe19c68a8e7` |
| `python/final_oracles/py_config_reload.json` | retained | 1392 | `aaf7c9ecca9addc7019d8cae8e7261b45088458e24dc3a6559edf4050cf5c051` |
| `python/final_oracles/py_sparse_catalog_updates.json` | replacement | 1416 | `09c29b2cfc19ad971dd47bee0cbcb758a2a0fb13059ce491ba2fdbd07a73129d` |
| `python/final_oracles/py_tail_checkpoint.json` | retained | 1163 | `990838d2c86b46c9de3556a1324e44b23e61f0bc5a25657a4bcff350ecfdb63a` |
| `python/oracles/py_config_reload.json` | retained | 1243 | `1aec36eb7c1d355fb4ca25f84ab7198ecec8ba6c0ffc62bba72f9d8ee2530cdc` |
| `python/oracles/py_sparse_catalog_updates.json` | replacement | 1452 | `ce8153c7bff9d3b200a1c741d83f7a9a7fe94a32f29551830010376e534c6407` |
| `python/oracles/py_tail_checkpoint.json` | retained | 1688 | `5688fd799f98e208f0282cbb4bd5e9d4061f84a94f4a0219a63c53abe300b4d9` |
| `sql/AUTHOR_ATTESTATION.json` | retained | 1553 | `5a6fdb587eaa38cd870ff21eafcf8a4f60d5d91217136acad9a575e9803ab01b` |
| `sql/cases/sql_notification_override_tristate.json` | retained | 4172 | `95a26634fb377bd6b5d5dff7eaf0e3e500ed961809a4afc11a536234c6c05b7f` |
| `sql/cases/sql_tenant_stock_ownership.json` | retained | 3009 | `c80f89a027415009deae63b8c895412edcd9c4360b64f60c664d122bb52ceab3` |
| `sql/cases/sql_ticket_archive_transitions.json` | retained | 4614 | `3c546d29576940c92babc832b0aca0a9af317badf154f72afff63bf9b58ecc4d` |
| `sql/final_oracles/sql_notification_override_tristate.json` | retained | 1339 | `5c3805272fc9d86a20f655c1e4fd8c84fad44c6f94f5b2eb44139b0e25dc8867` |
| `sql/final_oracles/sql_tenant_stock_ownership.json` | retained | 1369 | `0ddde2be64ef6639a9ed89ba609faaa6e1300d79eeb259c15e911edff95e97b2` |
| `sql/final_oracles/sql_ticket_archive_transitions.json` | retained | 1418 | `d53b1a63031ad6f9aa39739137769a98dfb639792466f480521d749ae5671f64` |
| `sql/oracles/sql_notification_override_tristate.json` | retained | 1634 | `df2259e0c362a62ade10ad3aceb235dc1f908df190db866021e1f03ec1e2329e` |
| `sql/oracles/sql_tenant_stock_ownership.json` | retained | 1564 | `5d60aa370b80b4d4cc9a737c98e36091f14c6b19b3bc47b372ab13ca700f04d5` |
| `sql/oracles/sql_ticket_archive_transitions.json` | retained | 1772 | `b0b52631d45d820cff7a5e50c646d3017fa4214a2edb0b087f8005a5144e6d91` |
