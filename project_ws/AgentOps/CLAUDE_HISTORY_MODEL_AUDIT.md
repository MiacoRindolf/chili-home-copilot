# Claude History Model Audit

Date: 2026-07-11

## Scope

Read-only inventory of the local Claude project archive. The audit records aggregate file counts, provider-native model labels, and privacy-minimized task families. It does not copy full conversations, credentials, tool payloads, attachments, or Fable answers into the repository.

The first pass saw only four directly visible transcript files under the user-profile path. That path is a junction. The complete audit follows the junction target at `D:\dev\.claude-projects`; the narrow first-pass result is not the archive total.

## Archive Inventory

- JSONL transcript files: **5,329**
- Archive bytes: **2,737,416,086** (about 2.55 GiB)
- Top-level transcript files: **982**
- Subagent transcript files: **4,347**

Provider-native assistant-event lines:

| Native model label | Transcript files | Assistant-event lines |
| --- | ---: | ---: |
| `claude-fable-5` | 366 | 30,546 |
| `claude-sonnet-5` | 38 | 4,106 |
| `claude-opus-4-8` | 3,523 | 170,631 |
| `claude-opus-4-7` | 54 | 12,349 |
| `claude-haiku-4-5-20251001` | 794 | 43,697 |

The Fable set contains **19 top-level** transcript files and **347 subagent** transcript files. Some top-level sessions contain more than one native model label, so session membership alone is not candidate identity proof.

## Trading Task Mining

The privacy-minimized pass paired a user event with its direct assistant child and retained it only when that child carried native model label `claude-fable-5`. Continuation summaries, tool results, task notifications, system reminders, and messages shorter than 60 normalized characters were excluded.

- Top-level sessions with meaningful Fable-directed prompts: **9**
- Meaningful Fable-directed prompts: **165**
- Meaningful trading prompts: **160**

The following family counts overlap because a real incident can exercise several mechanisms:

| Diagnostic family | Prompt hits |
| --- | ---: |
| Strategy/observed-behavior gap | 73 |
| Counterfactual or replay analysis | 35 |
| Safety, risk, halt, or microstructure | 28 |
| Data integrity or evidence coverage | 26 |
| Autonomy/reasoning quality | 24 |
| Live state or broker reconciliation | 18 |
| Concurrency, queue, or lifecycle state | 14 |
| Runtime, deployment, or service drift | 10 |

Representative task shapes include:

1. Reconcile broker positions, pending-entry state, duplicate opens, and local trade rows without creating new live risk.
2. Explain why live behavior diverges from the intended short-horizon strategy and determine whether code, state, data, or the strategy contract owns the gap.
3. Compare replay and live outcomes while detecting harness leakage, changed inputs, clock drift, and counterfactual overclaiming.
4. Diagnose missing fills or outcomes despite source tick coverage and trace producer-to-consumer starvation.
5. Review halt/resume, spread, BBO, sizing, stop, and re-entry behavior under explicit safety boundaries.
6. Distinguish stale runtime images or disabled workers from source-code defects.
7. Adversarially review race safety, runaway behavior, queue pressure, rollback handling, and state lifecycle.
8. Turn multi-source evidence into a minimal fix, focused validation, deployment decision, and post-change proof.

## Identity And Comparison Status

- Provider-native Fable 5 history: **available**.
- Provider-native model label target: **`claude-fable-5`**, not Opus 4.8.
- Fresh same-task CHILI versus Fable 5 head-to-head count: **0**.
- Blinded promotion-grade task count: **0**.
- Claude web-history UI inspection: optional and still pending sign-in; it is no longer required to establish that native Fable history exists locally.

Historical Fable tasks are valuable development replays, but they are not unseen holdouts. Some resulting fixes and task mechanics already exist in CHILI's source, tests, reports, or deterministic contract families. They must remain labeled `historical_development_replay` unless an independent evaluator freezes a new task before CHILI development sees its oracle.

Frontier candidate evidence now requires more than a model name somewhere in a transcript. The original response is SHA-256 bound to the transcript, and the exact matching assistant event must carry the expected provider-native model label. A stray Fable event cannot attest an Opus response, and recorder-declared model labels are not identity evidence.

## Next Evaluation Gate

1. Freeze at least 30 independently authored incident bundles with source revision, bounded logs/data, expected invariants, safety boundary, and sealed oracle.
2. Keep prompts/evidence visible while withholding Fable answers, root-cause labels, hidden tests, and post-fix commits from CHILI.
3. Run CHILI local-only and record evidence selection, hypothesis revisions, diagnosis, patch, validation, rollback behavior, latency, and premium calls.
4. Run authenticated Fable 5 independently on the same frozen bundles, or label archived Fable results only as historical reference.
5. Use blind human adjudication and report wins, ties, losses, unsafe actions, unsupported claims, and confidence intervals.

Until that gate is complete, the correct claim is: CHILI has a serious premium-independent diagnostic architecture and a real Fable-derived development corpus, but Fable 5 parity or superiority on unseen complex diagnosis is not yet proven.
