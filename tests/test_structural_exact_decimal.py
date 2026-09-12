"""Native price/volume precision survives the existing shared wave reducer."""
from dataclasses import replace
from decimal import Decimal as D, localcontext
from fractions import Fraction as F
import pytest

from app.services.trading.momentum_neural import structural_tape_prefix as m
from app.services.trading.momentum_neural import structural_context_journal as journal

def tick(i,price,*,bid=None,ask=None,size='0.00000000000000000001'):
    return m.Tick(i,D(price),D(size),None if bid is None else D(bid),
        None if ask is None else D(ask),i,i,i,('native-test',))

def add(p,row):
    return p.append_frontier([row],m.FrontierReceipt(row.known_ns,1,m.rows_sha256([row]),p.prefix_sha256))

def test_distinct_native_prices_do_not_collapse_into_a_plateau():
    p=m.Prefix('BTC/USD','exact',m.Limits(10,10,10))
    prices=['100000.000000000000000001','100000.000000000000000003','100000.000000000000000002']
    assert len({float(v) for v in prices})==1  # This fixture exposes the old conversion.
    for i,v in enumerate(prices,1):assert add(p,tick(i,v)).status=='applied'
    assert p.label(1)==(1,0) and p.label(2)==(-1,0)
    assert p.active_references()[0].kind=='peak'
    assert p.active_references()[0].origin_id==2
    assert p.extrema(0)[4]==D(prices[1])
    assert p.mass(0)[0]==F('0.00000000000000000002')

def test_exact_midpoint_does_not_depend_on_decimal_context_precision():
    row=tick(1,'100000.000000000000000003',bid='100000.000000000000000001',ask='100000.000000000000000005')
    with localcontext() as context:
        context.prec=3
        side,quote,mass=m.classify(row,None,0)
    assert (side,quote)==(0,0) and mass[3]==F(row.size)
    assert m.classify(replace(row,price=D('100000.000000000000000004')),None,0)[:2]==(1,1)

def test_quote_confirmed_native_valley_recovery_uses_shared_wave_geometry():
    p=m.Prefix('BTC/USD','exact',m.Limits(10,10,10))
    for i,price in enumerate(['10','15','12','13'],1):
        v=D(price)
        assert add(p,tick(i,price,bid=str(v-D('.1')),ask=str(v+D('.1')))).status=='applied'
    wave=p.wave_context
    assert wave.local_peak.price==15 and wave.local_valley.price==12
    assert wave.local_phase.reason=='recovery_after_confirmed_valley'
    assert wave.order_authority is False

def test_shared_journal_roundtrip_preserves_native_values_and_evidence_hash():
    row=tick(1,'0.000000000000000000012300',bid='0.000000000000000000012200',ask='0.000000000000000000012400')
    encoded=journal._encode(row)
    decoded=journal._decode(encoded)
    assert type(decoded.price) is D and str(decoded.price)==str(row.price)
    assert m.rows_sha256([decoded])==m.rows_sha256([row])
    assert m.rows_sha256([replace(row,price=D('0.000000000000000000012301'))])!=m.rows_sha256([row])

@pytest.mark.parametrize('bad',['NaN','Infinity','1e-10'])
def test_shared_journal_rejects_noncanonical_or_nonfinite_decimal(bad):
    with pytest.raises(ValueError):journal._decode({'decimal':bad})

def test_numeric_domain_change_requires_explicit_new_segment_and_mixed_row_is_invalid():
    row=tick(1,'10')
    with pytest.raises(ValueError,match='mixed_exact'):
        replace(row,size=1.0)
    p=m.Prefix('BTC/USD','exact',m.Limits(10,10,10));assert add(p,row).status=='applied'
    before=p.prefix_sha256
    legacy=m.Tick(2,11,1,None,None,2,2,2,('native-test',))
    assert add(p,legacy).reason=='numeric_domain_change_requires_new_segment'
    assert p.prefix_sha256==before and p.count==1

def test_recovery_identity_includes_actual_shared_implementation(monkeypatch):
    from pathlib import Path
    from app.services.trading.momentum_neural import structural_context_recovery as recovery
    from app.tick_math import structural_prefix
    original=Path.read_bytes
    target=Path(structural_prefix.__file__)
    before=recovery.code_identity()
    monkeypatch.setattr(Path,'read_bytes',lambda p:original(p)+(b'\n# changed implementation' if p==target else b''))
    assert recovery.code_identity()!=before
