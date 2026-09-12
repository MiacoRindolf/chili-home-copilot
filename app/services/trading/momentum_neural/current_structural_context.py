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

    def receipt(self, symbols, *, native_asset_ids=()):
        names = sorted(set(symbols))
        pub = self.publication
        views = {v.symbol: v for v in pub.snapshot.symbols} if pub else {}
        native = pub.snapshot.native_enrollment if pub else None
        aliases, assets, gaps_by_alias, gaps_by_id = {}, {}, {}, {}
        if native is not None:
            for view in views.values():
                identity = view.native_identity
                if identity is None:
                    continue
                assets.setdefault(identity.asset_id, []).append(view)
                for alias in identity.broker_symbols:
                    aliases.setdefault(alias, []).append(view)
            for gap in pub.snapshot.native_mapping_gaps:
                gaps_by_id.setdefault(gap.asset_id, []).append(gap)
                for alias in gap.broker_symbols:
                    gaps_by_alias.setdefault(alias, []).append(gap)

        def resolve(key, *, by_id=False):
            if native is None:
                return (None if by_id else views.get(key)), None
            candidates = (assets if by_id else aliases).get(key, [])
            current = [v for v in candidates if v.native_binding_current]
            gaps = (gaps_by_id if by_id else gaps_by_alias).get(key, [])
            identities = {v.native_identity.asset_id for v in current} | {g.asset_id for g in gaps}
            if len(identities) > 1 or len(current) > 1:
                return None, {'status': 'native_identity_ambiguous', 'native_asset_ids': sorted(identities)}
            if gaps:
                return None, {'status': 'native_mapping_gap', 'gaps': [asdict(g) for g in gaps]}
            if current:
                return current[0], None
            if candidates:
                return None, {'status': 'native_binding_not_current',
                    'native_asset_ids': sorted({v.native_identity.asset_id for v in candidates})}
            return None, None

        def state(key, *, by_id=False):
            view, unresolved = resolve(key, by_id=by_id)
            if unresolved is not None:
                return unresolved
            if view is None:
                return {'status': 'not_enrolled' if pub else 'unavailable'}
            return self._view_receipt(view)

        states = {}
        for symbol in names:
            states[symbol] = state(symbol)
        result = {'status': self.status, 'reason': self.reason,
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
        if native is not None:
            result['native_enrollment'] = asdict(native)
        if native_asset_ids:
            result['native_assets'] = {asset_id: state(asset_id, by_id=True)
                                       for asset_id in sorted(set(native_asset_ids))}
        return result

    def _view_receipt(self, view):
        wave = view.wave_context
        result = {'status': self.status if view.print_count else 'cold',
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
        if view.native_identity is not None:
            result.update(provider_symbol=view.symbol, native_asset_id=view.native_identity.asset_id,
                          native_binding_current=view.native_binding_current)
        return result


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
