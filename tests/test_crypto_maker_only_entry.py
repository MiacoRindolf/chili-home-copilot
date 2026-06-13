"""Maker-only crypto entry (2026-06-13): the live entry posted a marketable
guarded-ask limit that CROSSED and paid TAKER (~153bps) even with maker-only
enabled (first live TAO trade fee $1.77 = 2x its gross loss). Fix: for crypto +
maker-only, post a POST-ONLY limit at the BID; pass post_only ONLY to the
coinbase adapter (RH equity adapter has no such kwarg — never regress equity).
"""
import io


SRC = io.open(
    "app/services/trading/momentum_neural/live_runner.py", encoding="utf-8"
).read()


def test_maker_entry_branch_is_crypto_and_maker_gated():
    i = SRC.index("_maker_entry = (")
    block = SRC[i:i + 400]
    assert 'endswith("-USD")' in block               # crypto only
    assert "chili_coinbase_maker_only_enabled" in block
    assert "bid is not None" in block                # needs a real bid


def test_maker_entry_posts_at_bid_not_guarded_ask():
    assert "entry_limit_px = float(bid)" in SRC       # maker: post at the bid
    # the taker/equity path keeps the marketable guarded-ask
    assert "entry_limit_px = guarded_ask" in SRC


def test_post_only_passed_only_for_crypto_maker_never_to_rh():
    i = SRC.index("_entry_kwargs = dict(")
    block = SRC[i:i + 1200]
    # post_only is NOT an unconditional kwarg (would TypeError on the RH adapter)
    assert "post_only=_maker_entry" not in SRC
    # it is added conditionally, only when _maker_entry
    assert "if _maker_entry:" in block
    assert '_entry_kwargs["post_only"] = True' in block
    assert "adapter.place_limit_order_gtc(**_entry_kwargs)" in block
