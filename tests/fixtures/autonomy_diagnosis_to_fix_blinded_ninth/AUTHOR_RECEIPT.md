# Independent Holdout Author Receipt

## Isolation

The authoring process was confined to D:\dev\chili-holdout-author-ninth-20260711. The prohibited workspace and sources named
in the task were not accessed, no internet access was used, and no CHILI benchmark or model
was run.

## Composition

- Cases: 8
- Languages: Python 2, TypeScript 2, Dart 2, SQL through Python sqlite 2
- Multi-file incidents: 8
- Public case files: 8
- Repair-feedback oracle files: 8
- Final adjudication oracle files: 8

The suite covers these high-level mechanisms without embedding repairs or sealed
assertions in public artifacts:

- explicit configuration values crossing parser and precedence boundaries
- matrix context identity and repeated-result attribution
- nested workspace state ownership across creation, restore, and reset
- incremental UTF-8 decoding and stream completion behavior
- partial-update presence semantics across decoding and storage
- timestamp ties, sequence ordering, and cursor progression across batches
- relational history retention across identity erasure and reporting
- SQLite affinity and exact external-identifier matching

## Validation

Each case was reconstructed in three fresh directories using only its public case JSON.
The feedback or final files were then added from the corresponding sealed JSON for their
own runs. All public baselines passed; every feedback run failed on the defective baseline;
every independently reconstructed final run also failed on the defective baseline.

Structural validation additionally checked case-insensitive path separation, test
discoverability, candidate-file membership, partition file limits, manifest linkage,
language balance, and prohibited source behaviors. SHA-256 values for the manifest and all
24 case/oracle/final JSON files are recorded in AUTHOR_RECEIPT.json.
