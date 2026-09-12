import pytest

from app.crypto_execution.paper_http import PaperCycleHTTP, NoRedirect, NotTransported
from tests.test_native_crypto_lifecycle import ACCOUNT, INSTRUCTION
from tests.test_native_crypto_owner import Response


def client():
    return PaperCycleHTTP(paper=True,account_id=ACCOUNT,key='secret-fixture-key-12345',
        secret='secret-fixture-secret-12345',require_authority=lambda _:None,
        timeout_seconds=2,max_response_bytes=64)


@pytest.mark.parametrize('method,path,payload',[
    ('GET','https://api.alpaca.markets/v2/account',None),
    ('DELETE','/v2/positions',None),('POST','/v2/orders',dict(INSTRUCTION,symbol='AAPL')),
    ('POST','/v2/orders',dict(INSTRUCTION,side='sell',order_class='bracket')),
    ('PATCH','/v2/orders/10000000-0000-0000-0000-000000000001',{}),
])
def test_unsupported_surface_never_reaches_transport(method,path,payload):
    c=client();events=[]
    c._opener=object()
    with pytest.raises(ValueError):c.request(method,path,payload,record=events.append,before_transport=lambda:None)
    assert events==[]


def test_live_posture_is_rejected_before_client_creation():
    with pytest.raises(ValueError,match='requires_paper'):
        PaperCycleHTTP(paper=False,account_id=ACCOUNT,key='x',secret='y',
            require_authority=lambda _:None,timeout_seconds=2,max_response_bytes=64)


def test_oversized_raw_body_is_retained_as_incomplete_without_decoding():
    c=client();events=[]
    class Opener:
        def open(self,*args,**kwargs):return Response(200,b'x'*100)
    c._opener=Opener()
    with pytest.raises(ValueError,match='resource_capacity'):
        c.request('GET','/v2/account',None,record=events.append,before_transport=lambda:None)
    assert events[-1]['complete'] is False
    assert len(bytes.fromhex(events[-1]['body_hex']))==65


def test_redirect_is_not_followed_with_credentials():
    with pytest.raises(ValueError,match='redirect_refused'):
        NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid/')


def test_credential_echo_is_not_written_to_journal():
    c=client();events=[]
    class Opener:
        def open(self,*args,**kwargs):return Response(200,b'secret-fixture-key-12345')
    c._opener=Opener()
    with pytest.raises(ValueError,match='credential_echo_refused'):
        c.request('GET','/v2/account',None,record=events.append,before_transport=lambda:None)
    assert events[-1]['phase']=='credential_echo_refused'
    assert 'secret-fixture-key-12345' not in str(events)


def test_failure_after_entering_opener_never_claims_proven_unsent():
    c=client();events=[]
    class Opener:
        def open(self,*args,**kwargs):raise ConnectionError('unknown send progress')
    c._opener=Opener()
    with pytest.raises(RuntimeError,match='unknown_no_resubmit'):
        c.request('POST','/v2/orders',INSTRUCTION,record=events.append,before_transport=lambda:None)
    assert events[-1]['phase']=='transport_unknown'
    assert not any(e['phase']=='not_transported' for e in events)


def test_timestamps_separate_local_fences_from_http_without_changing_instruction(monkeypatch):
    from app.crypto_execution import paper_http
    now=[100];monkeypatch.setattr(paper_http.time,'time_ns',lambda:now[0])
    c=client();events=[]
    def authority(_):now[0]+=30
    def fence():now[0]+=20
    c.require_authority=authority
    class Opener:
        def open(self,request,**kwargs):
            import json
            assert json.loads(request.data)==INSTRUCTION
            now[0]+=7
            return Response(200,b'{}')
    c._opener=Opener()
    c.request('POST','/v2/orders',INSTRUCTION,record=events.append,before_transport=fence)
    assert events[0]['started_ns']==100
    assert (events[-1]['started_ns'],events[-1]['transport_started_ns'],events[-1]['received_ns'])==(100,150,157)


def test_refusal_timestamp_does_not_claim_http_started(monkeypatch):
    from app.crypto_execution import paper_http
    now=[100];monkeypatch.setattr(paper_http.time,'time_ns',lambda:now[0])
    c=client();events=[];c._opener=object()
    def refuse():
        now[0]=120
        raise ValueError('fixture withdrawn tick verdict')
    with pytest.raises(NotTransported):c.request('POST','/v2/orders',INSTRUCTION,record=events.append,before_transport=refuse)
    assert events[-1]['completed_ns']==120 and events[-1]['started_ns']==100
    assert 'transport_started_ns' not in events[-1]
