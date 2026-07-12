"""CLRO 2026-07-02 G4 exit A/B — the REAL live FSM (tick_live_session) driven across CLRO's
recorded tape, G4 grind-hold + re-entry escalation ON vs OFF. Reuses the replay_v3 engine
(ReplayV3Driver / seed_replay_session / MockBrokerAdapter). READ-ONLY on chili; the throwaway
sim DB is chili_test (a dedicated seeded session + source='replay_v3' ticks, cleaned each run).

Answers the operator's question: does grind-hold + escalation beat the +$13 scalp on the leg?
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.services.trading.momentum_neural.replay_v3 as rv3
import app.services.trading.momentum_neural.live_runner as lr
from app.services.trading.momentum_neural import market_profile as _mp
from app.services.trading.momentum_neural import risk_evaluator as _re
from app.services.trading.momentum_neural.replay_mock_broker import FillMode
from app.config import settings
from app.models.trading import TradingAutomationSession, TradingAutomationEvent

PROD = "postgresql://chili:chili@localhost:5433/chili"          # READ-ONLY source
SIM = os.environ.get("TEST_DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili_test")

SYMBOL = os.environ.get("SYMBOL", "CLRO")
# The grind window that carried the 6.48->7.27 leg (+$285 in the earlier counterfactual).
WIN_START = datetime.fromisoformat(os.environ.get("WIN_START", "2026-07-02T14:00:00"))
WIN_END = datetime.fromisoformat(os.environ.get("WIN_END", "2026-07-02T16:00:00"))
OHLCV_START = datetime.fromisoformat(os.environ.get("OHLCV_START", "2026-07-02T13:00:00"))
GRID_STEP_S = float(os.environ.get("GRID_STEP_S", "1.5"))
DIAG = os.environ.get("DIAG", "0") == "1"
ENTRY_DIAG = os.environ.get("ENTRY_DIAG", "0") == "1"
# EQUITY: env-overridable (2026-07-09) — 13000 mirrors the RH live account (the
# historical default); 100000 mirrors the Alpaca PAPER account for full-size runs.
# EXEC_FAMILY=alpaca_spot makes the paper full-size floor (#893) apply in-replay
# (the floor is gated on the alpaca_spot family + chili_alpaca_paper).
EQUITY = float(os.environ.get("EQUITY", "13000"))
RISK = float(os.environ.get("RISK", EQUITY * 0.01))
EXEC_FAMILY = os.environ.get("EXEC_FAMILY", "robinhood_agentic_mcp")


def _naive(t):
    return t.replace(tzinfo=None) if getattr(t, "tzinfo", None) else t


def load_prod():
    eng = create_engine(PROD)
    with eng.connect() as c:
        nbbo = pd.read_sql(text(
            "SELECT observed_at, bid, ask, mid FROM momentum_nbbo_spread_tape "
            "WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND bid>0 AND ask>=bid "
            "ORDER BY observed_at ASC"), c, params={"s": SYMBOL, "a": WIN_START, "b": WIN_END})
        # downsample ticks at the SQL level (keep every TICK_STRIDE-th) — the full CLRO run
        # window is 200k+ ticks (OOM risk); every 8th keeps ~30k, plenty for the 1m/5m resample
        # + forward-momentum slope direction. Volume is scaled back up by the stride so the
        # micro-frame volume magnitude stays approximately right.
        stride = int(os.environ.get("TICK_STRIDE", "8"))
        ticks = pd.read_sql(text(
            "SELECT observed_at, price, size*:st AS size, bid, ask FROM ("
            "  SELECT observed_at, price, size, bid, ask, "
            "         row_number() OVER (ORDER BY observed_at) AS rn "
            "  FROM iqfeed_trade_ticks "
            "  WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND price>0"
            ") q WHERE mod(rn, :st) = 0 ORDER BY observed_at ASC"),
            c, params={"s": SYMBOL, "a": OHLCV_START, "b": WIN_END, "st": stride})
    return nbbo, ticks


def build_grid(nbbo):
    """Downsample the recorded NBBO to ~GRID_STEP_S spacing -> the driver grid."""
    grid, last_t = [], None
    for _, r in nbbo.iterrows():
        t = _naive(pd.Timestamp(r["observed_at"]).to_pydatetime())
        if last_t is not None and (t - last_t).total_seconds() < GRID_STEP_S:
            continue
        last_t = t
        grid.append(rv3.RecordedNbboTick(ts=t, bid=float(r["bid"]), ask=float(r["ask"]),
                                         last=float(r["mid"]) if pd.notna(r["mid"]) else None))
    return grid


def build_printed_volume(grid, ticks):
    """Per-grid-tick printed volume = sum of trade-tick sizes in (prev_tick, this_tick].
    Feeds the mock's volume-cap fill model so resting limit orders fill against the REAL
    printed volume that traded in each window (the validated parity-fixture approach)."""
    if ticks.empty:
        return {}
    tv = ticks.copy()
    tv["observed_at"] = pd.to_datetime(tv["observed_at"]).map(_naive)
    tv = tv.sort_values("observed_at")
    vol = {}
    prev = None
    for gt in grid:
        if prev is None:
            lo = gt.ts - timedelta(seconds=GRID_STEP_S)
        else:
            lo = prev
        m = (tv["observed_at"] > lo) & (tv["observed_at"] <= gt.ts)
        vol[gt.ts] = float(tv.loc[m, "size"].sum()) if m.any() else 0.0
        prev = gt.ts
    return vol


def mirror_nbbo_streaming(sim_engine):
    """Mirror the prod NBBO/L1 window (momentum_nbbo_spread_tape) into the sink, per run.

    NBBO-GAP ROOT-CAUSE (2026-07-10): the harness DEPENDED on a sink NBBO table it never
    populated — it was only loaded at sink BUILD time (#890), so when the table went empty
    (rebuild/cleanup), `tape_confirms_hold`/`signed_tape_accel_features` failed CLOSED on the
    empty tape → escalation re-entries blocked → JEM +$15,034 → −$5,419 with NO code change
    (misattributed to two feature A/Bs). Bounded DELETE (symbol) + streaming re-mirror from
    prod per run makes the replay self-contained and immune to sink-table drift."""
    import psycopg2
    src = psycopg2.connect(PROD)
    src.set_session(readonly=True)
    scur = src.cursor(name="nbbo_mirror_stream")
    scur.itersize = 10000
    scur.execute(
        "SELECT observed_at, bid, ask, mid, spread_bps, day_volume, source "
        "FROM momentum_nbbo_spread_tape "
        "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s ORDER BY observed_at ASC",
        (SYMBOL, OHLCV_START, WIN_END))
    dst = sim_engine.raw_connection()
    dcur = dst.cursor()
    dcur.execute("DELETE FROM momentum_nbbo_spread_tape WHERE symbol=%s", (SYMBOL,))
    ins = ("INSERT INTO momentum_nbbo_spread_tape "
           "(symbol, observed_at, bid, ask, mid, spread_bps, day_volume, source) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)")
    total = 0
    while True:
        batch = scur.fetchmany(10000)
        if not batch:
            break
        rows = [(SYMBOL, r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in batch]
        dcur.executemany(ins, rows)
        total += len(rows)
        del batch, rows
    dst.commit()
    dcur.close(); dst.close()
    scur.close(); src.close()
    return total


def mirror_ticks(db, ticks):
    """Legacy in-memory mirror (downsampled ticks). Kept for the fallback path."""
    if ticks.empty:
        return 0
    ins = text("INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
               "VALUES (:sym,:at,:px,:sz,:bid,:ask,'replay_v3')")
    rows = [{"sym": SYMBOL, "at": _naive(pd.Timestamp(r["observed_at"]).to_pydatetime()),
             "px": float(r["price"]), "sz": float(r["size"]) if pd.notna(r["size"]) else 0.0,
             "bid": float(r["bid"]) if pd.notna(r["bid"]) else None,
             "ask": float(r["ask"]) if pd.notna(r["ask"]) else None} for _, r in ticks.iterrows()]
    for i in range(0, len(rows), 5000):
        db.execute(ins, rows[i:i+5000])
    db.flush()
    return len(rows)


def mirror_ticks_streaming(sim_engine):
    """FULL-DENSITY mirror WITHOUT loading all ticks into memory: a server-side cursor on the
    SOURCE (chili) reads batches, inserted into chili_test in batches, each batch freed. The
    cadence classifier + forward-momentum reads need FULL tick density (downsampling makes the
    tape look slow -> UNCERTAIN cadence + a broken 5m higher-low). No pandas, bounded memory."""
    import psycopg2
    src = psycopg2.connect(PROD)
    src.set_session(readonly=True)
    scur = src.cursor(name="mirror_stream")  # server-side cursor (streams, no full materialize)
    scur.itersize = 10000
    scur.execute(
        "SELECT observed_at, price, size, bid, ask FROM iqfeed_trade_ticks "
        "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s AND price>0 ORDER BY observed_at ASC",
        (SYMBOL, OHLCV_START, WIN_END))
    dst = sim_engine.raw_connection()
    dcur = dst.cursor()
    ins = ("INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
           "VALUES (%s,%s,%s,%s,%s,%s,'replay_v3')")
    total = 0
    while True:
        batch = scur.fetchmany(10000)
        if not batch:
            break
        rows = [(SYMBOL, r[0], float(r[1]), float(r[2] or 0), r[3], r[4]) for r in batch]
        dcur.executemany(ins, rows)
        total += len(rows)
        del batch, rows
    dst.commit()
    dcur.close(); dst.close()
    scur.close(); src.close()
    return total


class AsOfProvider:
    """As-of-t OHLCV from real ticks (no lookahead) — reads the sim clock via lr._utcnow().

    PREPEND_OHLCV=1 (2026-07-09 deeper-research fix): a late-subscribed tape starts
    MINUTES after the ignition, so tick-synthesized bars leave the detectors inside a
    <10-bar warmup dead zone at the exact actionable moment (VWAV dip 12:08-12:15 with
    tape from 12:06; CLRO-0702 curl with the 12:00-12:23 base missing) — LIVE would have
    had the FULL day's bars from the market-data providers. Prepend REAL historical 1m
    bars (yfinance, prepost) for the session BEFORE the first tick, so the replay's bar
    context matches what live saw. As-of safety: prepended bars are still filtered to
    index <= sim-now."""
    def __init__(self, ticks, pre_bars=None):
        t = ticks.copy()
        if not t.empty:
            t["observed_at"] = pd.to_datetime(t["observed_at"])
            t = t.set_index("observed_at").sort_index()
        self._t = t
        self._pre = pre_bars  # DataFrame indexed by naive-UTC minute, cols OHLCV; or None
        if self._pre is not None and not t.empty:
            # keep ONLY bars strictly before the first tick (tape owns everything after)
            self._pre = self._pre[self._pre.index < t.index[0]]
        self._rule = {"15m": "15min", "5m": "5min", "1m": "1min", "1d": "1D"}
        self._cache = {}

    def __call__(self, ticker, *, interval="1d", period="6mo"):
        now = _naive(lr._utcnow())
        rule = self._rule.get(str(interval), "5min")
        ck = (str(interval), int(now.timestamp() // 60))
        if ck in self._cache:
            return self._cache[ck].copy()
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        if self._t.empty:
            return empty
        sl = self._t[self._t.index <= now]
        if sl.empty:
            return empty
        o = sl["price"].resample(rule).ohlc()
        v = sl["size"].resample(rule).sum()
        bars = o.join(v.rename("Volume")).dropna()
        if bars.empty:
            return empty
        bars = bars.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
        bars = bars[["Open", "High", "Low", "Close", "Volume"]]
        # PREPEND real pre-tape session bars (as-of-bounded), resampled to the same rule.
        if self._pre is not None and not self._pre.empty:
            pre = self._pre[self._pre.index <= now]
            if not pre.empty:
                if rule != "1min":
                    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
                    pre = pre.resample(rule).agg(agg).dropna()
                bars = pd.concat([pre[pre.index < bars.index[0]], bars])
        bars = bars.reset_index(drop=True)
        self._cache[ck] = bars
        return bars.copy()


def run_arm(label, grid, ticks, g4_on):
    """Seed a fresh queued_live CLRO session + real ticks in SIM, run the REAL FSM over the
    grid with G4 flags on/off, mine the fills -> PnL + the grind/escalation event evidence."""
    settings.chili_momentum_g4_grind_exit_enabled = g4_on
    settings.chili_momentum_g4_reentry_escalation_enabled = g4_on
    settings.chili_momentum_live_runner_enabled = True
    # neutralize env-coupled gates orthogonal to the exit A/B (kill switch / broker-connectivity
    # / tradeable-now wall-clock) so the run isn't blocked by ops state — these do NOT touch the
    # entry-trigger geometry or the G4 exit machinery under test (same neutralization the
    # bespoke replay_v3_upc driver uses).
    lr._venue_broker_connected = lambda ef: True
    lr.is_kill_switch_active = lambda: False
    _re.is_kill_switch_active = lambda: False
    _re.get_kill_switch_status = lambda: {"active": False, "reason": None}
    _mp.is_tradeable_now = lambda symbol, **k: True
    # network guards: kill the real-venue marketdata reads the pending-entry / quote paths make
    # (the RH pricebook 401 that stalled the fill), and force all bars/quotes through the mock +
    # the replay OHLCV provider. Same set the bespoke replay_v3_upc driver installs.
    import app.services.trading.market_data as _md
    import app.services.trading.momentum_neural.universe as _uni
    import app.services.trading.momentum_neural.entry_features as _ef
    def _boom_fetch(*a, **k):
        raise AssertionError("NETWORK GUARD: real fetch_ohlcv_df during replay")
    def _boom_adapter(*a, **k):
        raise AssertionError("NETWORK GUARD: real live-spot adapter during replay")
    _md.fetch_ohlcv_df = _boom_fetch
    lr.resolve_live_spot_adapter_factory = _boom_adapter
    lr._entry_pricebook_snapshot = lambda symbol: None          # the RH-401 source
    lr._refetch_bbo_secondary = lambda symbol: None
    _uni.snapshot_dollar_volumes = lambda syms: {}
    _ef.macro_regime_features = lambda *a, **k: {}
    # THE placement blocker: schedule_window_now() defaults to datetime.now() (REAL wall-clock),
    # so during replay it returns "afterhours/closed" => sched_mult 0.0 => entry placement is
    # SKIPPED (live_entry_wait_late_window). Re-point it at the SIM clock (lr._utcnow(), frozen
    # to the replay instant by the driver's replay_clock) so it returns the window that was
    # ACTUALLY in effect at the recorded tick (CLRO 16:16Z = 12:16 ET = midday).
    _orig_swn = _mp.schedule_window_now
    _mp.schedule_window_now = lambda now=None, _o=_orig_swn: _o(now if now is not None else lr._utcnow())
    # TAPE AS-OF FIX (buyers-confirm validation): the entry-gate tape reads
    # (signed_tape_accel_features -> tape_confirms_hold / buyers_confirmed) use as_of=None in
    # tick_live_session, which in LIVE means the trailing real now(). In replay the mirrored ticks
    # live at the RECORDED instant, so a trailing-now() read finds NOTHING (empty -> fail-closed,
    # which would just DISABLE the gated touch triggers rather than test real buyer presence).
    # Re-point the tape read at the SIM clock (lr._utcnow()) when as_of is None so the buyers gate
    # reads the ACTUAL executed tape at the replayed instant — accurate buyers-confirmation.
    import app.services.trading.momentum_neural.entry_gates as _eg
    _orig_staf = _eg.signed_tape_accel_features
    def _staf_simclock(symbol, *, db=None, window_s=None, as_of=None, _o=_orig_staf):
        return _o(symbol, db=db, window_s=window_s,
                  as_of=(as_of if as_of is not None else lr._utcnow()))
    _eg.signed_tape_accel_features = _staf_simclock
    # FLOW-SLOPE AS-OF FIX (hold-into-rip validation): _live_flow_slope (ofi_level/ofi_slope from the
    # trade tape) uses as_of=None in tick_live_session -> trailing real now() -> empty in replay. The
    # Fix-G hold-into-rip gate reads it for the "buyers still accumulating" signal, so re-point it at
    # the SIM clock when as_of is None -> REAL OFI from the mirrored tape at the replayed instant.
    import app.services.trading.momentum_neural.pipeline as _pl
    _orig_lfs = _pl._live_flow_slope
    def _lfs_simclock(symbol, db=None, as_of=None, *a, _o=_orig_lfs, **k):
        return _o(symbol, db=db, as_of=(as_of if as_of is not None else lr._utcnow()), *a, **k)
    _pl._live_flow_slope = _lfs_simclock
    # LADDERING-VALUE MEASUREMENT (PYRAMID_OFI_SIM=1): the JEM/CELZ historical windows have only
    # ~486 sparse L2 depth rows, so _live_ofi_microprice returns None -> the pyramid's OFI
    # confirmation gate (paper_execution.py:844 fail-CLOSED) blocks EVERY add -> the add paths
    # can't be exercised in replay. To MEASURE the upper-bound VALUE of laddering (does adding to
    # the runner capture more?), simulate the LIVE condition where OFI is present: substitute a
    # modest POSITIVE OFI when the real read is None. This is a MEASUREMENT ONLY (not shipped) —
    # it over-fires vs live (ignores real negative OFI) so it gives the OPTIMISTIC ceiling: if
    # even this doesn't beat the trim-only baseline, laddering is not the lever.
    if os.environ.get("PYRAMID_OFI_SIM") == "1":
        import app.services.trading.momentum_neural.pipeline as _pl
        _orig_ofi = _pl._live_ofi_microprice
        def _ofi_sim(symbol, db=None, as_of=None, _o=_orig_ofi):
            v, e = _o(symbol, db=db, as_of=as_of)
            if v is None:
                return 0.35, (e if e is not None else 0.0)  # simulate a modest positive live OFI
            return v, e
        _pl._live_ofi_microprice = _ofi_sim
    # PROPOSED G4 FIX validation (GRIND_FIX=1): align grind ACTIVATION with MAINTENANCE + the
    # classifier's own semantics — accept UNCERTAIN cadence (which the classifier defaults to
    # "FAST/normal, no modulation" and which maintenance keeps as "NOT SLOW_CHOPPER"). Only
    # SLOW_CHOPPER / None still block. Implemented by promoting UNCERTAIN->FAST for the grind
    # decision only. All the OTHER strict gates (leader/1R/higher-low/EMA/floor/VWAP) unchanged.
    if os.environ.get("GRIND_FIX") == "1":
        import app.services.trading.momentum_neural.live_runner as _lr3
        _gmd0 = _lr3.grind_mode_decision
        def _gmd_fixed(*a, _o=_gmd0, **k):
            if str(k.get("cadence_cls") or "") == "UNCERTAIN":
                k = {**k, "cadence_cls": "FAST"}
            return _o(*a, **k)
        _lr3.grind_mode_decision = _gmd_fixed
    # GRIND DIAGNOSTIC: wrap grind_mode_decision to record WHY it (doesn't) activate on the
    # trailing ticks — the histogram of reasons + the max peak_r seen tells us if P1 should
    # have engaged (a real grind that grind-mode missed) or correctly stayed off.
    if os.environ.get("GRIND_DIAG") == "1":
        import app.services.trading.momentum_neural.live_runner as _lr2
        _grind_reasons = run_arm.__dict__.setdefault("_grind_reasons", {})
        _orig_gmd = _lr2.grind_mode_decision
        def _gmd_spy(*a, _o=_orig_gmd, **k):
            r = _o(*a, **k)
            try:
                key = f"{bool(r.get('active'))}:{r.get('reason')}"
                _grind_reasons[key] = _grind_reasons.get(key, 0) + 1
                pr = r.get("peak_r")
                if pr is not None:
                    _grind_reasons["_max_peak_r"] = max(_grind_reasons.get("_max_peak_r", -9), float(pr))
                _grind_reasons["_is_leader_true"] = _grind_reasons.get("_is_leader_true", 0) + (1 if k.get("is_day_leader") else 0)
                _cc = f"cadence={k.get('cadence_cls')!r}"
                _grind_reasons[_cc] = _grind_reasons.get(_cc, 0) + 1
            except Exception:
                pass
            return r
        _lr2.grind_mode_decision = _gmd_spy

    eng = create_engine(SIM)
    Sess = sessionmaker(bind=eng)
    db = Sess()
    # clean any prior replay_v3 ticks + stale seeded CLRO sessions
    db.execute(text("DELETE FROM iqfeed_trade_ticks WHERE source='replay_v3' AND symbol=:s"), {"s": SYMBOL})
    # VIABILITY-BOARD RESET (2026-07-10 contamination root-cause): viability rows accumulate
    # across runs (mixed freshness eras) and corrupt the day-leader read the escalation
    # ignition bypass keys on — the replayed symbol stops resolving as leader on a dirty
    # board. Each run starts from a CLEAN board: the seeded symbol is deterministically the
    # leader, and every replay is reproducible.
    db.execute(text("DELETE FROM momentum_symbol_viability"))
    try:
        db.execute(text("DELETE FROM momentum_viability_history"))
    except Exception:
        db.rollback()
    db.commit()

    arm = rv3.RecordedArm(symbol=SYMBOL, live_eligible_at_utc=WIN_START.isoformat(),
                          viability_score=0.9, atr_pct=0.05)
    seed = rv3.seed_replay_session(db, arm=arm, execution_family=EXEC_FAMILY)
    # FULL-SIZE seed override (2026-07-09): the seed hardcodes max_loss_per_trade_usd=50
    # (the P1 "sane small budget"); re-cap it at the documented 1%-of-EQUITY so the
    # replay sizes like the live paper lane (#893). Notional ceiling stays generous.
    _sess_seed = db.get(TradingAutomationSession, seed.session_id)
    _rs = dict(_sess_seed.risk_snapshot_json or {})
    _caps = dict(_rs.get("momentum_policy_caps") or {})
    _caps["max_loss_per_trade_usd"] = float(RISK)
    _caps["max_notional_per_trade_usd"] = float(EQUITY) * 4.0
    _rs["momentum_policy_caps"] = _caps
    _sess_seed.risk_snapshot_json = _rs
    db.commit()
    print(f"  seed caps: max_loss={_caps['max_loss_per_trade_usd']:.0f} notional_cap={_caps['max_notional_per_trade_usd']:.0f} family={EXEC_FAMILY}")
    # FULL-density streaming mirror (cadence + 5m higher-low need real tick density); falls back
    # to the in-memory downsampled mirror only if FULL_MIRROR=0.
    if os.environ.get("FULL_MIRROR", "1") == "1":
        db.commit()  # commit the seed first (streaming mirror uses its own raw connection)
        mirrored = mirror_ticks_streaming(eng)
    else:
        mirrored = mirror_ticks(db, ticks)
    db.commit()
    nbbo_mirrored = mirror_nbbo_streaming(eng)
    print(f"  nbbo_mirrored={nbbo_mirrored}")
    # VACUUM ANALYZE the freshly (re)loaded tape: each run's DELETE+INSERT leaves dead
    # tuples + stale stats, which flips the per-tick as-of reads from the btree to a
    # lossy BRIN bitmap (118ms -> 6s per call = a multi-hour window). Autocommit conn
    # (VACUUM can't run inside a transaction block).
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as _vc:
        _vc.execute(text("VACUUM ANALYZE iqfeed_trade_ticks"))
        _vc.execute(text("VACUUM ANALYZE momentum_nbbo_spread_tape"))

    # VALIDATED parity-fixture mock config ($0.05 fidelity, replay_parity.py:219): resting
    # limit orders (fill only when the recorded NBBO crosses), conservative adverse-side fills,
    # volume-capped partials, wall freshness (age~0 vs the sim clock). This is the accurate
    # setup — my earlier resting_limit_fills=False caused the exit-ladder submit spam.
    mock = rv3.MockBrokerAdapter(
        resting_limit_fills=True,
        volume_cap_enabled=True,
        fill_mode=FillMode.CONSERVATIVE,
        freshness_mode="wall",
    )
    # Feed the REAL per-tick printed volume so resting orders fill against actual traded volume
    # (the ReplayV3Driver sets clock+quote but NOT printed_volume — parity mode_i does; without
    # it, resting limits never fill). Wrap set_quote: after each quote, feed the bucket volume.
    _vol_by_ts = build_printed_volume(grid, ticks)
    _orig_set_quote = mock.set_quote
    def _set_quote_and_vol(pid, q, _o=_orig_set_quote):
        _o(pid, q)
        try:
            _t = mock._clock
            _v = _vol_by_ts.get(_t, 0.0)
            mock.set_printed_volume(pid, max(_v, 1.0))  # floor 1 so a marketable order can cross
        except Exception:
            pass
    mock.set_quote = _set_quote_and_vol
    pre_bars = None
    if os.environ.get("PREPEND_OHLCV", "0") == "1":
        # Real historical 1m session bars (premarket included) fetched ONCE at setup —
        # BEFORE the replay's network guard arms; converts yf's ET-aware index to naive UTC.
        #
        # PREPEND PIN (2026-07-12 determinism fix): the live yfinance fetch DRIFTS across
        # days (bar revisions / the sliding ~30-day 1m retention window) — the same JEM
        # window moved −697.07 → −692.73 overnight with ZERO code change, which poisons
        # cross-day A/B comparisons. First fetch per (symbol, session-date) is cached to
        # CSV; every later run replays the IDENTICAL bars. Delete the cache file to
        # deliberately refresh. Cache dir override: CHILI_REPLAY_PREPEND_CACHE_DIR.
        try:
            _cache_dir = os.environ.get(
                "CHILI_REPLAY_PREPEND_CACHE_DIR",
                r"D:\CHILI-Docker\chili-data\replay_prepend_cache",
            )
            os.makedirs(_cache_dir, exist_ok=True)
            _cache_fp = os.path.join(
                _cache_dir, f"{SYMBOL}_{WIN_START.date().isoformat()}_1m.csv"
            )
            _yfd = None
            if os.path.exists(_cache_fp):
                _yfd = pd.read_csv(_cache_fp, index_col=0, parse_dates=True)
                print(f"  PREPEND_OHLCV: cache HIT {_cache_fp} ({len(_yfd)} bars)")
            else:
                import yfinance as yf

                _d0 = WIN_START.date().isoformat()
                _d1 = (WIN_START.date() + timedelta(days=1)).isoformat()
                _yfd = yf.download(SYMBOL, start=_d0, end=_d1, interval="1m", prepost=True,
                                   progress=False, auto_adjust=False)
                if _yfd is not None and not _yfd.empty:
                    if isinstance(_yfd.columns, pd.MultiIndex):
                        _yfd.columns = [c[0] for c in _yfd.columns]
                    _yfd = _yfd[["Open", "High", "Low", "Close", "Volume"]].copy()
                    _yfd.index = pd.to_datetime(_yfd.index).tz_convert("UTC").tz_localize(None)
                    _yfd.to_csv(_cache_fp)
                    print(f"  PREPEND_OHLCV: cache MISS -> fetched + pinned {_cache_fp}")
            if _yfd is not None and not _yfd.empty:
                pre_bars = _yfd
                print(f"  PREPEND_OHLCV: {len(pre_bars)} real 1m bars loaded "
                      f"({pre_bars.index[0]} .. {pre_bars.index[-1]})")
        except Exception as _pe:
            print(f"  PREPEND_OHLCV failed ({_pe}); continuing tick-only")
    provider = AsOfProvider(ticks, pre_bars=pre_bars)
    driver = rv3.ReplayV3Driver(
        db, seed, mock=mock, ohlcv_provider=provider, grid=grid,
        risk_gate_allows=True,                 # short-circuit ONLY the pre-entry risk gate
        equity_provider=lambda *a, **k: EQUITY,
    )
    res = driver.run()

    # mine fills -> realized PnL (buys are cost, sells are proceeds; net of the mock's fees).
    # NormalizedFill (venue/protocol.py) fields: .side / .size / .price / .fee.
    fills, _ = mock.get_fills(limit=5000)
    def _pxsz(f):
        return float(getattr(f, "price", 0) or 0), float(getattr(f, "size", 0) or 0)
    buys = [_pxsz(f) for f in fills if str(f.side).lower() in ("buy", "bid", "long") and getattr(f, "price", None)]
    sells = [_pxsz(f) for f in fills if str(f.side).lower() in ("sell", "ask", "short") and getattr(f, "price", None)]
    cost = sum(p * q for p, q in buys)
    proceeds = sum(p * q for p, q in sells)
    # MARK-TO-MARKET any position still OPEN at window end (final_state trailing/entered/etc):
    # value the un-sold shares at the last grid bid (the honest liquidation value) so an
    # unclosed position is NOT counted as pure cost. net_open = bought - sold.
    net_open = sum(q for _, q in buys) - sum(q for _, q in sells)
    mtm = 0.0
    if net_open > 0.0001 and grid:
        last_bid = float(grid[-1].bid)
        mtm = net_open * last_bid
        proceeds += mtm
    pnl = proceeds - cost
    evs = [str(e.event_type) for e in db.query(TradingAutomationEvent)
           .filter(TradingAutomationEvent.session_id == seed.session_id)
           .order_by(TradingAutomationEvent.id.asc()).all()]
    grind_evts = [e for e in evs if "grind" in e.lower()]
    esc_evts = [e for e in evs if "escal" in e.lower()]

    # capture the session's entry-submit state BEFORE close (DIAG)
    _diag_rs = {}
    _entry_trace = []
    try:
        import json as _j
        _s = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.id == seed.session_id).one_or_none()
        _diag_rs = _j.loads(getattr(_s, "risk_snapshot_json", None) or "{}")
        # ENTRY-DECISION TRACE: the entry/fill/exit events with their trigger reason + ts,
        # so we see WHICH indicator fired for each entry, price-vs-VWAP, and HOLD duration
        # (Ross holds 1-2 min — is CHILI entering too early / on the wrong signal?).
        if ENTRY_DIAG:
            _evs = db.query(TradingAutomationEvent).filter(
                TradingAutomationEvent.session_id == seed.session_id).order_by(
                TradingAutomationEvent.id.asc()).all()
            for e in _evs:
                et = str(e.event_type)
                if any(k in et for k in ("entry_filled", "entry_candidate", "entry_submitted",
                                          "exit_filled", "partial_exit", "trail_ratchet",
                                          "ofi_exhaustion", "tape_accel_reversal", "sell_into_strength",
                                          "bailout", "stopped", "backside")):
                    pj = {}
                    try:
                        pj = _j.loads(e.payload_json) if isinstance(e.payload_json, str) else (e.payload_json or {})
                    except Exception:
                        pj = {}
                    _entry_trace.append((str(e.ts), et, pj.get("reason") or pj.get("trigger") or "",
                                         pj.get("price") or pj.get("fill_price") or pj.get("entry_price")))
    except Exception:
        _diag_rs = {}

    # cleanup this arm's rows
    db.execute(text("DELETE FROM iqfeed_trade_ticks WHERE source='replay_v3' AND symbol=:s"), {"s": SYMBOL})
    db.commit()
    db.close()

    print(f"\n===== {label} (G4 {'ON' if g4_on else 'OFF'}) =====")
    print(f"  grid_steps={len(grid)}  mirrored_ticks={mirrored}  final_state={res.final_state}")
    if DIAG:
        from collections import Counter
        print(f"  states_visited={res.states_visited}")
        reasons = Counter()
        for tk in res.ticks:
            r = tk.result or {}
            reasons[str(r.get("reason") or r.get("state") or r.get("blocked") or "ok")] += 1
        print(f"  top result reasons: {reasons.most_common(12)}")
        from collections import Counter as _C
        print(f"  event histogram: {_C(evs).most_common(15)}")
        # last 3 tick results (to see the pending-entry stall reason)
        for r in [tk.result for tk in res.ticks[-3:]]:
            print(f"  last_result: {dict(r)}")
        if os.environ.get("GRIND_DIAG") == "1":
            print(f"  GRIND decision histogram: {run_arm.__dict__.get('_grind_reasons', {})}")
        print(f"  le.entry_submitted={_diag_rs.get('entry_submitted')} "
              f"entry_order_id={_diag_rs.get('entry_order_id')} "
              f"entry_orders_resolved={_diag_rs.get('entry_orders_resolved')} "
              f"last_entry_block={_diag_rs.get('last_entry_block')} "
              f"pending_entry_submitted_at={_diag_rs.get('pending_entry_submitted_at_utc')}")
    print(f"  entries(buys)={len(buys)}  exits(sells)={len(sells)}  "
          f"net_open_shares={net_open:.0f}  mtm_value={mtm:+.2f} (@ last_bid {float(grid[-1].bid) if grid else 0:.2f})")
    if ENTRY_DIAG and _entry_trace:
        print(f"  --- ENTRY-DECISION TRACE (ts | event | reason | px) ---")
        for (ts, et, reason, px) in _entry_trace:
            print(f"    {ts[11:19]} | {et:32s} | {str(reason)[:34]:34s} | {px}")
    for i, (p, q) in enumerate(buys):
        print(f"    BUY  {q:.0f} @ {p:.4f}")
    for i, (p, q) in enumerate(sells):
        print(f"    SELL {q:.0f} @ {p:.4f}")
    print(f"  grind events: {len(grind_evts)}  {grind_evts[:3]}")
    print(f"  escalation events: {len(esc_evts)}  {esc_evts[:3]}")
    print(f"  >>> {label} PnL = {pnl:+.2f} USD")
    return pnl, len(buys), len(sells), len(grind_evts), len(esc_evts)


def main():
    print(f"Loading CLRO 07-02 tape ({WIN_START}..{WIN_END})...")
    nbbo, ticks = load_prod()
    print(f"  nbbo_rows={len(nbbo)}  tick_rows={len(ticks)}")
    grid = build_grid(nbbo)
    print(f"  grid_steps(after {GRID_STEP_S}s downsample)={len(grid)}")
    if not grid:
        print("NO GRID — tape missing. Abort."); return
    arm = os.environ.get("ARM", "both")
    if arm == "on":
        on = run_arm("CLRO-grind-window", grid, ticks, g4_on=True)
        print(f"\n[ARM=on] G4 ON PnL {on[0]:+.2f} entries={on[1]} exits={on[2]} grind={on[3]} esc={on[4]}")
        return
    if arm == "off":
        off = run_arm("CLRO-grind-window", grid, ticks, g4_on=False)
        print(f"\n[ARM=off] G4 OFF PnL {off[0]:+.2f} entries={off[1]} exits={off[2]} grind={off[3]} esc={off[4]}")
        return
    on = run_arm("CLRO-grind-window", grid, ticks, g4_on=True)
    off = run_arm("CLRO-grind-window", grid, ticks, g4_on=False)
    print("\n================ G4 A/B RESULT (CLRO 07-02) ================")
    print(f"  G4 ON : PnL {on[0]:+.2f}  entries={on[1]} exits={on[2]} grind_evts={on[3]} esc_evts={on[4]}")
    print(f"  G4 OFF: PnL {off[0]:+.2f}  entries={off[1]} exits={off[2]} grind_evts={off[3]} esc_evts={off[4]}")
    print(f"  DELTA (ON - OFF) = {on[0]-off[0]:+.2f} USD")


if __name__ == "__main__":
    main()
