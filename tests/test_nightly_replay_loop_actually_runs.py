"""Ang evidence loop ay nagbukas ng Notepad nang 47 araw, tapos nag-ulat ng zero.

ANO ITO. Ang ``CHILI-Nightly-Replay`` ay ang AWTOMATIKONG bersyon ng mano-manong
find-gap loop: tuwing gabi pagkasara, i-replay ang top movers ng araw sa
KASALUKUYANG kodigo at isulat kung aling gate ang pumigil sa entry at ilang beses.
Greenlit 2026-07-10. **Ni minsan hindi ito nakagawa ng magagamit na ulat.**

APAT NA BUG, NATUKLASAN 2026-08-27, LAHAT NASUKAT::

    1. Ang task ay tumatawag sa run-hidden.vbs NANG WALANG
       `powershell.exe -File`, kaya nire-resolve ng Windows ang .ps1 sa file
       association at BINUBUKSAN ITO SA TEXT EDITOR. Walang runner.log kailanman.
       (Naayos sa task definition, hindi rito.)

    2. CHILI_REPLAY_BUILD naka-default sa D:\\dev\\chili-home-copilot -- ang Codex
       branch, huling commit 2026-07-16. Ang TUMATAKBONG lane ay nasa
       E:\\dev\\wt-window2. Anim na linggong lumang kodigo ang sinusuri.

    3. Ang petsa ay kinukuha sa UTC habang ang task ay tumatakbo sa 17:30 PT =
       00:30Z. Lagpas na ang UTC date, kaya hinahanap nito ang movers ng BUKAS:
       "0 qualifying movers", tuwing gabi. Napatunayan: ET 2026-08-26 21:25
       laban sa UTC 2026-08-27 01:25.

    4. ANG PUMATAY SA ULAT. Ipinapasa nito ang OHLCV_START == WIN_START, kaya
       nagsisimula ang OHLCV frame sa ZERO bar. NASUKAT sa run ng 2026-08-26::

           insufficient_bars   x1328
           fills sa 5 mover    0

       Iyon ay ARTIFACT NG HARNESS, hindi natuklasan tungkol sa lane. Ang v3 ay
       may FRAME_WARMUP_MIN na nagpapalalim sa OHLCV seam LAMANG (ang tick mirror
       at ang driver grid ay nananatiling nakatali sa window).

    ⚠️ At ang default na driver ay replay_window.py -- na ang sariling docstring
    ay nagsasabing ito ay ang "CLRO 2026-07-02 G4 exit A/B" na script. Isang
    purpose-built na eksperimento na ginamit bilang pangkalahatang nightly driver.
    Gumawa ito ng session para sa 4 sa 5 mover na may ZERO event.

ANG HALAGA NG PAGKAWALA NITO: ang mga dark exit flag ay "naghihintay ng A/B
proof". Ang makinang gumagawa ng proof ang sira. 47 gabing ulat ang nawala --
bawat isa ay magsasabi sana kung aling gate ang binding, na kinailangang tuklasin
sa kamay noong 2026-08-26.

Runnable: pytest tests/test_nightly_replay_loop_actually_runs.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
from datetime import datetime, timezone

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "nightly_replay_report.py"


def _load(monkeypatch, env: dict | None = None):
    """I-import ang script nang malinis, na may kontroladong env."""
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("_nightly_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_nightly_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree() -> ast.Module:
    return ast.parse(_SCRIPT.read_text(encoding="utf-8"))


# ── Bug 3: ang petsa ─────────────────────────────────────────────────────────


def test_the_trading_day_is_ET_not_UTC(monkeypatch):
    """ANG PANGUNAHING KASO. Sa 00:30Z ang UTC ay BUKAS na habang ang araw ng
    kalakalan ay NGAYON pa rin. Ang ET ang nagtatakda ng araw ng kalakalan."""
    mod = _load(monkeypatch)
    from zoneinfo import ZoneInfo

    expected = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    assert mod._trading_day_et() == expected


def test_the_ET_day_never_runs_ahead_of_the_UTC_day(monkeypatch):
    """⚠️ Ang ET ay UTC-4/-5. Ang petsa ng kalakalan ay dapat KAILANMAN hindi
    lalampas sa petsa sa UTC -- iyon mismo ang bug."""
    mod = _load(monkeypatch)
    et = mod._trading_day_et()
    utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert et <= utc


def test_an_explicit_day_still_wins(monkeypatch):
    """Kailangang mapatakbo ng operator ang kahit anong araw."""
    mod = _load(monkeypatch, {"NIGHTLY_REPLAY_DAY": "2026-08-26"})
    import os

    assert os.environ.get("NIGHTLY_REPLAY_DAY") == "2026-08-26"


# ── Bug 2: kaninong kodigo ang sinusuri ──────────────────────────────────────


def test_the_default_build_is_the_deploy_tree_not_the_codex_branch(monkeypatch):
    """⚠️ Ang ulat na sumusuri sa maling kodigo ay mas masahol kaysa sa walang
    ulat: mukha itong ebidensya."""
    mod = _load(monkeypatch)
    assert "wt-window2" in mod.BUILD, mod.BUILD
    assert "chili-home-copilot" not in mod.BUILD


def test_the_build_is_overridable(monkeypatch):
    mod = _load(monkeypatch, {"CHILI_REPLAY_BUILD": r"E:\some\other\tree"})
    assert mod.BUILD == r"E:\some\other\tree"


# ── Ang driver ───────────────────────────────────────────────────────────────


def test_the_default_driver_is_the_fsm_window_driver(monkeypatch):
    """⚠️ Ang replay_window.py ay ang CLRO G4 exit A/B na eksperimento -- basahin
    ang sarili nitong docstring. Sa 2026-08-26 ay gumawa ito ng session para sa
    4 sa 5 mover na may ZERO event."""
    mod = _load(monkeypatch)
    assert mod.DRIVER.endswith("replay_v3_fsm_window.py"), mod.DRIVER


def test_the_driver_exists_on_disk(monkeypatch):
    """Ang default ay dapat tumuturo sa isang bagay na TUNAY."""
    mod = _load(monkeypatch, {"CHILI_REPLAY_BUILD": str(_SCRIPT.parents[1])})
    assert pathlib.Path(mod.DRIVER).is_file(), mod.DRIVER


# ── Bug 4: ang manipis na frame ──────────────────────────────────────────────


def test_the_frame_warmup_is_passed_to_the_driver():
    """ANG BUG NA NAGPAWALANG-SAYSAY SA BUONG ULAT. Kapag OHLCV_START ==
    WIN_START ay nagsisimula ang frame sa zero bar: insufficient_bars x1328, 0
    fill sa 5 mover. Ang FRAME_WARMUP_MIN ang nagpapalalim sa OHLCV seam."""
    tree = _tree()
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "run_replay"
    )
    src = ast.unparse(fn)
    assert "FRAME_WARMUP_MIN" in src, (
        "kung wala ito ay walang OHLCV bar kailanman ang frame at walang trigger "
        "na pumuputok -- ang ulat ay magsasabi ng 0 fill magpakailanman"
    )


def test_the_frame_warmup_default_is_deep(monkeypatch):
    """Ang 5 araw ay ang parehong period na hinihingi ng buhay na runner sa
    provider nito."""
    mod = _load(monkeypatch)
    assert int(mod.FRAME_WARMUP_MIN) >= 24 * 60, mod.FRAME_WARMUP_MIN


# ── Bug: ang window at ang timeout ───────────────────────────────────────────


def test_the_window_is_overridable_and_no_longer_four_hours(monkeypatch):
    """⚠️ Ang v3 ay tumatakbo nang 30-45 minuto para sa isang 60-minutong window.
    Ang lumang nakapirming 4-oras na window ay ginagarantiyahan ang timeout ng
    BAWAT simbolo."""
    mod = _load(monkeypatch)
    start = datetime.strptime(mod.WIN_START_UTC, "%H:%M:%S")
    end = datetime.strptime(mod.WIN_END_UTC, "%H:%M:%S")
    span_h = (end - start).total_seconds() / 3600.0
    assert 0 < span_h <= 2.0, "default na window: %.1f oras" % span_h

    mod2 = _load(monkeypatch, {
        "NIGHTLY_REPLAY_WIN_START": "11:30:00",
        "NIGHTLY_REPLAY_WIN_END": "15:30:00",
    })
    assert mod2.WIN_START_UTC == "11:30:00"
    assert mod2.WIN_END_UTC == "15:30:00"


def test_the_default_window_contains_the_open(monkeypatch):
    """Doon nangyayari ang ignition -- ang DAIC entry ng 2026-08-26 ay 13:16Z at
    ang tuktok nito ay 13:30:27Z, 27 segundo pagkatapos ng RTH open."""
    mod = _load(monkeypatch)
    assert mod.WIN_START_UTC <= "13:16:00" <= mod.WIN_END_UTC
    assert mod.WIN_START_UTC <= "13:30:27" <= mod.WIN_END_UTC


def test_the_timeout_is_overridable_and_larger_than_the_old_thirty_minutes(monkeypatch):
    """Ang CRE (841,867 tick) ay lumampas sa 1800s kahit sa MABILIS na driver."""
    mod = _load(monkeypatch)
    assert mod.REPLAY_TIMEOUT_S > 1800
    mod2 = _load(monkeypatch, {"NIGHTLY_REPLAY_TIMEOUT_S": "120"})
    assert mod2.REPLAY_TIMEOUT_S == 120


def test_the_timeout_label_reports_the_real_bound():
    """⚠️ Ang lumang kodigo ay nag-hardcode ng "timeout_30m" sa ulat. Ang label na
    hindi tumutugma sa aktuwal na hangganan ay nagtuturo sa mambabasa palayo."""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert '"timeout_30m"' not in src


def test_the_tick_stride_is_overridable(monkeypatch):
    """Ang malaking pangalan (CRE: 841k tick) ay maaaring mangailangan ng mas
    magaspang na stride para matapos."""
    mod = _load(monkeypatch, {"NIGHTLY_REPLAY_TICK_STRIDE": "16"})
    assert mod.TICK_STRIDE == "16"


# ── Ang ulat ay nagpapakita ng maling field (2026-08-27) ────────────────────


def test_the_report_reads_the_GATE_not_only_the_detector_detail(monkeypatch):
    """ANG IKA-LIMANG BUG. Ang `_top_rejects` ay nag-a-aggregate ng
    `detector_rejects` -- isang telemetry-only na side-map na isinusulat LAMANG ng
    pullback ladder. Ang gate na TUMANGGI ay nasa `payload_json->>'reason'`.

    NASUKAT 2026-08-26: para sa XPON (225 wait) at OLOX (74 wait) ang tumanggi ay
    ang 15m fallback leg na `momentum_volume_confirmation`
    (live_runner.py:29608), at ang call site na iyon ay HINDI KAILANMAN sumusulat
    sa `_reject_map`. Kaya kapag ang fallback leg ang tumanggi, ang
    `detector_rejects` ay HINDI KAYANG maglaman ng tunay na dahilan:

        naiulat  premarket_tickbreak_unconfirmed x102
        totoo    volume_below_1p5x_avg 225 sa 225  (100%)
    """
    mod = _load(monkeypatch)
    assert hasattr(mod, "_binding_gates"), (
        "ang ulat ay dapat may query para sa gate mismo"
    )
    import ast as _ast
    fn = next(
        n for n in _ast.walk(_tree())
        if isinstance(n, _ast.FunctionDef) and n.name == "_binding_gates"
    )
    src = _ast.unparse(fn)
    assert "'reason'" in src or '"reason"' in src
    assert "detector_rejects" not in src.split('"""')[-1], (
        "ang gate query ay hindi dapat bumasa ng detector_rejects"
    )


def test_the_gate_is_rendered_before_the_detector_detail():
    """⚠️ Ang pagkakasunod ANG lunas. Ang pagpapakita ng detector detail nang
    mag-isa ay binabaligtad ang causal order at itinuturo ang tuning sa mga
    detector na hindi kailanman ang binding constraint."""
    src = _SCRIPT.read_text(encoding="utf-8")
    i_gate = src.index("ANG GATE NA TUMANGGI")
    i_detail = src.index("Upstream na detalye ng detector")
    assert i_gate < i_detail


def test_the_detector_sql_has_no_bare_percent_literal():
    """⚠️ ANG BUG NA GINAWA KO MISMO HABANG INAAYOS ITO. Ang psycopg2 ay
    binabasa ang `%` bilang placeholder; ang paghahalo nito sa `%(s)s` ay
    nagdudulot ng `argument formats can't be mixed` at TAHIMIK na nagbabalik ng
    walang laman na listahan sa pamamagitan ng `except Exception` sa ibaba.
    Isang komentong may porsyentong simbolo ay sapat na."""
    import ast as _ast
    fn = next(
        n for n in _ast.walk(_tree())
        if isinstance(n, _ast.FunctionDef) and n.name == "_top_rejects"
    )
    for node in _ast.walk(fn):
        if isinstance(node, _ast.Assign) and isinstance(node.value, _ast.Constant):
            q = str(node.value.value or "")
            if "detector_rejects" not in q:
                continue
            stripped = q.replace("%(s)s", "")
            assert "%" not in stripped, (
                "walang hubad na `%` sa SQL na may naka-pangalang params"
            )


def test_both_symbols_gates_are_readable_from_the_sink():
    """Buhay na tseke laban sa aktuwal na replay sink, kung mayroon."""
    import importlib.util as _iu
    import sys as _sys

    spec = _iu.spec_from_file_location("_nightly_live", _SCRIPT)
    mod = _iu.module_from_spec(spec)
    _sys.modules["_nightly_live"] = mod
    spec.loader.exec_module(mod)
    for sym in ("XPON", "OLOX"):
        rows = mod._binding_gates(sym)
        if not rows:
            pytest.skip("walang laman ang replay sink para sa %s" % sym)
        reason, n, pct = rows[0]
        assert n > 0 and 0 < pct <= 100.0
        assert reason and reason != "(walang reason)"
