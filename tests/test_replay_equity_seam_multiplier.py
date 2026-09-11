"""[E] (2026-09-11): the replay equity seam serves the PINNED broker multiplier, and the
bench freezes the ceiling live would freeze.

Two defects, one measurement:
  * ``risk_policy._notional_ceiling_basis`` answered ``multiplier = 1.0`` for every replay,
    so a 13,000 canon account derived a 13,000 ceiling where live (Alpaca ``multiplier``
    4.0) derives 52,000.
  * and the bench never even reached that seam: the replay seed freezes
    ``LEGACY_DIAGNOSTIC_POLICY_CAPS`` (``max_notional_per_trade_usd = 100,000``) and the
    runner never rebuilds the admission snapshot, so every bench entry was sized under a
    100,000 literal (crossover 390 / 100,000 = 0.39%) with ``notional_ceiling_source =
    unrecorded``. ``replay_v3_fsm_window._freeze_live_notional_ceiling`` now freezes
    ``equity_relative_notional_cap_with_meta`` under the seam, where live freezes it.

Canon: equity 13,000 / loss 3% = 390 / broker multiplier 4.0 (live admission receipts,
sessions 22151-22163, 2026-09-11) -> ceiling min(52,000, 390 / 0.003 = 130,000) = 52,000,
crossover 390 / 52,000 = 0.0075 -- the same 0.75% the live lane freezes.

Runnable: pytest tests/test_replay_equity_seam_multiplier.py -v   (DB-free)
"""
from __future__ import annotations

import math
import pathlib
import sys
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import risk_policy as rp

_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import replay_live_pins as pins_mod  # noqa: E402

CANON_EQUITY = 13_000.0
CANON_LOSS_FRACTION = 0.03
LIVE_MULTIPLIER = 4.0


def _canon(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.0)
    monkeypatch.setattr(settings, "chili_momentum_risk_loss_fraction_of_equity", CANON_LOSS_FRACTION)
    # a replay must never reach the broker
    monkeypatch.setattr(rp, "_alpaca_account_cached", lambda: pytest.fail("broker read under replay"))


def test_seam_default_multiplier_unchanged_1_0(monkeypatch) -> None:
    """Every existing replay installs a bare callable: same (basis, 1.0, name) as before."""
    _canon(monkeypatch)
    with rp.replay_account_equity(lambda *_a, **_k: CANON_EQUITY):
        basis = rp._notional_ceiling_basis("alpaca_spot")
    assert basis == (CANON_EQUITY, 1.0, "replay_equity_seam", CANON_EQUITY)
    assert rp.replay_seam_multiplier(lambda *_a, **_k: 1.0) == (1.0, "replay_equity_seam")


def test_seam_serves_pinned_broker_multiplier_and_names_source(monkeypatch) -> None:
    _canon(monkeypatch)
    provider = pins_mod.ReplayEquityProvider(CANON_EQUITY, LIVE_MULTIPLIER, "broker_multiplier")
    with rp.replay_account_equity(provider):
        basis = rp._notional_ceiling_basis("alpaca_spot")
    assert basis == (
        CANON_EQUITY, LIVE_MULTIPLIER, "replay_equity_seam:broker_multiplier_pinned", CANON_EQUITY,
    )
    assert provider() == CANON_EQUITY  # still the equity seam's callable


@pytest.mark.parametrize("bad", [0.5, float("nan"), float("inf"), "four"])
def test_an_unusable_pinned_multiplier_is_never_used_silently(bad) -> None:
    provider = SimpleNamespace(replay_multiplier=bad, replay_multiplier_source="broker_multiplier")
    assert rp.replay_seam_multiplier(provider) == (1.0, "replay_equity_seam:pinned_multiplier_invalid")


def test_canon_crossover_13000_390_mult4_is_0_0075(monkeypatch) -> None:
    """The number the whole A/B is sized by, end to end through the frozen receipt."""
    _canon(monkeypatch)
    provider = pins_mod.ReplayEquityProvider(CANON_EQUITY, LIVE_MULTIPLIER, "broker_multiplier")
    with rp.replay_account_equity(provider):
        usd, meta = rp.equity_relative_notional_cap_with_meta(
            100_000.0, "alpaca_spot", loss_fixed_fallback_usd=390.0,
        )
    assert usd == pytest.approx(52_000.0)
    assert meta["source"] == "replay_equity_seam:broker_multiplier_pinned"
    assert meta["binding"] == "buying_power"
    assert meta["multiplier"] == pytest.approx(4.0)
    assert meta["loss_usd"] == pytest.approx(390.0)
    assert meta["loss_bound_usd"] == pytest.approx(390.0 / rp.RISK_FIRST_STOP_FLOOR_PCT)
    assert meta["crossover_stop_pct"] == pytest.approx(0.0075)
    assert meta["halt_to_zero_exposure_frac"] == pytest.approx(4.0)
    # and the submit receipt reports it under its own name (not "unrecorded")
    receipt = rp.notional_ceiling_receipt(
        {**meta, "frozen_usd": usd}, effective_ceiling_usd=usd, loss_usd=390.0, notional_usd=10_000.0,
    )
    assert receipt["notional_ceiling_source"] == "replay_equity_seam:broker_multiplier_pinned"
    assert receipt["notional_ceiling_frozen_crossover_stop_pct"] == pytest.approx(0.0075)
    # the seam at 1.0 (what the bench would have used had it reached the seam) is 4x tighter
    with rp.replay_account_equity(lambda *_a, **_k: CANON_EQUITY):
        usd1, meta1 = rp.equity_relative_notional_cap_with_meta(
            100_000.0, "alpaca_spot", loss_fixed_fallback_usd=390.0,
        )
    assert usd1 == pytest.approx(13_000.0) and meta1["crossover_stop_pct"] == pytest.approx(0.03)


def test_equity_provider_from_pins_carries_the_multiplier_or_nothing() -> None:
    with_mult = pins_mod.equity_provider_from_pins(
        CANON_EQUITY, {"broker_multiplier": {"multiplier": 4.0, "source": "broker_multiplier"}})
    assert (with_mult.replay_multiplier, with_mult.replay_multiplier_source) == (4.0, "broker_multiplier")
    assert rp.replay_seam_multiplier(with_mult) == (4.0, "replay_equity_seam:broker_multiplier_pinned")
    without = pins_mod.equity_provider_from_pins(
        CANON_EQUITY, {"broker_multiplier": {"multiplier": None, "source": "unavailable"}})
    assert without.replay_multiplier is None
    assert rp.replay_seam_multiplier(without) == (1.0, "replay_equity_seam")


class _FakeSess:
    def __init__(self, snap):
        self.risk_snapshot_json = snap


class _FakeDB:
    def __init__(self, sess):
        self._sess = sess
        self.commits = 0

    def get(self, _model, _sid):
        return self._sess

    def commit(self):
        self.commits += 1


def test_driver_freezes_the_live_admission_ceiling_not_the_seed_literal(monkeypatch) -> None:
    """The seed's LEGACY_DIAGNOSTIC_POLICY_CAPS literal (100,000) is replaced by the ceiling
    live would freeze, and the receipt lands where live puts it."""
    import replay_v3_fsm_window as drv
    from app.services.trading.momentum_neural.replay_v3 import LEGACY_DIAGNOSTIC_POLICY_CAPS

    _canon(monkeypatch)
    monkeypatch.setattr(drv, "EXEC_FAMILY", "alpaca_spot")
    caps = dict(LEGACY_DIAGNOSTIC_POLICY_CAPS)
    caps["max_loss_per_trade_usd"] = 390.0          # the bench's MAXLOSS_USD override
    sess = _FakeSess({"momentum_policy_caps": caps})
    db = _FakeDB(sess)
    provider = pins_mod.ReplayEquityProvider(CANON_EQUITY, LIVE_MULTIPLIER, "broker_multiplier")
    out = drv._freeze_live_notional_ceiling(db, 1, provider)
    frozen = sess.risk_snapshot_json
    assert frozen["momentum_policy_caps"]["max_notional_per_trade_usd"] == pytest.approx(52_000.0)
    ncd = frozen["momentum_policy_caps_derivation"]["notional_ceiling"]
    assert ncd["source"] == "replay_equity_seam:broker_multiplier_pinned"
    assert ncd["frozen_usd"] == pytest.approx(52_000.0)
    assert ncd["crossover_stop_pct"] == pytest.approx(0.0075)
    assert ncd["derivation_kind"] == "notional_ceiling"
    assert ncd["execution_family"] == "alpaca_spot"
    assert out["seed_literal_usd"] == pytest.approx(LEGACY_DIAGNOSTIC_POLICY_CAPS["max_notional_per_trade_usd"])
    assert db.commits == 1
    # the max-loss cap the bench set is untouched
    assert frozen["momentum_policy_caps"]["max_loss_per_trade_usd"] == pytest.approx(390.0)


def test_run_arm_freezes_after_the_maxloss_override_and_before_the_mirror() -> None:
    src = (_ROOT / "scripts" / "replay_v3_fsm_window.py").read_text(encoding="utf-8")
    body = src[src.index("def run_arm("):src.index("# Relations the replay itself writes")]
    i_maxloss = body.index("MAXLOSS_USD override")
    i_freeze = body.index("_freeze_live_notional_ceiling(db, seed.session_id, _equity_provider)")
    i_mirror = body.index("mirror_ticks_streaming(eng, publication=_pub")
    assert i_maxloss < i_freeze < i_mirror
    assert "equity_provider=_equity_provider" in body
    assert "equity_provider=lambda" not in body
    assert math.isclose(CANON_EQUITY * CANON_LOSS_FRACTION, 390.0)
