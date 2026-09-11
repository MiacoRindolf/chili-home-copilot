"""[27] REVIEW FIX (2026-09-11) — the live_runner sizing RECEIPT wiring, under test.

The review found ~91 lines of new receipt code in ``live_runner.tick_live_session`` with no
direct coverage, every block wrapped in a bare ``except Exception: pass`` — so a defect in it
(the min-type misnaming, a missing ``later_caps``, an add site that never got the account
headroom) produced a silently absent or wrong field rather than a failure, and no test would
notice. ``tick_live_session`` cannot be called from a unit test, so the fix was to move the
DECISIONS into pure, tested functions (``risk_policy.post_floor_binding_name``,
``notional_ceiling_receipt``, ``account_headroom_capped_ceiling`` — see
tests/test_equity_relative_notional.py) and to pin the WIRING here, structurally.

A structural test is not a behavioural one. It defends exactly the class of regression the
review found: a new multiplier recorded with the wrong kind, a ceiling site that forgets the
account headroom, or the receipt rule being inlined back into the runner where nothing can
reach it.

Runnable: pytest tests/test_live_runner_sizing_receipts.py -v
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RUNNER = (
    Path(__file__).resolve().parents[1]
    / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"
)
SRC = RUNNER.read_text(encoding="utf-8")

VALID_KINDS = {"mult", "min_cap", "reset"}

# The multipliers that do NOT compose multiplicatively — the three the old
# `min(mults, key=...)` rule named wrongly. Each must declare its real kind.
NON_MULTIPLICATIVE = {
    "thin_spread_hard_cap": "min_cap",
    "alpaca_hard_loss_cap": "min_cap",
    "combined_size_down_floor_lift": "reset",
}


def _record_calls() -> list[ast.Call]:
    tree = ast.parse(SRC)
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_record_post_floor"
    ]


def test_every_post_floor_record_declares_a_valid_kind() -> None:
    calls = _record_calls()
    assert len(calls) >= 8, f"expected the whole post-floor ledger, found {len(calls)} sites"
    for call in calls:
        assert len(call.args) >= 2, ast.dump(call)
        name_node, kind_node = call.args[0], call.args[1]
        assert isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)
        assert isinstance(kind_node, ast.Constant), (
            f"{name_node.value}: the cut KIND must be a literal so this test can read it"
        )
        assert kind_node.value in VALID_KINDS, (name_node.value, kind_node.value)


def test_the_three_non_multiplicative_cuts_declare_their_real_kind() -> None:
    """SCENARIO A / B from the review, pinned at the call sites. A MIN cap SETS the budget and
    a reset DISCARDS every earlier cut; recording either as a plain ``mult`` is what made
    ``binding`` name ``starter`` for a thin-spread-capped or floor-lifted entry."""
    by_name = {
        call.args[0].value: call.args[1].value
        for call in _record_calls()
        if isinstance(call.args[0], ast.Constant) and isinstance(call.args[1], ast.Constant)
    }
    for name, kind in NON_MULTIPLICATIVE.items():
        assert by_name.get(name) == kind, (
            f"{name} must be recorded as {kind!r}; it is {by_name.get(name)!r}. "
            f"A cut recorded with the wrong kind silently mis-names risk_mults.binding, and "
            f"the operator's next A/B targets the wrong lever."
        )


def test_the_binding_rule_is_the_pure_function_not_an_inline_min() -> None:
    """The rule must stay where a test can reach it."""
    assert "post_floor_binding_name(" in SRC
    assert "min(_rm_cuts, key=_rm_cuts.get)" not in SRC, (
        "the smallest-ratio rule is the defect; it must not come back inline"
    )


def test_every_notional_ceiling_site_applies_the_account_headroom() -> None:
    """THE BLOCKING FINDING, pinned structurally. The frozen ceiling is the account's whole
    buying power. It is enforced at the primary entry AND at each of the four add sites; every
    one of them must subtract what the account already carries, or two names 30 s apart pass
    the same ceiling."""
    # the four add sites each recompute the ceiling, then cap it by liquidity
    add_ceiling_vars = ["_add_ceiling", "_ceil_m", "_ceil_p", "_ceil_fb"]
    for var in add_ceiling_vars:
        assert re.search(
            rf"{re.escape(var)}\s*=\s*_account_headroom_add_ceiling\(", SRC
        ), f"add-site ceiling {var} never passes through the account headroom"
    # the primary entry site
    assert "account_headroom_capped_ceiling(" in SRC
    assert '_notional_cap_chain["account_headroom"]' in SRC
    # each add site writes its own named receipt
    for key in (
        "pyramid_add_notional_headroom",
        "micro_pullback_add_notional_headroom",
        "post_bailout_add_notional_headroom",
        "first_burst_add_notional_headroom",
    ):
        assert key in SRC, f"missing add-site headroom receipt: {key}"


def test_the_post_freeze_cap_chain_is_recorded_and_passed_to_the_receipt() -> None:
    """The receipt must name WHICH cap produced the effective ceiling. At the derived
    ceiling the liquidity cap binds on any name under ~$4.1M daily $-volume, so copying the
    admission derivation's source was wrong in the COMMON case, not the corner one."""
    for cap in ("allocation", "liquidity", "crypto_liquidity", "account_headroom"):
        assert f'_notional_cap_chain["{cap}"]' in SRC, f"cap {cap} not recorded in the chain"
    assert "later_caps=_notional_cap_chain or None" in SRC


def test_both_sizing_arms_emit_a_ceiling_receipt() -> None:
    """The adaptive arm — the path whose own ``equity_notional_cap`` [27] also rewrote — shipped
    with NO ``notional_ceiling_source`` at all, so its absence read as "the change did not
    ship" rather than "this submit used the other sizer"."""
    assert SRC.count("notional_ceiling_receipt(") >= 2, (
        "both the adaptive and the legacy sizing arms must emit the ceiling receipt"
    )
    assert '"source": "adaptive_risk_shared_resolver"' in SRC


def test_the_in_flight_notional_side_channel_is_persisted() -> None:
    """The headroom read charges each in-flight sibling the notional it actually submitted;
    without this write it would fall back to that session's frozen ceiling on every burst."""
    assert 'le["entry_inflight_notional_usd"]' in SRC


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
