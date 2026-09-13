"""Lifetime changes cannot remove PAPER identity or lease protections."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

# The real supervisor owns Windows Job Objects through pywin32. Exercise it
# on Windows; Linux CI must not fail collection importing native Windows APIs.
if sys.platform != 'win32':
    pytest.skip('Windows PAPER supervisor requires pywin32', allow_module_level=True)

spec = importlib.util.spec_from_file_location('paper_supervisor', Path(__file__).parents[1] / 'scripts/timeshare_supervisor.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)

def fixture_policy():
    args = SimpleNamespace(continuous_native_paper=True, end_utc=None, operator_authorized=True)
    lane = {'CHILI_ALPACA_PAPER':'1', 'CHILI_MOMENTUM_CRYPTO_EXECUTION_VIA_ALPACA_PAPER':'true',
            'CHILI_ALPACA_EXPECTED_ACCOUNT_ID':'11111111-1111-1111-1111-111111111111',
            'CHILI_MOMENTUM_NATIVE_CRYPTO_HOST_CONFIG_PATH':str(Path(__file__).resolve())}
    config = {'contract':'native_paper_host_v1','supervisor_path':s.__file__,
              'env_path':s.LANE_ENV_PATH,'supervisor_env_path':s.ENV_PATH}
    return args, lane, dict(lane), config

def policy():
    a,l,e,c=fixture_policy()
    return s.supervision_policy(a,lane_env=l,supervisor_env=e,config=c)

@pytest.mark.parametrize('hour',[2358,2359,0,1,1200])
def test_continuous_crosses_midnight(hour):
    assert s.supervision_stop(policy(),now_hhmm=hour,app_returncode=None,lease_held=True) is None

@pytest.mark.parametrize('code,held,reason',[(None,False,'lease_lost'),(0,True,'app_died'),(1,False,'app_died')])
def test_failure_still_stops(code,held,reason):
    assert s.supervision_stop(policy(),now_hhmm=1,app_returncode=code,lease_held=held)==reason

@pytest.mark.parametrize('field',['paper','native','account','config','supervisor','authorization','cutoff'])
def test_continuity_rejects_wrong_scope(field):
    a,l,e,c=fixture_policy()
    if field=='paper': e['CHILI_ALPACA_PAPER']='false'
    if field=='native': l['CHILI_MOMENTUM_CRYPTO_EXECUTION_VIA_ALPACA_PAPER']='false'
    if field=='account': e['CHILI_ALPACA_EXPECTED_ACCOUNT_ID']='22222222-2222-2222-2222-222222222222'
    if field=='config': c['contract']='unknown'
    if field=='supervisor': c['supervisor_path']=str(Path(__file__))
    if field=='authorization': a.operator_authorized=False
    if field=='cutoff': a.end_utc='2359'
    with pytest.raises(ValueError): s.supervision_policy(a,lane_env=l,supervisor_env=e,config=c)

@pytest.mark.parametrize('cutoff',['2400','2360','9999','1',None])
def test_timed_mode_rejects_sentinels(cutoff):
    with pytest.raises(ValueError): s.supervision_policy(SimpleNamespace(end_utc=cutoff))

def test_real_timed_mode_preserved():
    p=s.supervision_policy(SimpleNamespace(end_utc='2359'))
    assert s.supervision_stop(p,now_hhmm=2359,app_returncode=None,lease_held=True)=='window_end'

def test_continuous_cannot_place_legacy_cleanup_orders(monkeypatch):
    def forbidden(): raise AssertionError('stock cleanup must never run')
    monkeypatch.setattr(s,'_preshutdown_flatten',forbidden)
    result=s.shutdown_cleanup(policy())
    assert result['recovery_required'] and result['flat'] is None
    assert result['placed']==[] and not result['attempted']
