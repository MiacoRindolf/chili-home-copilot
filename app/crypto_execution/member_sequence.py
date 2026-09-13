"""Immutable, appendable evidence with a content-bound hash chain.

Every node commits to its parent root, exact new member fields and total count.
Nodes retain only their own rows, so snapshots share history without copying or
serializing it again. Chunk boundaries are provenance, not strategy windows.
"""
from collections.abc import Sequence
from dataclasses import dataclass,field

from scripts.crypto_history_pages import canonical,sha
from scripts.crypto_trade_frames import CryptoTrade,CryptoQuote


@dataclass(frozen=True,eq=False)
class MemberSequence(Sequence):
    channel: str
    rows: tuple
    parent: 'MemberSequence | None'=field(default=None,repr=False)
    root: str=field(init=False)
    count: int=field(init=False)

    def __post_init__(self):
        kind={'trades':CryptoTrade,'quotes':CryptoQuote}.get(self.channel)
        if (kind is None or type(self.rows) is not tuple or any(type(r) is not kind for r in self.rows) or
                self.parent is not None and (type(self.parent) is not MemberSequence or self.parent.channel!=self.channel)):
            raise ValueError('native_member_sequence_typed_immutable_rows_required')
        if self.channel=='trades':
            content=[(r.symbol,r.trade_id,r.event_ns,str(r.price),str(r.size),r.reported_taker_side) for r in self.rows]
        else:
            content=[(r.symbol,r.event_ns,str(r.bid),str(r.ask),str(r.bid_size),str(r.ask_size)) for r in self.rows]
        count=(len(self.parent) if self.parent is not None else 0)+len(self.rows)
        root=sha(canonical(dict(contract='native_member_sequence_v1',channel=self.channel,
            previous=self.parent.root if self.parent is not None else None,count=count,rows=content)))
        object.__setattr__(self,'count',count);object.__setattr__(self,'root',root)

    def __len__(self):return self.count

    def __iter__(self):
        nodes=[];node=self
        while node is not None:nodes.append(node);node=node.parent
        for node in reversed(nodes):yield from node.rows

    def __getitem__(self,index):
        if isinstance(index,slice):return tuple(self)[index]
        if type(index) is not int:raise TypeError('member index must be an integer')
        if index<0:index+=self.count
        if not 0<=index<self.count:raise IndexError(index)
        node=self
        while node.parent is not None and index<len(node.parent):node=node.parent
        return node.rows[index-(len(node.parent) if node.parent is not None else 0)]

    def __add__(self,rows):
        if type(rows) is not tuple:raise TypeError('append requires immutable tuple')
        return MemberSequence(self.channel,rows,self) if rows else self
