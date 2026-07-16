# Ross Source-Before Certification Queue - 2026-07-03

Purpose: unblock Replay v3 source/PnL certification for the Ross incident set without relying on transcript-only evidence. This is a proof queue, not a trading-behavior change.

Current evidence commands:

- `python scripts\audit_ross_visual_evidence.py --root project_ws\AgentOps\ross_video_evidence --review-manifest project_ws\AgentOps\ross_video_evidence\review_manifest.json --strict-all`
- `python scripts\run_counterfactual_replay_v3.py --date 2026-07-01 --symbols JEM CANF DXF TC LHAI --max-ticks-per-symbol 500 --max-trades-per-symbol 1 --joined-certification-queue-text`
- `python scripts\run_counterfactual_replay_v3.py --date 2026-07-01 --symbols JEM CANF DXF TC LHAI --max-ticks-per-symbol 500 --max-trades-per-symbol 1 --joined-certification-queue-only`
- JEM follow-up: `python scripts\run_counterfactual_replay_v3.py --date 2026-07-01 --symbols JEM --max-ticks-per-symbol 1000 --max-trades-per-symbol 3 --joined-certification-queue-only`

Current boundary:

- Visual evidence folders ready: 5 of 5
- Total frames available: 3,721
- Review manifest rows: 8 valid, 0 invalid
- Ross trade-outcome-certifying rows: 1
- Trade/no-trade-certifying rows: 0
- Source-before-opportunity-certifying rows: 0
- Replay label-ready symbols: 0
- PnL/minmax label-ready: false

Do not mark any row label-ready unless the reviewed frames show chart/trade context before the Replay opportunity timestamp. Scanner-only rows and post-opportunity chart rows stay noncertifying.

## Queue

| Symbol | Replay status | Opportunity timestamp | Current visual evidence | Required action | Dry-run marker |
|---|---|---:|---|---|---|
| CANF | `cert_source_after_opportunity` | `2026-07-01T13:25:55.321506+00:00` | `HTgOZI8GOV0` has post-opportunity chart review and one outcome-certifying row, but first certifiable source is `2026-07-01T21:53:58.933022+00:00`, 30,483.612 seconds after the opportunity. | Find or review chart/tape frames before `13:25:55.321506Z`; later source cannot label this opportunity. | `python scripts\mark_ross_trade_event.py CANF --action review_certified --ts 2026-07-01T13:25:55.321506+00:00 --visual-evidence-id EVIDENCE_ID --note "Reviewed chart-context frames before replay opportunity" --dry-run --visual-review-manifest "project_ws/AgentOps/ross_video_evidence/review_manifest.json"` |
| DXF | `source_not_certified` | `2026-07-01T09:12:28.948054+00:00` | Reviewed `HTgOZI8GOV0` frame evidence is noncertifying. Existing row is chart/no-trade context but does not certify trade/no-trade or source-before. | Find different pre-opportunity chart/trade or chart/no-trade frames; scanner/transcript mention is not enough. | `python scripts\mark_ross_trade_event.py DXF --action review_certified --ts 2026-07-01T09:12:28.948054+00:00 --visual-evidence-id EVIDENCE_ID --note "Reviewed chart-context frames before replay opportunity" --dry-run --visual-review-manifest "project_ws/AgentOps/ross_video_evidence/review_manifest.json"` |
| JEM | `source_not_certified` | `2026-07-01T15:22:57.923222+00:00` at 1000-tick cap; 500-tick cap has no candidate | Reviewed evidence ids `HTgOZI8GOV0`, `iZ-mXl0ga3U`, and `PnuyhYGHuUY` remain noncertifying. The 1000-tick Replay candidate is `ross_breakout_starter_tick` at entry `$6.67`, stop `$6.53868`, spread `75.24 bps`; it is not the later user-screenshot `$3.86` VWAP-reclaim zone. | Find source-before active JEM chart/trade frames for `15:22:57.923222Z`, or keep noncertifying. A heavier/uncapped replay is still useful for full-day stability, but the current higher-cap candidate is not label-ready without frame certification. | `python scripts\mark_ross_trade_event.py JEM --action review_certified --ts 2026-07-01T15:22:57.923222+00:00 --visual-evidence-id EVIDENCE_ID --note "Reviewed chart-context frames before replay opportunity" --dry-run --visual-review-manifest "project_ws/AgentOps/ross_video_evidence/review_manifest.json"` |
| LHAI | `source_not_certified` | `2026-07-01T13:25:55.214584+00:00` | `HTgOZI8GOV0` scanner/no-trade row is noncertifying; it lacks chart/VWAP/HOD/pullback/tape context. | Find pre-opportunity chart/trade or chart/no-trade frames. If evidence only says "too cheap" in scanner context, keep noncertifying. | `python scripts\mark_ross_trade_event.py LHAI --action review_certified --ts 2026-07-01T13:25:55.214584+00:00 --visual-evidence-id EVIDENCE_ID --note "Reviewed chart-context frames before replay opportunity" --dry-run --visual-review-manifest "project_ws/AgentOps/ross_video_evidence/review_manifest.json"` |
| TC | `source_not_certified` | `2026-07-01T08:40:48.032603+00:00` | `HTgOZI8GOV0` and `iZ-mXl0ga3U` are scanner/watch context only; active chart panels do not certify TC structure. | Find pre-opportunity TC chart frames showing VWAP/HOD/pullback/candles/tape context, or keep the row noncertifying. | `python scripts\mark_ross_trade_event.py TC --action review_certified --ts 2026-07-01T08:40:48.032603+00:00 --visual-evidence-id EVIDENCE_ID --note "Reviewed chart-context frames before replay opportunity" --dry-run --visual-review-manifest "project_ws/AgentOps/ross_video_evidence/review_manifest.json"` |

## Acceptance Rules

- Transcript text is discovery/index only.
- Reviewed frames must be listed in `project_ws\AgentOps\ross_video_evidence\review_manifest.json`.
- Certifying source-before rows must use chart/trade context or chart/no-trade context, not scanner-only context.
- `source_before_opportunity_certifiable=true` must be tied to a timestamp at or before the Replay opportunity.
- Run `mark_ross_trade_event.py --dry-run` first; only then write the manifest marker.
- After any marker update, rerun strict visual audit and joined Replay queue.

Replay/PnL remains fail-closed until at least one symbol has source-before certification and the live Replay window also has complete selected/broker outcome and missed-vs-taken opportunity labels.
