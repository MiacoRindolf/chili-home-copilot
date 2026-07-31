# 2026-07-31 — L1+L4 loss-mechanism levers MERGED (f5880f8) + daytime proof program

**Operator orders na sinunod:** "di mo ba kayang simulan na yung changes mo" (07-27, code now) →
"bakit di mo gawin ngayon na" (07-30, daytime run) → "kapag green na lahat i-merge mo na agad" →
"Ok i-merge mo na" (07-31 ~00:20 PT, merged habang tumatakbo pa ang final measurement arm).

## Ano ang na-merge (PR #957 → main `f5880f8`)

Dalawang default-ON na loss-mechanism governor mula sa golden-baseline autopsy (07-27),
parehong may env kill switch at pure-helper na testable core:

- **L1 — range-position chase governor** (`chase_defer_decision` sa risk_policy):
  ipinagpapaliban ang entry kapag ang frontside strength ay nasa regime-adaptive defer
  tail AT `day_range_pos ≥ 0.50` (verbatim binding; 0.0 legal). Autopsy basis: upper-half
  chases 1W:17L, −469.50/18 trades. Kumpirmasyon mula sa bagong Ross evidence: ang
  −$32,385 INLF red day ni Ross (07-28) ay EKSAKTONG ganitong entry.
- **L4 — rapid-whipsaw escalation** (`rapid_whipsaw_cadence_update` sa risk_policy):
  dobleng escalation increment kapag magkasunod na STOP-CLASS loss sa loob ng 120s
  (bailout ay hindi stop-class — hindi nagtatatak ni sumusukat, review M1). Autopsy
  basis: SILO 6-entries-in-90s −177.
- Kasama: sealed roster 9→11 + CLOSED compound arm `intended-minus-autopsy-0727`;
  captured-paper operational projection + env allowlists += 2 flags (attestation gap);
  248 tests sa 10 suites; 72-agent adversarial review (16 confirmed defects fixed
  pre-merge — kabilang ang `or`-swallows-zero bindings, any-loss cadence stamping, at
  ang L1 dist-note self-release).

## Proof record (daytime, 07-30)

- **E3 baseline-v2 (main build 656f58b, ARM=intended)**: 12/19 windows banked sa
  `replay_batch_v3` (net −1,018 sa 7 traded na ok windows; VTAK −684.11/24e ang
  pinakamalaki — unang malinis na takbo nito). Excluded/documented: TNMG+UPC-0629 (walang
  usable row kahit sa v1), CELZ (preflight probe 78s > 60s cap), CWD (run timeout),
  TVRD (offby1 print-rounding refusal — tingnan ang Learnings). 7 windows na natitira +
  scorecard v2 publish = umaga (cron 05:23 + Codex-idle check).
- **Parity gate (ang merge gate): PASADO, fill-level** — compound arm sa PR build vs
  intended sa main build: LHAI (35/35 fills), TC, CLRO-0702 (−11.37 canonical), LHSW
  (38/38, −253.12) — lahat identical sa sentimo. CLRO-0707 timeout (hindi mismatch).
- **Isolation (partial)**: L4 zero-effect sa runnable windows — ang churn doon ay
  bailout-class na sadyang exempt; ang stop-class whipsaw window (SILO) ay library-tier.
  L1: CLRO-0702 zero-effect; **LHSW measurement hindi natapos** (4× napatay — tingnan
  sa ibaba). Ang tunay na sukat ng dalawang lever = paper live-soak counters
  (`live_entry_wait_chase_deferred`, `rapid_whipsaw_double_increment`).

## Learnings na operational (mahalaga sa susunod na gabi)

1. **Codex activation quiesce = box-wide python massacre.** Ang r42 RecoverOnly
   (22:03:51) at ang mga kasunod na attempt (r45 Apply 00:20) ay pumapatay ng LAHAT ng
   python.exe — kasama ang replay batches at ang kapatid-session repro. HINDI puwedeng
   sabay ang Codex one-shots at mahahabang replay; kailangan ng operator-level
   sequencing o marker-file heads-up.
2. **`pg_terminate_backend` mula `pg_locks` ay CLUSTER-WIDE.** Ang unscoped sweep ko ay
   nakapatay ng backends ng ibang sessions + prod app (nag-recover ang pools). LAGING
   may `JOIN pg_stat_activity ... WHERE datname='<sink>'`.
3. **Sealed batch runner:** fresh root kada runner-version; ISANG selection identity
   kada root (`--only` ay bahagi ng run identity — hindi mapaghahalo ang full-tier at
   partial sa iisang root); ang pre-run source verification ay FATAL abort — i-preflight
   ang receipt query (60s cap) bago maglunsad; orphan driver logs = refusal (ilipat sa
   `orphaned/`).
4. **TVRD offby1 (kapatid-session root cause):** ang mock's fractional partial fills ay
   pini-print na `{q:.0f}` — per-leg rounding = phantom −1 sa parsed inventory. Fix PR
   paparating mula sa session na iyon; ang TVRD economics (−57.48/29e) ay totoo,
   evidence grade lang ang na-refuse.
5. **Ross intake (lumipat sa akin):** 6 bagong recaps transcribed + extracted + verified
   (12-agent workflow); manifest ngayon 190 windows. Bagong ground truth: 07-27 main
   +$32k / small +$4k record; **07-28 pinakamalaking red ni Ross (−$32,385 main INLF)**;
   07-29 +$22k NCRA.

## Expectation calibration (sinabi rin sa operator nang direkta)

Ang na-merge = proteksyon sa DALAWANG partikular na mekanismo ng pagkatalo — HINDI pa
profitability flip. Ang v2 baseline ay talo pa rin (~−1,018 sa 12 windows). Ang
pinakamalaking winner-killer (bail: held +73.25 sa 5/6W vs bailed −109.95 sa 1/18W) ay
ang SUSUNOD na lever (L2a disambiguation sweep — libre, built-in arm) — hindi pa ito ang
na-merge. Levers-first na ang cadence mula rito.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
