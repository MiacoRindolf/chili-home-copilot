"""L1 trade bridge open-lag: ang drain ay may DETERMINISTIKONG ceiling (2026-09-02).

NASUKAT, bridge.err.log 2026-09-02 13:30-14:07Z (RTH open, CATCHUP=3600):

    06:31 PT  n=12 batches  rows=21,611 quotes=21,582  avg insert=3,012ms release=1,840ms
    06:33 PT  n=13 batches  rows=23,496 quotes=23,300  avg insert=2,837ms release=1,750ms
    06:47 PT  n=13 batches  rows=33,393 quotes=13,400  avg insert=3,480ms release=1,096ms
    plateau: 720-780 retained events / wall second, backlog=True 13:30:30Z-13:40:07Z
             at 13:43:27Z-14:07:39Z (Codex sealed record: frontier 106s, 131s, 266s)

Tatlong DB-free na katotohanan ang naka-pin dito:
  1. Kada trade frame ay DALAWANG retained event (print + mandatory provenance
     quote), kaya sa 3,600 na budget ay 1,800 print lang kada drain.
  2. Ang serial na loop (insert → release → susunod na drain) ay may ceiling na
     max_events / batch_seconds; lampas doon ay lumalaki ang lag kahit ano pa.
  3. Ang kasalukuyang insert ay IISANG statement na may isang bind parameter
     kada cell (16 kada trade, 17 kada quote) — 59,400 bind sa isang 3,600 batch —
     na siyang sinusukat ng benchmark bilang client-side na gastos.

Runnable: pytest tests/test_iqfeed_drain_capacity.py -v
"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from scripts.iqfeed_drain_capacity import (
    DrainCostModel,
    fit_per_event_cost,
    prints_per_batch_under_backlog,
)

_BRIDGE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "iqfeed_trade_bridge.py"
_spec = importlib.util.spec_from_file_location("iqfeed_trade_bridge_draincap", _BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_spec)
if "iqfeed_trade_bridge_draincap" not in sys.modules:
    sys.modules["iqfeed_trade_bridge_draincap"] = bridge
    _spec.loader.exec_module(bridge)
else:  # pragma: no cover
    bridge = sys.modules["iqfeed_trade_bridge_draincap"]


def _frame(sym: str, seq: int) -> dict:
    return {"sym": sym, "connection_generation": 1, "source_frame_sequence": seq}


def _seed(trades, quotes):
    with bridge._pending_lock:
        bridge._pending.clear()
        bridge._pending_nbbo.clear()
        bridge._pending.extend(trades)
        bridge._pending_nbbo.extend(quotes)


# ── 1. ang budget ay binabayaran nang doble ng bawat print ───────────────────


def test_a_trade_frame_costs_two_retained_events_so_prints_are_half_the_budget():
    """3,000 trade frames (bawat isa may kapares na quote) sa 3,600 na budget →
    1,800 print + 1,800 mandatory quote, backlog=True. Ito ang dahilan kung bakit
    ang '3,600-event batch' ay 1,800 print lang kada 4-6 s sa open."""
    trades = [_frame(f"S{i % 480}", i + 1) for i in range(3000)]
    quotes = [_frame(f"S{i % 480}", i + 1) for i in range(3000)]
    _seed(trades, quotes)
    got_trades, got_quotes, backlog = bridge._drain_pending_write_batch(
        max_events=3600, hot_symbols=set(), collapse_hot_quotes=True,
    )
    assert len(got_trades) == 1800
    assert len(got_quotes) == 1800, "ang trade-frame quote ay mandatory (provenance pairing)"
    assert backlog is True
    assert prints_per_batch_under_backlog(3600, 3000) == 1800


def test_quote_only_frames_do_not_eat_the_print_budget_under_collapse():
    """Kontrol: 3,000 quote-only frames ng 480 symbols ay 480 retained event lang,
    kaya buo pa ang budget para sa prints na nasa likod nila."""
    quotes = [_frame(f"S{i % 480}", i + 1) for i in range(3000)]
    trades = [_frame(f"T{i}", 3000 + i + 1) for i in range(1000)]
    _seed(trades, quotes)
    got_trades, got_quotes, _ = bridge._drain_pending_write_batch(
        max_events=3600, hot_symbols={f"S{i}" for i in range(480)}, collapse_hot_quotes=True,
    )
    assert len(got_trades) == 1000
    assert len(got_quotes) == 480


# ── 2. serial loop ceiling ───────────────────────────────────────────────────


_OPEN_PROFILE_SAMPLES = [
    # (retained events, insert+release seconds) mula sa drain-profile 2026-09-02
    (789, 1.203), (945, 1.172), (1021, 1.110), (1016, 1.531), (2421, 3.000),
    (2712, 4.156), (3600, 5.047), (3599, 4.063), (3600, 5.312), (3600, 4.688),
    (3599, 4.281), (3599, 5.062), (3600, 4.469), (3599, 4.937), (3599, 4.625),
]


def test_measured_open_profile_gives_a_sub_800_event_per_second_ceiling():
    model = fit_per_event_cost(_OPEN_PROFILE_SAMPLES)
    assert 1.0e-3 <= model.per_event_s <= 1.6e-3, model
    capacity = model.steady_state_capacity(3600)
    assert 650 <= capacity <= 850, f"ceiling {capacity:.0f} events/s"
    # 2026-08-27 close burst measured ~3,285 trades/s (see A-2 note in the bridge):
    growth = model.lag_growth_per_wall_second(3285 * 2, 3600)
    assert growth > 0.85, "sa burst ay lumalaki ang lag ng >0.85 s kada s"


def test_ceiling_is_linear_in_batch_size_so_raising_catchup_cannot_fix_it():
    """Kapag per-event ang gastos, ang mas malaking batch ay HINDI nagpapataas ng
    throughput — pareho ang ceiling sa 2048, 3600 at 3640 (ang bind-param cap)."""
    model = DrainCostModel(fixed_s=0.05, per_event_s=1.3e-3)
    c2048 = model.steady_state_capacity(2048)
    c3600 = model.steady_state_capacity(3600)
    c3640 = model.steady_state_capacity(3640)
    assert c3640 - c2048 < 0.02 * c2048
    assert abs(c3640 - c3600) < 1.0
    assert model.lag_growth_per_wall_second(700, 3600) == 0.0
    assert model.lag_growth_per_wall_second(1400, 3600) == pytest.approx(1.0 - c3600 / 1400)


def test_fit_handles_degenerate_inputs():
    with pytest.raises(ValueError):
        fit_per_event_cost([])
    single = fit_per_event_cost([(3600, 4.7)])
    assert single.fixed_s == 0.0 and single.per_event_s == pytest.approx(4.7 / 3600)
    assert DrainCostModel(0.0, 1e-3).steady_state_capacity(0) == 0.0
    assert prints_per_batch_under_backlog(0, 10) == 0


# ── 3. ang insert statement ay isang bind kada cell ──────────────────────────


class _CapturingConnection:
    """Fake na SQLAlchemy connection: kinukuha ang statement, hindi nag-e-execute."""

    def __init__(self):
        self.statements = []

    def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)

        class _Result:
            rowcount = -1

            def scalars(self_inner):
                return iter(range(1, 1 + _n))

        _n = len(statement.compile(dialect=postgresql.dialect()).params) // 16
        return _Result()


def _row(i: int, *, quote: bool) -> dict:
    at = datetime.now(timezone.utc)
    base = {
        "sym": f"S{i % 480}", "at": at.replace(tzinfo=None), "bid": 9.99, "ask": 10.01,
        "provider_at": at, "received_at": at, "basis": "b", "bridge": "v", "message_type": "Q",
        "bridge_run_id": "run", "connection_generation": 1, "source_frame_sequence": i + 1,
        "provider_trade_reference_at": at,
        "source_frame_sha256": hashlib.sha256(str(i).encode()).hexdigest(),
    }
    if quote:
        return {**base, "mid": 10.0, "spread_bps": 20.0}
    return {**base, "px": 10.0, "sz": 100.0}


def test_current_insert_is_one_statement_with_one_bind_parameter_per_cell():
    trades = [_row(i, quote=False) for i in range(50)]
    quotes = [_row(i, quote=True) for i in range(40)]
    fake = _CapturingConnection()
    bridge._insert_pending_batch(fake, trade_rows=trades, quote_rows=quotes, return_row_ids=False)
    assert len(fake.statements) == 2, "isang statement kada tape"
    trade_sql, quote_sql = (s.compile(dialect=postgresql.dialect()) for s in fake.statements)
    assert len(trade_sql.params) == 50 * 16
    # 18 cells kada quote row, pero ang day_volume=None ay NULL literal → 17 bind.
    assert len(quote_sql.params) == 40 * 17
    assert "INSERT INTO iqfeed_trade_ticks" in str(trade_sql)
    assert "VALUES" in str(trade_sql) and "CAST(" in str(trade_sql)
    # Sa CATCHUP=3600 (1,800 + 1,800): 28,800 + 30,600 = 59,400 bind sa isang drain
    # (sinukat: ~1.2 s ng SQLAlchemy compile kada drain, walang cache reuse dahil
    # iba ang data kada batch).
    assert 1800 * 16 + 1800 * 17 == 59_400
    assert bridge.DB_RELEASE_CATCHUP_BATCH_EVENTS * 18 < 65_535
