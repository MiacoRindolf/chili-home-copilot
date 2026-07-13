# Independent Validator Receipt

**Verdict: REJECT**

Validation date: 2026-07-12  
Input scope: only `D:\dev\chili-twelfth-authors\python`, `node`, `dart`, and `sql`  
Output scope: only `D:\dev\chili-twelfth-validation`  

No author artifact was edited. No git repository, prior fixture/result/history, prior conversation content, internet resource, or CHILI model was inspected or called. Disposable test directories were created only under the validation directory and removed before receipt creation.

## Protocol Summary

| Check | Result | Finding |
|---|---:|---|
| Exactly 12 cases, 3 per language | PASS | Python 3; Node ESM 3; Dart 3; SQLite SQL 3. |
| Authored case/oracle/final schemas | PASS | Exact top-level schemas and matching identifiers for all 12 triples. |
| Baseline public/feedback/final pattern | PASS | 12/12 public passed; 12/12 feedback failed; 12/12 final failed in isolated runs. |
| Test-file disjointness | PASS | One pairwise-disjoint public, feedback, and final test path per case. |
| Materially new final boundary | PASS | Each final reaches an additional state, layer, transition, address family, parent lifecycle, or terminal condition. |
| Candidate ownership and max_files | PASS | Every case has 2-3 candidate owners; `expected_files` equals those owners and fits `max_files`. |
| Objective causal dimension | PASS | Each case has a predominant config, data, or state mechanism. |
| Safety and dependencies | PASS | Offline/runtime-only production mechanisms; no network, premium, unsafe, or external package requirement. |
| Author attestations | PASS | All authors attest assigned-directory-only work and all required exclusions. |
| Disclosed-family novelty | **REJECT** | Three cases replay arbitrary UTF-8/record framing across transport chunks. |
| Cross-suite mechanism diversity | **REJECT** | The three streaming cases duplicate one another; Node environment reconfiguration also duplicates Python live configuration replacement. |

## Rejection Findings

1. `py_stream_records`, `node_ndjson_chunk_ingest`, and `dart_stream_record_framing` all center on incremental UTF-8 decoding plus logical record framing across arbitrary byte chunks. The Python and Node cases are direct NDJSON/JSON-sequence replays. The Dart case applies the same transport/framing repair to quoted delimited records. This is an expressly excluded family and a three-language semantic duplicate.
2. `node_worker_env_reconfigure` and `py_config_reload` both require invalidating stale derived configuration after live reconfiguration while distinguishing removal/inheritance from ordinary overrides. Retain `py_config_reload`; replace the Node case.

## Per-Case Findings

| Case | Verdict | Dimension | Mechanism and final boundary | Overlap/novelty |
|---|---:|---|---|---|
| `py_config_reload` | PASS | config | Live replacement, derived endpoint rebinding, and omitted-override reset; final covers removal and empty replacement. | Retained canonical case in the config near-duplicate pair. |
| `py_stream_records` | **REJECT** | data | Incremental NDJSON, split UTF-8, and terminal flush; final covers an unterminated last record and idempotent finish. | Disclosed-family replay; near-duplicate of Node and Dart streaming cases. |
| `py_tail_checkpoint` | PASS | state | File-identity checkpoint provenance; final covers same-identity truncation past the saved offset. | Novel. |
| `node_ndjson_chunk_ingest` | **REJECT** | data | Incremental NDJSON, split UTF-8, and terminal error/flush; final covers malformed terminal UTF-8. | Disclosed-family replay; near-duplicate of Python and Dart streaming cases. |
| `node_policy_reload_consistency` | PASS | state | Request-scoped immutable policy generation and matching audit provenance; final covers absent-route insertion, distinct from role revocation. | Novel. |
| `node_worker_env_reconfigure` | **REJECT** | config | Live layered environment reconfiguration and null/undefined semantics; final covers per-launch suppression and inheritance. | Near-duplicate of `py_config_reload`. |
| `dart_release_reader_lifecycle` | PASS | state | Generation-specific reader accounting and deferred release reclamation; final covers multiple readers, generations, and repeated close. | Novel. |
| `dart_stream_record_framing` | **REJECT** | data | Incremental UTF-8 and quoted record framing across chunks/CRLF; final covers embedded newlines and malformed terminal UTF-8. | Disclosed-family replay; near-duplicate of Python and Node streaming cases. |
| `dart_trusted_proxy_chain` | PASS | config | CIDR trust and aligned forwarded-hop resolution; final covers IPv6 CIDR and chain alignment. | Novel. |
| `sql_notification_override_tristate` | PASS | config | Tri-state hierarchy with retained NULL provenance; final covers workspace-to-system fallback beyond member fallback. | Novel. |
| `sql_tenant_stock_ownership` | PASS | data | Composite tenant ownership and tenant-aligned projection; final covers parent deletion and identity mutation. | Novel. |
| `sql_ticket_archive_transitions` | PASS | state | Stored active-count transitions and active-only dashboard; final covers archived moves and restore-plus-reassignment. | Novel. |

All 12 ownership checks pass: each case has exactly 2-3 candidate source owners, all candidates exist in `repo_files`, `expected_files` matches the candidate set, and the count is feasible under `max_files`.

## Independent Baselines

Each row was run in three separate fresh directories. Only the named tier was invoked.

| Case | Public | Feedback | Final |
|---|---|---|---|
| `py_config_reload` | exit 0, 3 passed | exit 1, 2 failed | exit 1, 3 failed |
| `py_stream_records` | exit 0, 3 passed | exit 1, 3 failed | exit 1, 2 failed/1 passed |
| `py_tail_checkpoint` | exit 0, 2 passed | exit 1, 3 failed | exit 1, 2 failed |
| `node_ndjson_chunk_ingest` | exit 0, pass | exit 1, fail | exit 1, fail |
| `node_policy_reload_consistency` | exit 0, pass | exit 1, fail | exit 1, fail |
| `node_worker_env_reconfigure` | exit 0, pass | exit 1, fail | exit 1, fail |
| `dart_release_reader_lifecycle` | exit 0, pass | exit 255, fail | exit 255, fail |
| `dart_stream_record_framing` | exit 0, pass | exit 255, fail | exit 255, fail |
| `dart_trusted_proxy_chain` | exit 0, pass | exit 255, fail | exit 255, fail |
| `sql_notification_override_tristate` | exit 0, 3 passed | exit 1, 2 failed | exit 1, 2 failed |
| `sql_tenant_stock_ownership` | exit 0, 2 passed | exit 1, 2 failed | exit 1, 2 failed |
| `sql_ticket_archive_transitions` | exit 0, 2 passed | exit 1, 2 failed | exit 1, 2 failed |

## Exact Correction Requests

1. **Python / `py_stream_records`:** Replace the complete case/oracle/final triple with a new Python data case unrelated to byte-chunk decoding, UTF-8 carryover, delimiter or record framing, JSON sequences, or terminal stream flushing. Preserve the authored schemas, 2-4 owners, feasible `expected_files`/`max_files`, disjoint tiers, required baseline pattern, and a materially new final boundary. Update the Python attestation and `exact_files` list.
2. **Node / `node_ndjson_chunk_ingest`:** Replace the complete triple with a new Node ESM data case unrelated to byte-chunk decoding, UTF-8 carryover, delimiter or record framing, NDJSON/JSON sequences, or terminal stream flushing. Preserve all structural and baseline requirements and update the Node attestation.
3. **Node / `node_worker_env_reconfigure`:** Replace the complete triple with a new Node ESM config case that does not use runtime reload/reconfigure, cached or derived configuration invalidation, override omission/removal/inheritance, or layered environment semantics. Preserve all structural and baseline requirements and update the Node attestation.
4. **Dart / `dart_stream_record_framing`:** Replace the complete triple with a new Dart data case unrelated to byte-chunk decoding, UTF-8 carryover, delimiter or record framing, quoted multiline records, JSON sequences, CRLF chunk boundaries, or terminal stream flushing. Preserve all structural and baseline requirements and update the Dart attestation.

After replacement, resubmit exactly 12 triples and no additional author artifacts. Re-run public, feedback, and final baselines independently for each replacement.

## SHA-256 Input Hashes

| Path | Bytes | SHA-256 |
|---|---:|---|
| `dart/AUTHOR_ATTESTATION.json` | 1864 | `fa983ecd1e07a0adbd697f5789c179ab1a3598f1321f64e7bbd4bf5dcc65662f` |
| `dart/cases/dart_release_reader_lifecycle.json` | 3681 | `e67aaecc0249f1a7cb0fb61b74d47dc10d2a5bc946a3478066d08d784b123277` |
| `dart/cases/dart_stream_record_framing.json` | 2436 | `da23abad1ec6d02f50677f6cb45d8d3acdf73a5d5ffc88f034a1cf1158b82553` |
| `dart/cases/dart_trusted_proxy_chain.json` | 4439 | `fe905d47bbd43b8a1bb86cd298cae29c217267e9b40e63ecc13910fcdd8090de` |
| `dart/final_oracles/dart_release_reader_lifecycle.json` | 1555 | `1e7f665ecbee9b80b1dbc5b062da685a8679825dc6e581b0a5c863123212cd1d` |
| `dart/final_oracles/dart_stream_record_framing.json` | 1349 | `7dce88c2cc20cff362ee050aeed05d127785fcc823546e1a2c439e2e08e7a640` |
| `dart/final_oracles/dart_trusted_proxy_chain.json` | 1130 | `54dadd8e29c9eb36ec26043a74a5f31f5c76f359559fc4b577fd2afc3fa84dc2` |
| `dart/oracles/dart_release_reader_lifecycle.json` | 1057 | `74892d7b2d951c4691b5a7374c310f1d8495d33f38de5c7e01b7173ef68d6960` |
| `dart/oracles/dart_stream_record_framing.json` | 1339 | `a47b838b163ce873357133970d303f54077cdb2cd85ea3a84b9af2bcd20c122b` |
| `dart/oracles/dart_trusted_proxy_chain.json` | 1375 | `672feb1ce5b04633688021d4da251a77f6e5fb84ec8ae33525e14d307483ae01` |
| `node/AUTHOR_ATTESTATION.json` | 1517 | `bda29b8eff543b1658a08ad471ffafbaf2d8f2190dae95ac910461741c18a45a` |
| `node/cases/node_ndjson_chunk_ingest.json` | 3128 | `0af2c964a416b9bd4927d98fe91d8b2d40af43b24498d9c98d2425b0dc97686f` |
| `node/cases/node_policy_reload_consistency.json` | 3520 | `0a2552a5bc0ace443af09c431c48371795da2d5017cb55b31a35dcb71a769ff2` |
| `node/cases/node_worker_env_reconfigure.json` | 2970 | `a841e078565b7b9e2643ee44447e326c3f3e16efb1eed5187a8d2b86841187e4` |
| `node/final_oracles/node_ndjson_chunk_ingest.json` | 547 | `d6b940d1a3e1db56c7caad705a9c406fcec219b150ecf79fd1ebf75b1ac14e78` |
| `node/final_oracles/node_policy_reload_consistency.json` | 1239 | `c65bb4eefa54b59377daa8170993c665bef70a041758e094cb2ac0deb88e460f` |
| `node/final_oracles/node_worker_env_reconfigure.json` | 853 | `136111cfaf910db25e44210ee60297f0880c20a0f5cebfb693ce15a9c1ae3311` |
| `node/oracles/node_ndjson_chunk_ingest.json` | 1180 | `7929eceb5d532325cf1f63dcdc25d63283e32f7a895e692744421ffa2ddc59e5` |
| `node/oracles/node_policy_reload_consistency.json` | 1359 | `581c2dc23144047cba3667b4016c48f3a7738baf466b072463fac3a70252ea10` |
| `node/oracles/node_worker_env_reconfigure.json` | 914 | `a52ef5dbd855474574fbe308fb78adbd567352fa1aaa819cd246415b418ae63a` |
| `python/AUTHOR_ATTESTATION.json` | 1893 | `8d151cefc3d33940820d9fdbaddce62205196d821b6fe5fae3ab59989f2107a5` |
| `python/cases/py_config_reload.json` | 3661 | `581a889150674816b9e6148e3a98298835374f5db6760eea4e0e3b7c0cd2fbd4` |
| `python/cases/py_stream_records.json` | 1952 | `e00b11ea85be0e5190c38d036057a5d7f761148cf8f906d6a3bfe533962f3dfd` |
| `python/cases/py_tail_checkpoint.json` | 2692 | `aa87a5f6f5054bc82cb78352af380baf9f5372a2ce430063e5ce1fe19c68a8e7` |
| `python/final_oracles/py_config_reload.json` | 1392 | `aaf7c9ecca9addc7019d8cae8e7261b45088458e24dc3a6559edf4050cf5c051` |
| `python/final_oracles/py_stream_records.json` | 711 | `ff44985b7a516948d6bb4461ddffe19f2a8bf0f39d26aadff0dcd5f7a27095ee` |
| `python/final_oracles/py_tail_checkpoint.json` | 1163 | `990838d2c86b46c9de3556a1324e44b23e61f0bc5a25657a4bcff350ecfdb63a` |
| `python/oracles/py_config_reload.json` | 1243 | `1aec36eb7c1d355fb4ca25f84ab7198ecec8ba6c0ffc62bba72f9d8ee2530cdc` |
| `python/oracles/py_stream_records.json` | 1182 | `da9556f9ce34490699fe6697a5603728b7363b289d2542d651391648709bb16f` |
| `python/oracles/py_tail_checkpoint.json` | 1688 | `5688fd799f98e208f0282cbb4bd5e9d4061f84a94f4a0219a63c53abe300b4d9` |
| `sql/AUTHOR_ATTESTATION.json` | 1553 | `5a6fdb587eaa38cd870ff21eafcf8a4f60d5d91217136acad9a575e9803ab01b` |
| `sql/cases/sql_notification_override_tristate.json` | 4172 | `95a26634fb377bd6b5d5dff7eaf0e3e500ed961809a4afc11a536234c6c05b7f` |
| `sql/cases/sql_tenant_stock_ownership.json` | 3009 | `c80f89a027415009deae63b8c895412edcd9c4360b64f60c664d122bb52ceab3` |
| `sql/cases/sql_ticket_archive_transitions.json` | 4614 | `3c546d29576940c92babc832b0aca0a9af317badf154f72afff63bf9b58ecc4d` |
| `sql/final_oracles/sql_notification_override_tristate.json` | 1339 | `5c3805272fc9d86a20f655c1e4fd8c84fad44c6f94f5b2eb44139b0e25dc8867` |
| `sql/final_oracles/sql_tenant_stock_ownership.json` | 1369 | `0ddde2be64ef6639a9ed89ba609faaa6e1300d79eeb259c15e911edff95e97b2` |
| `sql/final_oracles/sql_ticket_archive_transitions.json` | 1418 | `d53b1a63031ad6f9aa39739137769a98dfb639792466f480521d749ae5671f64` |
| `sql/oracles/sql_notification_override_tristate.json` | 1634 | `df2259e0c362a62ade10ad3aceb235dc1f908df190db866021e1f03ec1e2329e` |
| `sql/oracles/sql_tenant_stock_ownership.json` | 1564 | `5d60aa370b80b4d4cc9a737c98e36091f14c6b19b3bc47b372ab13ca700f04d5` |
| `sql/oracles/sql_ticket_archive_transitions.json` | 1772 | `b0b52631d45d820cff7a5e50c646d3017fa4214a2edb0b087f8005a5144e6d91` |
