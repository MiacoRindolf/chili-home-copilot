# Fourteenth Node ESM Author Receipt

## Scope

- Authored exactly three diagnosis-to-fix bundles under
  tests/fixtures/autonomy_diagnosis_to_fix_blinded_fourteenth.
- Case metadata language: typescript.
- Repository sources and tests: Node ESM .mjs.
- Test runner: built-in node:test.
- Validation runtime: Node.js v24.15.0.
- Receipt path follows the corrected ownership instruction. No receipt was
  created under project_ws/AgentOps.
- No fixture manifest, application source, benchmark source, existing test, or
  source capability file was edited.

The thirteenth case/oracle/final-oracle schemas and the benchmark loader's
external-final partition checks were studied before authoring. The current
contract repair families in
app/services/project_autonomy/diagnostic_reasoning.py and every thirteenth case
mechanism were reviewed for novelty.

## Cases

### th14_node_http_preconditions

- Expected dimension: dependency (HTTP wire-protocol compatibility).
- Expected owners:
  src/entity-tag-list.mjs, src/request-preconditions.mjs.
- Distractor: src/catalog-response.mjs.
- Mechanism: quote-aware entity-tag list parsing composed with the asymmetric
  weak comparison required by read revalidation and strong comparison required
  by write preconditions.
- Novelty: this is not Vary/cacheability handling, configured media-type
  normalization, repeated query rendering, TLS policy, key rotation, or any
  thirteenth HTTP/configuration mechanism.
- Causality: disclosed tests independently fail the list parser and comparison
  policy. The sealed catalog-response tests compose a comma-bearing opaque tag
  with weak/strong validators, so repairing either owner alone remains
  insufficient.

### th14_node_partition_commits

- Expected dimension: state (checkpoint ownership and isolation).
- Expected owners:
  src/offset-tracker.mjs, src/batch-consumer.mjs.
- Distractor: src/commit-report.mjs.
- Mechanism: a per-partition contiguous commit watermark retains completed
  offsets behind gaps, while the concurrent consumer attributes each
  completion to the record's actual partition.
- Novelty: this is not job-attempt fencing, a monotonic materialized-head
  winner, source-provenance checkpoint reset, vector-clock convergence, or a
  thirteenth lifecycle/state transition.
- Causality: one disclosed test isolates gap-aware advancement and another uses
  a tracker spy to isolate partition forwarding. The sealed mixed batch
  requires both behaviors before its restart report is safe.

### th14_node_esm_plugin_loading

- Expected dimension: dependency (Node package/ESM compatibility).
- Expected owners:
  src/package-exports.mjs, src/plugin-loader.mjs.
- Distractor: src/plugin-registry.mjs.
- Mechanism: recursive conditional-exports selection honors active Node/import
  conditions and default, while module paths are converted to encoded file
  URLs so literal URL-significant package-directory characters remain path
  data.
- Novelty: this is not dependency-report schema adaptation, factory parameter
  binding, provider cancellation, TLS configuration, version selection, or any
  thirteenth dependency mechanism.
- Causality: disclosed tests independently exercise condition selection and a
  package cache path containing #. The sealed registry activation combines a
  nested condition object with that path, requiring both owners.

## Validation

All commands operated on fresh temporary repositories decoded from the JSON
payloads. Temporary directories were removed after each validation run.

JSON and loader-shape command:

    Get-Content -Raw <artifact> | ConvertFrom-Json

This was combined with assertions for exact top-level keys, matching case IDs,
language=typescript, test_runner=node_test, max_files=2, exactly two expected
owners, at least one distractor, candidate/source membership, oracle paths under
tests/, seeded/feedback/final path disjointness, independently authored
feedback/final payloads, discoverable tests, and exactly three files in each
owned JSON path group.

Result:

    th14_node_esm_plugin_loading LOADER_SHAPE_OK feedback_files=3 final_files=6
    th14_node_http_preconditions LOADER_SHAPE_OK feedback_files=1 final_files=1
    th14_node_partition_commits LOADER_SHAPE_OK feedback_files=1 final_files=1
    ASCII_OK=9

Syntax command for every decoded .mjs file:

    node --check <decoded-file.mjs>

Baseline commands in separate fresh repositories:

    node --test --test-reporter=spec tests/public.test.mjs
    node --test --test-reporter=spec tests/public.test.mjs tests/feedback.test.mjs
    node --test --test-reporter=spec tests/public.test.mjs tests/final.test.mjs

Results:

| Case | Syntax | Public baseline | Feedback baseline | Fresh final baseline |
| --- | --- | --- | --- | --- |
| th14_node_http_preconditions | 6 checked, all valid | exit 0, 3 pass, 0 fail | exit 1, 3 pass, 3 fail | exit 1, 4 pass, 1 fail |
| th14_node_partition_commits | 6 checked, all valid | exit 0, 2 pass, 0 fail | exit 1, 2 pass, 2 fail | exit 1, 2 pass, 1 fail |
| th14_node_esm_plugin_loading | 12 checked, all valid | exit 0, 2 pass, 0 fail | exit 1, 2 pass, 2 fail | exit 1, 2 pass, 1 fail |

The CHILI benchmark was not run. No local coding model or other coding model
was invoked. No solution patch is present in any fixture.

## Artifact Hashes

SHA-256 hashes cover the nine authored JSON payloads. The receipt excludes its
own hash because embedding a file's digest in that same file is
self-referential.

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| cases/th14_node_esm_plugin_loading.json | 4644 | bad66c237cf3eb929c1b597d607370aacbe1cf96ff40f2fd9f4f4aa9fa800a80 |
| cases/th14_node_http_preconditions.json | 4241 | 5a0bd7e893074609f3b07549c91be5f3aecdd34a4eed9c5f8989fc7474b464e3 |
| cases/th14_node_partition_commits.json | 4089 | c66bb8c99531aae44bb6e559ace01b31345e32d7d36173fd5c4ffe5502be5e56 |
| final_oracles/th14_node_esm_plugin_loading.json | 1594 | bc9c25b33113e7bf0cd295fc273dcb54aeda38617258e58c55f1825940d41bdc |
| final_oracles/th14_node_http_preconditions.json | 1079 | 3982592d1d5de00715a05247c1cb4db86f2e0ef293b37d33c346ef0454cc958e |
| final_oracles/th14_node_partition_commits.json | 1784 | 7d2c2da5ce7cf638bf5c25e840b9157c9c191f219b7efde2c5ca6d625aa70cc5 |
| oracles/th14_node_esm_plugin_loading.json | 1309 | 1562ba81d882c827abb82843f9b0643d2d6d077cf51e584e8542c0505a647250 |
| oracles/th14_node_http_preconditions.json | 1270 | 575f4170206f54794275c49f13f54610dd4c2e6fcfe7d24c36a65fc1104c1033 |
| oracles/th14_node_partition_commits.json | 1205 | ec2bd3d87c535803fd0742c78838046b4a0502e3e06514cd1546d614e5080d4d |
