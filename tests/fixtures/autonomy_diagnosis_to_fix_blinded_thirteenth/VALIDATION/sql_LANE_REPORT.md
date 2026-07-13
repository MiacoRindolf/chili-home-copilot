# SQL Lane Validation Report

## Verdict

**PASS** - all assigned gates were actually verified for exactly three SQL cases. No failed or incomplete gates remain.

## Scope and method

- Read root: `D:\dev\chili-thirteenth-author-sql` (read-only).
- Write root: `D:\dev\chili-thirteenth-validation-lane-sql` only.
- `AUTHOR_RECEIPT.md` was not opened or hashed because prior reports were out of scope.
- No model, external service, Git operation, container, broker, service, or unrelated process was invoked.
- Every run used a fresh directory below `temp/work`, a 20-second timeout, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and `PYTHONDONTWRITEBYTECODE=1`.
- Runtime: Python 3.13.11; pytest 9.0.2; SQLite 3.51.0.
- Harness command: cwd `D:\dev\chili-thirteenth-validation-lane-sql`; `python .\temp\validate_lane.py`; exit `0`; output: `case_count=3, run_count=24, static_checks=42, author_unchanged=true, automated_gates_pass=true`.

## Cases

| Case ID | Dimension | Candidates / expected owners | max_files | Static gates |
|---|---|---|---:|---|
| `th13_sql_delimited_profile` | `config` | `sql/render_customer_row.sql`, `sql/render_order_row.sql` | 2 | 14/14 PASS |
| `th13_sql_export_job_state` | `state` | `sql/claim_export_job.sql`, `sql/finish_export_job.sql` | 2 | 14/14 PASS |
| `th13_sql_package_units` | `data` | `sql/package_volume.sql`, `sql/oversize_queue.sql` | 2 | 14/14 PASS |

For each case, strict duplicate-key JSON parsing succeeded for `case.json`, `oracle.json`, and `final_oracle.json`; directory and case identities agree; language is `sql`; runner is `pytest`; and source/public/feedback/final maps are disjoint and correctly named. All embedded paths are relative, contained POSIX-style ASCII paths; all embedded contents are ASCII; author JSON nodes and materialized files are non-symlinks/regular files. Each case has two plausible parameterized SQL candidates, exactly the two expected owners, and `max_files=2`.

Leak audit: public JSON contains none of `expected_dimension`, `expected_files`, `feedback_files`, or `final_files`. Hidden test names are absent from public payloads, complete hidden files are absent, and normalized public/hidden test-function hashes are disjoint. Shared schema/helper boilerplate is ordinary fixture setup and exposes no hidden fixture values or assertions.

## Execution evidence

Commands below are exact. `cwd` is absolute; outputs are intentionally concise.

| # | Case / scenario | cwd | Command | Exit | Concise output |
|---:|---|---|---|---:|---|
| 1 | `th13_sql_delimited_profile` / `baseline_public` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_delimited_profile\baseline_public` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py` | 0 | 2 passed in 0.02s |
| 2 | `th13_sql_delimited_profile` / `baseline_feedback` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_delimited_profile\baseline_feedback` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_feedback.py` | 1 | FAILED tests/test_feedback.py::test_customer_rows_use_the_selected_profile_separator \| FAILED tests/test_feedback.py::test_order_rows_use_the_selected_profile_separator |
| 3 | `th13_sql_delimited_profile` / `baseline_final` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_delimited_profile\baseline_final` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_final.py` | 1 | FAILED tests/test_final.py::test_customer_values_containing_the_selected_separator_are_quoted \| FAILED tests/test_final.py::test_order_quoting_uses_the_selected_separator_and_doubles_quotes |
| 4 | `th13_sql_delimited_profile` / `repaired_all` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_delimited_profile\repaired_all` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 0 | 6 passed in 0.03s |
| 5 | `th13_sql_delimited_profile` / `owner_reverted_render_customer_row` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_delimited_profile\owner_reverted_render_customer_row` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_feedback.py::test_customer_rows_use_the_selected_profile_separator \| FAILED tests/test_final.py::test_customer_values_containing_the_selected_separator_are_quoted |
| 6 | `th13_sql_delimited_profile` / `owner_omitted_render_customer_row` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_delimited_profile\owner_omitted_render_customer_row` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_public.py::test_renders_a_customer_with_the_default_profile \| FAILED tests/test_feedback.py::test_customer_rows_use_the_selected_profile_separator \| FAILED tests/test_final.py::test_customer_values_containing_the_selected_separator_are_quoted |
| 7 | `th13_sql_delimited_profile` / `owner_reverted_render_order_row` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_delimited_profile\owner_reverted_render_order_row` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_feedback.py::test_order_rows_use_the_selected_profile_separator \| FAILED tests/test_final.py::test_order_quoting_uses_the_selected_separator_and_doubles_quotes |
| 8 | `th13_sql_delimited_profile` / `owner_omitted_render_order_row` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_delimited_profile\owner_omitted_render_order_row` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_public.py::test_renders_an_order_with_the_default_profile \| FAILED tests/test_feedback.py::test_order_rows_use_the_selected_profile_separator \| FAILED tests/test_final.py::test_order_quoting_uses_the_selected_separator_and_doubles_quotes |
| 9 | `th13_sql_export_job_state` / `baseline_public` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_export_job_state\baseline_public` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py` | 0 | 2 passed in 0.02s |
| 10 | `th13_sql_export_job_state` / `baseline_feedback` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_export_job_state\baseline_feedback` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_feedback.py` | 1 | FAILED tests/test_feedback.py::test_a_second_worker_cannot_take_an_active_job \| FAILED tests/test_feedback.py::test_a_non_owner_cannot_finish_a_running_job |
| 11 | `th13_sql_export_job_state` / `baseline_final` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_export_job_state\baseline_final` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_final.py` | 1 | FAILED tests/test_final.py::test_a_canceled_job_cannot_be_claimed_again - ass... \| FAILED tests/test_final.py::test_a_queued_job_cannot_be_finished_without_a_claim \| FAILED tests/test_final.py::test_rejected_interference_does_not_block_one_terminal_result |
| 12 | `th13_sql_export_job_state` / `repaired_all` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_export_job_state\repaired_all` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 0 | 7 passed in 0.04s |
| 13 | `th13_sql_export_job_state` / `owner_reverted_claim_export_job` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_export_job_state\owner_reverted_claim_export_job` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_feedback.py::test_a_second_worker_cannot_take_an_active_job \| FAILED tests/test_final.py::test_a_canceled_job_cannot_be_claimed_again - ass... |
| 14 | `th13_sql_export_job_state` / `owner_omitted_claim_export_job` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_export_job_state\owner_omitted_claim_export_job` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_public.py::test_a_worker_claims_a_queued_job - FileNotFound... \| FAILED tests/test_feedback.py::test_a_second_worker_cannot_take_an_active_job \| FAILED tests/test_final.py::test_a_canceled_job_cannot_be_claimed_again - Fil... |
| 15 | `th13_sql_export_job_state` / `owner_reverted_finish_export_job` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_export_job_state\owner_reverted_finish_export_job` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_feedback.py::test_a_non_owner_cannot_finish_a_running_job \| FAILED tests/test_final.py::test_a_queued_job_cannot_be_finished_without_a_claim \| FAILED tests/test_final.py::test_rejected_interference_does_not_block_one_terminal_result |
| 16 | `th13_sql_export_job_state` / `owner_omitted_finish_export_job` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_export_job_state\owner_omitted_finish_export_job` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_public.py::test_the_owning_worker_finishes_a_running_job - ... \| FAILED tests/test_feedback.py::test_a_non_owner_cannot_finish_a_running_job \| FAILED tests/test_final.py::test_a_queued_job_cannot_be_finished_without_a_claim \| FAILED tests/test_final.py::test_rejected_interference_does_not_block_one_terminal_result |
| 17 | `th13_sql_package_units` / `baseline_public` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_package_units\baseline_public` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py` | 0 | 2 passed in 0.02s |
| 18 | `th13_sql_package_units` / `baseline_feedback` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_package_units\baseline_feedback` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_feedback.py` | 1 | FAILED tests/test_feedback.py::test_converts_every_dimension_before_computing_cubic_volume \| FAILED tests/test_feedback.py::test_oversize_queue_compares_converted_lengths_to_centimeters |
| 19 | `th13_sql_package_units` / `baseline_final` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_package_units\baseline_final` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_final.py` | 1 | FAILED tests/test_final.py::test_fractional_unit_factors_apply_to_all_three_dimensions \| FAILED tests/test_final.py::test_mixed_units_respect_the_exact_oversize_boundary |
| 20 | `th13_sql_package_units` / `repaired_all` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_package_units\repaired_all` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 0 | 6 passed in 0.03s |
| 21 | `th13_sql_package_units` / `owner_reverted_package_volume` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_package_units\owner_reverted_package_volume` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_feedback.py::test_converts_every_dimension_before_computing_cubic_volume \| FAILED tests/test_final.py::test_fractional_unit_factors_apply_to_all_three_dimensions |
| 22 | `th13_sql_package_units` / `owner_omitted_package_volume` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_package_units\owner_omitted_package_volume` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_public.py::test_reports_volume_for_a_centimeter_package - F... \| FAILED tests/test_feedback.py::test_converts_every_dimension_before_computing_cubic_volume \| FAILED tests/test_final.py::test_fractional_unit_factors_apply_to_all_three_dimensions |
| 23 | `th13_sql_package_units` / `owner_reverted_oversize_queue` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_package_units\owner_reverted_oversize_queue` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_feedback.py::test_oversize_queue_compares_converted_lengths_to_centimeters \| FAILED tests/test_final.py::test_mixed_units_respect_the_exact_oversize_boundary |
| 24 | `th13_sql_package_units` / `owner_omitted_oversize_queue` | `D:\dev\chili-thirteenth-validation-lane-sql\temp\work\th13_sql_package_units\owner_omitted_oversize_queue` | `C:\Users\rindo\miniconda3\python.exe -m pytest -q --tb=line --disable-warnings tests/test_public.py tests/test_feedback.py tests/test_final.py` | 1 | FAILED tests/test_public.py::test_queues_centimeter_packages_over_the_limit \| FAILED tests/test_feedback.py::test_oversize_queue_compares_converted_lengths_to_centimeters \| FAILED tests/test_final.py::test_mixed_units_respect_the_exact_oversize_boundary |

Outcome interpretation: `baseline_public` and `repaired_all` were required to exit 0. Baseline feedback/final plus every owner-reverted and owner-omitted run were required to exit nonzero. All 24 outcomes matched those requirements; no run timed out.

## Repair and necessity findings

### `th13_sql_delimited_profile` (`config`)

- Mechanism: Profile-selected field/record separators with delimiter-sensitive quoting.
- Oracle-consistent repair in fresh copies: Both row renderers use p.field_separator for concatenation and quote detection while retaining quote doubling and the selected record terminator.
- Final-test novelty: Feedback checks non-comma selection on plain fields and CRLF. Final adds composition with a selected separator inside customer/order values plus embedded double-quote escaping; this is a new quoting boundary, not a rename.
- Distinct signature: SELECT text renderer joined to export_profile. Assertion family: Exact rendered strings. Final boundary: Configured separator inside a field and doubled embedded quotes.
- Owner necessity: reverting either owner independently failed feedback/final validation; omitting either owner independently also failed required tests.

### `th13_sql_export_job_state` (`state`)

- Mechanism: Guarded UPDATE state transitions using current state and worker ownership.
- Oracle-consistent repair in fresh copies: Claim is limited to unowned queued jobs; finish is limited to the owning worker's unfinished running job.
- Final-test novelty: Feedback checks competing claim and non-owner finish independently. Final adds canceled/queued source-state rejection and a composed foreign rejection -> owner completion -> replay sequence proving one immutable terminal result.
- Distinct signature: Compare-and-set-like UPDATE statements. Assertion family: Row counts plus persisted state, including a transition sequence. Final boundary: Invalid source states, rejected interference, terminal replay.
- Owner necessity: reverting either owner independently failed feedback/final validation; omitting either owner independently also failed required tests.

### `th13_sql_package_units` (`data`)

- Mechanism: Lookup-table unit normalization for cubic volume and oversize filtering.
- Oracle-consistent repair in fresh copies: Both queries join length_unit; volume converts all three axes and queue comparison converts each axis before max().
- Final-test novelty: Feedback uses inches around a non-equal threshold. Final adds a factor below one, three-axis fractional composition, exact equality versus just-below, feet, centimeters, and ordered mixed-unit results.
- Distinct signature: SELECT metric queries joined to length_unit. Assertion family: math.isclose for numeric volume and ordered row-set equality. Final boundary: Fractional factors, exact threshold equality, and mixed units.
- Owner necessity: reverting either owner independently failed feedback/final validation; omitting either owner independently also failed required tests.

## Cross-case and prohibited overlap

No duplicate mechanism, source skeleton, assertion family, or final boundary exists across the three cases. The mechanisms are configuration-driven text rendering, guarded state mutation, and lookup-driven numeric normalization; final boundaries are escaping composition, terminal-transition interference/replay, and fractional/exact mixed-unit thresholds.

- `fixed-point apportionment`: no material overlap. No allocation, quota, remainder, or fixed-point rounding; package arithmetic is unit scaling.
- `release-reader retirement`: no material overlap. No releases, readers, epochs, or retirement lifecycle.
- `trusted proxy CIDR chains`: no material overlap. No networking, proxy trust, addresses, or CIDR parsing.
- `canonical base64url`: no material overlap. No encoding or canonicalization.
- `request policy snapshots`: no material overlap. No request policy or snapshot semantics.
- `TLS client authentication`: no material overlap. No TLS, certificates, clients, or authentication protocol.
- `replacement config reload`: no material overlap. The profile is read by a row-rendering SELECT; there is no replacement/reload lifecycle.
- `source-aware tail checkpoints`: no material overlap. No streams, tails, sources, offsets, or checkpoints.
- `unordered category hierarchy`: no material overlap. No categories, trees, parentage, or unordered hierarchy.
- `tri-state override SQL`: no material overlap. No nullable inheritance/override resolution; job state is an explicit lifecycle enum.
- `composite tenant stock ownership`: no material overlap. Worker ownership is a single job predicate; there is no tenant, stock, or composite ownership key.
- `ticket archive/move accounting`: no material overlap. Export claim/finish has no tickets, archive/move operation, or accounting invariant.

## Author integrity

Raw-byte SHA-256 before and after validation:

| Required bundle file | Before | After |
|---|---|---|
| `th13_sql_delimited_profile/case.json` | `a03aec7dff223c5ebb94d419d2166d931ca4f5af1dcf77f3022abec68f9edbb9` | `a03aec7dff223c5ebb94d419d2166d931ca4f5af1dcf77f3022abec68f9edbb9` |
| `th13_sql_delimited_profile/final_oracle.json` | `edc1589f29144b5171d20fd546297045f8093134b47719275bedbcc3fda1b837` | `edc1589f29144b5171d20fd546297045f8093134b47719275bedbcc3fda1b837` |
| `th13_sql_delimited_profile/oracle.json` | `10c2780ecc6b688657f09d969b2b8465604bdeeb01bc4b69796d2f27ad755eb1` | `10c2780ecc6b688657f09d969b2b8465604bdeeb01bc4b69796d2f27ad755eb1` |
| `th13_sql_export_job_state/case.json` | `f356a264a35ee25caea803d96711d039409db705c1a5e4b6a3a34a6da1a85805` | `f356a264a35ee25caea803d96711d039409db705c1a5e4b6a3a34a6da1a85805` |
| `th13_sql_export_job_state/final_oracle.json` | `b3ac56deada2b902d17839f74371e8d4a992d4f42de3ebb086fc5127cbd72e1f` | `b3ac56deada2b902d17839f74371e8d4a992d4f42de3ebb086fc5127cbd72e1f` |
| `th13_sql_export_job_state/oracle.json` | `6fad52d6336775844256b1fb7e00819f8d596b3506b8aec266e8a895d8a618d6` | `6fad52d6336775844256b1fb7e00819f8d596b3506b8aec266e8a895d8a618d6` |
| `th13_sql_package_units/case.json` | `4f01315e73a75f8a5d3741bdcf62bff72ccb738cb7c832b39646c400fcee2d90` | `4f01315e73a75f8a5d3741bdcf62bff72ccb738cb7c832b39646c400fcee2d90` |
| `th13_sql_package_units/final_oracle.json` | `5a90d5d7f8af187b5d4e7f1d6f1d00c3e97fcb5eef817a54a9b5999fc41d859f` | `5a90d5d7f8af187b5d4e7f1d6f1d00c3e97fcb5eef817a54a9b5999fc41d859f` |
| `th13_sql_package_units/oracle.json` | `1c670711a24ddeb917c7913efb714666427723e9e67d8a3aae9def6166cd5339` | `1c670711a24ddeb917c7913efb714666427723e9e67d8a3aae9def6166cd5339` |

Sorted inventory SHA-256 (before): `5cc457b4f321ee3fd79eef23cb79323522c3b86187efb6650ff0bd2d09f83985`

Sorted inventory SHA-256 (after): `5cc457b4f321ee3fd79eef23cb79323522c3b86187efb6650ff0bd2d09f83985`

The before/after inventories are byte-for-byte identical. The author bundle was not modified.

## Supplemental Ten-File Audit

The original PASS and all 24 test-command records above are preserved. For this supplement, all ten authored files, including `AUTHOR_RECEIPT.md`, were hashed first; the baseline SQL skeleton audit was then performed; and all ten authored files were hashed again before either validation artifact was edited.

### Skeleton normalization

Normalization version: `sql_baseline_skeleton_v1`.

1. Use each candidate's baseline SQL string from `case.json` -> `repo_files`, encoded as UTF-8.
2. Tokenize deterministically and discard `--` line comments, `/* ... */` block comments, and source whitespace.
3. Replace single-quoted strings with `STR`, numeric literals with `NUM`, quoted identifiers with `QID`, and named parameters with `PARAM`.
4. Uppercase ASCII word tokens. Preserve SQL keywords. Preserve the semantic built-ins `COALESCE`, `INSTR`, `REPLACE`, `CHAR`, and `MAX` as `FUNC_<NAME>`. Collapse every other ordinary identifier to `ID`.
5. Preserve `||`, comparison/arithmetic operators, parentheses, commas, periods, and semicolons as individual structural tokens. Join tokens with one ASCII space and no leading/trailing space.
6. A candidate hash is SHA-256 of the normalized UTF-8 bytes. A combined hash is SHA-256 of UTF-8 bytes formed, for candidate paths sorted lexicographically, as repeated `<relative-path>\n<normalized-skeleton>\n` records.

### Explicit case audit

| Case ID | Dimension | Mechanism | Assertion family | Feedback boundary | Final boundary | Final novelty |
|---|---|---|---|---|---|---|
| `th13_sql_delimited_profile` | `config` | Profile-selected field/record separators with delimiter-sensitive quoting. | Exact rendered-string equality for customer and order rows. | Non-default pipe separator and CRLF on plain values. | Selected semicolon inside values requires quoting; embedded double quotes require doubling. | Composes profile selection with delimiter-sensitive quoting and quote escaping, absent from feedback. |
| `th13_sql_export_job_state` | `state` | Guarded UPDATE transitions using current state and worker ownership. | Affected-row counts plus persisted state/owner/result equality and a transition sequence. | Competing claim and non-owner finish rejection. | Canceled/queued source-state rejection plus foreign rejection, one owner completion, and terminal replay rejection. | Adds invalid source states and multi-step terminal immutability composition, absent from feedback. |
| `th13_sql_package_units` | `data` | Lookup-table unit normalization for volume and oversize filtering. | Tolerance-based numeric volume equality plus exact ordered row-set equality. | Inch conversion on all axes and converted lengths around 50 cm. | Factor below one, exact equality versus just below, mixed MM/FT/CM results, and all-axis composition. | Adds fractional factors, exact inclusive threshold, multiple units, and composition absent from feedback. |

### Source skeleton hashes

| Case ID | Candidate | `source_skeleton_sha256` |
|---|---|---|
| `th13_sql_delimited_profile` | `sql/render_customer_row.sql` | `6965c95b913e0c19b200acee58e0f24bbc165494064adc2b3c6bd412cbc96973` |
| `th13_sql_delimited_profile` | `sql/render_order_row.sql` | `6965c95b913e0c19b200acee58e0f24bbc165494064adc2b3c6bd412cbc96973` |
| `th13_sql_export_job_state` | `sql/claim_export_job.sql` | `226e6b5cfac002adf56c71ee63437eb1dd8be07915410bf0718ad8712798c284` |
| `th13_sql_export_job_state` | `sql/finish_export_job.sql` | `226e6b5cfac002adf56c71ee63437eb1dd8be07915410bf0718ad8712798c284` |
| `th13_sql_package_units` | `sql/oversize_queue.sql` | `542d6121f8da7bc5ddbd938f27f0f21d5226e0febb9194919495bb2292ecce4c` |
| `th13_sql_package_units` | `sql/package_volume.sql` | `00a9665967b4275d9de5a18cdad142d91f12b1845fb4cb941cb9a28e365ac3aa` |

| Case ID | `combined_source_skeleton_sha256` |
|---|---|
| `th13_sql_delimited_profile` | `f4fde1ad6acc10e29d0ac33312f6d9618a8c2869306b483ff4a3b31860df774b` |
| `th13_sql_export_job_state` | `278caf0ae1994a97c246c9284e5a5c08984795dcb0518181c23578297d5fc71e` |
| `th13_sql_package_units` | `f4d32d13b5e58c2a37775cc282a247174db180293152b280d49334812d485f70` |

The equal per-candidate hashes within the first two cases are intentional baseline sibling skeletons. The three combined hashes are pairwise distinct.

### Full authored inventory before and after supplement audit

Canonical inventory digest input is the ASCII concatenation `<relative-posix-path>\t<lowercase-sha256>\n`, sorted lexicographically by relative path.

| Authored file | Before SHA-256 | After SHA-256 | Equal |
|---|---|---|---|
| `AUTHOR_RECEIPT.md` | `d133e5ff6b2bc9c31b221303cbf777dd72cc1ceb20d7333d88de9dd2f4dd8fea` | `d133e5ff6b2bc9c31b221303cbf777dd72cc1ceb20d7333d88de9dd2f4dd8fea` | yes |
| `th13_sql_delimited_profile/case.json` | `a03aec7dff223c5ebb94d419d2166d931ca4f5af1dcf77f3022abec68f9edbb9` | `a03aec7dff223c5ebb94d419d2166d931ca4f5af1dcf77f3022abec68f9edbb9` | yes |
| `th13_sql_delimited_profile/final_oracle.json` | `edc1589f29144b5171d20fd546297045f8093134b47719275bedbcc3fda1b837` | `edc1589f29144b5171d20fd546297045f8093134b47719275bedbcc3fda1b837` | yes |
| `th13_sql_delimited_profile/oracle.json` | `10c2780ecc6b688657f09d969b2b8465604bdeeb01bc4b69796d2f27ad755eb1` | `10c2780ecc6b688657f09d969b2b8465604bdeeb01bc4b69796d2f27ad755eb1` | yes |
| `th13_sql_export_job_state/case.json` | `f356a264a35ee25caea803d96711d039409db705c1a5e4b6a3a34a6da1a85805` | `f356a264a35ee25caea803d96711d039409db705c1a5e4b6a3a34a6da1a85805` | yes |
| `th13_sql_export_job_state/final_oracle.json` | `b3ac56deada2b902d17839f74371e8d4a992d4f42de3ebb086fc5127cbd72e1f` | `b3ac56deada2b902d17839f74371e8d4a992d4f42de3ebb086fc5127cbd72e1f` | yes |
| `th13_sql_export_job_state/oracle.json` | `6fad52d6336775844256b1fb7e00819f8d596b3506b8aec266e8a895d8a618d6` | `6fad52d6336775844256b1fb7e00819f8d596b3506b8aec266e8a895d8a618d6` | yes |
| `th13_sql_package_units/case.json` | `4f01315e73a75f8a5d3741bdcf62bff72ccb738cb7c832b39646c400fcee2d90` | `4f01315e73a75f8a5d3741bdcf62bff72ccb738cb7c832b39646c400fcee2d90` | yes |
| `th13_sql_package_units/final_oracle.json` | `5a90d5d7f8af187b5d4e7f1d6f1d00c3e97fcb5eef817a54a9b5999fc41d859f` | `5a90d5d7f8af187b5d4e7f1d6f1d00c3e97fcb5eef817a54a9b5999fc41d859f` | yes |
| `th13_sql_package_units/oracle.json` | `1c670711a24ddeb917c7913efb714666427723e9e67d8a3aae9def6166cd5339` | `1c670711a24ddeb917c7913efb714666427723e9e67d8a3aae9def6166cd5339` | yes |

Ten-file sorted inventory SHA-256 before: `4bc7c9a92802e851f03ab6e984cda577e67848a3ad5f0d575868653c331fde95`

Ten-file sorted inventory SHA-256 after: `4bc7c9a92802e851f03ab6e984cda577e67848a3ad5f0d575868653c331fde95`

Exact equality: **true** (`10/10` file hashes equal; zero mismatches). The author bundle remained unmodified. Verdict remains **PASS**.
