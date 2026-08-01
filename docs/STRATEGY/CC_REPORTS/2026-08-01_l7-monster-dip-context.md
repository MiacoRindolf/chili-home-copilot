# 2026-08-01 — L7: monster-dip context (#965, MERGED → 4d9d652)

## Ano ito

Ika-limang lever ng resolutional program (kasunod ng L1/L4 #957, bail retirement #963,
L6 #964). Ang distribution study sa golden tape ay nagpakita na sira ang
recent-impulse retrace yardstick sa monster days: ang mga winner dip ng JLHL (+784%
intraday) ay 0.71–1.0 retrace laban sa saturated na cap ~0.61–0.69, kaya lahat ng
tamang dip ay na-reject bilang "pullback_too_deep" habang ang mga fade ng JZXN ay
komportable sa loob ng cap.

**Ang fix**: `monster_dip_context` pure helper (entry_gates.py) — aktibo lang kapag
(a) presyo ≥ 1.5× ng day low AT (b) dip ≤ 35% ng day range. Sa study: 7/8 JLHL winner
dips pasok, 0/19 JZXN fades — zero-overlap na separation. Sa loob ng context,
niluluwagan ang 3 geometry guards: retrace yardstick (skip ang pullback_too_deep),
bottoming-tail (0.50 → 0.30), at vwap reclaim (depth-for-duration: ≥1 bar below +
penetration ≥ 1.0×ATR% sa halip na bar-count lang). 5 settings, lahat verbatim-bound
at default-ON; 13th roster flag `chili_momentum_dip_monster_context_enabled`;
fail-toward-legacy sa anumang sirang input. Unit tests = mismong measured cases mula
sa tape (kasama ang 20:30 climax na dapat HINDI pumasok).

## Proof (sealed batch, 2 arms × 7 windows @7ff36c0, roots replay_957_l7a4/l7b4)

| Window | A (L7 ON) | B (L7 OFF) | Event-stream diff (timestamp-stripped) |
|---|---|---|---|
| JEM 06-30 (unlock) | 0e $0 | 0e $0 | IDENTICAL (117=117) |
| TC 07-01 (regression) | 4e −5.55 | 4e −5.55 | IDENTICAL (217=217) |
| CLRO 07-02 (regression) | 4e −11.37 | 4e −11.37 | IDENTICAL (146=146) |
| LHSW 07-06 (regression, context-eligible 1.61×) | 20e −253.12 | 20e −253.12 | IDENTICAL (893=893) |
| JLHL 07-09 (unlock) | 0e $0 | 0e $0 | **+1 event sa A**: dagdag na dip, vinето ng `benched_backside_sticky` (51 vs 50) |
| PLSM 07-13 (unlock) | 0e $0 | 0e $0 | IDENTICAL (449=449) |
| JZXN 07-21 (guard/fade) | 0e $0 | 0e $0 | IDENTICAL (2=2) — dormant ang context |

Verdict: regression 3/3 pasado (sentimo + event-identical), guard pasado, unlock
targets 0-entry na may dokumentadong dahilan — **ang binding constraint sa monster
days ay ang backside sticky bench** (`benched_backside_sticky` ×95 sa JEM, ×51 sa
JLHL sa PAREHONG arm), hindi na ang dip geometry. Ang L7 ang unang lever na
nagpapadala ng dip LAMPAS sa geometry (ang JLHL +1) — ang bench na ang pumapatay.

## Mga aral

1. **Wall-clock gotcha**: ang timestamps sa batch-runner window logs ay RUN wall-clock,
   HINDI tape time (log span ≤ run duration). Ang cross-arm na "time shifts" ay
   artifact ng magkaibang oras ng pagtakbo — muntik itong maging maling "2.2h earlier
   evaluation" verdict. Tamang paraan: timestamp-stripped event-sequence diff.
2. **Codex docker watchdog**: 5 proof attempts pinatay ng `docker stop` mula sa
   codex.exe app-server (containers matching `proof|replay|l7`), kahit tapos na ang
   kanilang run; nahuli via 2s-poll ancestry capture; operator ang huminto sa Codex
   task bago nakalusot ang v7. Coordination rule na napagkasunduan: quiesce lang
   habang RUNNING ang D:\cp milestone; GREEN/walang signal = bawal pumatay.

## Susunod (L8)

Backside sticky bench sa monster days — front-side monsters ang itinuturing na
backside/faded. May eksaktong numero na (95/51 sticky vetoes + chasing_top/below_vwap
splits sa proof logs). Kapag naayos, chained unlock: bench → L7 geometry → entry.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
