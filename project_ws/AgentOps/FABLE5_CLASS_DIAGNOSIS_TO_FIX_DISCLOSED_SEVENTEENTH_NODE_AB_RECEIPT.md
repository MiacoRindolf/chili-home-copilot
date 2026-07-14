# Disclosed Seventeenth Node Shared-State A/B Receipt

## Scope

This is a development replay of the already disclosed `node_coalesced_abort_poison` case. It is not a blinded
holdout and cannot support a Fable 5 parity or superiority claim.

The case requires one shared upstream operation per key while preserving two ownership boundaries:

- each subscriber cancellation affects only that subscriber; the upstream aborts only after all subscribers leave;
- failed or cancelled work is not retained as a result, while the first successful value remains cached.

## Implementation

- `56203b45`: added a source-shape-gated JavaScript repair for subscriber-scoped cancellation and success-only
  result retention, with exact-case, alternate-name transfer, and unrelated-owner fail-closed tests.
- `3c69c2c4`: fixed the benchmark honesty gate so a successful model call is not called Fable 5-class reasoning
  unless the model itself returns a confirmed causal conclusion with sufficient evidence and the retained family.
- `fa0cd8ff`: made repository evidence retrieval contract-aware, ranked behavioral owner lines ahead of constructor
  and utility-name matches, deduplicated overlapping snippets, and required full symptom closure in council prompts.
- `fe89a111`: added bounded static contract/source mismatch context from the prompt and candidate files only. These
  records remain inferred context, not fabricated intervention evidence.

No premium model, Fable 5 output, feedback test, or sealed-final test was used to construct the initial diagnosis
packet. Feedback remained available only to the bounded development repair phase; the final oracle was opened only
after the model-call ledger was frozen.

## A/B Evidence

Before behavior-ranked source retrieval, the local `qwen3:8b` investigator selected a key-normalization distractor
and retained `data` or `code` as its initial family. The candidate source packet had exhausted its early per-file
quota on constructors and utility names, omitting the direct abort and unresolved-promise cache lines.

After `fa0cd8ff`, the raw investigator moved to the correct `state` family and selected cache retention, but did not
close the subscriber-abort half of the incident. The preserved intermediate artifacts are:

- `FABLE5_CLASS_DIAGNOSIS_TO_FIX_DISCLOSED_SEVENTEENTH_NODE_EVIDENCE_RANKING.md`
- `fable5_class_diagnosis_to_fix_disclosed_seventeenth_node_evidence_ranking.json`

After `fe89a111`, the raw investigator produced both relevant mechanisms:

- `state`: unresolved shared promises retained by the result cache;
- `code`: abort propagation failing to isolate subscribers.

It still selected only the cache hypothesis as its leader and kept the conclusion `inconclusive`. Therefore the
strict live-reasoning qualification remained **0/1**, `fable5_class_reasoning_claim_eligible=false`, and the verdict
remained `needs_improvement`.

The complete CHILI system nevertheless scored **100/100** because the generic symbolic contract operator selected
exactly `src/flightPool.js` and `src/resourceClient.js`; public, repair-feedback, and fresh isolated sealed-final
tests all passed. The final replay used **2/2 successful local calls**, no model errors, **110.9 seconds**, and
**zero premium calls**. The atomic checkpoint was removed only after both outputs were written.

Final artifacts:

- `FABLE5_CLASS_DIAGNOSIS_TO_FIX_DISCLOSED_SEVENTEENTH_NODE_SUBSCRIBER_RECOVERY.md`
- `fable5_class_diagnosis_to_fix_disclosed_seventeenth_node_subscriber_recovery.json`

## Validation

- Shared-work focused wording, transfer, fail-closed, exact feedback, and isolated-final tests: passed.
- Diagnostic reasoning suite: **135 passed** with two pre-existing warnings.
- Full affected reasoning and diagnosis-to-fix suite: **314 passed** with two pre-existing warnings.
- `py_compile` and `git diff --check`: passed.

## Interpretation

This proves a useful premium-independent hybrid capability: bounded source evidence, a small local reasoner, strict
causal accounting, symbolic contract repair, exact owner selection, rollback protection, and sealed validation can
solve this disclosed multi-owner concurrency/cache incident end to end.

It does not prove that the local model independently reasons at Fable 5 level. The model found the correct family
and both component mechanisms only after generic evidence improvements, then still failed to synthesize the full
boundary as one leading causal hypothesis. The next authoritative evidence must be a fresh task frozen after
`fe89a111`; this disclosed case must not receive further score-driven tuning.
