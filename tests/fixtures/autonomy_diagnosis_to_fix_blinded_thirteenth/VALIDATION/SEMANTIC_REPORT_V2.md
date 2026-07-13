# Cross-Case Semantic Audit V2

**Verdict: PASS**

The supplemented four lane results provide complete semantic evidence for all 12 cases. Every case now has candidate and combined source-skeleton SHA-256 values, and every Dart case has an explicit assertion family, mechanism, feedback boundary, and final boundary.

## Verified Gates

- Case IDs: 12 unique.
- Dimensions: exact `code=1`, `clock=2`, `config=2`, `data=2`, `dependency=2`, `runtime=1`, `state=2`.
- Exact duplicates: none among case IDs, mechanisms, assertion families, feedback boundaries, final boundaries, or the 12 combined case-level skeleton hashes.
- Material equivalence: none. The closest same-dimension pairs remain distinct: calendar clamping versus offset-transition inversion; attempt fencing versus guarded SQL updates; media-type matching versus delimiter-aware rendering; missing-value rollup versus unit conversion; dependency injection versus report/license parsing.
- Final novelty: all 12 finals add a new boundary or composition beyond feedback.
- Prohibited overlap: none against the 12 recorded families. Similar words are non-material: Dart timezone offsets are not tail checkpoints, filename normalization is not canonical base64url, static config parsing/rendering is not replacement reload, and worker ownership is not composite tenant-stock ownership.
- Lane health: Python, Node, Dart, and SQL all report `PASS`; no required gate is false, failed, or incomplete.

The equal candidate hashes inside `th13_sql_delimited_profile` and `th13_sql_export_job_state` represent symmetric owner statements within each case. Their path-aware combined case hashes are unique, and their mechanisms, assertions, and boundaries are distinct from every other case.
