"""Maker-only crypto entry (2026-06-13): the live entry posted a marketable
guarded-ask limit that CROSSED and paid TAKER (~153bps) even with maker-only
enabled (first live TAO trade fee $1.77 = 2x its gross loss). Fix: for crypto +
maker-only, post a POST-ONLY limit at the BID; pass post_only ONLY to the
coinbase adapter (RH equity adapter has no such kwarg — never regress equity).

⚠️ PAGKUKUMPUNI 2026-08-25 — bakit AST na ito at hindi na substring.
Ang lumang bersyon ay nag-slice ng NAKAPIRMING 1200-character na window mula sa
``_entry_kwargs = dict(`` at naghanap ng substring sa loob. Lumaki ang dict
literal ng ~50 linya ng ORDER-TRUTH/PREMARKET na komento, kaya ang bantay
(``if _maker_entry:`` → ``post_only``) ay lumipat sa offset +3147 at bumagsak
ang test — hindi dahil nasira ang produksyon.

Ang TAMANG kumpuni ay HINDI ang pagpapalapad ng window. Napatunayan: ang
literal na ``adapter.place_limit_order_gtc(**_entry_kwargs)`` ay wala nang
executable na anyo sa live_runner.py — ang tunay na dispatch ay dumaan na sa
rail governor (``_governed_place(adapter, adapter.place_limit_order_gtc, …,
**_entry_kwargs)``). Ang tanging natitirang tugma ng lumang string ay isang
KOMENTO sa live_runner.py:35310. Ang paglapad ng window ay magpapa-BERDE sa
test sa pamamagitan ng isang komento — berde habang walang binabantayan.

Kaya ang bawat assertion dito ay nakatali na sa AST: sa mismong Assign/If/Call
node, hindi sa distansya sa character. Ang paglaki ng komento o ng dict literal
ay hindi na kayang buwagin ito, at hindi kayang pasayahin ito ng isang komento.
"""
import ast
import io


SRC_PATH = "app/services/trading/momentum_neural/live_runner.py"
# utf-8-sig: ilang file dito ay may BOM (tingnan tests/source_region.py).
SRC = io.open(SRC_PATH, encoding="utf-8-sig").read()
_LINES = SRC.split("\n")
_TREE = ast.parse(SRC)


def _node_src(node):
    """Ang TUNAY na teksto ng node — hanggang sa AST-derived nitong dulo
    (``end_lineno``), hindi hanggang sa isang hinulaang bilang ng character."""
    return "\n".join(_LINES[node.lineno - 1 : node.end_lineno])


def _assigns_to(node, name):
    return isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == name for t in node.targets
    )


def _maker_entry_ifs():
    """Lahat ng ``if _maker_entry:`` na branch (walang hulaang window)."""
    return [
        n
        for n in ast.walk(_TREE)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "_maker_entry"
    ]


def _is_post_only_true(node):
    """``_entry_kwargs["post_only"] = True`` — bilang STRUKTURA, hindi teksto."""
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Subscript)
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "_entry_kwargs"
        and isinstance(node.targets[0].slice, ast.Constant)
        and node.targets[0].slice.value == "post_only"
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    )


def test_maker_entry_branch_is_crypto_and_maker_gated():
    # Ang saklaw ay ang mismong Assign node ng `_maker_entry` — hindi 400 char.
    assigns = [n for n in ast.walk(_TREE) if _assigns_to(n, "_maker_entry")]
    assert len(assigns) == 1, f"inaasahan ang IISANG `_maker_entry = …`, may {len(assigns)}"
    block = _node_src(assigns[0])
    assert 'endswith("-USD")' in block               # crypto only
    assert "chili_coinbase_maker_only_enabled" in block
    assert "bid is not None" in block                # needs a real bid


def test_maker_entry_posts_at_bid_not_guarded_ask():
    # ⚠️ `entry_limit_px = float(bid)` ay lumilitaw nang DALAWANG beses (ang
    # pangalawa ay ang `_bailout_maker` equity re-entry sa :34137), kaya ang
    # lumang `... in SRC` ay masisiyahan kahit MABURA ang maker branch. Itinali
    # na ito sa mismong `if _maker_entry:` / `else:` na sanga.
    branches = [
        n for n in _maker_entry_ifs()
        if any(_assigns_to(s, "entry_limit_px") for s in n.body)
    ]
    assert len(branches) == 1, "walang (o dobleng) maker branch na nagtatakda ng entry_limit_px"
    branch = branches[0]

    maker_px = next(s for s in branch.body if _assigns_to(s, "entry_limit_px"))
    assert ast.unparse(maker_px.value) == "float(bid)"   # maker: post at the bid

    # the taker/equity path keeps the marketable guarded-ask — sundan ang buong
    # elif-chain hanggang sa panghuling `else`, kaya hindi ito mabubulok kapag
    # nagdagdag ng bagong elif sa gitna.
    tail = branch
    while tail.orelse and len(tail.orelse) == 1 and isinstance(tail.orelse[0], ast.If):
        tail = tail.orelse[0]
    fallback = [s for s in tail.orelse if _assigns_to(s, "entry_limit_px")]
    assert len(fallback) == 1, "nawala ang taker/equity fallback na sanga"
    assert ast.unparse(fallback[0].value) == "guarded_ask"


def test_post_only_passed_only_for_crypto_maker_never_to_rh():
    # 1) post_only ay HINDI unconditional kwarg ng dict literal (TypeError sa RH).
    dict_assigns = [n for n in ast.walk(_TREE) if _assigns_to(n, "_entry_kwargs")]
    literals = [
        n for n in dict_assigns
        if isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "dict"
    ]
    assert len(literals) == 1, "inaasahan ang IISANG `_entry_kwargs = dict(…)`"
    literal_keys = {k.arg for k in literals[0].value.keywords}
    assert "post_only" not in literal_keys

    # 2) BAWAT pagtatakda ng post_only ay nasa loob ng `if _maker_entry:` —
    #    hindi lang "may isang nakita sa loob ng window".
    all_post_only = [n for n in ast.walk(_TREE) if _is_post_only_true(n)]
    assert all_post_only, "nawala ang `_entry_kwargs['post_only'] = True`"
    guarded = [
        n for n in _maker_entry_ifs()
        if any(_is_post_only_true(s) for s in n.body)
    ]
    guarded_lines = {s.lineno for n in guarded for s in n.body if _is_post_only_true(s)}
    assert {n.lineno for n in all_post_only} == guarded_lines, (
        "may pagtatakda ng post_only sa LABAS ng `if _maker_entry:` — "
        "aabot ito sa RH equity adapter"
    )

    # 3) Ang KAPAREHONG dict ang tunay na naipapasa sa adapter. ⚠️ HUWAG ibalik
    #    ang lumang `"adapter.place_limit_order_gtc(**_entry_kwargs)" in SRC`:
    #    ang tanging natitirang tugma niyon ay isang KOMENTO (:35310). Ang buhay
    #    na dispatch ay `_governed_place(adapter, adapter.place_limit_order_gtc,
    #    …, **_entry_kwargs)`, kaya ang Call node ang tinatanong dito.
    spreads = [
        n for n in ast.walk(_TREE)
        if isinstance(n, ast.Call)
        and any(
            k.arg is None
            and isinstance(k.value, ast.Name)
            and k.value.id == "_entry_kwargs"
            for k in n.keywords
        )
    ]
    assert len(spreads) == 1, "inaasahan ang IISANG `**_entry_kwargs` na submit"
    submit = spreads[0]
    assert any(
        isinstance(a, ast.Attribute) and a.attr == "place_limit_order_gtc"
        for a in ast.walk(submit)
    ), "ang `**_entry_kwargs` ay hindi na dumadaan sa place_limit_order_gtc"
