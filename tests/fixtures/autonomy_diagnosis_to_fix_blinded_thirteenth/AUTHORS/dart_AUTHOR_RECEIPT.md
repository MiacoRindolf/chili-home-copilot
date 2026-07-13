# Author Receipt

Date: 2026-07-12

Authoring root: `D:\dev\chili-thirteenth-author-dart`

SDK used:

```text
Dart SDK version: 3.11.1 (stable) (Tue Feb 24 00:03:07 2026 -0800) on "windows_x64"
```

## Delivered cases

1. `th13_dart_portable_exports` - expected dimension `code`
2. `th13_dart_offset_schedule` - expected dimension `clock`
3. `th13_dart_dependency_report` - expected dimension `dependency`

Each case has exactly two candidate source paths, `max_files: 2`, exactly two expected causal source owners, one public test script, one read-only feedback test script, and one sealed final test script.

## Packed baseline runs

The commands below were run in fresh repositories decoded from the delivered JSON files. The temporary decoding directory was removed after verification.

### th13_dart_portable_exports

Working directory: `D:\dev\chili-thirteenth-author-dart\_packed_validation\th13_dart_portable_exports`

```text
> dart tests/public_test.dart
exit 0
public tests passed

> dart tests/feedback_test.dart
exit 1
Bad state: device names must be made portable

> dart tests/final_test.dart
exit 1
Bad state: a device basename remains reserved when it has an extension
```

### th13_dart_offset_schedule

Working directory: `D:\dev\chili-thirteenth-author-dart\_packed_validation\th13_dart_offset_schedule`

```text
> dart tests/public_test.dart
exit 0
public tests passed

> dart tests/feedback_test.dart
exit 1
Bad state: the new offset starts at the transition instant

> dart tests/final_test.dart
exit 1
Bad state: a wall-clock target created by the jump resolves to the transition instant
```

### th13_dart_dependency_report

Working directory: `D:\dev\chili-thirteenth-author-dart\_packed_validation\th13_dart_dependency_report`

```text
> dart tests/public_test.dart
exit 0
public tests passed

> dart tests/feedback_test.dart
exit 1
Bad state: reads artifacts from upgraded reports

> dart tests/final_test.dart
exit 1
Bad state: keeps all upgraded dependency records
```

## Coordinated fix verification

Disposable reference-fix copies were used only for author verification.

- For every case, editing only the first expected source owner left the feedback test failing on behavior owned by the second file.
- For every case, editing only the second expected source owner left the feedback test failing on behavior owned by the first file.
- With both expected source owners repaired, these commands exited `0` for every case: `dart tests/public_test.dart`, `dart tests/feedback_test.dart`, and `dart tests/final_test.dart`.
- Successful outputs were respectively `public tests passed`, `feedback tests passed`, and `final tests passed`.

## Final-test distinction

- `th13_dart_portable_exports`: final coverage combines a reserved basename with an extension and a collision created jointly by normalization and case folding.
- `th13_dart_offset_schedule`: final coverage schedules a wall-clock target whose resolved instant is exactly the offset transition boundary.
- `th13_dart_dependency_report`: final coverage composes the upgraded nested report shape with grouped `AND`/`OR` evaluation and runtime-scope filtering.

## Static checks

```text
dart format --output=none --set-exit-if-changed lib tests
```

Result for each decoded case: exit `0`, five files checked, zero files changed.

All nine JSON artifacts decoded successfully, matched their exact required top-level schemas, contained only the required repository/test paths, and were verified as ASCII. No package dependencies, network access, services, credentials, or external model invocations were used.
