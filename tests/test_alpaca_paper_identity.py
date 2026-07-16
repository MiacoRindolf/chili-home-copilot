from __future__ import annotations

import hashlib
import json

import pytest

from app.services.trading.momentum_neural.alpaca_paper_identity import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    AlpacaPaperAccountIdentityError,
    alpaca_paper_account_identity_payload,
    alpaca_paper_account_identity_sha256,
    canonical_alpaca_paper_account_id,
)


ACCOUNT_ID = "aaaaaaaa-2222-4333-8444-555555555555"


def test_identity_binds_broker_environment_and_canonical_uuid() -> None:
    payload = dict(alpaca_paper_account_identity_payload(ACCOUNT_ID))
    assert payload == {
        "broker": "alpaca",
        "environment": "paper",
        "account_id": ACCOUNT_ID,
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert alpaca_paper_account_identity_sha256(ACCOUNT_ID) == hashlib.sha256(
        canonical
    ).hexdigest()
    assert ALPACA_PAPER_ACCOUNT_SCOPE == "alpaca:paper"


@pytest.mark.parametrize(
    "value",
    (
        "",
        "not-a-uuid",
        "aaaaaaaa222243338444555555555555",
        "aaaaaaaa-2222-4333-8444-555555555555 ",
        "AAAAAAAA-2222-4333-8444-555555555555",
    ),
)
def test_identity_rejects_noncanonical_account_text(value: str) -> None:
    with pytest.raises(AlpacaPaperAccountIdentityError):
        canonical_alpaca_paper_account_id(value)
