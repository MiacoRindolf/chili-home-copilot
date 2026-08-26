"""Ang replay harness ay dapat magmirror ng L2 depth, hindi lang ng tape (2026-08-26).

ANG PUWANG. Ang ``replay_v3_fsm_window.py`` ay nagmi-mirror ng TRADE tape at NBBO,
pero **kailanman ay hindi ng libro**. Kaya ang buong pamilya ng exit lever na
nagbabasa ng depth ay **tahimik na no-op** doon -- at LAHAT ng natitirang naka-OFF na
exit lever ay nasa pamilyang iyon::

    exit_ladder_live               (ang "Ross ladder read")
    exit_ofi_hidden_seller_enabled
    exit_ofi_lock_partial_enabled
    exit_candle_confirm_live

NAPATUNAYAN: isang A/B ng ``exit_ladder_live`` 0 laban sa 1 sa XPON 08-24 ang nagbigay
ng **eksaktong parehong fill, parehong event histogram, parehong −67.74** -- ang flag
ay walang kayang basahin, kaya wala itong magagawa.

ANG DEADLOCK: ang exit ladder ay naka-OFF sa live dahil *"naghihintay ng A/B proof"*,
at ang A/B harness ay hindi makapagbigay ng proof dahil walang depth. Nananatili
itong naka-off **hindi dahil pumalpak kundi dahil walang paraan para subukan** --
habang ang nasukat na capture ratio ay **18.5%** (+108.35R na naabot, +20.02R lang
ang nakuha sa 5 araw).

⚠️⚠️ HINDI PA NITO NABUBUKSAN ANG LADDER, at hindi dapat sabihin ng testong ito na
nabuksan. Ang depth ng 08-24 ay **269 hilera sa 10 minuto at ZERO ang may
``provider_at``** -- nauna lang nang ilang araw ang migration 371. Ang ``_sis``
(flow-confirmed strength/exhaustion) ay hindi makakaputok sa 0.45 hilera/segundo na
walang quote clock. Ito ang UNANG KALAHATI ng isang instrumento; ang pangalawang
kalahati ay may-orasang depth, na sinisimulan pa lang ipunin ngayon.

Runnable: pytest tests/test_replay_mirrors_l2_depth.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "replay_v3_fsm_window.py"


def _fn(name: str) -> ast.FunctionDef:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"walang function na {name!r}")


def _body(name: str) -> str:
    fn = _fn(name)
    return "\n".join(_SRC.read_text(encoding="utf-8").splitlines()[fn.lineno - 1: fn.end_lineno])


def test_the_depth_mirror_exists():
    """ANG PANGUNAHING KASO."""
    assert _fn("mirror_depth_streaming") is not None


def test_it_is_actually_called():
    """⚠️ Ang isang mirror na hindi tinatawag ay walang naimimirror."""
    src = _SRC.read_text(encoding="utf-8")
    assert "mirror_depth_streaming(eng)" in src
    assert "mirrored_depth_rows" in src, "dapat iulat ang bilang para makita ang katahimikan"


def test_it_carries_the_quote_event_clock():
    """⚠️ WALANG provider_at, ANG BAWAT FRESHNESS GATE AY FAIL-CLOSED. Muli tayong
    susukat ng katahimikan sa halip na gawi."""
    body = _body("mirror_depth_streaming")
    assert "provider_at" in body


def test_it_reuses_gotcha_11_the_five_minute_slices():
    """⚠️ Isang mahabang read transaction ang pinapatay ng db_watchdog (>10 min mula
    query_start) -- pinatay nito ang tick mirror nang DALAWANG beses. Ang bawat
    slice ay dapat may sariling maikling transaction sa MAGKABILANG dulo."""
    body = _body("mirror_depth_streaming")
    assert "minutes=5" in body, "dapat maghati sa 5-minutong slice"
    assert body.count("commit()") >= 2, "dapat mag-commit sa source AT sa sim kada slice"


def test_it_reuses_gotcha_11b_batched_inserts():
    """⚠️ Ang row-by-row na executemany ay umabot ng 15+ minuto sa 164k na hilera --
    sapat para patayin ng isang backend terminator nang APAT na beses."""
    body = _body("mirror_depth_streaming")
    assert "execute_values" in body or "_ev(" in body
    assert "page_size" in body


def test_the_jsonb_columns_are_wrapped():
    """⚠️ ANG TUNAY NA BUG NA NAHULI. Ibinabalik ng psycopg2 ang bids_json bilang
    Python list at iniaadapt ito bilang PG ARRAY -> DatatypeMismatch:
    'column bids_json is of type jsonb but expression is of type numeric[]'."""
    body = _body("mirror_depth_streaming")
    assert "_Json(" in body, "ang jsonb na hanay ay dapat nakabalot sa Json()"
    assert body.count("_Json(") >= 2, "parehong bids_json at asks_json"


def test_a_null_ladder_is_not_wrapped():
    """Ang NULL ay dapat manatiling NULL -- ang Json(None) ay nagsusulat ng
    JSON-null na literal, na ibang bagay sa SQL NULL."""
    body = _body("mirror_depth_streaming")
    assert "if r[9] is not None else None" in body
    assert "if r[10] is not None else None" in body


def test_it_filters_to_a_usable_book():
    """Ang isang panig na libro ay hindi makakapagpaputok ng ladder; huwag itong
    dalhin."""
    body = _body("mirror_depth_streaming")
    assert "bid_top>0" in body.replace(" ", "") or "bid_top > 0" in body
    assert "ask_top>0" in body.replace(" ", "") or "ask_top > 0" in body


def test_the_source_connection_is_read_only():
    """⚠️ Ang source ay PRODUKSYON. Ang harness ay hindi dapat makasulat doon."""
    body = _body("mirror_depth_streaming")
    assert "readonly=True" in body


@pytest.mark.parametrize("column", [
    "bid_top", "ask_top", "bid_top_size", "ask_top_size",
    "bid5_size", "ask5_size", "imbalance5", "venues",
])
def test_every_column_the_exit_levers_read_is_carried(column):
    """Ang `imbalance5` ang binabasa ng B2 study gate; ang bid5/ask5 ang mga
    aggregate na pinapalaki ng multong venue. Kung may nawawala ay tahimik na
    magiging None ang gate."""
    assert column in _body("mirror_depth_streaming")
