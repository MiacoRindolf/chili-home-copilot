"""Ang name-spread percentile ay dapat i-cache (2026-08-24).

BAKIT, SINUKAT SA PRODUKSYON. Ang `name_spread_percentiles` ay nagpapatakbo ng
`percentile_cont` sa LAHAT ng row ng simbolo sa loob ng
`chili_momentum_spread_norm_lookback_days` (**20 araw**) ng
`momentum_nbbo_spread_tape` -- isang **26 GB / 41.8M row** na table na isinusulat
sa TICK SPEED (41,525 sample kada 15 minuto para sa isang mainit na pangalan).
Ang percentile ay nangangailangan ng buong sort ng tumugmang set.

Nasukat 2026-08-24 18:00 UTC sa `pg_stat_activity`: **TATLONG sabay** na kopya ng
query na ito, bawat isa ay **67 segundo**, IO-bound -- habang ang
`momentum_live_runner_batch` (dapat kada 10s) ay umabot sa **28.5 segundo**.

Nasukat na epekto sa trigger latency (9 araw)::

    candidate  -> pending_place   p50   12.1s
    pending_place -> final_bbo    p50  110.0s   (p90 622s)

Halos DALAWANG MINUTO mula trigger hanggang paglalagay. Doon namamatay ang scalp:
ang `bid_prop_book_deteriorating` na veto ay **98.6% TUMPAK** -- lumalala na
talaga ang libro pagdating natin.

Ang 20-ARAW na distribution ay halos hindi nagbabago kada minuto. WALANG
nagbabagong semantiko: pareho pa ring query at resulta, kinukuwenta lang nang
bihira.

Runnable: pytest tests/test_spread_percentile_cache.py -v
"""
from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.services.trading.momentum_neural import spread_cost_veto as scv


class _CountingDb:
    """Binibilang ang mga DB read at nagbabalik ng naka-script na row."""

    def __init__(self, row):
        self.row = row
        self.calls = 0


def _install(monkeypatch, db):
    """Palitan ang optional_fetchone na ini-import ng function sa loob."""
    import app.services.trading.momentum_neural.optional_db_read as odr

    def _fake(_db, _sql, _params):
        db.calls += 1
        return db.row

    monkeypatch.setattr(odr, "optional_fetchone", _fake)


@pytest.fixture(autouse=True)
def _clear_cache():
    with scv._SPREAD_PCT_CACHE_LOCK:
        scv._SPREAD_PCT_CACHE.clear()
    yield
    with scv._SPREAD_PCT_CACHE_LOCK:
        scv._SPREAD_PCT_CACHE.clear()


def _sym() -> str:
    return f"Z{uuid.uuid4().hex[:5].upper()}"


GOOD_ROW = (263.65, 392.16, 482.2, 1882)


# ── ang pangunahing kontrata ───────────────────────────────────────────────

def test_the_second_call_does_not_touch_the_db(monkeypatch):
    """ANG BUONG PUNTO: isang 67-segundong scan, hindi isa kada tick."""
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    s = _sym()

    a = scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    b = scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    c = scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)

    assert db.calls == 1, f"dapat isang DB read lang, nakita {db.calls}"
    assert a == b == c
    assert a["p50"] == pytest.approx(263.65)


def test_the_cached_value_is_identical_not_approximate(monkeypatch):
    """WALANG nagbabagong semantiko -- eksaktong parehong resulta."""
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    s = _sym()
    first = scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    cached = scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    assert first == cached
    assert set(first) == {"p50", "p75", "p90", "n"}


def test_each_symbol_is_cached_separately(monkeypatch):
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    a, b = _sym(), _sym()
    scv.name_spread_percentiles(db, a, lookback_days=20.0, use_cache=True)
    scv.name_spread_percentiles(db, b, lookback_days=20.0, use_cache=True)
    scv.name_spread_percentiles(db, a, lookback_days=20.0, use_cache=True)
    scv.name_spread_percentiles(db, b, lookback_days=20.0, use_cache=True)
    assert db.calls == 2


def test_expiry_recomputes(monkeypatch):
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    s = _sym()
    scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    assert db.calls == 1
    # gawing luma ang entry lampas sa TTL
    ttl = scv._spread_pct_cache_ttl_s()
    key = scv._spread_pct_cache_key(s, 20.0)
    with scv._SPREAD_PCT_CACHE_LOCK:
        stamped, val = scv._SPREAD_PCT_CACHE[key]
        scv._SPREAD_PCT_CACHE[key] = (stamped - ttl - 1.0, val)
    scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    assert db.calls == 2, "ang expired na entry ay dapat muling kuwentahin"


# ── ang manipis na pangalan ay hindi dapat mag-rescan ─────────────────────

def test_a_none_result_is_also_cached(monkeypatch):
    """Ang manipis na pangalan ay hinding-hindi dapat mag-scan ng 20 araw kada tick."""
    db = _CountingDb(None)
    _install(monkeypatch, db)
    s = _sym()
    assert scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True) is None
    assert scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True) is None
    assert db.calls == 1, "ang None ay dapat naka-cache din"


def test_below_min_samples_is_cached_as_none(monkeypatch):
    db = _CountingDb((263.65, 392.16, 482.2, 2))  # n=2 < min_samples
    _install(monkeypatch, db)
    s = _sym()
    assert scv.name_spread_percentiles(db, s, lookback_days=20.0, min_samples=8, use_cache=True) is None
    assert scv.name_spread_percentiles(db, s, lookback_days=20.0, min_samples=8, use_cache=True) is None
    assert db.calls == 1


# ── kaligtasan ─────────────────────────────────────────────────────────────

def test_ttl_zero_disables_the_cache(monkeypatch):
    """OFF ⇒ byte-identical sa dating landas."""
    monkeypatch.setattr(
        settings, "chili_momentum_spread_norm_cache_ttl_seconds", 0.0, raising=False
    )
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    s = _sym()
    scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    assert db.calls == 2, "naka-disable ang cache -- bawat tawag ay tumatama sa DB"
    with scv._SPREAD_PCT_CACHE_LOCK:
        assert not scv._SPREAD_PCT_CACHE, "walang dapat maimbak kapag OFF"


def test_the_caller_cannot_mutate_the_cached_entry(monkeypatch):
    """Ang naibalik na dict ay KOPYA -- ang pagbabago nito ay hindi dapat lumason."""
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    s = _sym()
    first = scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    first["p50"] = -999.0
    second = scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    assert second["p50"] == pytest.approx(263.65), "nalason ang cache ng caller"


def test_the_cache_has_a_hard_size_cap(monkeypatch):
    """CLAUDE.md: ang cache ay dapat may hard max size + TTL."""
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    cap = scv._SPREAD_PCT_CACHE_MAX
    for _ in range(cap + 40):
        scv.name_spread_percentiles(db, _sym(), lookback_days=20.0, use_cache=True)
    with scv._SPREAD_PCT_CACHE_LOCK:
        assert len(scv._SPREAD_PCT_CACHE) <= cap, (
            f"lumampas ang cache sa hard cap: {len(scv._SPREAD_PCT_CACHE)} > {cap}"
        )


def test_the_ttl_setting_is_live_and_on():
    """Walang dark flag: naka-ON ito sa produksyon."""
    ttl = float(settings.chili_momentum_spread_norm_cache_ttl_seconds)
    assert ttl > 0, "ang cache ay dapat LIVE at ON"
    assert ttl <= 3600, "ang TTL ay dapat may hangganan"


def test_the_helper_does_not_cache_by_default(monkeypatch):
    """Ang default ay OPT-OUT: ang purong query helper ay sariwa kada tawag,
    kaya walang umiiral na caller o test ang tahimik na nagbabago."""
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    s = _sym()
    scv.name_spread_percentiles(db, s, lookback_days=20.0)
    scv.name_spread_percentiles(db, s, lookback_days=20.0)
    assert db.calls == 2, "default ⇒ walang cache"


def test_an_as_of_read_never_uses_the_cache(monkeypatch):
    """⚠️ REPLAY: ang cache ay naka-index sa WALL CLOCK. Ang paghain mula rito sa
    isang as-of na desisyon ay magpapakain ng live-time na distribution."""
    from datetime import datetime, timezone

    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    s = _sym()
    asof = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    scv.name_spread_percentiles(db, s, lookback_days=20.0, now_utc=asof, use_cache=True)
    scv.name_spread_percentiles(db, s, lookback_days=20.0, now_utc=asof, use_cache=True)
    assert db.calls == 2, "as-of ⇒ hinding-hindi naka-cache"
    with scv._SPREAD_PCT_CACHE_LOCK:
        assert not scv._SPREAD_PCT_CACHE


def test_different_lookbacks_are_cached_separately(monkeypatch):
    """⚠️ Magkaibang window = MAGKAIBANG statistic."""
    db = _CountingDb(GOOD_ROW)
    _install(monkeypatch, db)
    s = _sym()
    scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    scv.name_spread_percentiles(db, s, lookback_days=5.0, use_cache=True)
    scv.name_spread_percentiles(db, s, lookback_days=20.0, use_cache=True)
    assert db.calls == 2, "ang lookback ay dapat bahagi ng key"
