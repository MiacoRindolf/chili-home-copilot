# CANF Ross Visual Frame Review

Date: 2026-07-02

Scope: conservative visual review of CANF candidate frame windows surfaced by `scripts/audit_ross_symbol_incidents.py` after adding guarded CANF ASR matching.

## Reviewed Frames

| Evidence id | Reviewed frame(s) | Visual observation | Certification effect |
|---|---|---|---|
| `HTgOZI8GOV0` | `project_ws\AgentOps\ross_video_evidence\HTgOZI8GOV0\frames\f0510.jpg` | Warrior running-up scanner frame shows CANF at about `5.08`, `24.63M` volume, `2.12M` float, and `16.49` relative volume. | Supports Ross scanner/source relevance for CANF. Does **not** certify a pre-opportunity entry label by itself. |
| `HTgOZI8GOV0` | `project_ws\AgentOps\ross_video_evidence\HTgOZI8GOV0\frames\f0529.jpg` | Warrior dashboard frame shows active CANF chart panels with VWAP/EMA/candle context, daily context, and scanner context. The surrounding transcript is a post-event breakdown of CANF hitting the scanner and the attempted VWAP/level break. | Supports chart-review context, but the reviewed source window is after Replay's first CANF candidate. Keep noncertifying for PnL/min-max labeling. |
| `HTgOZI8GOV0` | `project_ws\AgentOps\ross_video_evidence\HTgOZI8GOV0\frames\f0539.jpg` | Warrior dashboard shows active CANF 1m/10s chart context and filled CANF orders in the broker panel. The chart cursor is around the earlier 09:19 candle, while the dashboard time is after 10:03. | Supports post-trade Ross outcome evidence and the CANF VWAP/level-reclaim discussion. Does **not** prove source-before-opportunity availability. |
| `HTgOZI8GOV0` | `project_ws\AgentOps\ross_video_evidence\HTgOZI8GOV0\frames\f0575.jpg` | Warrior recap/account panel shows CANF closed with positive P&L along with DXST/DXST-like trade context. | Certifies post-trade outcome context only; it is not an entry-decision or source-before-opportunity frame. |

## Verdict

CANF is **reviewed** and has **post-trade Ross outcome evidence**, but remains **not Replay PnL-label certified** from these local frame windows.

The frames prove that CANF was a valid Ross/Warrior review subject with scanner and chart context. They do not prove that a certifiable Ross source existed before CHILI's replay opportunity timestamp. Replay currently shows the first CANF candidate at `2026-07-01T13:02:52.876051+00:00` and the first certifiable source at `2026-07-01T21:53:58.933022+00:00`, so this remains `cert_source_after_opportunity`.

Manifest classification: `ross_trade_outcome_certifiable=true`, `source_before_opportunity_certifiable=false`. Replay must use the second field for strict source/PnL labels.

## Follow-Up

- Keep this manifest row noncertifying for Replay opportunity labels unless a separate pre-opportunity visual/source row is found.
- If a pre-opportunity CANF source is discovered, link that source to reviewed chart-context frames and rerun Replay v3 strict opportunity labeling.
- Do not use this post-event breakdown to claim missed/taken PnL min-max readiness for the earlier CANF candidate.
