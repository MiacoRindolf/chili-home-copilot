"""Hash-bound policy A/B of the real live FSM across a recorded tape window.

The operator-approved strategy policy has nine default-ON levers. Authoritative
batch invocations must provide the complete closed vector, its canonical hash,
and a matching named arm. Empty/default mappings and arbitrary ``FLAGS_JSON``
overrides are forbidden.
Derived from replay_window.py (all 9 replay gotchas retained). The archive source is
explicit and read-only; the throwaway sink comes from TEST_DATABASE_URL and must end
in ``_test``. SYMBOL/WIN_START/WIN_END/OHLCV_START are explicit inputs.

Direct invocation is fail-closed: GOLDEN=1, isolated config/launcher markers,
explicit source/sink/equity/risk/execution-family values, and the exact disposable
sink confirmation are required before any app module imports.

This driver starts from a synthetic ``queued_live`` session. It executes only the
post-selection FSM; universe/catalyst selection and the ordinary risk gate are
not replayed. That bounded execution scope is hash-bound and child-attested.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from diagnostic_replay_db import (  # noqa: E402
    guarded_database_identity,
    verify_connected_endpoint,
)

STRATEGY_POLICY_SCHEMA = "chili.replay-resolved-strategy-policy.v1"
EXECUTION_SCOPE_SCHEMA = "chili.replay-execution-scope.v1"
APPROVED_STRATEGY_FLAGS_BY_SLUG = (
    (
        "universe-float",
        "chili_momentum_universe_float_gate_enabled",
    ),
    (
        "orb-ihs-stop",
        "chili_momentum_orb_ihs_structural_stop_enabled",
    ),
    (
        "ross-stop",
        "chili_momentum_ross_stop_alignment_enabled",
    ),
    (
        "flush-dip-volume",
        "chili_momentum_flush_dip_volume_gate_enabled",
    ),
    (
        "tick-break-tape",
        "chili_momentum_tick_break_tape_confirm_enabled",
    ),
    (
        "catalyst-arb-flat",
        "chili_momentum_catalyst_arb_flat_gate_enabled",
    ),
    (
        "bail-no-confirm",
        "chili_momentum_bail_on_no_confirmation_enabled",
    ),
    (
        "fresh-ignition-reentry",
        "chili_momentum_fresh_ignition_reentry_bypass_enabled",
    ),
    (
        "sub-vwap-trap",
        "chili_momentum_sub_vwap_trap_entry_enabled",
    ),
)
POST_SELECTION_SCOREABLE_POLICY_FLAGS = (
    "chili_momentum_orb_ihs_structural_stop_enabled",
    "chili_momentum_ross_stop_alignment_enabled",
    "chili_momentum_flush_dip_volume_gate_enabled",
    "chili_momentum_tick_break_tape_confirm_enabled",
    "chili_momentum_bail_on_no_confirmation_enabled",
    "chili_momentum_fresh_ignition_reentry_bypass_enabled",
    "chili_momentum_sub_vwap_trap_entry_enabled",
)
POST_SELECTION_UNSCOREABLE_POLICY_FLAGS = (
    "chili_momentum_universe_float_gate_enabled",
    "chili_momentum_catalyst_arb_flat_gate_enabled",
)
UNSCOREABLE_POST_SELECTION_ARMS = (
    "intended-minus-universe-float",
    "intended-minus-catalyst-arb-flat",
)
REPLAY_NEUTRALIZED_SETTINGS = {
    "chili_momentum_squeeze_fuel_tilt_enabled": False,
}


def _resolve_strategy_policy(label: str) -> dict:
    flags = {
        flag: label != "base"
        for _, flag in APPROVED_STRATEGY_FLAGS_BY_SLUG
    }
    if label == "base":
        pass
    elif label == "intended":
        pass
    elif label.startswith("intended-minus-"):
        slug = label.removeprefix("intended-minus-")
        matches = [
            flag
            for candidate_slug, flag in APPROVED_STRATEGY_FLAGS_BY_SLUG
            if candidate_slug == slug
        ]
        if len(matches) != 1:
            raise RuntimeError("unknown resolved strategy policy label")
        flags[matches[0]] = False
    else:
        raise RuntimeError("unknown resolved strategy policy label")
    return {
        "schema": STRATEGY_POLICY_SCHEMA,
        "label": label,
        "flags": flags,
    }


def _reject_duplicate_policy_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise RuntimeError("duplicate resolved strategy policy key")
        out[key] = value
    return out


def _reject_nonfinite_policy_value(value: str):
    raise RuntimeError(f"non-finite strategy policy value {value}")


def _load_resolved_strategy_policy() -> tuple[dict, str]:
    raw = str(
        os.environ.get("CHILI_REPLAY_RESOLVED_STRATEGY_POLICY_JSON") or ""
    ).strip()
    expected_sha256 = str(
        os.environ.get("CHILI_REPLAY_RESOLVED_STRATEGY_POLICY_SHA256") or ""
    ).strip()
    if (
        not raw
        or len(expected_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in expected_sha256)
    ):
        raise RuntimeError("exact resolved strategy policy authority is required")
    if str(os.environ.get("FLAGS_JSON") or "").strip():
        raise RuntimeError("arbitrary FLAGS_JSON is forbidden in sealed replay")
    try:
        policy = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_policy_keys,
            parse_constant=_reject_nonfinite_policy_value,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("resolved strategy policy is not strict JSON") from exc
    if type(policy) is not dict or set(policy) != {"schema", "label", "flags"}:
        raise RuntimeError("resolved strategy policy must be an exact object")
    if type(policy.get("schema")) is not str or type(policy.get("label")) is not str:
        raise RuntimeError(
            "resolved strategy policy schema and label must be strings"
        )
    flags = policy.get("flags")
    expected_flag_names = {
        flag for _, flag in APPROVED_STRATEGY_FLAGS_BY_SLUG
    }
    if (
        type(flags) is not dict
        or set(flags) != expected_flag_names
        or any(type(value) is not bool for value in flags.values())
    ):
        raise RuntimeError(
            "resolved strategy policy flags must be the exact boolean vector"
        )
    expected = _resolve_strategy_policy(policy["label"])
    if policy != expected:
        raise RuntimeError("resolved strategy policy is not the closed vector")
    if policy["label"] in UNSCOREABLE_POST_SELECTION_ARMS:
        raise RuntimeError(
            f"{policy['label']} changes only a pre-selection flag that this "
            "queued_live replay does not execute"
        )
    canonical = json.dumps(
        policy,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    actual_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError("resolved strategy policy hash mismatch")
    if str(os.environ.get("ARM") or "").strip() != policy["label"]:
        raise RuntimeError("ARM does not match resolved strategy policy")
    return policy, actual_sha256


def _replay_execution_scope() -> dict:
    return {
        "schema": EXECUTION_SCOPE_SCHEMA,
        "label": "post-selection-fsm",
        "pipeline_start_state": "queued_live",
        "selection_pipeline_executed": False,
        "viability_input_mode": "synthetic_live_eligible_seed",
        "entry_risk_gate_executed": False,
        "ortex_entry_admission_mode":
            "neutralized_missing_captured_ortex_authority",
        "quote_freshness_clock_mode": "replay_sim",
        "neutralized_settings": dict(REPLAY_NEUTRALIZED_SETTINGS),
        "scoreable_policy_flags": list(POST_SELECTION_SCOREABLE_POLICY_FLAGS),
        "unscoreable_policy_flags":
            list(POST_SELECTION_UNSCOREABLE_POLICY_FLAGS),
        "profitability_scope": "post_selection_fsm_conditional",
        "whole_policy_profitability_allowed": False,
    }


def _load_execution_scope() -> tuple[dict, str]:
    raw = str(
        os.environ.get("CHILI_REPLAY_EXECUTION_SCOPE_JSON") or ""
    ).strip()
    expected_sha256 = str(
        os.environ.get("CHILI_REPLAY_EXECUTION_SCOPE_SHA256") or ""
    ).strip()
    if (
        not raw
        or len(expected_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in expected_sha256)
    ):
        raise RuntimeError("exact replay execution-scope authority is required")
    try:
        scope = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_policy_keys,
            parse_constant=_reject_nonfinite_policy_value,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("replay execution scope is not strict JSON") from exc
    expected = _replay_execution_scope()
    if type(scope) is not dict:
        raise RuntimeError("replay execution scope is not the closed document")
    canonical = json.dumps(
        scope,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected_canonical = json.dumps(
        expected,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical != expected_canonical:
        raise RuntimeError("replay execution scope is not the closed document")
    actual_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError("replay execution-scope hash mismatch")
    return scope, actual_sha256


PROD = str(os.environ.get("REPLAY_SOURCE_DATABASE_URL") or "").strip()
SIM = str(os.environ.get("TEST_DATABASE_URL") or "").strip()
if not PROD:
    raise RuntimeError(
        "REPLAY_SOURCE_DATABASE_URL is required; current/prod DB fallback is forbidden"
    )
if not SIM:
    raise RuntimeError(
        "TEST_DATABASE_URL is required; replay sink fallback is forbidden"
    )


SOURCE_IDENTITY = guarded_database_identity(PROD, sink=False)
SINK_IDENTITY = guarded_database_identity(SIM, sink=True)
if SOURCE_IDENTITY.server_key == SINK_IDENTITY.server_key:
    raise RuntimeError("replay source and disposable sink must differ")
if os.environ.get("GOLDEN") != "1":
    raise RuntimeError("GOLDEN=1 is required; current operational tables are forbidden")
if os.environ.get("CHILI_CAPTURED_PAPER_CONFIG_ISOLATED") != "true":
    raise RuntimeError("isolated config loading is required before app imports")
if os.environ.get("CHILI_DIAGNOSTIC_REPLAY_ISOLATED") != "true":
    raise RuntimeError("sealed diagnostic replay launcher marker is required")
if (
    os.environ.get("CHILI_REPLAY_TEST_SINK_CONFIRMATION")
    != "RESET_DISPOSABLE_REPLAY_TEST_SINK"
):
    raise RuntimeError("exact disposable replay sink confirmation is required")

_SECRET_PREFIXES = (
    "ALPACA",
    "APCA_",
    "ANTHROPIC",
    "IQFEED",
    "LLM_API",
    "MASSIVE",
    "OPENAI",
    "ORTEX",
    "POLYGON",
    "PREMIUM_API",
    "TELEGRAM",
)
_CHILI_SENSITIVE_TOKENS = (
    "ALPACA",
    "ANTHROPIC",
    "BROKER",
    "IQFEED",
    "LIVE_API",
    "MASSIVE",
    "OPENAI",
    "ORTEX",
    "POLYGON",
    "ROBINHOOD",
    "TELEGRAM",
)
_inherited_secrets = sorted(
    key
    for key in os.environ
    if key.upper().startswith(_SECRET_PREFIXES)
    or (
        key.upper().startswith("CHILI_")
        and any(token in key.upper() for token in _CHILI_SENSITIVE_TOKENS)
    )
)
if _inherited_secrets:
    raise RuntimeError(
        "provider/broker/LLM credentials are forbidden in diagnostic replay"
    )

_expected_build_sha = str(
    os.environ.get("CHILI_REPLAY_EXPECTED_BUILD_SHA") or ""
).strip()
_expected_driver_sha256 = str(
    os.environ.get("CHILI_REPLAY_EXPECTED_DRIVER_SHA256") or ""
).strip()
if (
    len(_expected_build_sha) not in {40, 64}
    or any(ch not in "0123456789abcdef" for ch in _expected_build_sha)
    or len(_expected_driver_sha256) != 64
    or any(ch not in "0123456789abcdef" for ch in _expected_driver_sha256)
):
    raise RuntimeError("exact replay build/driver authority is required")
with open(__file__, "rb") as _driver_file:
    _actual_driver_sha256 = hashlib.sha256(_driver_file.read()).hexdigest()
if _actual_driver_sha256 != _expected_driver_sha256:
    raise RuntimeError("replay driver bytes do not match authority")
_actual_build_sha = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=os.path.dirname(SCRIPT_DIR),
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
_dirty_build = subprocess.run(
    ["git", "status", "--porcelain"],
    cwd=os.path.dirname(SCRIPT_DIR),
    capture_output=True,
    text=True,
    check=True,
).stdout
if _actual_build_sha != _expected_build_sha or _dirty_build.strip():
    raise RuntimeError("replay driver requires the exact clean build authority")

(
    _RESOLVED_STRATEGY_POLICY,
    _RESOLVED_STRATEGY_POLICY_SHA256,
) = _load_resolved_strategy_policy()
(
    _RESOLVED_EXECUTION_SCOPE,
    _RESOLVED_EXECUTION_SCOPE_SHA256,
) = _load_execution_scope()

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

# Only the pinned diagnostic archive is reachable. Sink-side writes land in the
# explicitly confirmed disposable _test database.
GOLDEN = True
SRC_TICKS = "replay_golden_ticks"
SRC_NBBO = "replay_golden_nbbo"

def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


SYMBOL = _required_env("SYMBOL").upper()
WIN_START = datetime.fromisoformat(_required_env("WIN_START"))
WIN_END = datetime.fromisoformat(_required_env("WIN_END"))
OHLCV_START = datetime.fromisoformat(_required_env("OHLCV_START"))
GRID_STEP_S = float(os.environ.get("GRID_STEP_S", "1.5"))
DIAG = os.environ.get("DIAG", "0") == "1"
ENTRY_DIAG = os.environ.get("ENTRY_DIAG", "0") == "1"
EQUITY = float(_required_env("EQUITY"))
RISK = float(_required_env("RISK"))
EXEC_FAMILY = _required_env("EXEC_FAMILY")
if EQUITY <= 0 or RISK <= 0:
    raise RuntimeError("EQUITY and RISK must be positive")


def _naive(t):
    return t.replace(tzinfo=None) if getattr(t, "tzinfo", None) else t


def load_prod():
    # -c max_parallel_workers_per_gather=0: ang docker postgres ay may 64MB /dev/shm at
    # ang parallel gather sa golden tables ay humihingi ng 57-67MB DSM segments (DiskFull,
    # 2026-07-26 VRAX/VTAK/JLHL). Index-range reads ito — walang halaga ang parallelism.
    eng = create_engine(
        PROD, connect_args={"options": "-c max_parallel_workers_per_gather=0"})
    probe = eng.raw_connection()
    try:
        verify_connected_endpoint(probe, SOURCE_IDENTITY)
    finally:
        probe.close()
    with eng.connect() as c:
        c.execute(
            text(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
        )
        c.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        nbbo = pd.read_sql(text(
            "SELECT observed_at, bid, ask, mid FROM " + SRC_NBBO + " "
            "WHERE symbol=:s AND observed_at>=:a AND observed_at<:b AND bid>0 AND ask>=bid "
            "ORDER BY observed_at ASC, id ASC"), c, params={"s": SYMBOL, "a": WIN_START, "b": WIN_END})
        # downsample ticks at the SQL level (keep every TICK_STRIDE-th) — the full CLRO run
        # window is 200k+ ticks (OOM risk); every 8th keeps ~30k, plenty for the 1m/5m resample
        # + forward-momentum slope direction. Volume is scaled back up by the stride so the
        # micro-frame volume magnitude stays approximately right.
        stride = int(os.environ.get("TICK_STRIDE", "8"))
        ticks = pd.read_sql(text(
            "SELECT observed_at, price, size*:st AS size, bid, ask FROM ("
            "  SELECT observed_at, price, size, bid, ask, "
            "         row_number() OVER (ORDER BY observed_at, id) AS rn "
            "  FROM " + SRC_TICKS + " "
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
    src.set_session(readonly=True, isolation_level="REPEATABLE READ")
    verify_connected_endpoint(src, SOURCE_IDENTITY)
    with src.cursor() as preflight:
        preflight.execute("SET LOCAL TIME ZONE 'UTC'")
    scur = src.cursor(name="nbbo_mirror_stream")
    scur.itersize = 10000
    scur.execute(
        "SELECT observed_at, bid, ask, mid, spread_bps, day_volume, source "
        "FROM " + SRC_NBBO + " "
        "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s ORDER BY observed_at ASC, id ASC",
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
    src.set_session(readonly=True, isolation_level="REPEATABLE READ")
    verify_connected_endpoint(src, SOURCE_IDENTITY)
    with src.cursor() as preflight:
        preflight.execute("SET LOCAL TIME ZONE 'UTC'")
    scur = src.cursor(name="mirror_stream")  # server-side cursor (streams, no full materialize)
    scur.itersize = 10000
    scur.execute(
        "SELECT observed_at, price, size, bid, ask FROM " + SRC_TICKS + " "
        "WHERE symbol=%s AND observed_at>=%s AND observed_at<%s AND price>0 ORDER BY observed_at ASC, id ASC",
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
    had the FULL day's bars from the market-data providers. Prepend retained historical
    1m bars from the required local cache for the session BEFORE the first tick. As-of
    safety: prepended bars are still filtered to index <= sim-now."""
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


def _fmt_fill_qty(q: float) -> str:
    """FILL-line qty at 1e-10 quantization (trailing zeros stripped). The mock's
    volume-capped partials are FRACTIONAL shares; the old {q:.0f} print rounded
    each leg independently, so one split round-trip could parse as net ±1 share
    (phantom oversell → batch coverage_unavailable on a truly flat window). The
    batch inventory check budgets exactly this per-fill quantization."""
    s = f"{float(q):.10f}".rstrip("0").rstrip(".")
    return s or "0"


def run_arm(label, grid, ticks, flags):
    """Seed a fresh queued_live session + real ticks in SIM, run the REAL FSM over the
    grid with the given settings-flag overrides, mine the fills -> PnL + entry evidence."""
    for _fk, _fv in dict(flags or {}).items():
        setattr(settings, _fk, _fv)
    for _setting, _value in _RESOLVED_EXECUTION_SCOPE[
        "neutralized_settings"
    ].items():
        # The diagnostic archive has no captured Ortex-v2 admission receipt.
        # Neutralize only inside this isolated child and attest it as part of
        # the hashed post-selection execution scope; production defaults stay ON.
        setattr(settings, _setting, _value)
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

    # Parehong NaN/Inf->null JSON serializer ng app engine — ang FSM sink writes ay
    # dumadaan DITO, at tinatanggihan ng Postgres JSONB ang bare NaN token (ang CLRO
    # 07-07 vol_ratio crash x3 bago ito). Ang #953 fail-closed lever ang nag-aalis ng
    # NaN sa gates; ito ang backstop sa data layer.
    from app.db import _json_dumps_nan_safe as _nan_safe_dumps
    eng = create_engine(SIM, json_serializer=_nan_safe_dumps)
    _sink_probe = eng.raw_connection()
    try:
        verify_connected_endpoint(_sink_probe, SINK_IDENTITY)
    finally:
        _sink_probe.close()
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
    # VARIANT/RUN-STATE RESET (2026-07-24): seed_replay_session inserts a NEW active
    # replay_v3_* variant every run and nothing deletes them -> active variants
    # ACCUMULATE across runs -> variant resolution diverges from the session's own
    # variant -> viability_missing live_error, zero entries (the first run works, all
    # later runs die). Each run starts from a clean board: events/outcomes/sessions
    # first (FK direction), then the variants themselves.
    db.execute(text("DELETE FROM trading_automation_events"))
    try:
        db.execute(text("DELETE FROM momentum_fill_outcomes"))
    except Exception:
        db.rollback()
    db.execute(text("DELETE FROM trading_automation_sessions"))
    db.execute(text("DELETE FROM momentum_strategy_variants"))
    db.commit()

    arm = rv3.RecordedArm(symbol=SYMBOL, live_eligible_at_utc=WIN_START.isoformat(),
                          viability_score=0.9, atr_pct=0.05)
    seed = rv3.seed_replay_session(db, arm=arm, execution_family=EXEC_FAMILY)
    print(f"  replay_session_id={seed.session_id}")
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
    # PARALLEL 0: ang parallel vacuum workers ay nag-a-allocate ng DSM sa 64MB /dev/shm
    # ng docker postgres — ang 500k+-row mirrors ay humingi ng 57-67MB at namatay sa
    # DiskFull (2026-07-26). Serial vacuum = walang DSM, sapat ang bilis per-run.
    with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as _vc:
        _vc.execute(text("VACUUM (ANALYZE, PARALLEL 0) iqfeed_trade_ticks"))
        _vc.execute(text("VACUUM (ANALYZE, PARALLEL 0) momentum_nbbo_spread_tape"))

    # VALIDATED parity-fixture mock config ($0.05 fidelity, replay_parity.py:219): resting
    # limit orders (fill only when the recorded NBBO crosses), conservative adverse-side fills,
    # volume-capped partials, and replay-sim freshness. Wall freshness is not
    # causal for historical data and can make an old quote appear future/fresh
    # relative to ReplayV3's simulated clock.
    mock = rv3.MockBrokerAdapter(
        resting_limit_fills=True,
        volume_cap_enabled=True,
        fill_mode=FillMode.CONSERVATIVE,
        freshness_mode="sim",
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
        # Replay warmup is local-only. Missing or unreadable cache is a coverage
        # failure; there is no provider/network or silent tick-only fallback.
        _cache_dir = str(
            os.environ.get("CHILI_REPLAY_PREPEND_CACHE_DIR") or ""
        ).strip()
        if not _cache_dir:
            raise RuntimeError("CHILI_REPLAY_PREPEND_CACHE_DIR is required")
        _cache_fp = os.path.join(
            _cache_dir, f"{SYMBOL}_{WIN_START.date().isoformat()}_1m.csv"
        )
        if not os.path.isfile(_cache_fp):
            raise RuntimeError(
                f"PREPEND_OHLCV coverage unavailable: cache missing {_cache_fp}"
            )
        _expected_sha = str(
            os.environ.get("CHILI_REPLAY_PREPEND_CACHE_SHA256") or ""
        ).strip().lower()
        if not _expected_sha:
            raise RuntimeError("CHILI_REPLAY_PREPEND_CACHE_SHA256 is required")
        with open(_cache_fp, "rb") as _cache_file:
            _actual_sha = hashlib.sha256(_cache_file.read()).hexdigest()
        if _actual_sha != _expected_sha:
            raise RuntimeError("PREPEND_OHLCV coverage unavailable: cache hash mismatch")
        _yfd = pd.read_csv(_cache_fp, index_col=0, parse_dates=True)
        if _yfd.empty:
            raise RuntimeError("PREPEND_OHLCV coverage unavailable: cache is empty")
        _required_columns = {"Open", "High", "Low", "Close", "Volume"}
        if not _required_columns.issubset(_yfd.columns):
            raise RuntimeError("PREPEND_OHLCV coverage unavailable: invalid cache schema")
        if _yfd.index.hasnans or not _yfd.index.is_monotonic_increasing:
            raise RuntimeError("PREPEND_OHLCV coverage unavailable: invalid cache clock")
        pre_bars = _yfd
        print(f"  PREPEND_OHLCV: cache HIT {_cache_fp} ({len(pre_bars)} bars)")
        print(f"  PREPEND_OHLCV: {len(pre_bars)} real 1m bars loaded "
              f"({pre_bars.index[0]} .. {pre_bars.index[-1]})")
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
        # JSONB columns come back as dict already — json.loads(dict) raises TypeError and
        # silently killed this whole diag block (incl. the ENTRY-TRACE) via the except below.
        _raw_rs = getattr(_s, "risk_snapshot_json", None)
        _diag_rs = _raw_rs if isinstance(_raw_rs, dict) else _j.loads(_raw_rs or "{}")
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

    print(f"\n===== {label} flags={flags} =====")
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
        print(f"    BUY  {_fmt_fill_qty(q)} @ {p:.4f}")
    for i, (p, q) in enumerate(sells):
        print(f"    SELL {_fmt_fill_qty(q)} @ {p:.4f}")
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
    arm_name = _RESOLVED_STRATEGY_POLICY["label"]
    flags = dict(_RESOLVED_STRATEGY_POLICY["flags"])
    print(
        f"[STRATEGY_POLICY={arm_name}] "
        f"sha256={_RESOLVED_STRATEGY_POLICY_SHA256}"
    )
    print(
        f"[EXECUTION_SCOPE={_RESOLVED_EXECUTION_SCOPE['label']}] "
        f"sha256={_RESOLVED_EXECUTION_SCOPE_SHA256}"
    )
    print(f"  merged_flags={flags}")
    res = run_arm(f"{SYMBOL}-{arm_name}", grid, ticks, flags)
    print(f"\n[ARM={arm_name}] {SYMBOL} PnL {res[0]:+.2f} entries={res[1]} exits={res[2]}")


if __name__ == "__main__":
    main()
