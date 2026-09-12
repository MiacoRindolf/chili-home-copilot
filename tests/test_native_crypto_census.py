"""PAPER HTTP census pagination, exact provider fields and completeness failures."""
import json
from uuid import uuid4

import pytest

from app.crypto_execution.census import ORDER_PAGE,read_account_census
from app.crypto_execution.paper_http import PaperCycleHTTP
from tests.test_native_crypto_lifecycle import ACCOUNT,ASSET,order,position
from tests.test_native_crypto_owner import Response


def reader(pages,*,positions=None,account=None):
    journal=[];paths=[]
    broker=PaperCycleHTTP(paper=True,account_id=ACCOUNT,key='test-key-long',secret='test-secret-long',
        require_authority=lambda account_id:None,timeout_seconds=5,max_response_bytes=1000000)
    def open(request,timeout):
        path=request.full_url.removeprefix('https://paper-api.alpaca.markets')
        assert request.get_method()=='GET' and request.data is None
        paths.append(path)
        if path=='/v2/positions':body=[] if positions is None else positions
        elif path=='/v2/account':body=dict(id=ACCOUNT) if account is None else account
        else:body=pages.pop(0)
        return Response(200,json.dumps(body).encode())
    broker._opener.open=open
    def run(limit=10):
        return read_account_census(broker,record=journal.append,before_transport=lambda:None,max_order_pages=limit)
    return run,journal,paths


def test_short_pages_continue_by_order_id_and_keep_all_asset_classes_and_exact_quantities():
    crypto=order(status='new',filled_qty='0',filled_avg_price=None)
    equity=dict(id=str(uuid4()),asset_class='us_equity',symbol='EQ',submitted_at='2026-09-12T00:00:00Z')
    run,journal,paths=reader([[crypto],[equity],[]],positions=[position()])
    result=run()
    assert result['orders']==[crypto,equity] and result['positions']==[position()]
    assert result['orders'][0]['qty']=='0.000100001'
    assert paths==['/v2/positions',ORDER_PAGE,ORDER_PAGE+'&before_order_id='+crypto['id'],
                  ORDER_PAGE+'&before_order_id='+equity['id'],'/v2/account']
    assert result['order_pages']==3 and result['order_pagination_exhausted']
    assert not result['broker_snapshot_atomic']
    assert len([e for e in journal if e['phase']=='response' and e['complete']])==5


@pytest.mark.parametrize('case',['repeat','resource','account','position_duplicate','legs'])
def test_census_never_promotes_incomplete_or_wrong_account_evidence(case):
    row=order()
    if case=='repeat':run,_,_=reader([[row],[row]])
    elif case=='resource':run,_,_=reader([[row]])
    elif case=='account':run,_,_=reader([[]],account=dict(id=str(uuid4())))
    elif case=='position_duplicate':run,_,_=reader([[]],positions=[position(),position()])
    else:run,_,_=reader([[dict(row,legs=[order()])]])
    with pytest.raises(ValueError):run(1 if case=='resource' else 10)


def test_page_resource_bound_is_explicit_not_a_symbol_selection_limit():
    run,_,paths=reader([[]])
    with pytest.raises(ValueError,match='resource_bound_required'):run(0)
    assert paths==[]
