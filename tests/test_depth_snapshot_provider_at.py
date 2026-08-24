"""Ang depth snapshot ay dapat magdala ng QUOTE-EVENT clock (2026-08-24).

ANG PUWANG. Ang `observed_at` ng `iqfeed_depth_snapshots` ay oras ng BRIDGE
(`datetime.now()` sa snapshot writer), hindi ng exchange. Tama ang docstring ng
`get_execution_bbo`::

    "IQFeed Q/reference rows still cannot stand in: they carry no quote-event
     clock at all, and a trade-time proxy cannot authorize an order."

PERO ang `BookLevel` ay MAY tunay na `provider_at`, pinar-parse mula sa sariling
date+time field ng L2 line (`p[10], p[9]`) -- hindi lang ito naisusulat.

BAKIT ITO MAHALAGA, SINUKAT (2026-08-24): sa **136** na entry-side na BBO block,
**127 (93.4%)** ay may sariwang IQFeed depth quote sa **1.3s** na average edad.
Ang IQFeed ay **26-39 venue** laban sa IEX-only na entitlement ng Alpaca. Ang
BBO block ay puro PREMARKET -- eksaktong session kung saan kumikita si Ross.

⚠️ Ang column na ito ay EBIDENSYA, hindi authority. Ang paggamit nito bilang
execution stand-in ay hiwalay na trabaho at ENTRY-ONLY.

Runnable: pytest tests/test_depth_snapshot_provider_at.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

_BRIDGE = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "iqfeed_depth_bridge.py"
)


@pytest.fixture(scope="module")
def bridge():
    name = "_iqfeed_depth_bridge_pa"
    spec = importlib.util.spec_from_file_location(name, _BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass ay nagre-resolve sa pamamagitan ng sys.modules
    spec.loader.exec_module(mod)
    return mod


def _put(book, venue, side, px, sz, *, provider_at, seq):
    now = datetime.now(timezone.utc)
    return book.update(
        venue, side, px, sz,
        provider_at=provider_at,
        received_at=now,
        connection_generation=1,
        source_frame_sequence=seq,
        source_frame_sha256=f"{seq:064d}",
        condition_code="4",
    )


def test_the_snapshot_carries_a_provider_clock(bridge):
    b = bridge.Book()
    now = datetime.now(timezone.utc)
    _put(b, "EDGX", "B", 10.0, 100, provider_at=now - timedelta(seconds=2), seq=1)
    _put(b, "EDGX", "A", 10.1, 200, provider_at=now - timedelta(seconds=2), seq=2)
    snap = b.snapshot()
    assert snap is not None
    assert snap["provider_at"] is not None
    assert abs((now - snap["provider_at"]).total_seconds() - 2.0) < 0.5


def test_it_writes_the_OLDER_leg_not_the_newer(bridge):
    """⚠️ FAIL-CLOSED. Ang BBO ay isang PARES; kasing-sariwa lang ito ng
    pinakamatanda nitong bahagi. Ang pagsulat ng mas bago ay magpapalabas na mas
    sariwa ang pares kaysa sa totoo -- fail-OPEN sa isang gate na nagpapahintulot
    ng order."""
    b = bridge.Book()
    now = datetime.now(timezone.utc)
    _put(b, "EDGX", "B", 10.0, 100, provider_at=now - timedelta(seconds=9), seq=1)
    _put(b, "EDGX", "A", 10.1, 200, provider_at=now - timedelta(seconds=1), seq=2)
    snap = b.snapshot()
    age = (now - snap["provider_at"]).total_seconds()
    assert 8.0 < age < 10.0, f"dapat ang MAS LUMANG binti (9s), nakuha {age:.1f}s"


def test_a_missing_leg_clock_yields_none_not_a_half_truth(bridge):
    """Kung ang isang binti ay walang provider clock ay walang MAPAGKAKATIWALAANG
    pares na oras. Mas mabuti ang None kaysa sa oras ng isang binti lamang."""
    b = bridge.Book()
    now = datetime.now(timezone.utc)
    _put(b, "EDGX", "B", 10.0, 100, provider_at=None, seq=1)
    _put(b, "EDGX", "A", 10.1, 200, provider_at=now, seq=2)
    snap = b.snapshot()
    assert snap is not None, "ang snapshot mismo ay dapat gumana pa rin"
    assert snap["provider_at"] is None


def test_the_wire_format_of_the_ladders_is_unchanged(bridge):
    """Ang bids_json/asks_json ay [[price, size]] pa rin -- walang consumer na nasira."""
    b = bridge.Book()
    now = datetime.now(timezone.utc)
    _put(b, "EDGX", "B", 10.0, 100, provider_at=now, seq=1)
    _put(b, "ARCX", "B", 9.99, 300, provider_at=now, seq=2)
    _put(b, "EDGX", "A", 10.1, 200, provider_at=now, seq=3)
    snap = b.snapshot()
    for lad in (snap["bids_json"], snap["asks_json"]):
        for row in lad:
            assert len(row) == 2, f"[price, size] pa rin ang inaasahan, nakuha {row}"
            assert all(isinstance(x, (int, float)) for x in row)
    # ang mga aggregate ay dapat magsama pa rin sa lahat ng level
    assert snap["bid5_size"] == pytest.approx(400.0)
    assert snap["ask5_size"] == pytest.approx(200.0)
    assert snap["bid_top"] == pytest.approx(10.0)
    assert snap["bid_top_size"] == pytest.approx(100.0)


def test_the_insert_names_the_column(bridge):
    """Bantayan ang write path -- ang column ay walang silbi kung hindi naisusulat."""
    import inspect

    src = inspect.getsource(bridge)
    assert "bids_json, asks_json, provider_at)" in src
    assert '"pat": snap.get("provider_at")' in src


def test_migration_371_is_registered_and_additive():
    """Nullable + walang default ⇒ metadata-only sa PG11+, kaya INSTANT sa
    2.2 GB / 3.04M na row. Walang table rewrite sa isang buhay na tape."""
    import inspect

    from app import migrations as mg

    ids = [mid for mid, _fn in mg.MIGRATIONS]
    assert "371_depth_snapshot_provider_at" in ids
    assert len(ids) == len(set(ids)), "ang mga migration ID ay dapat natatangi"

    src = inspect.getsource(mg._migration_371_depth_snapshot_provider_at)
    # SQL LANG ang suriin -- ang docstring ay nagpapaliwanag KUNG BAKIT walang
    # NOT NULL/DEFAULT, kaya ang paghahanap ng mga salitang iyon sa buong source
    # ay tumutugma sa paliwanag mismo.
    sql = src[src.index("ALTER TABLE"):src.index('"""', src.index("ALTER TABLE"))]
    assert "ADD COLUMN IF NOT EXISTS provider_at" in sql
    assert "TIMESTAMPTZ" in sql.upper()
    assert "NOT NULL" not in sql.upper(), "ang NOT NULL ay magpupuwersa ng table rewrite"
    assert "DEFAULT" not in sql.upper(), "ang DEFAULT ay magpupuwersa ng table rewrite"
