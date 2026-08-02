"""Determinism ng tape reads (TC-divergence follow-up, 2026-08-02).

Root cause ng ±$30 arm split sa flag-inert na TC window: mga tape read na
`ORDER BY observed_at` na WALANG tie-break, habang ang tape ay puno ng
equal-timestamp na grupo (JEM: 287k/341k rows; max 118 trades sa isang
timestamp) — ang equal-ts permutation ay naging f(query plan + heap layout) =
f(sink write history), at ang sequence-sensitive na tick-rule aggressor
imbalance ay nag-amplify ng ~1e-5 qty diff papunta sa magkaibang trail-exit.
Ang `id` tie-break ay deterministic AT tape-faithful: ang replay mirror ay
nag-i-stream mula sa source na `ORDER BY observed_at ASC, id ASC`.
"""
from __future__ import annotations

import re
from pathlib import Path

_PIPELINE = (
    Path(__file__).resolve().parent.parent
    / "app" / "services" / "trading" / "momentum_neural" / "pipeline.py"
)


def test_lahat_ng_observed_at_order_ay_may_id_tie_break():
    src = _PIPELINE.read_text(encoding="utf-8")
    naked = [
        m.group(0)
        for m in re.finditer(r"ORDER BY observed_at (?:ASC|DESC)(?!, id)", src)
    ]
    assert naked == [], f"tie-ambiguous ORDER BY sa pipeline.py: {naked}"
    # At buhay pa ang mga read mismo (hindi nawala ang mga site):
    assert len(re.findall(r"ORDER BY observed_at ASC, id ASC", src)) >= 4
    assert len(re.findall(r"ORDER BY observed_at DESC, id DESC", src)) >= 3


def test_batch_child_env_pins_hashseed():
    from scripts import replay_benchmark_batch as batch

    env = batch.isolated_child_env()
    assert env.get("PYTHONHASHSEED") == "0"
