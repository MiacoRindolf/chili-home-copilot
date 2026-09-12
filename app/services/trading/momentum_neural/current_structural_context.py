"""Current shared evidence for ordinary PAPER selection; no event replay claim."""
from dataclasses import asdict, dataclass

import sqlalchemy as sa

from scripts.iqfeed_print_publications import capture_frontier
from .alpaca_paper_identity import alpaca_paper_account_identity_sha256
from .structural_context_journal import read_current_context, ContextPublication

# One complete snapshot allocation cap, not a candidate or tick-count horizon.
# Oversized snapshots remain explicitly unavailable; no symbol payload is cut.
CURRENT_CONTEXT_READ_BYTES = 16 * 1024 * 1024


def ordinary_paper_context_stream(expected_account_id):
    return 'ordinary-paper:' + alpaca_paper_account_identity_sha256(expected_account_id)


@dataclass(frozen=True)
class CurrentStructuralObservation:
    status: str
    publication: ContextPublication | None
    current_source: object | None
    writer_present: bool
    reason: str | None = None

    def receipt(self, symbols):
        names = sorted(set(symbols))
        pub = self.publication
        views = {v.symbol: v for v in pub.snapshot.symbols} if pub else {}
        states = {}
        for symbol in names:
            view = views.get(symbol)
            if view is None:
                states[symbol] = {'status': 'not_enrolled' if pub else 'unavailable'}
                continue
            wave = view.wave_context
            states[symbol] = {'status': self.status if view.print_count else 'cold',
                'prefix_sha256': view.prefix_sha256, 'print_count': view.print_count,
                'last_print_id': view.last_print.id if view.last_print else None,
                'local_phase': asdict(wave.local_phase) if wave else None,
                'parent_phase': asdict(wave.parent_phase) if wave else None,
                'parent_status': wave.parent_status if wave else 'local_unavailable',
                'quote_freshness': view.quote_freshness,
                'flow_references': [{'basis': e.reference.basis, 'kind': e.reference.kind,
                    'order': e.reference.order, 'origin_id': e.reference.origin_id,
                    'confirmation_id': e.reference.confirmation_id,
                    'formation_bid_change': str(e.formation.bid_change) if e.formation.bid_change is not None else None,
                    'follow_bid_change': str(e.follow_through.bid_change) if e.follow_through.bid_change is not None else None,
                    'follow_conditional_quote_net_bounds': [str(v) for v in e.follow_through.conditional_quote_net_bounds]}
                    for e in view.wave_evidence]}
        return {'status': self.status, 'reason': self.reason,
            'context_cursor': asdict(pub.cursor) if pub else None,
            'context_source': asdict(pub.snapshot.source) if pub else None,
            'context_status': pub.snapshot.status if pub else None,
            'context_reason': pub.snapshot.reason if pub else None,
            'current_source': asdict(self.current_source) if self.current_source else None,
            'writer_present': self.writer_present,
            'stale_demand_sources': list(pub.snapshot.stale_demand_sources) if pub else None,
            'symbols': states, 'event_history_replayed': False, 'event_offset_advanced': False,
            'order_authority': False, 'provider_completeness_certified': False,
            'admission_policy': 'legacy_rules; shared_context_observation_only'}


def read_observation(c, *, stream_id, max_payload_bytes=CURRENT_CONTEXT_READ_BYTES):
    read = read_current_context(c, stream_id=stream_id, max_payload_bytes=max_payload_bytes)
    source = capture_frontier(c)
    pub = read.publications[-1] if read.publications else None
    status, reason = 'not_published', None
    if pub is not None:
        if pub.snapshot.source.epoch != source.epoch:
            status, reason = 'source_gap', 'source_epoch_changed'
        elif pub.snapshot.source.revision != source.revision:
            status, reason = 'source_gap', 'source_revision_not_current'
        elif pub.snapshot.status != 'observed_prefix':
            status, reason = pub.snapshot.status, pub.snapshot.reason
        else:
            status = 'observed_prefix'
    if not read.writer_present:
        status, reason = 'writer_absent', reason or 'shared_context_writer_not_present'
    return CurrentStructuralObservation(status, pub, source, read.writer_present, reason)


def observe_selection(db, *, symbols, expected_account_id, decision_at=None):
    """Separate read-only transaction cannot poison the ORM arm transaction.

    Missing schema/stream is explicit while the source rollout is pending.
    This first caller integration records evidence only and changes no rule.
    """
    if decision_at is not None:
        # Current reads cannot be attached to an earlier caller-selected
        # decision instant. Such callers need an explicitly bound source cursor.
        return CurrentStructuralObservation('unavailable', None, None, False,
            'explicit_decision_frontier_requires_bound_context').receipt(symbols)
    try:
        stream = ordinary_paper_context_stream(expected_account_id)
        with db.get_bind().connect().execution_options(isolation_level='REPEATABLE READ') as c:
            c.execute(sa.text('SET TRANSACTION READ ONLY'))
            c.execute(sa.text("SET LOCAL statement_timeout='20s'"))
            observation = read_observation(c, stream_id=stream)
        return observation.receipt(symbols)
    except Exception as exc:
        # SQL/connection text may contain configuration; retain category only.
        reason = str(exc) if type(exc) is ValueError and str(exc).startswith('context_') else type(exc).__name__
        if getattr(getattr(exc, 'orig', None), 'pgcode', None) == '42P01':
            reason = 'shared_context_schema_not_installed'
        return CurrentStructuralObservation('unavailable', None, None, False, reason).receipt(symbols)
