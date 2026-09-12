"""Real PostgreSQL transactions on an isolated test schema, never the live ledger."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.crypto_execution.funding import FundingClaim, crypto_account_truth
from app.crypto_execution.store import NativeCycleStore, schema_statements
from app.crypto_execution.lifecycle import exposure, next_action
from tests.test_native_crypto_lifecycle import ACCOUNT, ASSET, CYCLE, INSTRUCTION, order


@pytest.fixture
def store():
    url = os.environ['TEST_DATABASE_URL']
    assert make_url(url).database == 'chili_rossbench26_test'
    assert os.environ['DATABASE_URL'] == url
    schema = 'astra_native_' + uuid4().hex
    engine = create_engine(url, connect_args={'options': '-c statement_timeout=20000'})
    with engine.begin() as c:
        c.execute(text(f'CREATE SCHEMA {schema}'))
        for statement in schema_statements(schema):
            c.execute(text(statement))
    owned = NativeCycleStore(engine, account_id=ACCOUNT, schema=schema,
                             max_event_bytes=131072, lock_timeout_ms=3000)
    try:
        yield owned
    finally:
        # This exact UUID-derived schema was created above on DB26 only.
        with engine.begin() as c:
            c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        engine.dispose()


def admission(c, *, funding='10', risk='10', external='0'):
    assert c.in_transaction()
    account = crypto_account_truth(dict(id=ACCOUNT, currency='USD', status='ACTIVE',
        crypto_status='ACTIVE', non_marginable_buying_power=funding, cash=funding,
        accrued_fees='0', account_blocked=False, trading_blocked=False,
        trade_suspended_by_user=False), expected_account_id=ACCOUNT)
    return dict(account=account, observation_id=str(uuid4()),
        external_claims=[FundingClaim('equity-claim', ACCOUNT, 'USD', Decimal(external), Decimal(0))],
        account_risk_budget=risk, external_risk_upper_bound=external)


def reserve(store, *, cycle=CYCLE, asset=ASSET, instruction=INSTRUCTION, reader=admission):
    return store.reserve(cycle_id=cycle, asset=asset, instruction=instruction,
                         context_sha256='a'*64, admission_reader=reader)


def apply(store, state, kind, **payload):
    if kind == 'position_observed':
        read_id = str(uuid4())
        state = apply(store, state, 'position_read_started', read_id=read_id)
        payload['read_id'] = read_id
        payload['read_revision'] = state['revision']
    result = store.apply(state['cycle_id'], event_id=str(uuid4()),
        event=dict(kind=kind, payload=payload), expected_revision=state['revision'], admission_reader=admission)
    assert result['applied']
    return result['state']


def another(store):
    return NativeCycleStore(store.engine, account_id=ACCOUNT,
        schema=store.cycles.split('.')[0], max_event_bytes=131072, lock_timeout_ms=3000)


def test_restart_keeps_exact_claim_and_deduplicated_intent_cannot_reauthorize_post(store):
    state = reserve(store)
    eid = str(uuid4())
    event = dict(kind='entry_transport_started')
    first = store.apply(CYCLE, event_id=eid, event=event, expected_revision=0, admission_reader=admission)
    restored = another(store)
    second = restored.apply(CYCLE, event_id=eid, event=event, expected_revision=0)
    assert first['applied'] and not second['applied']
    assert first['state'] == second['state']
    assert next_action(restored.read(CYCLE)) == 'reconcile_entry_by_client_id'
    assert exposure(restored.audit(CYCLE, max_events=2))['debit'] == '6.0000700001'
    assert reserve(restored) == first['state']


def test_native_funding_and_external_claims_are_both_charged(store):
    with pytest.raises(ValueError, match='funding_exceeded'):
        reserve(store, reader=lambda c: admission(c, external='4'))
    with pytest.raises(ValueError, match='not_found'):
        store.read(CYCLE)
    state = reserve(store, reader=lambda c: admission(c, funding='20', risk='20', external='4'))
    assert state['revision'] == 0


def test_full_unprotected_risk_is_not_reduced_to_a_guessed_stop(store):
    with pytest.raises(ValueError, match='risk_budget_exceeded'):
        reserve(store, reader=lambda c: admission(c, funding='100', risk='6'))


@pytest.mark.parametrize('funding,admitted', [('10', 1), ('20', 2)])
def test_concurrent_affordable_assets_share_one_account_budget(store, funding, admitted):
    barrier = Barrier(2)
    def run(index):
        worker = another(store)
        asset = dict(ASSET, id=str(uuid4()), symbol=f'TEST{index}/USD')
        instruction = dict(INSTRUCTION, symbol=asset['symbol'], client_order_id=f'parallel-{index}')
        barrier.wait(timeout=5)
        try:
            reserve(worker, cycle=str(uuid4()), asset=asset, instruction=instruction,
                reader=lambda c: admission(c, funding=funding, risk='100'))
            return 'admitted'
        except ValueError as error:
            assert str(error) == 'native_cycle_funding_exceeded_or_unavailable'
            return 'funding_exceeded'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [1, 2]))
    assert results.count('admitted') == admitted
    with store.engine.connect() as c:
        total = c.execute(text(f'SELECT sum(debit) FROM {store.cycles}')).scalar_one()
    assert total == Decimal('6.0000700001') * admitted
    assert total <= Decimal(funding)


def test_same_asset_cannot_have_two_independent_open_owners(store):
    reserve(store)
    with pytest.raises(ValueError, match='asset_already_owned'):
        reserve(store, cycle=str(uuid4()), instruction=dict(INSTRUCTION, client_order_id='different'))


def test_callback_observes_existing_account_lock_identity(store):
    from app.services.trading.momentum_neural.adaptive_risk_account_lock import AdaptiveRiskAccountLockIdentity
    from app.crypto_execution.store import ACCOUNT_LOCK, ADAPTIVE_NAMESPACE
    identity = AdaptiveRiskAccountLockIdentity.for_scope('alpaca:paper')
    assert identity.action_advisory_key == ACCOUNT_LOCK
    assert identity.adaptive_advisory_namespace == ADAPTIVE_NAMESPACE
    def reader(c):
        # A distinct backend cannot take either lock while admission is reading.
        with store.engine.begin() as competitor:
            assert not competitor.execute(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': ACCOUNT_LOCK}).scalar_one()
            assert not competitor.execute(text('SELECT pg_try_advisory_xact_lock(:ns,hashtext(:scope))'),
                {'ns': ADAPTIVE_NAMESPACE, 'scope': 'alpaca:paper'}).scalar_one()
        return admission(c)
    reserve(store, reader=reader)


def test_revision_conflict_and_invalid_event_do_not_change_claim(store):
    s = reserve(store)
    s = apply(store, s, 'entry_transport_started')
    with pytest.raises(ValueError, match='revision_changed'):
        store.apply(CYCLE, event_id=str(uuid4()), event=dict(kind='exit_requested'), expected_revision=0)
    with pytest.raises(ValueError, match='entry_not_submittable'):
        apply(store, s, 'entry_transport_started')
    assert store.audit(CYCLE, max_events=2) == s


def test_journal_is_append_only_and_projection_corruption_is_detected(store):
    reserve(store)
    with pytest.raises(DBAPIError, match='append only'):
        with store.engine.begin() as c:
            c.execute(text(f'DELETE FROM {store.events}'))
    with pytest.raises(DBAPIError, match='append only'):
        with store.engine.begin() as c:
            c.execute(text(f'UPDATE {store.events} SET state_sha256=:sha'), {'sha': '0'*64})
    with pytest.raises(DBAPIError, match='append only'):
        with store.engine.begin() as c:
            c.execute(text(f'TRUNCATE {store.events}'))
    for change in ('debit=0', 'closed=true', 'revision=100'):
        with pytest.raises(DBAPIError):
            with store.engine.begin() as c:
                c.execute(text(f'UPDATE {store.cycles} SET {change}'))
    with store.engine.begin() as c:
        c.execute(text(f'UPDATE {store.cycles} SET head_sha256=:sha'), {'sha': '0'*64})
    with pytest.raises(ValueError, match='projection_head_mismatch'):
        store.read(CYCLE)


def test_zero_fill_and_native_flatness_close_then_allow_reentry(store):
    s = apply(store, reserve(store), 'entry_transport_started')
    s = apply(store, s, 'entry_observed', **order(filled_qty='0', filled_avg_price=None))
    s = apply(store, s, 'position_observed', found=False)
    assert exposure(s)['closed']
    assert store.audit(CYCLE, max_events=5) == s
    replacement = reserve(store, cycle=str(uuid4()), instruction=dict(INSTRUCTION, client_order_id='reentry'))
    assert not replacement['closed']


def test_duplicate_event_identity_cannot_change_payload(store):
    reserve(store)
    eid = str(uuid4())
    store.apply(CYCLE, event_id=eid, event=dict(kind='entry_transport_started'), expected_revision=0, admission_reader=admission)
    with pytest.raises(ValueError, match='identity_conflict'):
        store.apply(CYCLE, event_id=eid, event=dict(kind='transport_observation_unknown'), expected_revision=1)


def test_resource_exhaustion_leaves_durable_claim_intact(store):
    s = reserve(store)
    with pytest.raises(ValueError, match='event_resource_capacity'):
        apply(store, s, 'transport_observation_unknown', raw='x'*131072)
    assert store.audit(CYCLE, max_events=1) == s
    s = apply(store, s, 'entry_transport_started')
    with pytest.raises(ValueError, match='audit_resource_capacity'):
        store.audit(CYCLE, max_events=1)
    assert store.read(CYCLE) == s


def test_repeatable_read_cannot_hide_a_previous_owners_new_reservation(store):
    altered = another(store)
    altered.engine = store.engine.execution_options(isolation_level='REPEATABLE READ')
    with pytest.raises(ValueError, match='read_committed_required'):
        reserve(altered)


def test_pretransport_funding_is_rechecked_and_failure_preserves_unsubmitted_claim(store):
    state = reserve(store)
    with pytest.raises(ValueError, match='funding_exceeded'):
        store.apply(CYCLE, event_id=str(uuid4()), event=dict(kind='entry_transport_started'),
            expected_revision=0, admission_reader=lambda c: admission(c, funding='5'))
    assert store.audit(CYCLE, max_events=1) == state
    assert next_action(state) == 'submit_entry'
    with pytest.raises(ValueError, match='locked_admission_reader_required'):
        store.apply(CYCLE, event_id=str(uuid4()), event=dict(kind='entry_transport_started'), expected_revision=0)


def test_pretransport_retains_locked_funding_receipt_for_audit(store):
    state = apply(store, reserve(store), 'entry_transport_started')
    import json
    with store.engine.connect() as c:
        event = json.loads(c.execute(text(f'SELECT event_json FROM {store.events} WHERE revision=1')).scalar_one())
    receipt = event['locked_admission_receipt']
    assert receipt['unreflected_debit_upper_bound'] == '6.0000700001'
    assert receipt['account_risk_required'] == '6.0000700001'
    assert not receipt['external_coverage_certified_by_store']
    assert store.audit(CYCLE, max_events=2) == state


def test_schema_reapplication_preserves_existing_journal_and_guards(store):
    state = apply(store, reserve(store), 'entry_transport_started')
    with store.engine.begin() as c:
        for statement in schema_statements(store.cycles.split('.')[0]):
            c.execute(text(statement))
    assert store.audit(CYCLE, max_events=2) == state
    with pytest.raises(DBAPIError, match='append only'):
        with store.engine.begin() as c:
            c.execute(text(f'DELETE FROM {store.events}'))


def test_projection_failure_rolls_back_new_intent_event_as_one_transaction(store):
    state = reserve(store)
    schema = store.cycles.split('.')[0]
    with store.engine.begin() as c:
        c.execute(text(f"""CREATE FUNCTION {schema}.refuse_test_update() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'simulated projection failure'; END $$"""))
        c.execute(text(f"""CREATE TRIGGER refuse_test_update BEFORE UPDATE ON {store.cycles}
            FOR EACH ROW EXECUTE FUNCTION {schema}.refuse_test_update()"""))
    with pytest.raises(DBAPIError, match='simulated projection failure'):
        apply(store, state, 'entry_transport_started')
    assert store.audit(CYCLE, max_events=1) == state
