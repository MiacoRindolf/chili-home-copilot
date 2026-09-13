from dataclasses import FrozenInstanceError,replace
import pytest

from app.crypto_execution.member_sequence import MemberSequence
from tests.test_native_crypto_frontier_members import seed


def test_append_commits_to_old_rows_and_new_rows_without_copying_old_nodes():
    first=seed().trades
    row=replace(first[0],trade_id=2)
    second=first+(row,)
    assert second.parent is first and second.rows==(row,)
    assert tuple(second)==(first[0],row) and len(first)==1 and len(second)==2
    assert second[-1] is row and second[0] is first[0]
    assert second[:1]==tuple(first)
    assert second.root!=first.root
    assert replace(second).root==second.root
    changed=replace(second,parent=MemberSequence('trades',(replace(first[0],trade_id=3),)))
    assert changed.root!=second.root
    assert (second+()) is second
    with pytest.raises(FrozenInstanceError):second.root='forged'


def test_constructor_rejects_mutable_or_cross_channel_members():
    first=seed().trades
    with pytest.raises(ValueError):MemberSequence('trades',list(first))
    with pytest.raises(ValueError):MemberSequence('quotes',tuple(first))
    with pytest.raises(ValueError):MemberSequence('quotes',(),first)
    with pytest.raises(TypeError):first+[first[0]]


def test_replaced_state_recomputes_content_binding_and_preserves_unchanged_binding():
    original=seed();new=replace(original,trades=original.trades+(replace(original.trades[0],trade_id=2),))
    assert replace(new).identity==new.identity
    altered=replace(new,trades=(replace(new.trades[0],trade_id=5),new.trades[1]))
    assert altered.identity!=new.identity
    assert original.trades.root==new.trades.parent.root


def test_deep_history_iterates_and_indexes_without_python_recursion():
    row=seed().trades[0];chain=MemberSequence('trades',())
    for i in range(1100):chain=chain+(replace(row,trade_id=i),)
    assert len(chain)==1100 and chain[0].trade_id==0 and chain[-1].trade_id==1099
    assert [r.trade_id for r in chain]==list(range(1100))
    with pytest.raises(IndexError):chain[1100]
