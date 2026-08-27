"""Ang pag-cache ng tawag sa network ay hindi pag-aalis nito.

ANG PAGKAKAMALI KO, INAYOS (2026-08-27). Ang PR #1193 ay naglagay ng araw-araw na
cache sa ``_prior_day_close`` at ang pamagat nito ay nagsabing inaayos nito ang
"buhay na HTTP sa loob ng tick -- at ito ang humaharang sa tape replay".

Hindi. Ang cache ay tumutulong lamang sa PANGALAWANG tawag. Ang UNA ay HTTP pa
rin, at sa replay ay **isa ay sapat na**::

    ReplayNetworkAccessError: diagnostic ReplayV3 provider swallowed a
    forbidden network attempt

Sinasadya ng replay na ituring na nakamamatay kahit ang NALAMONG pagtatangka: ang
tahimik na nalamong tawag ay nangangahulugang lumihis ang replay sa buhay na gawi
nang walang nakakaalam. Ang ``_prior_day_close`` ay may ``except Exception:
return None``, kaya nalamon nito ang pagtanggi at nagpatuloy -- at pagkatapos ay
namatay ang buong takbo sa dulo ng tick.

NASUKAT (2026-08-27). Bumagsak ang nightly replay counterfactual sa 2 sa 2 mover
DITO MISMO, at TAHIMIK itong iniulat bilang::

    ## XPON — +58.5% mover (6.39 → hi 10.13)
    - Replay PnL: **n/a**  (fills: 0)

Hindi iyon zero na resulta. Iyon ay WALANG resulta na nagpapanggap na zero. Ang
nagturo sa akin sa tama ay hindi ang ulat kundi ang ORAS: 2.5 minuto para sa
isang bagay na dapat ay 30. Kapag masyadong mabilis ang resulta, hindi ito
nagawa.

PAGKATAPOS NG LUNAS: XPON 13:00-13:20Z, 4 entry, 3 exit, PnL -27.10 -- isang
tunay na sukat.

⚠️ AT ANG FRAME ANG SAGOT, HINDI ANG ``None``. Kung bumalik lang tayo ng ``None``
sa replay ay hindi kailanman pumuputok ang red-to-green gate doon, at hindi
masusuri ng replay ang isang buhay na landas ng entry. Ang OHLCV frame ay MAY
dalang nakaraang araw kapag malalim ang warmup (``FRAME_WARMUP_MIN`` ay 5 araw
bilang default -- katumbas ng ``period="5d"`` na hinihingi ng buhay na runner sa
provider nito), kaya nariyan na ang sagot at kailangan lang itong basahin.

Runnable: pytest tests/test_prior_day_close_never_networks_in_replay.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_gates as EG
from app.services.trading.momentum_neural import live_runner as LR


@pytest.fixture(autouse=True)
def _clean():
    EG.reset_prior_day_close_cache()
    yield
    EG.reset_prior_day_close_cache()


def _frame(rows):
    idx = pd.DatetimeIndex([r[0] for r in rows], tz="UTC")
    return pd.DataFrame({"Close": [r[1] for r in rows]}, index=idx)


# ── Ang detector ─────────────────────────────────────────────────────────────


def test_outside_a_replay_the_network_is_allowed():
    """Fail-open. Ang buhay na lane ay hindi dapat magbago kahit kaunti."""
    assert EG._replay_forbids_network() is False


def test_inside_a_replay_runtime_the_network_is_forbidden():
    """ANG PANGUNAHING KASO. Ang signal ay ang clock domain ng bound decision
    runtime -- itinatakda ito ng ReplayV3 sa pamamagitan ng
    ``live_runner.decision_runtime_state``."""
    state = LR.DecisionRuntimeState()
    state.clock_domain = "replay_utc"
    with LR.decision_runtime_state(state):
        assert EG._replay_forbids_network() is True
    assert EG._replay_forbids_network() is False, "dapat naibabalik sa paglabas"


def test_a_bound_runtime_with_a_live_clock_still_allows_network():
    """⚠️ Ang pagiging naka-bind ay HINDI nangangahulugang replay. Ang
    ``replay_utc`` lamang ang nagbabawal."""
    state = LR.DecisionRuntimeState()
    with LR.decision_runtime_state(state):
        assert EG._replay_forbids_network() is False


# ── Ang frame bilang pinagmulan ──────────────────────────────────────────────


def test_the_prior_day_close_comes_from_the_frame():
    """Ang huling close ng NAKARAANG araw ng kalakalan sa ET, hindi ang una at
    hindi ang ngayon."""
    df = _frame([
        ("2026-08-25 13:30", 4.10),
        ("2026-08-25 19:55", 4.44),   # <- huling print ng nakaraang araw
        ("2026-08-26 13:30", 6.11),
        ("2026-08-26 14:00", 6.69),
    ])
    assert EG._prior_day_close_from_frame(df) == pytest.approx(4.44)


def test_a_single_day_frame_has_no_prior_close():
    """Fail-closed: lalaktaw ang tumatawag imbes na gamitin ang open ng session
    -- iyon mismo ang bug na inaayos ng R8."""
    df = _frame([("2026-08-26 13:30", 6.11), ("2026-08-26 14:00", 6.69)])
    assert EG._prior_day_close_from_frame(df) is None


def test_a_naive_index_is_treated_as_utc():
    """Ang frame ng replay ay maaaring walang tz. Huwag mag-crash; huwag maling
    basahin ang araw."""
    idx = pd.DatetimeIndex(["2026-08-25 19:55", "2026-08-26 14:00"])
    df = pd.DataFrame({"Close": [4.44, 6.69]}, index=idx)
    assert EG._prior_day_close_from_frame(df) == pytest.approx(4.44)


@pytest.mark.parametrize("bad", [None, pd.DataFrame()])
def test_a_useless_frame_returns_none_not_an_exception(bad):
    assert EG._prior_day_close_from_frame(bad) is None


def test_the_ET_day_boundary_is_used_not_the_UTC_one():
    """⚠️ Ang 2026-08-26 00:30Z ay 2026-08-25 20:30 ET -- KAHAPON pa rin. Ang
    paggamit ng UTC dito ay maghahati sa araw ng kalakalan sa maling lugar. Ito
    ang KAPAREHONG pagkakamali ng UTC-laban-sa-ET na pumatay sa pagpili ng petsa
    ng nightly report."""
    df = _frame([
        ("2026-08-26 00:10", 4.40),   # 25 Ago 20:10 ET  -> KAHAPON
        ("2026-08-26 00:30", 4.44),   # 25 Ago 20:30 ET  -> KAHAPON (huli)
        ("2026-08-26 13:30", 6.11),   # 26 Ago 09:30 ET  -> ngayon
    ])
    assert EG._prior_day_close_from_frame(df) == pytest.approx(4.44)


# ── Ang mahalagang gawi ──────────────────────────────────────────────────────


def test_a_replay_never_reaches_the_provider(monkeypatch):
    """ANG BANTAY. Kung mag-i-import pa rin ito ng massive_client sa loob ng
    replay ay sasabog ang buong takbo -- kahit nalamon ang eksepsiyon."""
    called = []

    import app.services.massive_client as MC

    def _explode(*a, **k):
        called.append(a)
        raise AssertionError("humipo ng network sa loob ng replay")

    monkeypatch.setattr(MC, "get_last_quote", _explode, raising=False)

    df = _frame([("2026-08-25 19:55", 4.44), ("2026-08-26 14:00", 6.69)])
    state = LR.DecisionRuntimeState()
    state.clock_domain = "replay_utc"
    with LR.decision_runtime_state(state):
        out = EG._prior_day_close("XPON", df=df)
    assert out == pytest.approx(4.44)
    assert called == [], "hindi dapat tinawag ang provider"


def test_outside_a_replay_the_provider_is_still_used(monkeypatch):
    """⚠️ ANG BUHAY NA LANE AY HINDI NAGBABAGO. Ang provider ang awtoridad doon;
    ang frame ay fallback lamang para sa replay."""
    import app.services.massive_client as MC

    monkeypatch.setattr(
        MC, "get_last_quote", lambda s: {"previous_close": 3.21}, raising=False)
    out = EG._prior_day_close("XPON", df=_frame([
        ("2026-08-25 19:55", 4.44), ("2026-08-26 14:00", 6.69)]))
    assert out == pytest.approx(3.21), "dapat manaig ang provider sa labas ng replay"


def test_the_replay_result_is_cached_too(monkeypatch):
    """Ang landas ng replay ay hindi dapat muling magbuo ng frame kada tick."""
    df = _frame([("2026-08-25 19:55", 4.44), ("2026-08-26 14:00", 6.69)])
    state = LR.DecisionRuntimeState()
    state.clock_domain = "replay_utc"
    with LR.decision_runtime_state(state):
        first = EG._prior_day_close("XPON", df=df)
        second = EG._prior_day_close("XPON", df=None)  # walang frame sa 2x
    assert first == pytest.approx(4.44)
    assert second == pytest.approx(4.44), "dapat galing sa cache"


def test_crypto_is_still_refused_before_anything_else():
    assert EG._prior_day_close("BTC-USD") is None


# ── Bantay sa istruktura ─────────────────────────────────────────────────────


def test_the_replay_check_precedes_the_network_import():
    """⚠️ AST, hindi text window. Ang pagkakasunod ang buong lunas: ang tseke ay
    dapat mangyari BAGO ang ``from ...massive_client import get_last_quote``. Ang
    import na iyon ay nasa loob ng function nang sadya, kaya kapag inuna ito ay
    hindi na ito mahahabol."""
    src = pathlib.Path(EG.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_prior_day_close"
    )
    guard_line = None
    import_line = None
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_replay_forbids_network"
        ):
            guard_line = n.lineno if guard_line is None else min(guard_line, n.lineno)
        if isinstance(n, ast.ImportFrom) and "massive_client" in (n.module or ""):
            import_line = n.lineno if import_line is None else min(import_line, n.lineno)
    assert guard_line is not None, "nawawala ang tseke ng replay"
    assert import_line is not None, "inaasahang lazy na import ng provider"
    assert guard_line < import_line, (
        "ang tseke ng replay (linya %s) ay dapat nauuna sa import ng provider "
        "(linya %s)" % (guard_line, import_line)
    )


def test_the_caller_passes_the_frame_through():
    """Kung walang frame ang tumatawag ay walang mahahango ang replay at babalik
    tayo sa tahimik na `None` -- ang mismong puwang sa katapatan na sinasara
    nito."""
    src = pathlib.Path(EG.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "red_to_green_confirmation"
    )
    assert "_prior_day_close(symbol, df=df)" in ast.unparse(fn)
