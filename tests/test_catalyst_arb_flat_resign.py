"""Ross-parity L3 (2026-07-25): catalyst re-sign — SEC dilution-filing forms join the WEAK
class, and CONFIRMED buyout-TARGET headlines move out of STRONG into the new ARB-FLAT class
(price pinned at the deal — no intraday long). Collision-safety: bare "s-1"/"s-3"/"f-1" are
substrings of ordinary words, so only "form"-prefixed / "registration"-suffixed variants and
the "424b" prospectus stem are classified.
"""
from __future__ import annotations

from app.services.trading.momentum_neural.catalyst import (
    _is_arb_flat_catalyst,
    _is_strong_catalyst,
    _is_weak_catalyst,
)


# ── SEC dilution forms → WEAK ────────────────────────────────────────────────

def test_sec_form_headlines_are_weak():
    assert _is_weak_catalyst("Acme Files Form S-1 for Proposed Public Offering") is True
    assert _is_weak_catalyst("Acme announces S-1 registration statement") is True
    assert _is_weak_catalyst("Acme files Form S-3 shelf") is True
    assert _is_weak_catalyst("Acme S-3 registration declared effective") is True
    assert _is_weak_catalyst("Acme Files Form F-1 With SEC") is True
    assert _is_weak_catalyst("Acme F-1 registration statement filed") is True
    assert _is_weak_catalyst("Acme 424B5 Prospectus Supplement") is True
    assert _is_weak_catalyst("Acme files 424b3 prospectus") is True


def test_collision_safe_bare_forms_not_weak():
    # bare "s-1"/"f-1" collide with ordinary hyphenations — must NOT classify
    assert _is_weak_catalyst("Acme recalls Class-1 medical device") is False
    assert _is_weak_catalyst("Acme wins F-15 maintenance contract") is False
    assert _is_weak_catalyst("Acme launches Model S-10 sensor line") is False


# ── ARB-FLAT class (confirmed buyout target) ─────────────────────────────────

def test_buyout_target_headlines_are_arb_flat():
    assert _is_arb_flat_catalyst("Acme to be acquired by BigCo for $12.00 per share") is True
    assert _is_arb_flat_catalyst("BigCo announces buyout of Acme") is True
    assert _is_arb_flat_catalyst("Acme agrees to takeover by BigCo") is True
    assert _is_arb_flat_catalyst("BigCo commences tender offer for Acme shares") is True


def test_arb_flat_headlines_no_longer_strong():
    # the four re-signed phrasings must be OUT of the strong classifier
    assert _is_strong_catalyst("Acme to be acquired by BigCo for $12.00 per share") is False
    assert _is_strong_catalyst("BigCo announces buyout of Acme") is False
    assert _is_strong_catalyst("Acme agrees to takeover by BigCo") is False
    assert _is_strong_catalyst("BigCo commences tender offer for Acme shares") is False


def test_acquirer_side_deal_making_stays_strong():
    # the buyER can run — acquirer-side phrasings remain strong catalysts
    assert _is_strong_catalyst("BigCo to acquire Acme in $2B merger") is True
    assert _is_strong_catalyst("BigCo enters definitive agreement for acquisition") is True
    assert _is_strong_catalyst("BigCo and Acme announce merger") is True


def test_arb_flat_not_weak_class():
    # arb-flat is its OWN class — the weak-keyed refinements must never touch it
    assert _is_weak_catalyst("Acme to be acquired by BigCo for $12.00 per share") is False
    assert _is_weak_catalyst("BigCo commences tender offer for Acme shares") is False


def test_empty_or_none_title_not_arb_flat():
    assert _is_arb_flat_catalyst("") is False
    assert _is_arb_flat_catalyst(None) is False


def test_dual_match_headline_is_both_strong_and_arb_flat():
    # precedence is enforced at the CONSUMER (viability): here both classifiers may
    # legitimately match a dual headline — document the contract
    t = "Acme enters definitive agreement to be acquired by BigCo"
    assert _is_arb_flat_catalyst(t) is True
    assert _is_strong_catalyst(t) is True  # "definitive agreement" — consumer must let arb-flat win
