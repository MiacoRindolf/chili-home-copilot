"""Ang argv guard ay tungkol sa MGA LIHIM, hindi sa BILANG ng token.

ANG DEADLOCK (sinukat 2026-08-24). Ang IQFeed sealed capture rail ay ganap na
hindi nakabuklod sa produksyon::

    34,780 na babala, LAHAT "X of X"  -- 100% tanggi, zero partial
    5,302,433 na row ang tinanggihan sa isang session (14:30-20:48 UTC)

Ugat: ang ``_capture_handoff`` ay ``None`` sa mga bridge, kaya bawat provider
frame ay "unbound". Tanging ang ``scripts/iqfeed_capture_host.py`` ang tumatawag
ng ``bind_capture_handoff()``, at hindi ito tumatakbo.

At hindi ito maaayos, dahil ito ay umiikot::

    hindi nakabuklod ang capture host
      -> kailangan ng bridges ang --allow-uncaptured-diagnostic para mabuhay
        -> tatlong token ang argv, kaya PROCESS_ARGV_UNSUPPORTED
          -> hindi makolekta ang host snapshot
            -> walang rollback authority ang cutover
              -> hindi maaaring mag-Apply ang cutover
                -> nananatiling hindi nakabuklod ang capture host

Sabi mismo ng orihinal na komento: *"Anything else is a new launch policy and
must be reviewed before it can become rollback authority."* Ito ang pagsusuring
iyon. Ang bantay ay laban sa mga LIHIM sa argv -- kaya ang lunas ay isang
TAHASANG ALLOWLIST, hindi pagluluwag ng bilang ng token.

Runnable: pytest tests/test_capture_host_launch_policy.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str):
    path = _ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_lp_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_lp_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cut():
    return _load("captured_paper_host_cutover")


EXE = r"C:\Users\rindo\miniconda3\envs\chili-env\python.exe"
SCRIPT = r"E:\dev\wt-window2\scripts\iqfeed_trade_bridge.py"
FLAG = "--allow-uncaptured-diagnostic"


def test_the_plain_two_token_identity_is_still_accepted(cut):
    """Walang regression sa orihinal na kontrata."""
    assert cut.legacy_bridge_flags_supported((EXE, SCRIPT)) is True


def test_the_real_production_argv_is_accepted(cut):
    """ANG EKSAKTONG KASO NA NAKA-DEADLOCK."""
    assert cut.legacy_bridge_flags_supported((EXE, SCRIPT, FLAG)) is True


def test_an_unknown_flag_is_still_rejected(cut):
    """⚠️ ANG BUONG PUNTO: allowlist, hindi pagluluwag ng bilang."""
    assert cut.legacy_bridge_flags_supported((EXE, SCRIPT, "--something-new")) is False


def test_a_flag_that_could_carry_a_secret_is_rejected(cut):
    """Ang orihinal na dahilan ng bantay -- walang lihim sa argv."""
    assert cut.legacy_bridge_flags_supported((EXE, SCRIPT, "--token=hunter2")) is False
    assert cut.legacy_bridge_flags_supported((EXE, SCRIPT, FLAG, "--api-key=x")) is False


def test_the_allowlist_is_small_and_explicit(cut):
    """Ang isang lumalaking allowlist ay dahan-dahang pagbura ng bantay."""
    assert cut.SUPPORTED_LEGACY_BRIDGE_FLAGS == (FLAG,)


def test_repeated_allowlisted_flags_are_accepted(cut):
    assert cut.legacy_bridge_flags_supported((EXE, SCRIPT, FLAG, FLAG)) is True


def test_the_collector_uses_the_shared_allowlist():
    """Isang pinagmumulan ng katotohanan -- huwag doblehin ang patakaran."""
    src = (_ROOT / "scripts" / "collect_captured_paper_host_snapshot.py").read_text(
        encoding="utf-8"
    )
    assert src.count("legacy_bridge_flags_supported") >= 3, (
        "lahat ng tatlong argv site sa collector ay dapat gumamit ng shared allowlist"
    )
    assert "len(cmdline) != 2" not in src, "natirang hard-coded na bilang ng token"
    assert "len(item.cmdline) != 2" not in src


def test_the_secret_free_assertion_is_untouched():
    """⚠️ Hindi pinapaluwag ng pagbabagong ito ang tsek sa lihim."""
    src = (_ROOT / "scripts" / "collect_captured_paper_host_snapshot.py").read_text(
        encoding="utf-8"
    )
    assert "_assert_secret_free" in src


def test_the_full_cmdline_is_still_what_gets_hashed():
    """Ang rollback ay dapat magbalik ng EKSAKTONG argv na naobserbahan --
    ang pagputol ng flag ay magpapabalik ng bridge na mamamatay agad."""
    src = (_ROOT / "scripts" / "collect_captured_paper_host_snapshot.py").read_text(
        encoding="utf-8"
    )
    assert "sha256_json(list(item.cmdline))" in src
