"""Ang stand-in allow-list at ang authority ladder ay dapat MAGKATUGMA (#1268).

ITO ANG PANGATLONG PAG-ULIT NG IISANG DEPEKTO. Kapag nagdagdag ng bagong
execution-BBO stand-in tier, DALAWANG lugar sa ``live_runner._final_entry_bbo``
ang dapat matuto ng bagong ``timestamp_basis``:

  1. ang allow-list na tuple na nagbibigay sa tier ng SARILI nitong freshness
     na kontrata (kung hindi, hinuhusgahan ito ng direct cap ng caller at
     hindi ito kailanman pumuputok sa mahihigpit na seam), at
  2. ang ``quote_authority`` ladder (kung hindi, ang cross-source na quote ay
     nagkukunwaring ``alpaca_direct`` at magpe-presyo ang final limit seam off
     dito).

Kasaysayan:
  * 2026-08-28 (#1233) — ang L2 tier ``iqfeed_l2_provider_at`` ay nawawala sa
    PAREHONG lugar. 831 block, 824 ang papasa sa sariling kontrata.
  * 2026-08-30 (#1249) — idinagdag ang Tier-2.75 trade-embedded tier at MULING
    IPINASOK ang eksaktong parehong depekto.
  * 2026-09-01 (#1268) — nasukat sa prod, perpektong kontrol sa iisang
    talahanayan ng event: ``iqfeed_trade_embedded`` -> cap 10.0 /
    ``alpaca_direct`` (127 block) laban sa ``iqfeed_l1`` -> cap 15.0 /
    ``stand_in_iqfeed_l1`` (1 block). Parehong code path, magkaibang basis.

Ang dalawang naunang ayos ay parehong tama at parehong walang test, kaya
naulit ang pattern. Ang INVARIANT — hindi ang listahan ng pangalan — ang
pumapatay dito: ang dalawang set ay dapat MAGKAPAREHO.

AST ang gamit, hindi regex: ang negatibong assertion sa isang lumilipat na
fixed window ay tahimik na pumapasa kapag gumalaw ang code (aral mula sa
``reference_source_guard_windows_rot``).

Runnable: pytest tests/test_stand_in_basis_allowlist_parity.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"
)

# Ang bawat stand-in tier na maibabalik ng alpaca_spot.get_execution_bbo sa
# ilalim ng allow_stand_in=True, sa pagkakasunod-sunod ng tier.
KNOWN_STAND_IN_BASES = {
    "massive_sip_unix_ms": "stand_in_massive_sip",                  # Tier 2
    "iqfeed_q_bid_ask_time_clock": "stand_in_iqfeed_l1",            # Tier 2.5
    "iqfeed_selected_trade_date_timems_exact":                       # Tier 2.75
        "stand_in_iqfeed_trade_embedded",
    "iqfeed_l2_provider_at": "stand_in_iqfeed_l2",                  # Tier 3
}


@pytest.fixture(scope="module")
def tree() -> ast.AST:
    return ast.parse(_SRC.read_text(encoding="utf-8"))


def _final_entry_bbo(tree: ast.AST) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_final_entry_bbo":
            return node
    pytest.fail("hindi mahanap ang _final_entry_bbo — gumalaw ang function")


def _allowlist_bases(fn: ast.FunctionDef) -> set[str]:
    """Ang mga basis string sa `... in (...)` na nagbibigay ng stand-in cap."""
    found: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.In) for op in node.ops):
            continue
        for comparator in node.comparators:
            if not isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                continue
            vals = {
                e.value for e in comparator.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
            # ito lang ang tuple na binubuo ng mga timestamp basis
            if vals & set(KNOWN_STAND_IN_BASES):
                found |= vals
    return found


def _authority_map(fn: ast.FunctionDef) -> dict[str, str]:
    """basis -> quote_authority, hinango sa nested na IfExp ladder."""
    out: dict[str, str] = {}

    def walk_ifexp(node: ast.AST) -> None:
        if not isinstance(node, ast.IfExp):
            return
        test = node.test
        basis = None
        if isinstance(test, ast.Compare) and len(test.comparators) == 1:
            rhs = test.comparators[0]
            if isinstance(rhs, ast.Constant) and isinstance(rhs.value, str):
                basis = rhs.value
        if basis is not None and isinstance(node.body, ast.Constant):
            out[basis] = node.body.value
        walk_ifexp(node.orelse)

    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "quote_authority"
                    and isinstance(v, ast.IfExp)
                ):
                    walk_ifexp(v)
    return out


def test_every_known_basis_is_in_the_allowlist(tree):
    """Nakukuha ng bawat tier ang sarili nitong freshness na kontrata."""
    got = _allowlist_bases(_final_entry_bbo(tree))
    missing = set(KNOWN_STAND_IN_BASES) - got
    assert not missing, (
        f"wala sa stand-in allow-list: {sorted(missing)} — hinuhusgahan ang mga "
        f"tier na ito ng DIRECT cap ng caller at hindi kailanman puputok"
    )


def test_every_known_basis_has_its_own_authority(tree):
    """Walang cross-source na quote na nagkukunwaring alpaca_direct."""
    amap = _authority_map(_final_entry_bbo(tree))
    for basis, expected in KNOWN_STAND_IN_BASES.items():
        assert amap.get(basis) == expected, (
            f"{basis} -> {amap.get(basis)!r}, inaasahan {expected!r}; ang "
            f"nawawalang branch ay bumabagsak sa 'alpaca_direct' at magpe-presyo "
            f"ang final limit seam off sa isang cross-source na quote"
        )


def test_the_two_sets_are_identical(tree):
    """ANG INVARIANT. Dalawang beses nang nasira ito sa PAREHONG paraan.

    Hindi ang listahan ng pangalan ang binabantayan — ang ASIMETRIYA. Ang
    bagong tier na idinagdag sa isang lugar lamang ay bumabagsak dito kahit
    hindi na-update ang test na ito.
    """
    fn = _final_entry_bbo(tree)
    allow = _allowlist_bases(fn)
    auth = set(_authority_map(fn)) - {"__never__"}
    assert allow == auth, (
        f"asimetriya sa allow-list/authority: nasa allow-list lang "
        f"{sorted(allow - auth)}; may authority lang {sorted(auth - allow)}"
    )


def test_trade_embedded_is_wired_end_to_end(tree):
    """Ang partikular na tier na sira noong 2026-09-01 (127 block)."""
    fn = _final_entry_bbo(tree)
    basis = "iqfeed_selected_trade_date_timems_exact"
    assert basis in _allowlist_bases(fn)
    assert _authority_map(fn)[basis] == "stand_in_iqfeed_trade_embedded"


def test_alpaca_direct_remains_the_default(tree):
    """Ang hindi kilalang basis ay dapat PA RIN mahulog sa alpaca_direct."""
    src = _SRC.read_text(encoding="utf-8")
    assert 'else "alpaca_direct"' in src, (
        "nawala ang default na sangay — ang hindi kilalang basis ay dapat "
        "bumagsak nang sarado sa alpaca_direct, hindi sa isang stand-in"
    )
