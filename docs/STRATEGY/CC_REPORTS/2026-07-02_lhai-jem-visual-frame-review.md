# LHAI / JEM Ross Visual Frame Review

Date: 2026-07-02

Scope: conservative visual review of LHAI and JEM/GEM candidate frame windows surfaced by `scripts/audit_ross_symbol_incidents.py` after adding ASR alias matching.

## Reviewed Frames

| Symbol | Evidence id | Reviewed frame(s) | Visual observation | Certification effect |
|---|---|---|---|---|
| LHAI | `HTgOZI8GOV0` | `project_ws\AgentOps\ross_video_evidence\HTgOZI8GOV0\frames\f0270.jpg` | Warrior scanner frame shows LHAI as the top leading gainer with price, volume, float, and relative-volume context. The adjacent transcript says LHAI is a little too cheap and in a price range Ross does not typically do well with. No LHAI chart/VWAP/HOD/pullback/tape context is visible. | Supports scanner/no-entry context. Does **not** certify a positive LHAI setup or trade/no-trade correctness. |
| JEM | `HTgOZI8GOV0` | `project_ws\AgentOps\ross_video_evidence\HTgOZI8GOV0\frames\f0311.jpg` | Transcript says GEM/JEM context, and JEM appears in the scanner list, but active chart panels show DXF. | Supports scanner/watch context only. Does **not** certify JEM chart structure. |
| JEM | `iZ-mXl0ga3U` | `project_ws\AgentOps\ross_video_evidence\iZ-mXl0ga3U\frames\f0263.jpg` | Warrior dashboard shows CANF active charts; JEM appears only in side lists/positions/source context. | Supports source/watch context only. Does **not** certify JEM chart structure. |
| JEM | `PnuyhYGHuUY` | `project_ws\AgentOps\ross_video_evidence\PnuyhYGHuUY\frames\f0319.jpg`, `project_ws\AgentOps\ross_video_evidence\PnuyhYGHuUY\frames\f0320.jpg`, `project_ws\AgentOps\ross_video_evidence\PnuyhYGHuUY\frames\f0650.jpg`, `project_ws\AgentOps\ross_video_evidence\PnuyhYGHuUY\frames\f0651.jpg` | Warrior dashboard does show active JEM 5m/1m/1D/10s charts. Around 10:18 ET, JEM is a former runner pulling back from the spike; the 1m/10s panels are under pressure near the VWAP/EMA cluster, while JEM remains in scanner/position context. Around 10:24 ET, the active JEM charts show continued fade/chop below the prior spike, with the 10s chart weak and the 1m chart not showing a clean reclaim continuation. | Supports active-chart review context. Does **not** certify a positive JEM entry or a clean missed VWAP-reclaim trade. Replay should keep JEM source/PnL labels fail-closed unless a separate pre-opportunity frame shows a clear Ross trade/no-trade decision. |

## Verdict

LHAI and JEM are **reviewed** but remain **not positive-entry certified** from these local frame windows.

The LHAI window is especially important because it is a Ross-style negative selection comment, not a missed-entry instruction. The JEM windows confirm that local video evidence mentions the symbol, but neither reviewed window shows a JEM chart with VWAP/HOD/pullback/tape context.

The additional `PnuyhYGHuUY` JEM frames do show active JEM charts, but they are not enough to certify a missed positive entry. The visible structure is a post-spike pullback/fade/chop context, not a clean source-before-opportunity Ross entry label. This is useful for explaining why a conservative system might wait/block, but it is not strong enough to label Replay v3 PnL counterfactuals as source-certified.

2026-07-03 Replay follow-up: the original 500-tick capped replay had no stable JEM opportunity timestamp, but a 1000-tick capped run produced one current-gate candidate at `2026-07-01T15:22:57.923222+00:00`: `ross_breakout_starter_tick`, entry `$6.67`, stop `$6.53868`, spread `75.24 bps`. That candidate still remains `source_not_certified` because all reviewed JEM frame evidence is scanner/watch or post-spike noncertifying context. It also does not match the user's later `$3.86` VWAP-reclaim screenshot zone, so it should not be used to conclude that CHILI missed that screenshot entry.

2026-07-03 live DB follow-up for the `$3.86` screenshot-like zone: the price/time matches the June 30, 2026 `19:35-19:55Z` tape much better than the July 2 window. IQFeed trade tape for June 30 had 19,714 JEM prints, price range `$3.33-$4.19`, average about `$3.84`, and average NBBO spread about `44 bps`. The three live JEM sessions in that window (`9970`, `9988`, `10036`) did **not** reach setup evaluation: each has `live_arm_requested` followed only by `live_arm_expired`, with `0` `live_arm_confirmed`, `0` `live_runner_started`, and `0` setup wait/candidate events. Therefore this historical screenshot should be classified as an **arm lifecycle gap / unconfirmed arm**, not as proof that the VWAP-reclaim or tape gate incorrectly blocked a valid setup. Added Replay-audit `arm_lifecycle` diagnostics so this class is now machine-visible. By contrast, the later confirmed JEM sessions (`10162`, `10180`, `10390`) did start runners; their refusals were backside/pullback-depth/VWAP-wait/stale pre-submit or Ross-profile boundary decisions, not the June 30 unconfirmed-arm class.

## Follow-Up

- Keep these manifest rows noncertifying unless a separate frame window shows the symbol's active chart and a clear trade/no-trade decision context.
- Replay v3 should continue to fail closed for PnL/min-max while certifiable source-before-opportunity remains absent.
- For JEM specifically, search for a separate active JEM chart window before saying CHILI should or should not have entered a VWAP reclaim; if the candidate is the June 30 `$3.86` window, investigate arm confirmation/runner lifecycle first, not setup gates.
