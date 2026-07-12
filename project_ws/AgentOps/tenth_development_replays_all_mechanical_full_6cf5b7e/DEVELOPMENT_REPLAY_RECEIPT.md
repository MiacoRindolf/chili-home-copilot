# Tenth-Suite All-Mechanical Development Replay Receipt

Run completed: 2026-07-12

## Classification

This is a **disclosed development replay**. The tenth-suite cases informed the implementation and are no longer
untouched. The generated report retains their historical `blinded_holdout` split label, but this receipt is the
authoritative classification. The result cannot replace the original untouched 68.75/100 score or establish
Fable 5 parity.

## Frozen Inputs And Policy

- Source commit: `6cf5b7e0e7da6840da57dec678f8846796265091`
- Source tree: `5d7f8f60b0699247a3ad2f352f8bd5267f95cb2a`
- Fixture tree: `f3c327cacdfba373dce2b635ad0c2db576abd667`
- Primary model: local `qwen2.5-coder:7b`
- Configured escalation model: local `qwen2.5-coder:14b`
- Repair policy: two primary rounds plus one final escalation round, all unused
- Per-call timeout: 240 seconds
- Premium calls allowed: 0
- Runner/source changes during execution: none

## Result

- Overall: **100/100**, `shadow_ready`
- Sealed-final functional solves: **8/8**
- Correct causal families: **8/8**
- Exact changed-file sets: **8/8**
- Public regressions preserved: **8/8**
- Premium calls: **0**
- Local calls: **24**, all primary diagnostic calls
- Escalation calls: **0**
- Generative repair rounds: **0**
- Recognized mechanical repairs: **8/8**
- Case-time sum: **605.2 seconds (10.1 minutes)**
- Average case time: **75.7 seconds**
- Process wall time: **610.1 seconds**

Relative to the frozen `5905f63` disclosed replay, final solves rose from 2/8 to 8/8, calls fell from 168 to 24
(85.7% lower), and case time fell from 6,334.5 to 605.2 seconds (90.4% lower).

## Architecture Audit

Every case used three local diagnostic stages followed by a CHILI-owned source-shape operator. Every operator was
derived from prompt and source before repair-feedback or final-oracle access, then retained only after public and
feedback validation. Sealed final adjudication ran once in a separate repository. No case invoked a model editor,
repair reviewer, compiler correction, or 14B escalation.

## Artifact Integrity

- `DEVELOPMENT_REPLAY.md` SHA-256: `766238471af43cef5a27053447663ec5634e1175fe54aa610e1c85ead0068444`
- `development_replay.json` SHA-256: `c20eb12cbd50b8f832f4fe39ff56d6108033cdc7d0d72ce6276464111d6260cd`

## Interpretation

This proves repeatable local-only regression coverage for eight disclosed multi-file mechanisms and demonstrates
that CHILI is not a wrapper around Fable 5 or another premium model. It does not prove unseen transfer. The next
authoritative gate must be independently authored after this source freeze and must contain mechanism variants and
unknown families that cannot be answered by these fixtures.
