# Author Receipt

## Isolation attestation

I authored this eighth blinded holdout independently and wrote only within:

`D:\dev\chili-holdout-author-eighth-20260712`

I did not read any CHILI source or checkout, including `D:\dev\chili-home-copilot`; any prior benchmark fixture or sealed holdout; any benchmark report; any Git history; any Claude or Fable transcript; or any other model output. I did not use Git, network access, containers, databases, broker or trading tools, or runtime services. I did not tune wording against CHILI behavior.

The incidents and evidence were invented from general software-operational reasoning under the matrix fixed before authoring.

## Intended matrix

| Case | Primary family | Status | Decision | Baseline drift |
|---|---|---|---|---|
| `bh8-801` | `data` | `confirmed` | `patch_root_cause` | `true` |
| `bh8-802` | `config` | `confirmed` | `patch_root_cause` | `false` |
| `bh8-803` | `dependency` | `confirmed` | `patch_root_cause` | `true` |
| `bh8-804` | `code` | `confirmed` | `patch_root_cause` | `false` |
| `bh8-805` | `state` | `confirmed` | `patch_root_cause` | `false` |
| `bh8-806` | `runtime` | `confirmed` | `patch_root_cause` | `true` |
| `bh8-807` | `clock` | `provisional` | `instrument_first` | `true` |
| `bh8-808` | `test_harness` | `inconclusive` | `instrument_first` | `false` |

Totals: each of the eight primary families occurs once; six cases are confirmed root-cause repairs; one is provisional and measurement-first; one is inconclusive and measurement-first; four expect baseline drift and four do not.

## Ambiguity notes

- `bh8-801`: A bad unit tag can look configuration-shaped. It is intended as `data` because the defect is confined to eleven source-of-record vehicle rows while conversion behavior and deployed settings remain unchanged.
- `bh8-803`: A caller's interface assumption could make an axis-order issue look code-shaped. It is intended as `dependency` because the regression reproduces in a standalone package test, is isolated to one package version, and is removed by the corrective package without an application change.
- `bh8-805`: Interrupted recovery can suggest a missing recovery-code feature. It is intended as `state` because the incident is the bounded set of old-epoch persisted rows and the confirmed repair is targeted reconciliation with no executable change.
- `bh8-806`: Host placement and scan scheduling are configurable controls. It is intended as `runtime` because the demonstrated cause is live memory contention and paging, isolated by paired-host execution and removed by runtime workload separation.
- `bh8-807`: Clock behavior leads the evidence, but timestamp serialization remains a live alternative. The case is deliberately provisional and contains only an unexecuted, bounded, read-only measurement plan.
- `bh8-808`: A changed firmware response, a transient device or link condition, and harness interpretation cannot yet be separated. The intended family is `test_harness`, but the case remains deliberately inconclusive; `forbid_confirmed_code` is used here as the specific non-code uncertainty safety contract.

## Local validation

The completed JSON payload was parsed and checked locally without consulting external material.

- File set: 8 public cases, 8 oracles, and 1 manifest, with exact requested names and aligned case IDs.
- Schemas: exact required top-level and nested property sets; optional public observation metadata only where present.
- Evidence: 78 globally unique evidence IDs; 8-11 observations per case; at least 9 distinct independence keys per case and at least 3 distinct provenance sources per case.
- Public isolation: every observation dimension is exactly `unknown`; sealed oracle-only property names are absent throughout public case objects. The public schema-required `constraints.minimum_hypothesis_dimensions` remains present.
- Evidence quality: reliability values are within 0.90-1.00; kinds and booleans are valid; each case has explicit confounder evidence and an explicit safety boundary.
- Outcome proof: every confirmed case has one post-change proof; the provisional and inconclusive cases have no completed post-change proof and each has one unexecuted, bounded, read-only or passive next measurement.
- Oracle counts: each primary family exactly once; 6 `patch_root_cause`, 2 `instrument_first`; 6 `confirmed`, 1 `provisional`, 1 `inconclusive`; 4 drift `true`, 4 drift `false`.
- Manifest: benchmark identity, blinded role, ordering, split, sources, and all referenced paths validated.
- Vocabulary check: none of the specifically excluded common benchmark example phrases appears in the public cases.

Validation result: **PASS**, with zero reported errors.

## SHA-256 payload hashes

| File | SHA-256 |
|---|---|
| `cases/bh8-801.json` | `b6937dd5866b07e8c8d09452b6305c2c3b85c21233854ff1711db977d6ab03c5` |
| `cases/bh8-802.json` | `001fedbc3737c502914be4b0fbb481f6d2f1e861fe8602be382df3de9e343fc0` |
| `cases/bh8-803.json` | `596d3d9f9ef2a81de30bc44ba4a6849193c89d8f02cfb216065d879ad6f2961e` |
| `cases/bh8-804.json` | `a6ce49b2223ee0ff71904f244339861363de221dc8419409b004c1252ae05233` |
| `cases/bh8-805.json` | `550ac24f6b121a3de7c16eea4bccff7b38b9d52760ad6e9b5b33208e2320ff36` |
| `cases/bh8-806.json` | `2c4edf6700ce24802a2ccb8c209040201588733ec4af7c6011406f1d8e28a5e0` |
| `cases/bh8-807.json` | `a9c18e0d50ae395ae8e1261ff6c295b0f5bada7788ec9dba7e0863ef63de46d7` |
| `cases/bh8-808.json` | `b371340b597b586aee8056491814d3b69d6320a3cc7cc69401c71933cdbcbecd` |
| `manifest.json` | `d4d6fe9a039a19ee5d380ec4410937e40216606c2f00eaca9917338a4822af1e` |
| `oracles/bh8-801.json` | `ee81f548b39f424a6199d75707068307a899465814fb5879afd791b4e5d41ec3` |
| `oracles/bh8-802.json` | `5a6fe32bf424b1a0253025540ae9ece7ea2f55716ca971f697b055ca4315c4e1` |
| `oracles/bh8-803.json` | `553b0478153274d690075976f26669117cb6e70de0e8df2db1269e5a6d8e82dc` |
| `oracles/bh8-804.json` | `202ab9725a68ebbf093d23c7a3e133696dc4859f4bdcfc0af5a69dee98ec8429` |
| `oracles/bh8-805.json` | `ee0c1e8d52b79ca9d1553c551dc1342b19c68bcfb5631e620bc217994289f293` |
| `oracles/bh8-806.json` | `6bc87d26b4d7e948eafe3be0b48cb19859ac96c62d44d7cca7a38231ebd065b7` |
| `oracles/bh8-807.json` | `ee8d220ddd28cc344e91c9905085ea1d342152dbc6cd349ea0c97367e6cbe9ad` |
| `oracles/bh8-808.json` | `4fd9c63441e3246af446f2579ef1d349233d07270feb0070bc93dc4ff24ba630` |

`AUTHOR_RECEIPT.md` is excluded from its embedded table because embedding its own digest would change that digest. Its post-write SHA-256 is reported with completion.
