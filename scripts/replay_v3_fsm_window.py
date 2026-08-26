"""ACCURATE FSM WINDOW REPLAY — drive the REAL live FSM (``tick_live_session``) across a
symbol's recorded tape for a chosen window, so entry triggers, exits, recycle and re-entry
run FRESH (not re-priced). Highest-fidelity momentum backtest we have: reuses the ``replay_v3``
engine (``ReplayV3Driver`` / ``seed_replay_session`` / ``MockBrokerAdapter``) with the validated
$0.05-fidelity mock config, feeds the REAL per-tick printed volume so resting limit orders fill
against actual traded volume, and neutralizes the network/venue guards + re-points the schedule
and tape clocks at the SIM clock (see the monkeypatch block).

READ-ONLY on the source DB (``chili``); the throwaway sim DB is ``chili_test`` (a dedicated
seeded session + ``source='replay_v3'`` ticks, cleaned each run — never point this at prod).

100% env-config so it is a clean A/B harness — flip any live flag between two runs of the SAME
window and diff PnL / entries / escalation count:

    PYTHONPATH=. DATABASE_URL=postgresql://chili:chili@localhost:5433/chili \
    TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test CHILI_PYTEST=1 \
    DIAG=1 FULL_MIRROR=1 ARM=on SYMBOL=CELZ TICK_STRIDE=8 GRID_STEP_S=1.0 \
    WIN_START=2026-06-30T12:35:00 WIN_END=2026-06-30T14:30:00 OHLCV_START=2026-06-30T12:35:00 \
    conda run -n chili-env python scripts/replay_v3_fsm_window.py

Env knobs: SYMBOL, WIN_START/WIN_END (replayed window, UTC-naive), OHLCV_START (as-of OHLCV
warm-up), FRAME_WARMUP_MIN (minutes of pre-WIN_START tape resampled into the OHLCV frames;
default 5d = the period the runner requests live; tape-bounded), TICK_STRIDE (downsample tape
1/N), GRID_STEP_S (sim grid), FULL_MIRROR (1=full-density streaming mirror — needed for
cadence + 5m higher-low), ARM (on/off/both), DIAG/ENTRY_DIAG/GRIND_DIAG (diagnostics).

⚠️ NINE GOTCHA-LAYERS cracked for parity (do NOT remove without re-checking): schedule_window_now
real-clock -> sim clock; signed_tape_accel_features as_of -> sim clock (else the mirrored tape
reads empty vs trailing now()); RH-401 pricebook -> None; stale_bbo -> freshness_mode='wall';
OOM on tick load -> streaming server-side-cursor mirror; SQL %% -> mod(); execution_family=
robinhood_agentic_mcp (not _spot); NormalizedFill .price/.size (not the ORDER's
average_filled_price); MTM the open position at window-end. See project_fsm_replay_instrument.
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

# READ-ONLY source DB (defaults to the local chili). SIM is the throwaway seeded DB (chili_test)
# — its name MUST end in _test as a guard against ever pointing the seeded/cleaned run at prod.
PROD = os.environ.get("DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili")
SIM = os.environ.get("TEST_DATABASE_URL", "postgresql://chili:chili@localhost:5433/chili_test")
if not SIM.rstrip("/").endswith("_test"):
    raise SystemExit(f"refusing to run: TEST_DATABASE_URL must end in _test (got {SIM!r})")

SYMBOL = os.environ.get("SYMBOL", "CELZ")
WIN_START = datetime.fromisoformat(os.environ.get("WIN_START", "2026-06-30T12:35:00"))
WIN_END = datetime.fromisoformat(os.environ.get("WIN_END", "2026-06-30T14:30:00"))
OHLCV_START = datetime.fromisoformat(os.environ.get("OHLCV_START", "2026-06-30T12:35:00"))
# P1 FRAME WARM-UP (2026-08-21 HUIZ shelf diagnostics): the OHLCV frames used to be
# resampled from ticks loaded at OHLCV_START (usually == WIN_START), so every frame started
# the window at ZERO bars — pullback_break_confirmation (10 bars) and the 25-bar
# momentum_volume ladder sat in "insufficient_bars" for most/all of the window and only the
# tick-stream fallback ever ran, while LIVE gets period="5d" frames from the providers with
# a full session (plus prior days) of depth. Frames now resample from a WIDER tick load that
# reaches back FRAME_WARMUP_MIN minutes before WIN_START (default = the 5d period the runner
# actually requests; tape-bounded — a symbol subscribed at ignition simply yields what
# exists). Printed volume, the SIM tick mirror and the driver grid are UNTOUCHED (still
# OHLCV_START/WIN_START-bounded); only the OHLCV provider seam gains depth.
FRAME_WARMUP_MIN = float(os.environ.get("FRAME_WARMUP_MIN", str(5 * 24 * 60)))
FRAME_START = min(OHLCV_START, WIN_START - timedelta(minutes=FRAME_WARMUP_MIN))
GRID_STEP_S = float(os.environ.get("GRID_STEP_S", "1.5"))
DIAG = os.environ.get("DIAG", "0") == "1"
ENTRY_DIAG = os.environ.get("ENTRY_DIAG", "0") == "1"
EQUITY = float(os.environ.get("EQUITY", "13000"))
RISK = float(os.environ.get("RISK", "130"))


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
        # P1 FRAME WARM-UP: the wider (stride-downsampled) load that feeds ONLY the
        # OHLCV provider, so frames carry real pre-window depth like live's 5d fetch.
        if FRAME_START < OHLCV_START:
            frame_ticks = pd.read_sql(text(
                "SELECT observed_at, price, size*:st AS size FROM ("
                "  SELECT observed_at, price, size, "
                "         row_number() OVER (ORDER BY observed_at) AS rn "
                "  FROM iqfeed_trade_ticks "
                "  WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND price>0"
                ") q WHERE mod(rn, :st) = 0 ORDER BY observed_at ASC"),
                c, params={"s": SYMBOL, "a": FRAME_START, "b": WIN_END, "st": stride})
        else:
            frame_ticks = ticks
    return nbbo, ticks, frame_ticks


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
    """FULL-DENSITY mirror WITHOUT loading all ticks into memory.

    GOTCHA 11 (2026-08-20): the original single server-side cursor held ONE
    read-only transaction on the SOURCE for the whole minutes-long mirror, and
    its ``query_start`` never advanced — so the window app's db_watchdog
    (kills idle-in-transaction > 10 min measured from query_start) TERMINATED
    the mirror mid-run twice ("administrator command"). The mirror now walks
    the window in 5-minute time slices, each in its OWN short transaction on
    both ends (commit after every slice), so no connection ever shows an old
    idle transaction. Same rows, same order (slices are contiguous and each is
    ORDER BY observed_at ASC), bounded memory per slice.
    """
    import psycopg2
    from psycopg2.extras import execute_values as _ev
    from datetime import timedelta as _td
    src = psycopg2.connect(PROD)
    src.set_session(readonly=True)
    dst = sim_engine.raw_connection()
    dcur = dst.cursor()
    # GOTCHA 11b (2026-08-20): row-by-row executemany took 15+ min for 164k
    # rows — long enough for an as-yet-unidentified backend terminator to kill
    # the mirror four times ("administrator command", no janitor logged it).
    # execute_values batches the VALUES lists (~10-50x faster), so the whole
    # mirror finishes in well under a minute and outruns whatever kills
    # long-lived replay connections.
    ins = ("INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, source) "
           "VALUES %s")
    total = 0
    slice_start = OHLCV_START
    while slice_start < WIN_END:
        slice_end = min(slice_start + _td(minutes=5), WIN_END)
        scur = src.cursor()
        scur.execute(
            "SELECT observed_at, price, size, bid, ask FROM iqfeed_trade_ticks "
            "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s AND price>0 ORDER BY observed_at ASC",
            (SYMBOL, slice_start, slice_end))
        batch = scur.fetchall()
        scur.close()
        src.commit()  # isara ang read tx — sariwa ang query_start sa susunod
        if batch:
            rows = [(SYMBOL, r[0], float(r[1]), float(r[2] or 0), r[3], r[4], 'replay_v3') for r in batch]
            _ev(dcur, ins, rows, page_size=5000)
            dst.commit()  # maiksi ang bawat SIM tx din
            total += len(rows)
            del rows
        del batch
        slice_start = slice_end
    dcur.close(); dst.close()
    src.close()
    return total


def mirror_depth_streaming(sim_engine):
    """FULL-DENSITY mirror ng L2 DEPTH -- ang nawawalang kalahati ng harness na ito.

    ⚠️ BAKIT ITO UMIIRAL (2026-08-26). Ang replay ay nagmi-mirror ng TRADE tape at
    NBBO, pero KAILANMAN ay hindi ng libro. Kaya ang buong pamilya ng exit lever na
    nagbabasa ng depth ay HINDI MASUSUKAT dito -- at LAHAT ng natitirang naka-OFF na
    exit lever ay nasa pamilyang iyon::

        exit_ladder_live             (ang "Ross ladder read")
        exit_ofi_hidden_seller_enabled
        exit_ofi_lock_partial_enabled
        exit_candle_confirm_live

    Napatunayan: ang isang A/B ng `exit_ladder_live` 0 laban sa 1 sa XPON 08-24 ay
    nagbigay ng EKSAKTONG parehong fill at parehong -67.74 -- ang flag ay walang
    kayang basahin, kaya wala itong magagawa.

    ANG DEADLOCK NA BINABASAG NITO: ang exit ladder ay naka-OFF sa live dahil
    "naghihintay ng A/B proof", at ang A/B harness ay hindi makapagbigay ng proof
    dahil walang depth. Nananatili itong naka-off nang hindi dahil pumalpak kundi
    dahil walang paraan para subukan -- habang ang nasukat na capture ratio ay
    18.5% (+108.35R na naabot, +20.02R lang ang nakuha sa 5 araw).

    ⚠️ KAPAREHONG DALAWANG GOTCHA ng tick mirror, at sinasadya iyon:
      * GOTCHA 11  -- 5-minutong slice, bawat isa sa SARILING maikling transaction
        sa magkabilang dulo, kaya walang koneksyon na nagpapakita ng lumang
        idle transaction sa db_watchdog (na pumapatay ng >10 min mula query_start).
      * GOTCHA 11b -- `execute_values` na batched, hindi row-by-row executemany.

    ⚠️ DALA ANG `provider_at` (migration 371) -- ang quote-event clock. Kung wala
    ito, ang anumang gate na sumusuri ng freshness ng libro ay makakakita ng NULL
    at fail-closed, at muli tayong susukat ng katahimikan sa halip na gawi.
    """
    import psycopg2
    from psycopg2.extras import execute_values as _ev, Json as _Json
    from datetime import timedelta as _td
    src = psycopg2.connect(PROD)
    src.set_session(readonly=True)
    dst = sim_engine.raw_connection()
    dcur = dst.cursor()
    ins = (
        "INSERT INTO iqfeed_depth_snapshots "
        "(symbol, observed_at, bid_top, ask_top, bid_top_size, ask_top_size, "
        " bid5_size, ask5_size, imbalance5, venues, source, bids_json, asks_json, provider_at) "
        "VALUES %s"
    )
    total = 0
    slice_start = OHLCV_START
    while slice_start < WIN_END:
        slice_end = min(slice_start + _td(minutes=5), WIN_END)
        scur = src.cursor()
        scur.execute(
            "SELECT observed_at, bid_top, ask_top, bid_top_size, ask_top_size, "
            "       bid5_size, ask5_size, imbalance5, venues, bids_json, asks_json, provider_at "
            "FROM iqfeed_depth_snapshots "
            "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s "
            "  AND bid_top>0 AND ask_top>0 ORDER BY observed_at ASC",
            (SYMBOL, slice_start, slice_end))
        batch = scur.fetchall()
        scur.close()
        src.commit()  # isara ang read tx -- sariwa ang query_start sa susunod
        if batch:
            rows = [
                # ⚠️ Ang bids_json/asks_json ay JSONB. Ibinabalik sila ng psycopg2
                # bilang Python list, at kung walang balot ay iniaadapt niya sila
                # bilang PG ARRAY -> DatatypeMismatch. Ang Json() ang nagsasabi
                # sa driver na jsonb ang patutunguhan.
                (SYMBOL, r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                 'replay_v3',
                 _Json(r[9]) if r[9] is not None else None,
                 _Json(r[10]) if r[10] is not None else None,
                 r[11])
                for r in batch
            ]
            _ev(dcur, ins, rows, page_size=5000)
            dst.commit()
            total += len(rows)
            del rows
        del batch
        slice_start = slice_end
    dcur.close(); dst.close()
    src.close()
    return total


class AsOfProvider:
    """As-of-t OHLCV from real ticks (no lookahead) — reads the sim clock via lr._utcnow().

    P1 PRODUCTION-PARITY FRAMES (2026-08-21 HUIZ shelf diagnostics), two fixes:
      * DEPTH — construct with the FRAME-warm-up tick load (see FRAME_WARMUP_MIN) so the
        1m/5m/15m frames carry the pre-window session depth live gets from period="5d",
        instead of starting the window at zero bars (insufficient_bars everywhere).
      * INDEX — keep the naive-UTC DatetimeIndex on the served frame. Live provider frames
        are datetime-indexed; reset_index(drop=True) silently disabled every timestamp read
        downstream — the forming-bar elapsed fraction (the 08-19 YJ volume-rate fix), the
        _today_session_frame session slice, bar-width detection — all fell back to their
        degraded paths ONLY in replay. As-of safety is unchanged (index <= sim-now)."""
    def __init__(self, ticks):
        t = ticks.copy()
        if not t.empty:
            t["observed_at"] = pd.to_datetime(t["observed_at"])
            t = t.set_index("observed_at").sort_index()
        self._t = t
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
        self._cache[ck] = bars
        return bars.copy()


def run_arm(label, grid, ticks, frame_ticks, g4_on):
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
    # GOTCHA 10 (2026-08-19): the shelf-registration damper primes its cache from
    # SEC EDGAR over HTTP. In a live lane that is a watch-start daemon thread, but
    # inside ReplayV3 the network guard blocks it, the module swallows the raised
    # ReplayNetworkAccessError, and the driver then aborts the whole run on the
    # bumped attempt_count — which is what broke this harness entirely:
    #   prime_shelf_cache -> _fetch_state -> _load_ticker_map -> _http_get_json
    # Neutralise the PRIMER only. cached_shelf_state stays untouched: it is
    # cache-only by construction, so it correctly returns "unknown" here and the
    # damper fails open exactly as it does live on a cold cache.
    try:
        from app.services.trading.momentum_neural import shelf_registration as _shelf

        _shelf.prime_shelf_cache = lambda *a, **k: None
    except Exception:
        pass
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
    # P1 FORMING-BAR CLOCK (2026-08-21): _forming_bar_elapsed_fraction measures how much of
    # the LAST bar has elapsed via entry_gates._utcnow_for_bars = the REAL wall clock. In
    # replay the frame's last bar sits at the RECORDED instant, so real-now reads every bar
    # as long complete and the volume-rate normalization (the 08-19 YJ measurement fix)
    # silently disables — replay then re-runs the exact bug live was cured of. Re-point it
    # at the SIM clock (naive UTC, the same awareness as the recorded frame index).
    _eg._utcnow_for_bars = lambda sample: lr._utcnow()
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
    db.commit()

    arm = rv3.RecordedArm(symbol=SYMBOL, live_eligible_at_utc=WIN_START.isoformat(),
                          viability_score=0.9, atr_pct=0.05)
    seed = rv3.seed_replay_session(db, arm=arm, execution_family="robinhood_agentic_mcp")
    # MAXLOSS_USD: experiment knob — the diagnostic seed freezes LEGACY_DIAGNOSTIC_POLICY_CAPS
    # ($50/trade) into the session snapshot; no setting reaches it. Rewriting the frozen cap
    # post-seed is the only lever that scales per-trade size without touching equity. Results
    # stay non-certifying (legacy_config_diagnostic) — dollar-scale exploration only.
    _maxloss_env = os.environ.get("MAXLOSS_USD")
    if _maxloss_env:
        _sess = db.get(TradingAutomationSession, seed.session_id)
        _rs = dict(_sess.risk_snapshot_json or {})
        _caps = dict(_rs.get("momentum_policy_caps") or {})
        _caps["max_loss_per_trade_usd"] = float(_maxloss_env)
        _rs["momentum_policy_caps"] = _caps
        _sess.risk_snapshot_json = _rs
        db.commit()
        print(f"[harness] MAXLOSS_USD override: frozen max_loss_per_trade_usd -> {float(_maxloss_env)}")
    # FULL-density streaming mirror (cadence + 5m higher-low need real tick density); falls back
    # to the in-memory downsampled mirror only if FULL_MIRROR=0.
    if os.environ.get("FULL_MIRROR", "1") == "1":
        db.commit()  # commit the seed first (streaming mirror uses its own raw connection)
        mirrored = mirror_ticks_streaming(eng)
        # ANG LIBRO (2026-08-26). Walang ito, ang bawat depth-reading na exit lever
        # ay tahimik na no-op at ang A/B ay sumusukat ng katahimikan.
        mirrored_depth = mirror_depth_streaming(eng)
        print("  mirrored_depth_rows=%s" % mirrored_depth)
    else:
        mirrored = mirror_ticks(db, ticks)
    db.commit()

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
    provider = AsOfProvider(frame_ticks)
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
    print(f"Loading {SYMBOL} tape ({WIN_START}..{WIN_END})...")
    nbbo, ticks, frame_ticks = load_prod()
    print(f"  nbbo_rows={len(nbbo)}  tick_rows={len(ticks)}  "
          f"frame_tick_rows={len(frame_ticks)} (warmup from {FRAME_START})")
    grid = build_grid(nbbo)
    print(f"  grid_steps(after {GRID_STEP_S}s downsample)={len(grid)}")
    if not grid:
        print("NO GRID — tape missing. Abort."); return
    arm = os.environ.get("ARM", "both")
    if arm == "on":
        on = run_arm(SYMBOL, grid, ticks, frame_ticks, g4_on=True)
        print(f"\n[ARM=on] G4 ON PnL {on[0]:+.2f} entries={on[1]} exits={on[2]} grind={on[3]} esc={on[4]}")
        return
    if arm == "off":
        off = run_arm(SYMBOL, grid, ticks, frame_ticks, g4_on=False)
        print(f"\n[ARM=off] G4 OFF PnL {off[0]:+.2f} entries={off[1]} exits={off[2]} grind={off[3]} esc={off[4]}")
        return
    on = run_arm(SYMBOL, grid, ticks, frame_ticks, g4_on=True)
    off = run_arm(SYMBOL, grid, ticks, frame_ticks, g4_on=False)
    print(f"\n================ FSM A/B RESULT ({SYMBOL}) ================")
    print(f"  G4 ON : PnL {on[0]:+.2f}  entries={on[1]} exits={on[2]} grind_evts={on[3]} esc_evts={on[4]}")
    print(f"  G4 OFF: PnL {off[0]:+.2f}  entries={off[1]} exits={off[2]} grind_evts={off[3]} esc_evts={off[4]}")
    print(f"  DELTA (ON - OFF) = {on[0]-off[0]:+.2f} USD")


if __name__ == "__main__":
    main()
