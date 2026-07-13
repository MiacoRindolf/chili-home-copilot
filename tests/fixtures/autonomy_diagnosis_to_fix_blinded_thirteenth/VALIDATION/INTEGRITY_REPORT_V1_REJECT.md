# Cross-Integrity Audit

- Result: **REJECT**
- Verification: complete
- Authored file count: 40
- Aggregate SHA-256: `e05b205217ab8e618d6946b7811df744c836db4b71ddbd81adf09cd4f4761803`
- Incomplete gates: none
- Failed gates: lane_post_hash_coverage

## Findings

- FAIL `lane_post_hash_coverage`: SQL lane post-validation inventory omits AUTHOR_RECEIPT.md (9 recorded hashes for 10 current authored files).
- PASS `recorded_post_hash_matches`: 39 of 39 lane-recorded post-validation hashes match current raw bytes exactly.
- PASS `before_equals_after`: Before and after hashes, inventory digests/counts where present, and unchanged flags agree in all four lanes.
- PASS `triplets`: Each author root contains exactly three case/oracle/final_oracle triplets plus AUTHOR_RECEIPT.md.
- PASS `identities`: Found 12 identities and 12 unique identities; each case_id matches its directory.
- PASS `language_counts`: python=3, typescript=3, dart=3, sql=3.
- PASS `contained_relative_paths`: All authored and lane-recorded inventory paths are contained relative paths.
- PASS `symlinks`: No symlinks, junctions, reparse points, or special filesystem entries were found.
- PASS `ascii`: All 40 authored files and their normalized relative paths are 7-bit ASCII.
- PASS `lane_author_roots`: Each lane's recorded author_root matches the audited root.

## Aggregate Algorithm

Use one SHA-256 state. Process root labels in the exact order `dart`, `node`, `python`, `sql`. Within each root, convert path separators to `/` and sort normalized relative paths in ascending ordinal order. For every authored file, including `AUTHOR_RECEIPT.md`, feed:

```text
UTF-8(<label>/<relative/path>) || 0x00 || raw file bytes || 0x00
```

No separator or finalization bytes other than the two stated NUL bytes per file are fed.

## Lane Hash Coverage

| Root | Current | Recorded post | Matches | Exact coverage | Before=after | Missing from lane |
|---|---:|---:|---:|---|---|---|
| dart | 10 | 10 | 10 | yes | yes | none |
| node | 10 | 10 | 10 | yes | yes | none |
| python | 10 | 10 | 10 | yes | yes | none |
| sql | 10 | 9 | 9 | no | yes | AUTHOR_RECEIPT.md |

## Sorted Inventory Hashes

Canonical root order is used; relative paths are sorted within each root.

| Path | Bytes | SHA-256 |
|---|---:|---|
| `dart/AUTHOR_RECEIPT.md` | 3602 | `cb4df78902ca5cc7613687ec3e486b1ea6589e44c7eedf9b7a53387429924ce9` |
| `dart/th13_dart_dependency_report/case.json` | 3621 | `c843b3a4c89fbd76a130941f7497bd577c0d222a3028919849945d4804eb83e1` |
| `dart/th13_dart_dependency_report/final_oracle.json` | 1568 | `50631c7489a3c74621a002d978f1181fe298158e0e54abd20b248c813aa7e142` |
| `dart/th13_dart_dependency_report/oracle.json` | 1532 | `ef029d99b0be51028bec790edb6c32c109d5a9a7d24a9b000e2c7ccdf3ed6efd` |
| `dart/th13_dart_offset_schedule/case.json` | 4141 | `d499a3740aceafe6fcc571cb6472d396a6312b7ae254a1bb02a4ab3976407500` |
| `dart/th13_dart_offset_schedule/final_oracle.json` | 770 | `9b772a97de211b81fabe7cc72c704d162260728673b05c6d9a9119646a4ed679` |
| `dart/th13_dart_offset_schedule/oracle.json` | 1055 | `57724ffe83f033b9bad01c0e4488436e459214893a7fda14c42d4670cd2c6506` |
| `dart/th13_dart_portable_exports/case.json` | 2348 | `1d5101622bf012f0fdd1c60bf834de667a04bee28ed65c5bbc1c5690c7488a9a` |
| `dart/th13_dart_portable_exports/final_oracle.json` | 802 | `d397532681ee5b69d938da4831e0244d0de162563996197b54b81049d3275c46` |
| `dart/th13_dart_portable_exports/oracle.json` | 956 | `aa7d390219813b95306fc61f7df2c5463d5843019f832f74f2ec87a81533d5be` |
| `node/AUTHOR_RECEIPT.md` | 2619 | `701dd6714520577479134476b555d68029a1ddebfa7df2a041e75ccd95b460b0` |
| `node/th13_node_facility_rollup/case.json` | 3982 | `0ed58bccf859151c4fbc461c8afe793f70fc05050172de4175011457ecad951f` |
| `node/th13_node_facility_rollup/final_oracle.json` | 1071 | `0d368ec47b9165462815dde4b2fcea52a011387e93b23956c05b89388b650ff7` |
| `node/th13_node_facility_rollup/oracle.json` | 1216 | `2f67641daf429c8a969ec5ab4efe5439a4b173ec445c2c493d3e0e1acf8c9bdc` |
| `node/th13_node_job_recovery/case.json` | 4721 | `bb32a60140b3b3863fcd3b9f8467b22f2f033594d51c12177baf297d76fac8d1` |
| `node/th13_node_job_recovery/final_oracle.json` | 1776 | `ba14e700a88f25ce0655010d84719cab6a96d4a09030fd01c223f9828eeb5910` |
| `node/th13_node_job_recovery/oracle.json` | 1736 | `012d157421fa001f74465a0f887ae1ed156bf05b4111623b7a826bfb4a096eba` |
| `node/th13_node_response_compression/case.json` | 2820 | `7eb9ff54c94516b078dc221c623b39d34c6ee132aa7384e11e377cd49295a2ac` |
| `node/th13_node_response_compression/final_oracle.json` | 738 | `2162a2880c77ec92f44238360eba2f7fec6933201e66b6dfc8c3fff6f9db0bc1` |
| `node/th13_node_response_compression/oracle.json` | 1130 | `b4a511a77692333bb0f3882f7189aff5af282fbac97f6bdd005303c42ce10cbf` |
| `python/AUTHOR_RECEIPT.md` | 2170 | `cc580e69e5a43cd09a16d3189d2f11186d2fd6de65d1bb1dbcaebc95a42fbb1c` |
| `python/th13_py_factory_binding/case.json` | 2901 | `30906cb6b6364bcfe6a166d8bb8b6e8b1d338d28d644c1fa28939687f981f3f6` |
| `python/th13_py_factory_binding/final_oracle.json` | 1024 | `35dbeb6fee30c8868ad06fe84a2a8a55438a5ea51c72d4458e25fc0dc95648f1` |
| `python/th13_py_factory_binding/oracle.json` | 922 | `421cf8c120c5d1e4e35d84c78892c5dd0d4e853b734fc5e0f0f150394e812672` |
| `python/th13_py_monthly_settlement/case.json` | 2962 | `08e975278eba8f2b739ba48b0a85d91e4f484b4049b1e841a9b2b28e730692e2` |
| `python/th13_py_monthly_settlement/final_oracle.json` | 475 | `87a4db172687698d435a144fd3756a98b9c304aa2800cf2817865b2aa14c0d54` |
| `python/th13_py_monthly_settlement/oracle.json` | 910 | `2c87cfe4075b2892a90aad230210405a14212074b1d98d864990ea0c70dbc4cd` |
| `python/th13_py_task_teardown/case.json` | 2237 | `9bc9aa757d6059aa24531860982b57f8e83b294c6deafd54ea43f7bb7e525fc6` |
| `python/th13_py_task_teardown/final_oracle.json` | 764 | `cf7a7133c70679e074c6990cfbb3b71be75fa7f18cdf46b982f7de70c209c3d4` |
| `python/th13_py_task_teardown/oracle.json` | 1191 | `46e85aa47c1fd2fac3f4bac592574163cb52e575708074ef74273067e81c5845` |
| `sql/AUTHOR_RECEIPT.md` | 2924 | `d133e5ff6b2bc9c31b221303cbf777dd72cc1ceb20d7333d88de9dd2f4dd8fea` |
| `sql/th13_sql_delimited_profile/case.json` | 3636 | `a03aec7dff223c5ebb94d419d2166d931ca4f5af1dcf77f3022abec68f9edbb9` |
| `sql/th13_sql_delimited_profile/final_oracle.json` | 2086 | `edc1589f29144b5171d20fd546297045f8093134b47719275bedbcc3fda1b837` |
| `sql/th13_sql_delimited_profile/oracle.json` | 1896 | `10c2780ecc6b688657f09d969b2b8465604bdeeb01bc4b69796d2f27ad755eb1` |
| `sql/th13_sql_export_job_state/case.json` | 2448 | `f356a264a35ee25caea803d96711d039409db705c1a5e4b6a3a34a6da1a85805` |
| `sql/th13_sql_export_job_state/final_oracle.json` | 2824 | `b3ac56deada2b902d17839f74371e8d4a992d4f42de3ebb086fc5127cbd72e1f` |
| `sql/th13_sql_export_job_state/oracle.json` | 2016 | `6fad52d6336775844256b1fb7e00819f8d596b3506b8aec266e8a895d8a618d6` |
| `sql/th13_sql_package_units/case.json` | 2474 | `4f01315e73a75f8a5d3741bdcf62bff72ccb738cb7c832b39646c400fcee2d90` |
| `sql/th13_sql_package_units/final_oracle.json` | 2007 | `5a90d5d7f8af187b5d4e7f1d6f1d00c3e97fcb5eef817a54a9b5999fc41d859f` |
| `sql/th13_sql_package_units/oracle.json` | 2010 | `1c670711a24ddeb917c7913efb714666427723e9e67d8a3aae9def6166cd5339` |
