"""Replay every archived source frontier; compare all 61 fixed structural views."""
from collections import defaultdict
from dataclasses import asdict
from fractions import Fraction
from itertools import groupby
from pathlib import Path
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import sys
import time

ROOT = None  # supplied explicitly by the CLI; frozen research packet directory
MODULE = Path(__file__).resolve().parents[1]/'app/services/trading/momentum_neural/structural_tape_prefix.py'


def module(name,path,expected=None):
    blob=path.read_bytes()
    if expected: assert hashlib.sha256(blob).hexdigest()==expected
    spec=importlib.util.spec_from_file_location(name,path)
    out=importlib.util.module_from_spec(spec)
    sys.modules[name]=out
    spec.loader.exec_module(out)
    return out


def main():
    started=time.monotonic()
    engine_hash=hashlib.sha256(MODULE.read_bytes()).hexdigest()
    m=module('structural_prefix_packet_module',MODULE)
    prior=module('structural_prefix_packet_sources',ROOT/'audit_tick_scope_transitions.py',
        '813c45eea802af984ba396e68282a6adefd61bb1dcd60e08bc6b90aa85b607de')
    raw_verifier=module('structural_prefix_raw_classifier',ROOT/'verify_g_membership_transport_raw.py',
        '43084e45900895b51c9eb954822c5e5294e649456cb5b13f925d9a6da629d14b')
    raw=prior.load('ASTRA_TNON_FULL_RUN_TRAJECTORY.json.gz')
    tnon=[dict(zip(raw['columns'],r)) for r in raw['rows']]
    provfile=ROOT/'ASTRA_TNON_FULL_RUN_PROVENANCE.json.gz'
    assert hashlib.sha256(provfile.read_bytes()).hexdigest()=='b6df61d52e6b4899b384b0db13ecaa851bfd85aac97c867fc697e2148a276568'
    prov=json.loads(gzip.decompress(provfile.read_bytes()))
    assert [r[0] for r in prov['rows']]==[r['id'] for r in tnon]
    tnon=[{**r,**dict(zip(prov['columns'],p))} for r,p in zip(tnon,prov['rows'])]
    packets={'TNON_0911':tnon}
    for sid,sym in ((21589,'TPET'),(21592,'PCLA')):
        name=f'sep10_broker_cohort_streams/{sid}_{sym}_prints.jsonl.gz'
        blob=(ROOT/name).read_bytes()
        assert hashlib.sha256(blob).hexdigest()==prior.PINS[name]
        packets[str(sid)]=[json.loads(line) for line in gzip.decompress(blob).decode().splitlines() if line]
    snapshots=defaultdict(list)
    for packet,symbol,anchor,market in prior.snapshots():
        snapshots[packet].append((prior.ns(market['frontier']),anchor,market))
    summaries=[]; checkpoints=[]; all_checks=0
    for name,rows in packets.items():
        tick_rows=[m.Tick(r['id'],r['price'],r['size'],r['bid'],r['ask'],
            prior.ns(r['observed_at']),prior.ns(r['received_at']),prior.ns(r['available_at']),
            (r['bridge_run_id'],r['connection_generation'],r['bridge_version'],r['timestamp_basis']),
            r['source_frame_sequence'],r['source_frame_sha256']) for r in rows]
        assert len({r.epoch for r in tick_rows})==1
        assert all(a.known_ns<=b.known_ns and a.cursor<b.cursor for a,b in zip(tick_rows,tick_rows[1:]))
        frontier_rows=[list(group) for _,group in groupby(tick_rows,key=lambda r:r.known_ns)]
        max_front=max(map(len,frontier_rows))
        # Capacities are sufficient for this whole FILE, not selected strategy N.
        prefix=m.Prefix(name,'frozen_recorded_packet',m.Limits(len(rows),max_front,len(rows)))
        raw_labels=raw_verifier.classify(rows)
        raw_prefix={k:[Fraction(0)] for k in m.MASS_FIELDS}
        for r in rows:
            for k in m.MASS_FIELDS:
                raw_prefix[k].append(raw_prefix[k][-1]+Fraction(raw_labels[r['id']][k]))
        by_id={r['id']:i for i,r in enumerate(rows)}
        cursor=0; checks=0; append_seconds=0; max_append_seconds=0; peak_active=0
        def advance(group):
            nonlocal checks,append_seconds,max_append_seconds,peak_active
            receipt=m.FrontierReceipt(group[0].known_ns,len(group),m.rows_sha256(group),prefix.prefix_sha256)
            t=time.monotonic()
            result=prefix.append_frontier(group,receipt)
            dt=time.monotonic()-t
            append_seconds+=dt;max_append_seconds=max(max_append_seconds,dt)
            assert result.status=='applied',(name,result)
            peak_active=max(peak_active,len(prefix.active_references()))
            # Independent classifier at every appended row, including unknowns.
            for i in range(prefix.count-len(group),prefix.count):
                raw_mass=raw_labels[rows[i]['id']]
                expected_sign=1 if raw_mass['inferred_buy'] else -1 if raw_mass['inferred_sell'] else 0
                expected_quote=1 if raw_mass['quote_buy'] else -1 if raw_mass['quote_sell'] else 0
                assert prefix.label(i)==(expected_sign,expected_quote)
                checks+=1
        for stamp,anchor,expected in sorted(snapshots[name],key=lambda x:x[0]):
            while cursor<len(frontier_rows) and frontier_rows[cursor][0].known_ns<=stamp:
                advance(frontier_rows[cursor]);cursor+=1
            assert prefix.count==expected['eligible_n']
            assert prefix.tick(prefix.count-1).id==expected['last_known_print']['id']
            refs=prefix.active_references()
            expected_refs={r['reference_id']:r for r in expected['references']}
            assert {f'{r.kind}:{r.origin_id}' for r in refs}==set(expected_refs)
            digest=hashlib.sha256()
            for ref in refs:
                e=expected_refs[f'{ref.kind}:{ref.origin_id}']
                assert ref.confirmation_id==e['first_confirmation']['id']
                context=prefix.context(ref)
                role_names=('origin_to_latest_directed_extreme','latest_directed_extreme_to_latest_opposite_extreme',
                            'latest_opposite_extreme_to_frontier')
                expected_bounds=tuple(tuple(e['geometric_path_components'][role]['geometry_indices_inclusive']) for role in role_names)
                assert context['phase_bounds']==expected_bounds,(name,stamp,ref)
                for (origin,end),values,role in zip(context['phase_bounds'],context['phase_mass'],role_names):
                    expected_mass=e['geometric_path_components'][role]['flow']
                    for k,value in zip(m.MASS_FIELDS,values):
                        assert value==raw_prefix[k][end+1]-raw_prefix[k][origin+1]
                        assert value==Fraction(str(expected_mass[k])),(name,ref,k,value,expected_mass[k])
                        checks+=2
                for k,value in zip(m.MASS_FIELDS,context['whole_mass']):
                    assert value==Fraction(str(e['whole_extent']['flow'][k]))
                    assert value==raw_prefix[k][prefix.count]-raw_prefix[k][ref.origin_index+1]
                    checks+=2
                # Last plateau endpoint must link to the correct FIRST endpoint
                # for future completed_swing_facts candidate identity joins.
                first=ref.origin_index
                while first and rows[first-1]['price']==rows[ref.origin_index]['price']:first-=1
                assert (ref.plateau_first_index,ref.plateau_first_id)==(first,rows[first]['id'])
                digest.update(json.dumps({'reference':asdict(ref),'context':context},default=str,sort_keys=True).encode())
            checkpoints.append({'packet':name,'anchor':anchor,'frontier':expected['frontier'],
                'eligible_n':prefix.count,'active_references':len(refs),'matched':True,'context_sha256':digest.hexdigest()})
        # Exercise the remaining file rows; these are NEVER exposed to prior checkpoints.
        while cursor<len(frontier_rows):advance(frontier_rows[cursor]);cursor+=1
        result={'packet':name,'rows':len(rows),'source_frontiers':len(frontier_rows),'checkpoints':len(snapshots[name]),
            'raw_and_frozen_checks':checks,'peak_active_references':peak_active,
            'max_frontier_rows':max_front,'append_seconds':append_seconds,'max_append_seconds':max_append_seconds,
            'prefix_sha256':prefix.prefix_sha256,'final_active_references':len(prefix.active_references())}
        summaries.append(result);all_checks+=checks
        print(json.dumps(result),flush=True)
    assert hashlib.sha256(MODULE.read_bytes()).hexdigest()==engine_hash
    receipt={'at':datetime.now(timezone.utc).isoformat(),'status':'PASS','engine_sha256':engine_hash,
       'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'packets':summaries,
       'checkpoints':checkpoints,'total_checks':all_checks,'elapsed_seconds':time.monotonic()-started,
       'limits':['All 61 saved snapshots checked, not every intermediate scope at every frontier.',
                 'Recorded supplied frontier membership; no independent DB commit/provider-completeness proof.',
                 'Source labels are historical quote/tick inference, not aggressor truth/freshness.',
                 'No policy or runtime caller; no fillable PnL conclusion; same-author validation.']}
    (ROOT/'ASTRA_STRUCTURAL_PREFIX_PACKET_VERIFY.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k not in ('packets','checkpoints')}),flush=True)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-root',type=Path,required=True)
    args=parser.parse_args()
    ROOT=args.evidence_root.resolve(strict=True)
    main()
