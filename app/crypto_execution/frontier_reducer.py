"""Publish grouped REST input through the existing exact tick reducer.

This is a single-writer component. The caller retains every raw observation
before apply and fsyncs publication records through ``record``. Failed apply or
publication invalidates this instance: reconstruct from the seed and committed
observations, not from a partially mutated in-memory book.
"""
import json

from scripts.crypto_history_pages import canonical, sha
from .history_source import _event_messages
from .frontier_members import seed_members, merge_members
from .source_capture import publication


class FrontierReducer:
    def __init__(self,seed,*,book_factory,published_ns,max_trades,max_quotes):
        self.members=seed_members(seed);self.factory=book_factory
        self.max_trades=max_trades;self.max_quotes=max_quotes
        if any(type(n) is not int or n<=0 for n in (max_trades,max_quotes)):
            raise ValueError('native_frontier_reducer_resources_required')
        if len(self.members.trades)>max_trades or len(self.members.quotes)>max_quotes:
            raise ValueError('native_frontier_seed_capacity')
        self.book=self._new_book()
        self.book.append_rest_batch(seed,published_ns=published_ns)
        self.revision=0;self.valid=True
        self._views=tuple(self.book.view(s) for s in self.members.symbols)
        self.last_publication=None

    def _new_book(self):
        book=self.factory()
        if (book.source_kind!='rest_pages' or book.location!=self.members.location or
                tuple(book.assets)!=self.members.symbols or book.sequence!=0 or book.failure is not None or
                hasattr(self,'book') and book.identity!=self.book.identity):
            raise ValueError('native_frontier_seed_book_binding_invalid')
        return book

    def current_views(self):
        if not self.valid:raise ValueError('native_frontier_reconstruction_required')
        return self._views

    def apply(self,observation,*,published_ns,record):
        if not self.valid:raise ValueError('native_frontier_reconstruction_required')
        merged=merge_members(self.members,observation,max_trades=self.max_trades,max_quotes=self.max_quotes)
        if type(published_ns) is not int or published_ns<max(self.book.published_ns,merged.state.known_ns):
            raise ValueError('native_frontier_publication_clock_invalid')
        previous=publication(self.book)
        reconstruct=bool(merged.reconstruction_symbols)
        rows=(merged.state.trades,merged.state.quotes) if reconstruct else (merged.new_trades,merged.new_quotes)
        normalization=dict(contract='native_grouped_rest_normalization_v1',observation_sha256=observation.identity,
            previous_publication=previous,member_state_sha256=merged.state.identity,
            mode='current_revision_reconstruction' if reconstruct else 'new_observed_members',
            combined_known_ns=merged.state.known_ns,
            provider_event_time_finality_certified=False,equal_timestamp_source_order_certified=False)
        try:
            candidate=self._new_book() if reconstruct else self.book
            # This is a normalized grouped observation, not a fabricated HTTP
            # request with one uniform start. Its digest binds every real group.
            candidate._append(canonical(_event_messages(*rows)),candidate.frame_sequence+1,
                merged.state.known_ns,published_ns,rest_evidence=sha(canonical(normalization)))
            candidate._rest_end=merged.state.through_ns
            views=tuple(candidate.view(s) for s in self.members.symbols)
            body=dict(kind='frontier_context_published',contract='native_frontier_reducer_publication_v1',
                revision=self.revision+1,observation_sha256=observation.identity,
                member_state_sha256=merged.state.identity,published_ns=published_ns,
                reconstruction_symbols=merged.reconstruction_symbols,normalization=normalization,
                view=publication(candidate),order_authority=False)
            # Publication must be durable before exposing new immutable views.
            body=json.loads(canonical(body))
            record(json.loads(canonical(body)))
            self.book=candidate;self.members=merged.state;self._views=views
            self.revision+=1;self.last_publication=body
            return json.loads(canonical(body))
        except Exception:
            self.valid=False
            raise
