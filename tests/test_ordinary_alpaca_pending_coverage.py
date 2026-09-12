"""An old owner ID alone cannot authorize unbudgeted broker buy exposure."""
import pytest

from app.services.trading.momentum_neural import alpaca_orphan_claims as claims
from tests.test_exit_verdict_f_review_fixes import _LedgerDB
from tests.test_alpaca_account_risk_reservations import TEST_ALPACA_ACCOUNT_ID, _entry_order_request


@pytest.mark.parametrize('broker_open,budgeted', [(True,False), (False,False), (True,True)])
def test_legacy_pending_order_requires_risk_coverage_only_when_still_open(broker_open,budgeted):
    live = {'entry_submitted':True,'entry_order_id':'legacy-order',
            'entry_client_order_id':'legacy-cid','side_long':True}
    if budgeted:
        live.update(entry_order_request=_entry_order_request('AAA','legacy-cid',qty='10'),
                    entry_inflight_risk_usd=10)
    snapshot = {'alpaca_account_scope':'alpaca:paper',
                'alpaca_account_id':TEST_ALPACA_ACCOUNT_ID,'momentum_live_execution':live}
    db = _LedgerDB([], [(1,'AAA','alpaca_spot','live_pending_entry',snapshot)])
    result = claims._certify_alpaca_owned_entry_posture(
        db,broker_positions=[],broker_orders=([{'order_id':'legacy-order',
        'client_order_id':'legacy-cid'}] if broker_open else []),
        account_scope='alpaca:paper',alpaca_account_id=TEST_ALPACA_ACCOUNT_ID)
    if broker_open and not budgeted:
        assert not result['ok'], result
        assert result['reason'] == 'alpaca_open_entry_risk_coverage_unproven'
    else:
        assert result['ok'], result
