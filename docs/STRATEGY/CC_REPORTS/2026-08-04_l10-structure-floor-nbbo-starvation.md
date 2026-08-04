# 2026-08-04 — L10 structure-floor + NBBO-starvation fallback (PR #977 → 515e5494)

## TL;DR

- **Merged sa main**: L10 monster-conditioned 15s structure-floor trail candidate (default-ON, tighten-only, kill switch `chili_momentum_monster_structure_floor_enabled`) **+ replay-harness NBBO-starvation fallback** (tick-embedded quote synthesis).
- **Tapat na attribution**: ang HYFM|2026-08-03 improvement (−88.52 → **+0.22**) ay gawa ng **harness fix**, hindi ng L10 flag. Ang L10 ay zero measurable effect sa lahat ng 5 proof windows (content-identical A/B) — ang replay verticals ay nag-e-exit bago umarma ang TRAILING state kung saan naka-gate ang L10 site. Live-soak ang susunod na ebidensya nito.
- **Pinakamalalim na natuklasan**: ang HYFM live-day loss ay hindi exit-discipline defect kundi **NBBO capture starvation** (173 quotes sa 2h45m — ang mismong subscribe-hint gap na sinara ng L9 C1 #976). Bulag ang live runner noong araw na iyon; ang replay ay nagmana ng parehong pagkabulag hanggang inayos ang harness.

## Root-cause chain (3 patong; frame-anchor trap #3 + bagong starved-capture trap)

1. **Round 1** (1e70191): zero L10 events. Ang `monster_ctx` activation condition (day-low-anchored `up_off_low ≥1.5`) ay imposibleng mag-true sa replay frame na nagsisimula mid-move (HYFM uol ≈1.1 sa frame vs 500% totoong araw). **Fix**: tinanggal ang condition (89f74f0) — ang band inadequacy ang tunay na separator per ang study mismo; dinagdagan ng `monster_structure_floor_reject` observability (minsan kada reason transition; quiet reasons hindi ini-emit).
2. **Round 2** (89f74f0): zero events PA RIN — kahit rejects. Flow decomposition: ang HYFM exit ay galing sa `tape_accel_reversal_exit` path; **zero `live_trail_ratchet`** (vs 20 sa LHSW); ang buong composed-trail section (live_runner ~35754) ay `st == STATE_LIVE_TRAILING`-gated at hindi naabot.
3. **Ang ilalim**: ang HYFM capture ay **173 NBBO rows lang** sa buong window (starvation ng Lunes ng umaga + docker socket outage 12:10Z). NBBO-driven ang replay decision grid → 162 steps sa 2h45m → hindi makaandar ang trail/ladder machinery. Ang ticks ay BUO (79,937 in-window, 100% may embedded bid/ask, sakop ang buong anatomy hanggang hwm 4.12).

## Harness fix (081eec8): NBBO-starvation fallback

Sa `scripts/replay_ab_dark_flags.py`: kapag ang mean quote spacing sa window ay lampas sa `NBBO_STARVATION_MAX_SPACING_S = 15.0` (ISANG documented base = ang 15s bar cadence, ang pinakapinong structure na pinag-iisipan ng strategy), ang quote frame ay sini-synthesize mula sa tick-embedded bid/ask at ginagamit sa **grid AT sink-tape mirror** (parehong frame → self-consistent na run). Fail-toward-existing kapag walang usable embedded quotes. Sa 15-window roster, **HYFM lang** ang tumatama sa floor (57.2s spacing; lahat ng iba sub-1s).

## Round-3 proof (081eec8, 2 arms × 5 windows, sink chili_replay_bail_test)

| Window | intended | minus-L10 | Canon check |
|---|---|---|---|
| TC 07-01 | −5.55/4e | −5.55 | ✓ identical |
| CLRO 07-02 | −11.37/4e | −11.37 | ✓ identical |
| LHSW 07-06 | −253.12/20e | −253.12 | ✓ identical |
| JLHL 07-09 | +23.63/4e | +23.63 | ✓ identical |
| HYFM 08-03 | **+0.22/1e** | **+0.22** | bagong canon (dating −88.52 = artifact) |

Dense-grid HYFM anatomy: entry 3.66 → **partial into strength @3.92 (`scale_out_limit` — gumana na ang Ross ladder)** → natirang lot @3.55 stop; wala nang pangalawang losing entry. Ang dating −88.52 (entry 3.76→3.57 + bailout 3.46→3.43) ay grid-cadence artifact.

Bakit hindi na-exercise ang L10 sa dense grid: ang replay entry ay mas maaga/mas mababa (3.66), peak bid ~3.92 = +7.1% < trail-arm threshold → hindi umabot sa TRAILING. Sa live day ang run ay +9.6% (3.76→4.12) — doon aarma at makikilos ang L10. Tighten-only + 3 guards (leg <180s, band inadequacy, halt suppression) + INVARIANT-A.

## Bakit merged kahit no-op sa replay

No-dark-flags + overfit→default-ON+live-test na polisiya: (a) 5/5 no-regression na A/B, (b) tighten-only na hindi makakapag-loosen ng stop, (c) kill switch, (d) ang tanging paraan para makakuha ng ebidensya ay live verticals na umaabot sa TRAILING. 51 local tests green (CI systemically red — kilala).

## Operational na natuklasan sa gabi

- **Codex resident watchdog** pumatay sa unang round-2 container (`l10run2`, rc=137, ~20min) habang bukas ang Codex app. Workaround na tumalab: retag ng image sa `chili-bench:656f58b` + innocuous na container name (`hyfmv3`) — nakaligtas nang buo ang 2 sunod na run.
- Monitor `tail -1` gotcha: magkasabay na DONE + queued lines sa isang poll = nilalamon ang una; basahin ang container log para sa nawalang verdict.

## Susunod

- **Live-soak receipts para sa L10**: sa susunod na totoong vertical na aabot sa TRAILING, hanapin ang `monster_structure_floor_candidate`/`_reject` events.
- **L10b** (nakapila): price-structure partials (whole/half-dollar anchors); design PR.
- **Trade#2-class entry admission** (dying-tape last-gasp): nakarota sa L7 entry-gate sites bilang sariling lever.
- Codex side (relayed na): X1 writer-lease crash fix, selection_runtime entry_streak, X2 ignition unseal, HWM print-anchor verify + replay banner stale-label.
