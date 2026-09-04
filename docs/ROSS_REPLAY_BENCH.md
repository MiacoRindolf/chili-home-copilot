# The Ross Parity Bench (Tier 1)

> **Status:** Tier 1 instrument. **DIAGNOSTIC_ONLY** — every output carries
> `evidence_grade: DIAGNOSTIC_ONLY`, `causal_use_allowed: false`, `admission_claim: false`.
> **Author:** CHILI Code, 2026-09-04, branch `seam/rossbench-instrument-0904` (base
> `origin/main` 078487738).
> **Scope:** Drive the REAL live FSM (`tick_live_session`) over hydrated tape for the exact
> symbol-day windows in Ross's master ledger, so that "what would CHILI have done on the trade
> Ross actually took" is a *measured* answer with a named mechanism, per case, rather than an
> argument. It is the **lever-selection instrument**. It is not the success instrument — that
> stays `scripts/ross_baseline_tracker.py` on LIVE `momentum_fill_outcomes`.
> **Engine:** `scripts/replay_v3_fsm_window.py` (see
> [`docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md`](DESIGN/REPLAY_V3_LIVE_FSM_SIM.md) §12.3).
> **Related:** [[project_ross_master_ledger_0903]] · [[project_fsm_replay_instrument]] ·
> [[feedback_mine_ross_misses_not_just_trades]] · [[reference_golden_batch_canonical_params]]

---

## 0. Read this first: the one sentence that stops the worst mistake

**Tier 1 force-seeds admission.** The bench writes `live_eligible=True` into the seeded arm
(`scripts/replay_v3_fsm_window.py:966-967`, `RecordedArm(live_eligible_at_utc=WIN_START,
viability_score=0.9)`) and passes `risk_gate_allows=True` into the driver
(`scripts/replay_v3_fsm_window.py:1039`). The symbol is handed to the runner already chosen,
already eligible, already past the pre-entry risk gate.

So a Tier 1 result answers **"given that CHILI was watching this name at this instant, what did
its entry/exit machinery do?"** It can never answer **"would CHILI have been watching?"** — that
is the question 71 % of the historical gap actually turns on (§3).

The FSM itself records the bypass: `ReplayResult.certification_failures` carries
`entry_risk_gate_bypassed` (`app/services/trading/momentum_neural/replay_v3.py:6223`), and the
run receipt echoes that list. **If a report line ever reads as an admission-latency or
selection claim, that report is wrong — regardless of how good the number looks.**

---

## 1. What the bench can and cannot answer

### 1.1 CAN (force-seeded name, real FSM, recorded tape)

| Question | Where the answer comes from |
|---|---|
| Did the entry trigger fire inside Ross's window, and if not, which gate held it? | ordered `events[]` in `run.json`, mapped to a stage by the scorer |
| Was the entry price/shape comparable to his? | `fills[]` (mock crossings against the recorded NBBO) |
| Did CHILI hold as long as he did, or bail? | `states_visited[]` + exit events + `mtm_usd` at window end |
| Capture ratio on his winners; did the guard correctly refuse his losers? | RPI Capture / Avoidance (§2) |
| Is a *veto* valid — i.e. did refusing this trade save money? | the L set is the negative control; a veto on a losing Ross day is a **win**, not a miss |
| Does flag X change any of the above? | interleaved A/B, `--arms base,<name>=arm.json` |

### 1.2 CANNOT (structurally, not "not yet")

| Question | Why the harness cannot reach it |
|---|---|
| **Selection / universe / admission** | the name is seeded; `replay_ab_dark_flags.py:17-19` — "universe/catalyst selection and the ordinary risk gate are not replayed" |
| **Own-order market impact** | the recorded tape is the market *without* our order in it. Mitigated (not removed) by the conservative volume cap — see `DESIGN/REPLAY_V3_LIVE_FSM_SIM.md` §12.1 |
| **Broker ack timing** | the mock's ack delay is a sampled distribution, not this venue on this day. Worse here than in the v3 day-runner: **no script had ever run the FSM replay on `alpaca_spot` before this branch**, so the Alpaca fill path has no replay track record at all |
| **BBO-source mix** | live blends `iqfeed_l1` / `massive_ws` / `DataFeed.IEX` / `iqfeed_trade_embedded`; the bench reads one hydrated vendor per window (`nbbo_vendor` in the receipt) |
| **Liveness** | a replay is alive by construction. Liveness is measured on recorded heartbeats, never on a replay |
| **Leader-board rank effects** | the sink holds ONE viability row, so the seeded symbol is #1 by construction. Leader-gated exemptions are over-granted in Tier 1. Reported as `leader_board_mode="isolated_single_symbol"` |
| **Any depth-reading exit lever** | `chili_hydrated.iqfeed_depth_snapshots` is empty → 0 depth rows mirrored → those levers are silent no-ops. Reported as `depth_levers_unmeasurable=true`, never as a zero delta |

Everything in 1.2 needs a **paper soak with pre-registered pass criteria**, not a bench run.
The shape that has worked: P1 mechanism fires → P2 conversion improves → **P3 money is the gate**
→ P4 no regression elsewhere; kill the lever when P3 < 0
(`project_ws/AgentOps/missed_mover_forensics_20260901.md` §4).

### 1.3 Tier 2 (not built)

Tier 2 replaces the force-seed with `replay_eligibility.py` TIER A — recompute `live_eligible`
by running the same `ross_momentum.score_universe` over recorded universe-snapshot inputs as-of
`t` — **and** hydrates the whole candidate universe of the day, not just Ross's symbol (cost
reference: the Phase-4 corpus was 119 symbol-days / 35.5 M rows). Until then, admission latency
is measured on LIVE only. Tier 1 and Tier 2 stages are **never mixed in one column**.

---

## 2. The Ross Parity Index — four numbers, never blended

The RPI is **not** a weighted score. It is four numbers reported together, each with its own
**numerator, denominator, and the case list behind it**. A single blended number would let a
Capture gain pay for an Avoidance loss silently, which is exactly the trade the guard exists to
refuse.

| Number | Definition | Denominator |
|---|---|---|
| **Capture** | share of Ross's **winners** that CHILI got: entered inside his phase window AND per-winner capture fraction (CHILI $ ÷ Ross $ at normalized size) > 0 | \|S ∩ scored\| |
| **Capture+** | share of the trades **Ross admitted he missed** that CHILI took | \|S+ ∩ scored\| |
| **Avoidance** | count of Ross's **losing** days where CHILI $ ≥ 0 | \|L ∩ scored\| |
| **Precision** | Σ CHILI $ over all scored days ÷ Σ \|CHILI $ on CHILI's losers\| (profit factor on the bench) | — |
| **Liveness** | share of S with a live consumer at `t_ross_entry − Δ` | \|S\| — **`null` in Tier 1** (`tier2_required`), reported separately as `recorded_liveness` from heartbeats |

**The promotion gate for any lever:** *Capture up, Avoidance not down, Precision not down*, in an
**interleaved** A/B on **dense** tape. Three conditions, all of them, or the lever does not ship.

### 2.1 Why Capture is a percentage of winners, not dollars

Chosen by the operator on 2026-07-03 (`project_ross_capture_matrix_0703`: "PERCENT of Ross's
winners captured, not dollars"). Dollars are **size-confounded**: in the 07-09 Ross-mode replay
(EQUITY 400 k BP-basis, RISK 4 k) CHILI beat Ross on %-of-account in 3/3 windows (+25.8 % vs
+7.3 %) while "losing" every window under a $50 cap. So dollars appear in **two columns —
raw and normalized** (÷ size ratio) — and the **size canon is declared per run**. The golden
canon (`--equity 13000 --risk-fraction 0.01 --exec-family robinhood_agentic_mcp`,
`reference_golden_batch_canonical_params`) and this bench's canon (`alpaca_spot`) are **different
canons and are never compared across that boundary.**

### 2.2 Capture+ is the real target

`feedback_mine_ross_misses_not_just_trades`: success is not "imitated Ross", it is "**took the
trade Ross admitted he missed**". Those misses are a catalogue of *human* limits — attention, one
name at a time, hand speed, emotion — that a machine does not have. Each is a self-validating
capability spec. Classify each as (a) CHILI can already do it — verify it did, (b) small fix,
(c) structural machine advantage. **Prioritise (c).**

The starting S+ set is the 30 non-trade rows already in the master ledger (§3.2); a fuller S+
needs a transcript extraction pass that does not exist yet.

### 2.3 The five designed refusals are `valid_veto`, not misses

SVRE leg-1 knife, JEM `extended_verticality` (+6 %/s), SDOT ratchet before the −13 % halt resume,
CLRO structural stop in the halt-reopen whipsaw, VWAV red-candle bench. *Ross won some legs by
accepting knife risk the machine is built to refuse.* These are encoded as `EventTimeVetoEvidence`
fixtures **so the bench can never reward "fixing" them.**

---

## 3. The two populations — and why mixing them slanders the strategy

### 3.1 Scored population vs availability bucket

**71 % of the historical gap is uptime, not strategy.** Put those cases in the Capture
denominator and the strategy looks far worse than the evidence supports; hide them and the
program optimises the wrong thing. So they are reported **side by side, never summed**:

- **Scored population** — symbol-days where CHILI actually *made a decision*: `armed_no_entry`,
  `entered_wrong_leg`, a gate veto, or (Tier 1) a replay that ran. Capture / Capture+ /
  Avoidance / Precision live here.
- **Availability bucket** — `never_armed` / `not_in_universe`. This is **Liveness**, and it is an
  ops program (watchdogs, control-loop uptime, discovery), not a strategy lever.

Measured on `project_ws/AgentOps/ross/ross_master_ledger.json` at the time of writing, over its
68 `xref` symbol-days ($428,049.53 of Ross P&L):

| `chili_verdict` | rows | Ross $ |
|---|---:|---:|
| `never_armed` | 33 | 290,798.88 |
| `not_in_universe` | 20 | 107,405.02 |
| `armed_no_entry` | 8 | 28,870.00 |
| `entered_wrong_leg` | 1 | 975.63 |
| `unknown_no_data` | 1 | 0.00 |
| *(null — the 08-17/08-18 lane-alive cases)* | 5 | 0.00 |

**CHILI's side of all 68: $0.00.** Not small — none.

### 3.2 Two cut lines exist. Name the one you used.

The 09-03 scorecard's headline is **"48 of 68 (71 %, $321 k) is not a fight with the strategy —
that is uptime"** (`project_ws/AgentOps/ross/ross_vs_chili_scorecard_2026-09-03.md:39`). That
48 / $321,157 is its mechanism classes **1 + 2** (no lane existed yet, 18 rows / $163,277;
control loop dead, 30 rows / $157,880). The coarser `chili_verdict` groupby above gives a
**different** cut: `never_armed` + `not_in_universe` = **53 / 68 (77.9 %) and $398,203.90**,
because it also absorbs the kill-switch lockout, the late/frozen selection cases and the
unconfirmed UPC arm.

Both are defensible. Neither is "the" number. **The reporter must print the numerator, the
denominator and the case list for every RPI figure, and name which cut line it used.** A bare
percentage in this program is a defect.

The genuinely load-bearing fight is the scorecard's **class 6** — 8 cases where a runner gate
actually decided — and in those 8, **Ross lost $13,190 net.** That is the honest starting
scoreboard, and it is why Avoidance is a first-class RPI number rather than a footnote.

### 3.3 Do not trust the ledger's own summary block

The ledger carries a top-level `verdicts` dict. It does **not** reproduce as a groupby of the
`xref` rows: the block says `never_armed: 34, armed_no_entry: 12`; the rows give **33** and **8**,
with **5** rows carrying `chili_verdict: null`. Recompute from rows. (The 5 nulls are the
08-17/08-18 lane-alive cases — WETO, GNPX, PFSA, SLE, SXTC — which belong in the **scored**
population, not the availability bucket, since each has a named runner mechanism.)

---

## 4. The ledger is narrative data. Design for that.

`ross_master_ledger.json` is schema `chili.ross_master_ledger.v1`: 187 `trades`, 68 `xref`,
55 `hydration_worklist`, 28 `source_files`. It is transcript-derived, not broker-derived. Every
consumer must handle the following; all counts below were measured against the file in this tree.

**(a) 30 of the 187 rows are not trades.** They are no-trade / miss records merged in by a
`walk_lists` heuristic from five different sub-schemas. **Separate them by `_path`:** trades live
under `trades` (137) and `rows` (20); the non-trade rows are `no_trade_references` (16),
`misses_and_no_trades` (11), `ross_no_trade_context` (3). They are the seed of S+ (§2.2) — they
are not zero-P&L trades.

**(b) `entry_time_et` is narrative prose, not a timestamp.** Of the 157 trade rows: 132 start
with a clock (`^\s*~?\s*(\d{1,2}:\d{2})`), 14 have a clock mid-sentence, 11 have none at all —
e.g. `"premarket (clock time not stated; the squeeze began right after the two scalps)"`. Only
**18** rows of 187 carry `entry_time_utc`. This is why every case is **tape-pinned** (§5) instead
of trusted: the pin is what places the replay window.

**(c) `0` is a NULL SENTINEL, and the None rule is load-bearing.** Within the 157 trade rows:
`entry_px` is 0 in **67**, `exit_px` in **103**, `shares` in **118**, `pnl_usd` in **30**.
Treating a `0` `pnl_usd` as a real zero silently reclassifies a *missing* outcome as a *flat*
one — which pollutes Avoidance (a flat day counts as avoided) and Capture (a flat winner counts
as a denominator). **Map 0 → `None` at the manifest boundary and let it propagate as
`unscorable`, never as a value.**

**(d) `confidence` is mostly inferred:** `inferred` 90, `approx` 59, `exact` 8 (157 trade rows).
Mapped to `pnl_confidence`: exact → `stated_verbatim`, approx → `narrated`, inferred →
`inferred`.

**(e) `account` has three vocabularies and is present on only 90 of 187 rows:** `main` 49,
`small` 25, `big` 16. **Collapse `big` → `main`; small and main are never summed**
(`build_ross_manifest.py` already enforces this for the older layers). Where the key is absent,
the account is `null` — **do not guess `main`.**

**(f) The honest scoreable core is ~86 rows.** Trade rows with a clock anywhere in
`entry_time_et` AND `entry_px > 0`: **86 rows = 49 wins + 21 losses + 16 zero-sentinel**
(unscorable). Under the stricter leading-clock rule it is 80 = 45 + 19 + 16. **Report which rule
the run used.** Everything else is a hydration or extraction problem, not a bench result.

**(g) IQFeed's 180-day retention is not currently binding, but it moves.** Only 3 trade rows
(all 2026-03-04) sit below today's cliff, and none of them has a parseable clock, so no scoreable
row is lost right now. The next ledger date is 2026-04-20, so the cliff starts eating scoreable
history around **2026-10-17**. `feedback_buy_the_history_dont_accept_missing_data`: hydrate the
corpus *before* then; a missing tape is a purchase order, not a blocker.

---

## 5. Anatomy of a run

```
ross_master_ledger.json ──► build_ross_manifest.py (4th layer: source_kind="master_ledger")
                                    │
                                    ▼
                            rossbench_pin_ross_events.py ──► pins.json (chili.ross_event_pins.v1)
                                    │            pin method: frame_audit_stated → price_match
                                    │            → level_cross → stated_only
                                    ▼
                            ross_manifest_adapter.py ──► AdaptedCase / ValidatedPhaseWindow
                                    │   WIN_START = entry_pin − lead ; WIN_END = exit_pin + lag
                                    ▼
  chili_hydrated ─────► scripts/ross_replay_bench.py  (argparse; interleaved (case, arm) plan)
      (DATABASE_URL)          │  one subprocess per run → scripts/replay_v3_fsm_window.py
                              │  sink = chili_rossbench_test (TEST_DATABASE_URL, TRUNCATEd)
                              ▼
             run.json (chili.replay_v3_fsm_window_result.v1)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
    rossbench_timeline.py  ross_bench_scoring.py   rossbench_report.py
      timeline.md /.jsonl   stages + RPI            report.md + rpi.json
```

**Hindsight fence, coded and tested:** the pin is used ONLY by the grading window, the window
placement, and the timeline overlay. **The driver subprocess environment carries no `ROSS_*`
key at all**, and `window_basis` in the receipt is `ross_pin_minus_lead` or
`ross_stated_minus_lead` — never a peak of the tape. If a future edit lets a Ross fact reach the
FSM's inputs, the bench stops being a measurement and becomes a look-ahead machine.

### 5.1 The receipt

Every arm writes one `run.json`, schema `chili.replay_v3_fsm_window_result.v1`, **before** the
end-of-arm cleanup deletes the evidence. It carries: `tree` (HEAD + tree object + `dirty` — a
branch name is not proof, and a stale build tree once produced a fake "the fix works" result),
`env` (every knob echoed back), `tape_sources`, `sink_reset`, `mirrored` (tick / nbbo / depth row
counts), `density`, `grid_steps`, `mock` config, `seed_session_id`, `execution_family` + `venue`,
`economic_seed_mode`, `certification_eligible` + `certification_failures`, `final_state`,
`states_visited`, `fills`, `pnl_usd` / `mtm_usd` / `cost_usd` / `proceeds_usd`, `entries` /
`exits`, `event_histogram`, and the full ordered `events[]` with a payload whitelist.
`ARM=both` writes two files (`…​.g4_on.json` / `…​.g4_off.json`) so the arms cannot overwrite
each other.

### 5.2 The divergence stages (the vocabulary the report is written in)

Replay side (Tier 1 emits these and everything below them):

- `runner_never_started(no_bbo)` — confirmed arm, no `live_runner_started`, `no_bbo` in payload
- `arm_unconfirmed` — requested, never confirmed (this is UPC 06-29 session 9498)
- `armed_no_candidate(bench_veto | silent | trigger_wait:<top reason>)` — the binding gate is the
  top `live_entry_trigger_wait.payload.reason`, per `nightly_replay_report.py:192-209`, **not**
  `detector_rejects`
- `candidate_no_submit(<veto>)`
- `submit_no_fill(<terminal>)`
- `filled_exited_worse(<exit event>)`

Recorded-only side (source = recorded session events, else `xref_verdict`): `not_admitted`
(← `not_in_universe`), `not_alive` (← `never_armed` whose evidence names a dead control loop /
no live session), `eligible_not_armed`. **These stages never come from a replay.** Every report
line prints `recorded_stage` and `replay_stage` together, each labelled with its source, plus the
`code_ref` (`file:line` of the `_emit(...)` site in the verified build tree) — so "why" is one
click, not one hour.

---

## 6. Every knob, with its derivation

No magic numbers. Each default is either derived from a named distribution or is a required
argument with no silent default.

| Knob | Default | Derivation |
|---|---|---|
| `ROSSBENCH_TICK_STRIDE` | **1** (allowed {1, 2}) | the 2026-08-28 downsampling incident: the same window replayed **+$193.92 at stride 1 and −$4.66 at stride 10**. Enforced by invariant 1 |
| `ROSSBENCH_GRID_STEP_S` | **1.0** | ≤ p10 of first-decision latency, 3.9 s (n = 1,032, 14 d, live) — a grid coarser than the fastest decisions cannot see them |
| `ROSSBENCH_WINDOW_LEAD_S` | **180** | p90 first-decision latency 162 s, rounded up to a whole minute; also gives the cold-start guard room to clear before the pinned entry |
| `ROSSBENCH_WINDOW_LAG_S` | `settings.chili_momentum_risk_max_hold_seconds` | the FSM's own hold cap — the window must not end before the machine would have |
| `ROSSBENCH_COLD_START_MIN_UPTIME_S` | **60** | brackets the measured p50 17.5 s / p90 162 s first-decision latency |
| `ROSSBENCH_COLD_START_MIN_TICKS` | **6** | 60 s ÷ the 10 s live tick cadence (`live_runner.py:10681`) |
| `ROSSBENCH_PIN_HALFWIDTH_S` | **computed at run time** | median width of every explicitly stated range in the manifest — derived from the corpus, not chosen |
| pin price tolerance | **none** | `max($0.01, spread_at_t)` read off the tape at the candidate instant |
| `ROSSBENCH_DENSITY_MIN_RATIO` | **1.0** | hydrated ⊇ recorded is the Phase-3 fidelity finding (`docs/HISTORICAL_TICK_HYDRATOR.md:489-494`); below it → `density_regression` |
| `--equity` / `--risk` | **REQUIRED** | 07-16 ran $13 k / $130, nightly runs $100 k / $4 k. A size canon that is not stated is a result that cannot be compared |
| `--timeout-s` | **3600** | nightly takes 30–45 min per 60-min window; re-measure on the first real run |
| `SOURCE_FILTER` | **required for hydrated runs** | measured on TMCR 2026-08-24: 16,933 `iqfeed_lookup_hist` + 16,933 `polygon_v3_trades` rows came back as 33,866 ticks — double the prints, double the volume, and nothing about the rows looks malformed. Unset ⇒ no predicate ⇒ byte-identical to every legacy caller |
| `EXEC_FAMILY` | `robinhood_agentic_mcp` (bench uses `alpaca_spot`) | until 2026-09-04 this was a **silent no-op**: `nightly_replay_report.py:164` had been setting `alpaca_spot` for weeks while the seed site hard-coded Robinhood. Normalised through `normalize_execution_family` so a typo cannot become a different rail |
| `MAXLOSS_USD` | unset | the ONLY lever that scales per-trade size. `LEGACY_DIAGNOSTIC_POLICY_CAPS` freezes `max_loss_per_trade_usd = 50.0` into the seeded session snapshot (`replay_v3.py:201-205`) and **no setting reaches it** |
| `RISK` | 130 | ⚠️ **INERT in this driver.** It is read at `replay_v3_fsm_window.py:173` and echoed into the receipt at `:783`, and used nowhere else. Sizing comes from `EQUITY` via `equity_provider` plus the frozen cap above. Do not read a `RISK` value in a receipt as a risk budget that bound anything |
| `FULL_MIRROR` | `1` | cadence and the 5 m higher-low need real tick density; `0` downsamples the trade tape (the NBBO mirror still runs either way) |
| `REPLAY_KEEP_SINK` | **must stay unset** | invariant 2 |

---

## 7. The run recipe

```bash
# ---- environment -------------------------------------------------------------
export PYTHONIOENCODING=utf-8
PY=C:/Users/rindo/miniconda3/envs/chili-env/python.exe
BUILD=E:/dev/wt-bench                       # the tree that will RUN (verify_tree checks it)
SRC=postgresql://chili:chili@localhost:5433/chili_hydrated
SINK=postgresql://chili:chili@localhost:5433/chili_rossbench_test

# ---- 0) ONE TIME: create the sink ---------------------------------------------
# Same path as tests/conftest.py:468-472 (Base.metadata.create_all + run_migrations).
# _reset_sim_sink() REFUSES to run against a sink missing a seed table.
$PY scripts/rossbench_init_sink.py --sink "$SINK"

# ---- 1) manifest + pins + corpus ----------------------------------------------
ROSS_EVIDENCE_DIR=... ROSS_MASTER_LEDGER=project_ws/AgentOps/ross/ross_master_ledger.json \
  $PY scripts/build_ross_manifest.py
$PY scripts/build_ross_manifest.py --check          # MUST exit 0 — drift gate
$PY scripts/rossbench_pin_ross_events.py            # -> project_ws/AgentOps/ross/pins.json
$PY scripts/rossbench_corpus.py                     # -> corpus.csv + corpus.json

# ---- 2) hydrate anything missing (off-hours; NOT 07:50-08:35Z = Codex preflight)
$PY scripts/historical_tick_hydrator.py --provider iqfeed --csv corpus.csv
$PY scripts/hydration_canonicalize.py --apply && $PY scripts/hydration_canonicalize.py --check
$PY scripts/hydration_price_scale_check.py          # a flag here means a SPLIT: rescale ross_entry_px
$PY scripts/hydration_quote_seam_check.py --json    # reports the NBBO vendor split
$PY scripts/rossbench_density_check.py              # hydrated t/s vs live, identical predicate

# ---- 3) the bench ------------------------------------------------------------
$PY scripts/ross_replay_bench.py \
    --manifest project_ws/AgentOps/ross_video_evidence/manifest.json \
    --pins     project_ws/AgentOps/ross/pins.json \
    --corpus   corpus.json \
    --cases    SDOT:2026-06-26,ZDAI:2026-06-26,ILLR:2026-06-25,UPC:2026-06-29,\
IPST:2026-08-17,WETO:2026-08-17,PFSA:2026-08-18,SLE:2026-08-18 \
    --arms     base,sticky_backside_bench_off=arms/sticky_off.json \
    --build    "$BUILD" --ref origin/seam/rossbench-instrument-0904 \
    --source   "$SRC" --sink "$SINK" \
    --equity   13000 --risk 130 \
    --tick-stride 1 --grid-step-s 1.0 --exec-family alpaca_spot \
    --out-dir  D:/CHILI-Docker/chili-data/rossbench/<run_id>

# ---- 4) read it --------------------------------------------------------------
$PY scripts/rossbench_report.py --run-dir D:/CHILI-Docker/chili-data/rossbench/<run_id>
#    -> report.md  +  rpi.json (chili.ross_parity_index.v1)
```

**One bench per sink.** Two concurrent replays against the same sink delete each other's session
mid-run (`ReplaySessionRowVanishedError`). Do not share `chili_rossbench_test` with the nightly
`chili_replay2_test`.

**The A/B lives in the settings env, not in `ARM`.** The driver's own `ARM` knob is the G4 flag
pair and stays `on`; an arm file is a JSON dict of environment overrides, e.g.
`{"CHILI_MOMENTUM_STICKY_BACKSIDE_BENCH_ENABLED": "0"}`.

**Interleaved, always.** The plan is case-major — every arm of one window runs before the next
window starts. All-A-then-all-B measures the source DB's drift under a 120-hour frame warm-up,
not the arm.

---

## 8. Verification sequence — STOP on any red

Run these in order. A red at step N invalidates everything after it; do not "note it and
continue".

1. **Harness suites green before any edit and after every step:** `test_replay_v3_parity`,
   `test_replay_v3_fill_model`, `test_replay_sink_reset_truncate_guards`,
   `test_replay_mirrors_l2_depth`, `test_nightly_replay_loop_actually_runs`,
   `test_ross_replay_benchmark`, `test_ross_playlist_manifest`. A red parity gate means the
   harness no longer reproduces reality, which means **every number it prints is untrustworthy**.
2. **New unit tests** for the manifest layer, pins, grader diagnostic mode, driver extensions,
   invariants, bench runner, scorer.
3. `build_ross_manifest.py --check` exits **0** after a rebuild (the input must be in git — a
   manifest built from an untracked file cannot be re-derived).
4. **No-op A/B.** One case, arms `base` and `base_copy`. `run.json` must be byte-identical apart
   from the timestamp, and the RPI delta must be **0.000**. This is the single most valuable
   check in the list: it catches nondeterminism before it is mistaken for a finding.
5. **Known answer.** CLRO 2026-07-02 on the golden path (−11.37 / 4 entries / 4 exits,
   `docs/STRATEGY/CC_REPORTS/2026-07-26_golden-library-baseline-scorecard.md:83`), then the same
   window from `chili_hydrated`. **The hydrated-vs-golden delta is a FINDING to report, not an
   expected zero** — they are different tapes. Same for the delta between the two `EXEC_FAMILY`
   values.
6. **Scorer fixtures** reproduce the 4 recorded stages: SDOT 9198, ZDAI 9185, UPC 9498, SLE 14003.
7. **Density** on JEM 2026-06-30 with an identical window and predicate. (Hydrated JEM
   13:30–14:30Z measured 155,059 ticks = 43 t/s; the often-quoted "63 t/s live" was measured on a
   *different* query shape and must be re-measured identically before the two are compared.)
8. **The first real bench:** the 8 lane-alive cases, arms `base` vs `sticky_backside_bench_off`,
   stride 1, `alpaca_spot`. Then read `report.md` line by line against `timeline.md`. If a line
   in the report has no supporting second in the timeline, the report is wrong.

---

## 9. The invariants, and the incident each is named after

`scripts/replay_harness_invariants.py` — pure, stdlib-only, no DB, no network, so a scorer, a
plan builder, a test or the driver itself can import it. Each raises `AssertionError` naming the
incident rather than returning a bool a caller can forget to check. **The rule is FAIL CLOSED**,
because a replay that measures silence, reads a contaminated sink, or downsamples the behaviour
under test still prints a clean number with a plus sign in front of it — and that looks like a
finding.

| Function | The incident it is named after |
|---|---|
| `assert_dense_stride(stride, question, *, max_stride=2)` | **2026-08-28 downsampling artifact.** Same symbol/window: **+$193.92 at stride 1, −$4.66 at stride 10**. The published "exit churn" finding was the stride. Armed only when the declared `BENCH_QUESTION` contains `exit`/`flow`/`bench`; an empty question asserts nothing, so legacy callers stay byte-identical |
| `assert_clean_sink(env)` | **2026-08-29 sink contamination.** A reused sink moved a MIMI baseline **+60.60 → +46.59 with no code change**, and nearly rejected the correct 0.6R-rung experiment (#1240) on a ghost kill inherited from a prior run. `REPLAY_KEEP_SINK` is not a shortcut |
| `interleave(cases, arms)` / `assert_interleaved(plan)` | The live source DB drifts under a 120-hour frame warm-up, so an all-A-then-all-B plan hands that drift to one arm. Requires contiguous, complete, non-repeating arms per case |
| `assert_as_of_reads(driver_src)` (AST) | With a trailing real-`now()` read the mirrored tape looks **EMPTY**, so the buyers-confirm gate and the forming-bar volume normalisation fail closed and the replay silently re-runs the exact bug live was already cured of. Also fences `REQUIRED_SIM_CLOCK_ANCHORS`: `schedule_window_now`, `signed_tape_accel_features`, `_utcnow_for_bars`, `name_spread_percentiles` |
| `assert_tie_stable_sql(driver_src)` (AST) | Rows sharing an `observed_at` (routine inside a burst) came back in **physical scan order**, so the same window mirrored in a different order and filled differently — an "A/B delta" that was really a heap-layout delta. Every tape SELECT must end `ORDER BY observed_at ASC, id ASC` |
| `verify_tree(build_dir, ref, sentinel_file, sentinel)` | A stale local branch — the fix was never checked out in the build tree — produced a confident **"the fix works"** result for code that was not in the run. Checks HEAD *and* greps a sentinel string, because HEAD alone does not prove the working tree carries the change |
| `cold_start_tags(events, runner_started_ts, min_ticks, min_uptime_s)` | **2026-09-02 (#1287 A/B).** A cold-start seed armed on the FIRST grid step against a frame HOD inherited from the warm-up and blocked a **+1.515 R** entry — the only delta in an otherwise identical 4-window A/B. Returns tags; it does not raise |
| `additive_count_check(previous, current, *, tolerance=0)` | 2026-08-29 again: post-run counts that GREW run-over-run were the visible surface of the sink contamination. Two identical runs on a clean sink produce identical counts |
| `assert_mock_parity(mock)` | `resting_limit_fills=False` caused exit-ladder submit **spam**; a non-conservative or uncapped mock over-credits fills the recorded tape could not have supplied. Either way the PnL is not comparable to any baseline. Checked at **startup**, read back off the instance |

The driver calls the startup subset (1, 2, 4, 5, 9) **before it loads a single tick**; the bench
runner calls the rest around each run.

> ⚠️ The plan also names `assert_nbbo_mirrored` and `assert_isolated_leader_board`. At the time
> this document was written they were **not** in `replay_harness_invariants.__all__`. `__all__`
> is the authoritative list — check it before citing an invariant as enforced.

### 9.1 The tenth layer: the NBBO mirror

Until 2026-09-04 the driver mirrored the trade tape and (since 08-26) the L2 book, but **never
`momentum_nbbo_spread_tape`** — while the FSM reads that table directly from the sink in three
places: `_build_micro_bar_df` (`live_runner.py:23426`, the 15 s micro-pullback frame, also the
exit block at `:45492`), the C1 IQFeed phantom-loss cross-check (`:24335`), and the adaptive
spread-cost veto's rolling spread percentiles (`:38885`). **Every replay before this branch ran
the micro-pullback detector and the spread veto against an empty table** — measuring silence, the
same defect the depth mirror fixed on 08-26.

Turning a silent read into a live one is not free. The mirror pre-loads the whole window at
`t=0`, so every reader must be as-of bounded or it reads the future. Two of the three already
were. `name_spread_percentiles` was not: measured on `chili_test` (60 min of tape, 30 min at
20 bps then 30 min at 400 bps, sim-now = +35 min) it returned **p50 = 210.0, n = 60** where the
as-of answer is **p50 = 20.0, n = 36**, and end-to-end through the real
`adaptive_spread_cost_veto_derate` the size multiplier moved **0.70 → 0.50** on identical input.
The look-ahead was making the gate *more permissive*. Worse, on a wall clock `since` walks off the
replayed day, so tape older than the 20-day lookback returns `None` and the gate fails open —
making the verdict depend on **the calendar date the bench runs**. Fixed in the SQL upper bound
and by a sim-clock re-point that is now a `REQUIRED_SIM_CLOCK_ANCHOR`.

---

## 10. Reading a result at 4 a.m.

Before you believe a number, check these in the receipt. Each has burned this program before.

1. `tree.dirty` is `false` and `tree.head` is the ref you meant. Otherwise stop.
2. `env.TICK_STRIDE` is 1 or 2, and `env.BENCH_QUESTION` is set for any exit/flow claim.
3. `sink_reset` is present and `env.REPLAY_KEEP_SINK` is unset.
4. `mirrored.nbbo_rows > 0` — a zero means the micro-pullback frame and the spread veto were
   reading silence.
5. `mirrored.depth_rows` — expect **0** on `chili_hydrated`; every depth-reading lever in that run
   is a no-op, not a neutral result.
6. `env.SOURCE_FILTER` is non-empty on a hydrated source. Empty means possible double-counted
   ticks from two providers.
7. `mock` equals the parity config (`resting_limit_fills: true`, `volume_cap_enabled: true`,
   `fill_mode: conservative`, `freshness_mode: wall`).
8. `certification_failures` contains `entry_risk_gate_bypassed` — it always should in Tier 1.
   Its presence is the reminder that this run cannot speak about admission.
9. `execution_family` / `venue` are the ones you intended, and you are not comparing them across
   the canon boundary.
10. `env.RISK` did **not** bind anything (§6). If the size looks wrong, look at `env.EQUITY` and
    at `MAXLOSS_USD` against the frozen $50 cap.
11. Every RPI figure in `rpi.json` has a numerator, a denominator and a case list. A bare
    percentage is a defect.
12. `report.md` says `admission_claim: false`, `causal_use_allowed: false`,
    `evidence_grade: DIAGNOSTIC_ONLY`, `leader_board_mode: isolated_single_symbol`.

---

## 11. Known limits, stated once, plainly

- **Tier 1 pretends admission.** Repeated here because it is the failure everyone makes twice.
- **The isolated leader board over-grants leader-gated exemptions** — one viability row makes the
  seeded name #1 by construction.
- **No depth in `chili_hydrated`** → depth exit levers are unmeasurable on this bench today.
- **NBBO vendor differs by month:** Jun/Jul hydration is IQFeed at-trade only
  (`iqfeed_lookup_bbo`); Polygon quotes exist only for Aug/Sep. The quote *between* prints is not
  visible in the earlier months. Reported per run as `nbbo_vendor`.
- **The Alpaca FSM path has never been exercised in replay.** The first `alpaca_spot` run may hit
  a network read (`alpaca_lists_symbol`) → `ReplayNetworkAccessError`. Add it to the
  neutralisation block **only** if it is purely a network call; otherwise stop and report.
- **ZDAI 2026-06-26**: the golden tape is dead (29 of 60,306 ticks — the pruner ate it during the
  rescue), and it is the largest frame-verified Ross loss of the month (−$25,431). Use the
  hydrated tape and state that the golden lineage is unavailable for that case.
- **Own-order impact and broker-vs-tape basis** — inherited from the v3 engine; see
  `docs/DESIGN/REPLAY_V3_LIVE_FSM_SIM.md` §12.1.
- **A Tier 1 divergence ledger is a hypothesis about levers, not a measurement of the gap.**
  The 71 % uptime classification was made from *recorded* evidence; the bench cannot confirm or
  refute it, because the harness cannot simulate `not_alive` / `not_admitted`. Those stay ops
  work (watchdogs, control-loop uptime, the discovery loop) and are measured on heartbeats.

---

## 12. Provenance of this document

Measured against this worktree on 2026-09-04 while writing: every count in §3 and §4 (recomputed
from `project_ws/AgentOps/ross/ross_master_ledger.json`), the invariant list and its incident
text (read from `scripts/replay_harness_invariants.py`), the receipt key list and the force-seed
sites (read from `scripts/replay_v3_fsm_window.py`), the frozen `$50` cap
(`app/services/trading/momentum_neural/replay_v3.py:201-205`), and the inertness of `RISK`
(grep: two occurrences, declaration and echo).

Not measured here, and taken from the approved plan and the cited incident reports: the live
latency percentiles (p10 3.9 s / p50 17.5 s / p90 162 s, n = 1,032), the 08-28 stride numbers, the
08-29 sink numbers, the #1287 cold-start result, the spread-percentile look-ahead measurement, and
the CLRO / JEM known-answer figures. Each is attributed inline. **If you are about to act on one
of them, re-measure it — that is the whole point of this instrument.**
