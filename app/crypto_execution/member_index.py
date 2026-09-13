"""Immutable lookup/range indexes derived from exact retained members."""
from bisect import bisect_left,bisect_right
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class MemberIndex:
    by_key: object
    by_symbol: object
    times: object

    def within(self,symbol,start,end):
        times=self.times[symbol]
        return self.by_symbol[symbol][bisect_left(times,start):bisect_right(times,end)]


def build_index(rows,symbols,key):
    members={};groups={s:[] for s in symbols}
    for row in rows:
        members[key(row)]=row;groups[row.symbol].append(row)
    ordered={s:tuple(sorted(values,key=lambda r:r.event_ns)) for s,values in groups.items()}
    times={s:tuple(r.event_ns for r in values) for s,values in ordered.items()}
    return MemberIndex(MappingProxyType(members),MappingProxyType(ordered),MappingProxyType(times))


def extend_index(index,rows,key):
    if not rows:return index
    members=dict(index.by_key);ordered=dict(index.by_symbol);times=dict(index.times);groups={}
    for row in rows:
        members[key(row)]=row;groups.setdefault(row.symbol,[]).append(row)
    for symbol,values in groups.items():
        additions=tuple(sorted(values,key=lambda r:r.event_ns));old=ordered[symbol]
        if not old or additions[0].event_ns>=old[-1].event_ns:
            ordered[symbol]=old+additions
            times[symbol]=times[symbol]+tuple(r.event_ns for r in additions)
        else:
            ordered[symbol]=tuple(sorted(old+additions,key=lambda r:r.event_ns))
            times[symbol]=tuple(r.event_ns for r in ordered[symbol])
    return MemberIndex(MappingProxyType(members),MappingProxyType(ordered),MappingProxyType(times))
