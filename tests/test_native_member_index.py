from dataclasses import replace
import pytest
from app.crypto_execution.frontier_members import trade_key
from tests.test_native_crypto_frontier_members import seed,observe,merge
from tests.test_native_crypto_history_source import BASE,t,q


def test_late_members_preserve_old_index_and_inclusive_exact_range():
    original=seed();old=original.indexes['trades']
    result=merge(original,observe(original,trades=[t(85,tid=2),t(90)],quotes=[q(80)],mode='full_anchor_audit'))
    new=result.state.indexes['trades']
    assert [r.trade_id for r in old.within('BTC/USD',BASE,BASE+100)]==[1]
    assert [r.trade_id for r in new.within('BTC/USD',BASE+85,BASE+90)]==[2,1]
    assert new.within('BTC/USD',BASE+86,BASE+89)==()
    assert result.state.histories()['trades']['BTC/USD']==(BASE+85,BASE+90)
    assert result.state.indexes['quotes'] is original.indexes['quotes']
    with pytest.raises(TypeError):new.by_key[('BTC/USD',99)]=original.trades[0]
    with pytest.raises(TypeError):new.times['BTC/USD']=()


def test_replacing_member_fields_cannot_reuse_old_derived_index():
    original=seed();old=original.indexes
    changed=replace(original,trades=(replace(original.trades[0],trade_id=7),))
    assert changed.indexes is not old
    assert set(changed.indexes['trades'].by_key)=={('BTC/USD',7)}
    assert set(old['trades'].by_key)=={('BTC/USD',1)}


def test_fast_query_still_rejects_missing_boundary_member_after_index_extension():
    original=seed()
    current=merge(original,observe(original,trades=[t(90),t(150,tid=2)],quotes=[q(80),q(140)])).state
    missing=observe(current,trades=[t(190,tid=3)],quotes=[q(140)],end=300)
    with pytest.raises(ValueError,match='observed_trade_missing'):merge(current,missing)
