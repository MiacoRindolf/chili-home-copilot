"""Item E ng 09-02: ang out-of-band na tagabantay ay HINDI nawawala -- INERT ito.

Tinanggihan ng PR #1296 ang item E dahil sa paniniwalang walang diskriminador na
malaya sa maling CRITICAL sa bawat natapos na trade. May diskriminador, at nakasulat
na: ang `run_alpaca_orphan_reconcile` ay account-wide at BROKER-TRUTH-driven
(`_read_exact_order_truth` kada order, `_sweep_unmanaged_positions` sa TUNAY na
posisyon sa broker), kaya hindi ito maaaring umiyak ng lobo sa natapos na trade --
ang natapos na trade ay FLAT sa broker. Ang problema ay hindi kulang na tsek; ang
tsek na mayroon tayo ay HINDI TUMATAKBO.

ANG MEKANISMO. `_persisted_reconcile_quarantine_reason` ay nagbabalik ng DICT
{reason: count} kapag ANUMANG na-scan na hilera ay may dahilan, at itinuturing iyon
ng tumatawag bilang PASS-WIDE na skip -- bumabalik BAGO pa maitayo ang
`AlpacaSpotAdapter()`, `broker_calls=0`.

BAKIT PERMANENTENG LASON. Ang scan predicate ay humihila rin ng TERMINAL na hilera.
Ni-replay nang berbatim laban sa live DB noong 2026-09-02: 31 hilera, LAHAT NG 31 ay
terminal. 16 sa kanila (827 OTLK .. 13257 ACTU, ginawa 2026-06-12..2026-07-13) ay may
WALANG LAMAN na `alpaca_account_scope` kaya bawat isa ay nagbubunga ng
`alpaca_account_scope_unfrozen_or_mismatched`. Patay na hilera na walang maglilinis,
kaya hindi-walang-laman ang dict sa BAWAT pass, HABAMBUHAY.

PATUNAY MULA SA RUNTIME. `trading_automation_events`, event_type
'alpaca_orphan_reconcile': 14 na hilera KAILANMAN; una 2026-07-09 15:51:12, huli
2026-07-14 16:36:50. ZERO sa nakaraang ~50 araw, sa job na naka-120s IntervalTrigger.
Tahimik ito sa pamamagitan ng disenyo: ang scheduler ay nagla-log LAMANG kapag
`flattened or cancelled`.

ANG AYOS AY HINDI "TANGGALIN ANG GATE" -- MAY TUNAY ITONG GINAGAWA.
`_sweep_unmanaged_positions` (#1266) ay nagpapadala ng
`place_market_order(side="sell", base_size=abs(qty))` sa ANUMANG posisyong walang
may-ari at walang nakaupong order. Hindi nito sinusuri ang asset class at kinukuha
nito ang `abs()` ng dami -- kaya sa SHORT ay MAGBEBENTA PA ITO NANG HIGIT (dinodoble
ang short, hindi isinasara) at sa crypto ay makikipagkalakalan ito sa instrumentong
hindi sertipikado ang deployment na ito. Kaya:

  (1) ang SHAPE na dahilan (`alpaca_live_posture_not_certified`,
      `alpaca_crypto_execution_not_certified`,
      `alpaca_short_execution_not_certified`) ay NANANATILING pass-wide;
  (2) ang GENERATION na dahilan (`alpaca_account_scope_unfrozen_or_mismatched`,
      `alpaca_account_generation_mismatch`) ay HINDI -- ang hilerang isinulat sa
      lumang account identity ay hindi maaaring maglarawan ng imbentaryo sa
      naka-pin na account ngayon, na hiwalay na binibaripika ng tumatawag;
  (3) ang bantay na TALAGANG mahalaga ay inilipat sa PUNTO NG AKSYON, sa broker
      truth: tinatanggihan na ngayon ng sweep ang negatibong dami at ang crypto-like
      na simbolo, kung saan eksaktong alam ang tanda at ang simbolo.

SINUKAT PAGKATAPOS NG AYOS (live DB, 2026-09-02, ang SQL mismo ng branch): 31 -> 19
hilera; 0 na SHAPE na dahilan; 12 na GENERATION na dahilan; umaabot na sa broker.

Runnable: pytest tests/test_alpaca_reconcile_rearm_0902.py -v -p no:randomly
"""
from __future__ import annotations

import inspect
import time as _time

import pytest

from app.services import trading_scheduler as TS
from app.services.trading import alerts as AL
from app.services.trading.momentum_neural import alpaca_reconcile as AR


# ── E1: a GENERATION quarantine no longer darkens the whole pass ────────────


def test_generation_quarantine_no_longer_short_circuits_the_pass(monkeypatch):
    """Ang 16 na patay na scope-less na hilera ang pumatay ng pass sa ~50 araw."""
    seen = {}
    monkeypatch.setattr(
        AR, "_persisted_reconcile_quarantine_reason",
        lambda _db: {"alpaca_account_scope_unfrozen_or_mismatched": 16},
    )
    _stop = _StopAtAdapter()
    monkeypatch.setattr(AR, "settings", _enabled_settings(), raising=False)
    monkeypatch.setattr(
        "app.services.trading.venue.alpaca_spot.AlpacaSpotAdapter", _stop,
    )
    out = AR.run_alpaca_orphan_reconcile(_QuietDb(seen))
    assert out.get("skipped") != "alpaca_execution_quarantined", out
    assert out["persisted_execution_quarantines"] == {
        "alpaca_account_scope_unfrozen_or_mismatched": 16
    }, "ang mga dahilan ay dapat pa ring maitala para sa observability"
    assert _stop.built is True, "hindi umabot sa adapter"


@pytest.mark.parametrize("reason", [
    "alpaca_live_posture_not_certified",
    "alpaca_crypto_execution_not_certified",
    "alpaca_short_execution_not_certified",
])
def test_shape_quarantine_still_darkens_the_whole_pass(monkeypatch, reason):
    """HINDI ito tinatanggal. Ang `_sweep_unmanaged_positions` ay nagbebenta ng
    `abs(qty)` nang walang tsek sa asset class o tanda."""
    monkeypatch.setattr(
        AR, "_persisted_reconcile_quarantine_reason", lambda _db: {reason: 1},
    )
    _stop = _StopAtAdapter()
    monkeypatch.setattr(AR, "settings", _enabled_settings(), raising=False)
    monkeypatch.setattr(
        "app.services.trading.venue.alpaca_spot.AlpacaSpotAdapter", _stop,
    )
    out = AR.run_alpaca_orphan_reconcile(_QuietDb({}))
    assert out["skipped"] == "alpaca_execution_quarantined"
    assert out["broker_calls"] == 0
    assert out["shape_quarantines"] == {reason: 1}
    assert _stop.built is False, "ang uncertified na hugis ay umabot sa adapter"


def test_a_mixed_batch_is_darkened_by_the_shape_row_alone(monkeypatch):
    monkeypatch.setattr(
        AR, "_persisted_reconcile_quarantine_reason",
        lambda _db: {
            "alpaca_account_scope_unfrozen_or_mismatched": 16,
            "alpaca_short_execution_not_certified": 1,
        },
    )
    _stop = _StopAtAdapter()
    monkeypatch.setattr(AR, "settings", _enabled_settings(), raising=False)
    monkeypatch.setattr(
        "app.services.trading.venue.alpaca_spot.AlpacaSpotAdapter", _stop,
    )
    out = AR.run_alpaca_orphan_reconcile(_QuietDb({}))
    assert out["skipped"] == "alpaca_execution_quarantined"
    assert out["shape_quarantines"] == {"alpaca_short_execution_not_certified": 1}
    assert _stop.built is False


def test_the_shape_reason_set_is_exactly_the_uncertified_shapes():
    assert AR._SHAPE_QUARANTINE_REASONS == frozenset({
        "alpaca_live_posture_not_certified",
        "alpaca_crypto_execution_not_certified",
        "alpaca_short_execution_not_certified",
    })


def test_the_str_quarantine_is_still_pass_wide_fail_closed():
    """Ang hindi-mabasang persistence view ay NANANATILING fail-closed."""
    src = inspect.getsource(AR.run_alpaca_orphan_reconcile)
    i_elif = src.index("elif persisted_quarantine is not None:")
    tail = src[i_elif:i_elif + 400]
    assert 'out["skipped"] = "alpaca_execution_quarantined"' in tail
    assert "return out" in tail


def test_a_pass_that_reaches_the_broker_says_so():
    """Ang scheduler ay kailangang makilala ang 'tumakbo, walang nakita' laban sa
    'hindi kailanman umabot sa broker'."""
    src = inspect.getsource(AR.run_alpaca_orphan_reconcile)
    i_flag = src.index('out["reached_broker"] = True')
    i_sweep = src.index("_settle_submitted_orphan_flattens(")
    assert i_flag < i_sweep


# ── E2: the scan can no longer be pinned by dead, fully-resolved rows ───────


def test_position_arm_uses_the_text_operator():
    """`->` ay nagbabalik ng jsonb null para sa JSON null, at ang `jsonb null IS
    NOT NULL` ay TRUE. Sinukat: 9 na hilera ang tumutugma sa `->`, 0 sa `->>`."""
    sql = _scan_sql()
    assert "->'momentum_live_execution'->>'position'" in sql
    assert "->'momentum_live_execution'->'position'" not in sql


def test_entry_order_arm_is_bounded_by_its_resolution_record():
    """Ang order na may resolution record (`adopted`/`void`) ay nareconcile na;
    ang UNRESOLVED lang ang natitirang gawain sa broker."""
    sql = _scan_sql()
    assert "entry_orders_resolved" in sql
    i_oid = sql.index("->>'entry_order_id', '') <> ''")
    i_res = sql.index("entry_orders_resolved")
    assert i_oid < i_res, "ang resolution guard ay dapat kasama ng entry arm"
    assert "NOT (coalesce" in sql


# ── E2b: the guard that matters now lives at the point of action ───────────


def test_the_unmanaged_sweep_refuses_a_short_and_a_crypto_position():
    """Ang tanging aksyon ay `side='sell'` sa `abs(qty)`. Sa short ay dinodoble
    nito ang exposure; sa crypto ay hindi sertipikado."""
    src = inspect.getsource(AR._sweep_unmanaged_positions)
    i_guard = src.index("raw_qty < 0 or alpaca_symbol_is_crypto_like(sym)")
    i_place = src.index("place_market_order(")
    assert i_guard < i_place, "ang bantay ay dapat mauna sa order"
    assert "unmanaged_uncertified_shape" in src
    # and the sign is no longer discarded before the test
    i_abs = src.index("qty = abs(raw_qty)")
    assert i_guard < i_abs, "ang tanda ay hindi dapat itapon bago ang bantay"


# ── E3: the skip is audible, and a run of inert passes pages ───────────────


class _Db:
    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def rig(monkeypatch):
    """Drives _run_alpaca_orphan_reconcile_job over a scripted summary sequence."""
    state = {
        "summaries": [], "pages": [], "deliver": True,
        "already_delivered": False, "clock": 1_000_000.0, "tick_s": 0.0,
    }

    monkeypatch.setattr(TS, "_aor_inert_passes", 0, raising=False)
    monkeypatch.setattr(TS, "_aor_alarm_signature", None, raising=False)
    monkeypatch.setattr(TS, "_aor_page_attempts", 0, raising=False)
    monkeypatch.setattr(TS, "_aor_page_last_ts", 0.0, raising=False)
    monkeypatch.setattr(
        "app.db.SessionLocal", lambda *a, **k: _Db(), raising=False,
    )
    monkeypatch.setattr(
        TS, "run_scheduler_job_guarded", lambda _name, work: work(),
    )
    # The job imports `time` inside `_work`, so patching the stdlib module is what
    # the scheduler actually reads. The clock only advances when a test asks it to.
    monkeypatch.setattr(_time, "time", lambda: state["clock"])

    def _dispatch(**kw):
        state["pages"].append(kw)
        return state["deliver"]

    monkeypatch.setattr(AL, "dispatch_alert", _dispatch)
    monkeypatch.setattr(
        AL, "alert_already_delivered",
        lambda _db, _t, _s, **_k: bool(state["already_delivered"]),
        raising=False,
    )

    def _drive(summaries):
        it = iter(summaries)
        monkeypatch.setattr(
            "app.services.trading.momentum_neural.alpaca_reconcile."
            "run_alpaca_orphan_reconcile",
            lambda _db: next(it),
        )
        for _ in summaries:
            TS._run_alpaca_orphan_reconcile_job()
            state["clock"] += float(state["tick_s"])

    state["drive"] = _drive
    return state


def test_every_skip_is_logged(rig, caplog):
    with caplog.at_level("WARNING", logger=TS.logger.name):
        rig["drive"]([{
            "skipped": "alpaca_execution_quarantined",
            "persisted_execution_quarantines": {
                "alpaca_account_scope_unfrozen_or_mismatched": 16,
            },
            "broker_calls": 0,
        }])
    msgs = [r.getMessage() for r in caplog.records]
    assert any("alpaca_orphan_reconcile SKIPPED" in m for m in msgs), msgs
    assert any("alpaca_account_scope_unfrozen_or_mismatched" in m for m in msgs), msgs


def test_a_run_of_inert_passes_pages_exactly_once(rig, caplog):
    inert = {"skipped": "alpaca_execution_quarantined", "broker_calls": 0}
    with caplog.at_level("CRITICAL", logger=TS.logger.name):
        rig["drive"]([dict(inert) for _ in range(60)])
    assert len(rig["pages"]) == 1, rig["pages"]
    page = rig["pages"][0]
    assert page["alert_type"] == AL.ALPACA_RECONCILE_INERT
    assert page["skip_throttle"] is True
    assert "INERT" in page["message"]
    assert any("ALPACA RECONCILER INERT" in r.getMessage() for r in caplog.records)


def test_an_undelivered_page_never_becomes_a_critical_log_storm(rig, caplog):
    """THE HOLE THIS CLOSES. `dispatch_alert` returns False when the channel is
    simply TURNED OFF — `alerts_enabled=False`, or a Telegram TIER_A preference
    cell that is off — and it writes an AlertHistory row on every call regardless.
    Latching on delivery therefore meant one CRITICAL line and one durable row
    every 120s, forever, for a condition this branch measures as a ~50-DAY
    outage: 720 lines and 720 rows a day. The alarm now latches on being RAISED;
    only the SEND retries, three times, 15 minutes apart."""
    rig["deliver"] = False
    rig["tick_s"] = 120.0          # the real IntervalTrigger
    inert = {"skipped": "alpaca_execution_quarantined", "broker_calls": 0}
    with caplog.at_level("CRITICAL", logger=TS.logger.name):
        rig["drive"]([dict(inert) for _ in range(400)])   # ~13 hours of passes
    criticals = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(criticals) == 1, [r.getMessage()[:60] for r in criticals]
    assert len(rig["pages"]) == TS._AOR_PAGE_MAX_ATTEMPTS, len(rig["pages"])


def test_a_restart_does_not_re_page_a_condition_someone_already_paged(rig, caplog):
    """Module memory dies with the process; the 120s job on a restarting box
    would otherwise page the same condition forever. Ported from
    control_loop_watchdog._already_paged: only a DELIVERED prior page counts."""
    rig["already_delivered"] = True
    inert = {"skipped": "alpaca_execution_quarantined", "broker_calls": 0}
    with caplog.at_level("WARNING", logger=TS.logger.name):
        rig["drive"]([dict(inert) for _ in range(60)])
    assert rig["pages"] == []
    assert not [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert any("already paged" in r.getMessage() for r in caplog.records)


def test_the_dedupe_failing_open_still_pages(rig, monkeypatch):
    """An alarm that goes quiet because its dedupe query errored is worse than a
    duplicate page."""
    monkeypatch.setattr(
        AL, "alert_already_delivered",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db gone")),
        raising=False,
    )
    inert = {"skipped": "alpaca_execution_quarantined", "broker_calls": 0}
    # The real helper swallows this; here the stub raises, so the job's own
    # guard has to hold. Either way the pass must not crash the scheduler AND the
    # page must still go out.
    rig["drive"]([dict(inert) for _ in range(20)])
    assert len(rig["pages"]) == 1


def test_the_alarm_needs_a_sustained_run_not_one_wobble(rig):
    inert = {"skipped": "alpaca_execution_quarantined", "broker_calls": 0}
    rig["drive"]([dict(inert) for _ in range(getattr(TS, "_AOR_INERT_PASS_CAP", 15) - 1)])
    assert rig["pages"] == []


def test_a_live_pass_resets_the_counter(rig):
    inert = {"skipped": "alpaca_execution_quarantined", "broker_calls": 0}
    live = {"reached_broker": True, "flattened": 0, "cancelled": 0}
    seq = [dict(inert) for _ in range(getattr(TS, "_AOR_INERT_PASS_CAP", 15) - 1)]
    seq.append(dict(live))
    seq += [dict(inert) for _ in range(getattr(TS, "_AOR_INERT_PASS_CAP", 15) - 1)]
    rig["drive"](seq)
    assert rig["pages"] == [], "ang buhay na pass ay dapat nag-reset ng counter"


# Literal, not read off the module: this file must COLLECT on origin/main so the
# individual defects fail one by one rather than as one collection error.
_INTENDED_SKIPS = [
    "adapter_disabled",
    "alpaca_not_paper_ready",
    "flag_off",
    "live_runner_disabled_without_standalone_authority",
]


def test_the_intended_skip_set_is_exactly_the_configuration_ones():
    assert sorted(TS._AOR_INTENDED_SKIPS) == _INTENDED_SKIPS


@pytest.mark.parametrize("reason", _INTENDED_SKIPS)
def test_a_deliberate_kill_switch_never_pages(rig, reason):
    """Kung pinatay ito ng operator para sa isang launch window, hindi ito dapat
    mag-page kada oras."""
    rig["drive"]([{"skipped": reason, "broker_calls": 0} for _ in range(60)])
    assert rig["pages"] == []


def test_an_undelivered_page_is_retried_on_ITS_OWN_schedule(rig):
    """A dropped page must still be retried — discarding the send result is how a
    dropped page becomes a silent one. But the 120s tick is NOT the retry: at that
    cadence an unconfigured channel produces 720 rows a day. The retry is
    _AOR_PAGE_RETRY_SECONDS, and it is capped."""
    rig["deliver"] = False
    inert = {"skipped": "alpaca_execution_quarantined", "broker_calls": 0}

    rig["tick_s"] = 0.0            # clock frozen: the tick alone must not retry
    rig["drive"]([dict(inert) for _ in range(getattr(TS, "_AOR_INERT_PASS_CAP", 15) + 20)])
    assert len(rig["pages"]) == 1, "ang tick mismo ay hindi retry"

    rig["clock"] += TS._AOR_PAGE_RETRY_SECONDS + 1.0
    rig["drive"]([dict(inert)])
    assert len(rig["pages"]) == 2, "lampas sa retry window ay dapat may pangalawa"

    rig["clock"] += TS._AOR_PAGE_RETRY_SECONDS + 1.0
    rig["drive"]([dict(inert)])
    rig["clock"] += TS._AOR_PAGE_RETRY_SECONDS + 1.0
    rig["drive"]([dict(inert)])
    assert len(rig["pages"]) == TS._AOR_PAGE_MAX_ATTEMPTS, "may hangganan ang pag-uulit"


def test_alert_type_is_tier_a_and_individual():
    assert AL.classify_alert_tier(AL.ALPACA_RECONCILE_INERT) == AL.TIER_A
    assert AL.ALPACA_RECONCILE_INERT in AL._INDIVIDUAL_MSG_TYPES
    assert AL.ALPACA_RECONCILE_INERT not in AL._STOP_CRITICAL_TYPES


# ── E4: the discriminator is broker truth, so a finished trade cannot page ──


def test_the_rearmed_sweeps_are_broker_truth_driven():
    """Ang tutol ng #1296 ay ang maling CRITICAL sa bawat natapos na trade. Ang
    saklaw dito ay eksaktong order truth at tunay na posisyon sa broker -- ang
    natapos na trade ay FLAT sa broker, kaya hindi ito maaaring pumutok."""
    src = inspect.getsource(AR.run_alpaca_orphan_reconcile)
    for sweep in (
        "_settle_submitted_orphan_flattens",
        "_sweep_detached_entry_claims",
        "_sweep_active_orphan_claims",
        "_sweep_unmanaged_positions",
    ):
        assert sweep + "(db, adapter)" in src, sweep
    settle = inspect.getsource(AR._settle_submitted_orphan_flattens)
    assert "_read_exact_order_truth(" in settle
    unmanaged = inspect.getsource(AR._sweep_unmanaged_positions)
    assert "adapter" in unmanaged


def test_the_paper_only_hard_gate_is_untouched():
    """Ang re-arm ay hindi nagpapaluwag ng anuman: PAPER pa rin lamang."""
    src = inspect.getsource(AR.run_alpaca_orphan_reconcile)
    i_gate = src.index('out["skipped"] = "alpaca_not_paper_ready"')
    i_quar = src.index("persisted_quarantine = _persisted_reconcile_quarantine_reason")
    assert i_gate < i_quar, "ang PAPER gate ay dapat mauna sa quarantine read"
    assert 'chili_alpaca_paper' in src
    assert 'chili_momentum_alpaca_orphan_reconcile_enabled' in src


# ── helpers ─────────────────────────────────────────────────────────────────


def _scan_sql() -> str:
    """The scan SQL as the module actually builds it (not a source substring)."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(AR.__file__).read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "_persisted_reconcile_quarantine_reason"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "text":
            cand = ast.literal_eval(node.args[0])
            if "trading_automation_sessions" in cand:
                return cand
    raise AssertionError("scan SQL not found")


class _StopAtAdapter:
    """Records whether the pass got as far as constructing the adapter."""

    def __init__(self):
        self.built = False

    def __call__(self):
        self.built = True
        raise RuntimeError("stop-at-adapter")


class _QuietDb:
    def __init__(self, seen):
        self.seen = seen

    def rollback(self):
        pass

    def commit(self):
        pass

    def execute(self, *a, **k):
        raise AssertionError("no query expected in these tests")


def _enabled_settings():
    from types import SimpleNamespace

    return SimpleNamespace(
        chili_momentum_alpaca_orphan_reconcile_enabled=True,
        chili_momentum_live_runner_enabled=True,
        chili_momentum_alpaca_orphan_reconcile_standalone_enabled=False,
        chili_alpaca_enabled=True,
        chili_alpaca_paper=True,
        chili_alpaca_api_key="paper-key",
    )
