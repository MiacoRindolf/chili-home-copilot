# Fable 5-Class Diagnosis-to-Fix Seventeenth Result Receipt

## Verdict

CHILI is **not ready to replace Fable 5 for unseen complex diagnosis-to-fix work**.

The frozen local-only run completed all eight sealed cases and recorded **27.5/100**, `needs_improvement`,
with **0/8 final repairs**, **2/8 exact owner sets**, and zero premium calls. After the result was sealed, review
found that the fixture used mechanism phrases in plural `expected_dimensions`, while the v6 scorer requires one
canonical singular `expected_dimension`. The diagnosis component is therefore not interpretable. Even awarding
all 15 diagnosis points to every case would raise the aggregate only to **42.5/100**, so the negative readiness
verdict does not depend on that malformed component.

## Frozen Execution

- Policy commit: `5580882cff0224a02aca31b433f1a7997b1d12cb`
- Frozen implementation: `19de897466158027d65742f06d5e562e4597d2f1`
- Final fixture commit: `a46fb494869a2fda53cf18dc000736e4a4e96019`
- Untouched result commit: `3f47d396`
- Primary editor: `qwen2.5-coder:7b`
- Causal reasoner: `qwen3:8b`
- Escalation: disabled
- Repair rounds: two maximum
- Per-call timeout: 150 seconds
- Per-case model-wall budget: 480 seconds
- Premium calls: zero
- Wall time: 3,906 seconds

The fresh contestant clone was clean, contained neither the isolated author commit nor output files, and matched
the frozen runner and diagnostic-reasoning hashes before inference. The policy and fixture commits were verified
ancestors. The model-independent fixture preflight passed 8/8 cases.

## Result Integrity

- Sealed final adjudications: **8/8**
- Post-final model calls: **0/8 cases**
- Checkpoint case commits: **8/8**
- Checkpoint restored cases: **0**
- Checkpoint removed only after atomic report/result output: yes
- Run-policy digest reverified after all cases: yes
- Source drift from the implementation freeze: none
- Premium calls: **0**
- Unsafe runtime, broker, database, Docker, or network actions: none

## Capability Evidence

- Functional sealed-final solves: **0/8**
- Exact owner sets: **2/8**
- Mechanically reported diagnosis matches: **0/8**, excluded from interpretation because of the oracle schema flaw
- Live-reasoning-qualified cases: **6/8**
- Local model calls: **57**
- Local call errors/timeouts: **14**
- Average case duration: **487.5 seconds**
- Recorded score: **27.5/100**
- Diagnosis-credit upper bound: **42.5/100**

The two SQL cases had no qualified live reasoning because repeated local call timeouts consumed their case
budgets. Other cases exposed generic edit-bundle failures: stale SEARCH anchors, incomplete or duplicate path
groups, invalid repair plans, and rollback after no validated progress. These are development targets after the
untouched result, not reasons to discard it.

## Oracle Defect

The executable fixture validator checked test safety, baseline behavior, sealing, and final-oracle separation but
did not validate the diagnosis oracle schema. Every seventeenth repair oracle supplied plural mechanism labels
instead of the scorer's singular taxonomy value. The next implementation must reject this shape before model
access and a fresh independently authored post-fix holdout must supply a canonical diagnosis dimension.

## Claim Boundary

This run is decisive negative functional evidence, but it is not a complete Fable 5 comparison. No authenticated
same-task Fable 5 output was run. CHILI remains a premium-independent, safety-gated autonomous coding system;
Fable 5 parity or superiority is not supported.
