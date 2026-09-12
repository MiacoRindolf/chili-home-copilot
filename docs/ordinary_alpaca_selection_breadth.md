# Ordinary Alpaca PAPER selection breadth — planner [13]

The operator requires concurrent qualified momentum opportunities across symbols.
The ordinary time-share route previously required a flat account and rejected a
second unresolved entry claim. A separate historical episode quota also refused
entries at its ceiling of five unless a rank-one exception applied. The bounded
September 11 audit recorded 224 quota refusals, including 217 whose recorded
open/inflight count was zero. These counts do not establish profitable missed trades.

This slice retires the historical quota for Alpaca PAPER long entries and permits
ordinary primary/repeg entries against a shared account reservation. Qualification
still belongs to the entry path. The quota receipt explicitly reports that it has
not granted financial admission. Short and non-PAPER routes retain their previous
quota behavior.

## Arithmetic and authority

| Binding | Definition / authority |
| --- | --- |
| Held structural risk | Quantity × max(entry basis − stored stop, 0); missing stop charges full entry notional. This models stop risk, not guaranteed realized loss under gaps/slippage. |
| Pending structural risk | Full frozen instruction reservation until terminal; exact owner/CID partial positions covered by that reservation are not counted twice. Ambiguous lineage prevents admission. |
| Account budget | Fresh broker equity × the existing configured aggregate risk fraction (operator canon 3%); no new fraction or symbol-count limit. |
| Per-symbol budget | Existing ordinary cap; transport revalidation preserves the frozen cap and scales it down if equity falls. It does not increase the cap after reservation. |
| Buying power | Full pending instruction notional + candidate notional must fit available broker BP. Pending orders already reflected by Alpaca may be charged again: an explicit conservative bound until reconciliation can prove the exact overlap. |
| Direction | Broker side must explicitly be long, quantity positive, and local ownership agree; negative, short, unknown, manual or unreconciled exposure cannot be silently treated as another owned long. |
| Historical quota retirement | Operator concurrent-opportunity directive; no inference from past win rate, episode count or rank-one status. |

The pure arithmetic sums decimal inputs exactly before producing receipt values.
It authenticates no account or owner: the caller performs those checks under the
existing account reservation lock. Existing account/symbol comparison tolerances
remain in the surrounding reservation seam; they are not new calibrated signals.

The owned-posture check also distinguishes a stored buy order ID from a funded
pending instruction. A malformed historical row with no open broker order does
not freeze the account. A still-open stored buy must have an eligible legacy
reservation or an unresolved funded claim.

## Literal submission boundary

The ordinary caller passes its final certified account snapshot to the transport
fence. The fence reuses the account reservation scan in validation-only mode and
holds that transaction's account lock through its creator-generation CAS. It
cannot acquire, rebind or resurrect a released claim. Revalidation records both
successful and rejected budget decisions; rejection leaves the claim unsubmitted
for the caller's existing proven-no-HTTP release. Missing ordinary revalidation
cannot bypass the new claim's transport requirement.

The broker snapshot is an observation, not an atomic transaction with Alpaca.
No broker HTTP occurs while the database account lock is held. Existing quote and
ownership checks remain; the effect of the added scan on literal-POST quote age
is an explicit outstanding verification item.

## Verification and remaining work

- Initial ledger/quota run: 29 passed, including two committed symbols and racing
  workers sharing the same remaining risk budget.
- Ownership/coverage regression before transport revalidation: 66 passed.
- Transport regression: 44 passed; one deterministic-interleaving test exhausted
  pytest's two database connections because the fixture retained an idle read
  checkout. Returning that checkout fixed the test without changing runtime code.
- Recheck plus full governed pipeline: 3 passed. The pipeline uses real persisted
  owners, reservations, revalidation, release and transport CAS, with a simulated
  broker and healthy fixtures for unrelated financial breakers. It proves second
  symbol submission with a held sibling, and no submission when final BP drops.
- Three adaptive transport/release lifecycle neighbors passed in the 44-pass run.
- Separate prior neighbor failures were reproduced on the exact unchanged base:
  21 adaptive/governed fixtures and one adapter 404 fixture. They are not waived
  or described as green checks.

Evidence and full logs are in the operator's handoff folder:
`project_ws/AgentOps/handoff_astra_0911/ASTRA_SELECTION_BREADTH_*.log` and receipts.

This is a draft implementation, not a deployment. Remaining checks include actual
partial-fill/terminal claim transitions, added-scan quote aging, relevant baseline
fixture contracts, and two independent adversarial reviews plus final verification.
Shared parent/local tick context and validated backside admission are separate
unfinished planner requirements; this accounting slice does not implement them or
claim that every detected wave can be profitably executed.
