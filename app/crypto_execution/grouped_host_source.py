"""Explicit V1-sealed transition adapter for the application source owner."""
from pathlib import Path

from scripts.crypto_history_pages import canonical
from .frontier_capture import FrontierCapture
from .host_config import read_json
from .legacy_seed import load_sealed_seed
from .source_capture import NativeSourceCapture


class GroupedHostSource:
    def __init__(self,root,config,metadata,stack):
        root=Path(root).resolve(strict=True)
        manifest,digest=read_json(config['grouped_source_manifest_path'],config['max_event_bytes'])
        if (digest!=config['grouped_source_manifest_sha256'] or type(manifest) is not dict or
                set(manifest)!={'contract','legacy_directory','grouped_directory','seed_origin'} or
                manifest['contract']!='native_grouped_source_transition_v1'):
            raise ValueError('native_grouped_transition_manifest_changed')
        legacy=Path(manifest['legacy_directory']);grouped=Path(manifest['grouped_directory'])
        if (not legacy.is_absolute() or not grouped.is_absolute() or
                legacy.resolve()!=root/'source' or grouped.resolve()!=root/'source-grouped-v1'):
            raise ValueError('native_grouped_transition_directory_changed')
        if (legacy/'source.jsonl').stat().st_size!=manifest['seed_origin']['journal_bytes']:
            raise ValueError('native_grouped_transition_unsealed_history_remains')
        seed=load_sealed_seed(legacy,manifest['seed_origin'])
        if (legacy/'source.jsonl').stat().st_size!=seed.origin['journal_bytes']:
            raise ValueError('native_grouped_transition_legacy_writer_changed')
        if canonical(seed.metadata)!=canonical(metadata):raise ValueError('native_grouped_transition_metadata_changed')
        self.metadata=metadata;self.seed_revision=seed.origin['revision'];self.manifest_sha=digest
        factory=object.__new__(NativeSourceCapture);factory.metadata=metadata;factory.resources=metadata['resources']
        resources={k:metadata['resources'][k] for k in ('max_pages','max_trades','max_quotes','max_page_bytes','max_journal_bytes')}
        self.capture=stack.enter_context(FrontierCapture(grouped,seed=seed.batch,seed_origin=seed.origin,
            seed_published_ns=seed.publication_record['raw_published_ns'],book_factory=factory._new_book,resources=resources))

    def observe(self,end_ns,get):
        return self.capture.observe(end_ns,get,page_limit=self.metadata['page_limit'])

    def collect_audit(self,get):
        return self.capture.collect_audit(None,get,page_limit=self.metadata['page_limit'])

    def publish_audit(self):return self.capture.publish_audit()

    def current_views(self):return self.capture.current_views()

    def status(self):
        value=self.capture.status()
        return dict(value,source_id=self.metadata['source_id'],source_kind='rest_pages',
            revision=self.seed_revision+value['revision'],grouped_revision=value['revision'],
            legacy_seed_revision=self.seed_revision,transition_manifest_sha256=self.manifest_sha,
            observation_mode='grouped_frontiers_with_full_anchor_audits',revision_can_include_late_events=True,
            print_counts={s:p.count for s,p in self.capture.reducer.book.prefixes.items()})
