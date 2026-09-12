"""Broker-held and pending coverage demands, independent of strategy sessions.

This inventory requests observation of exposure; it never authorizes an order,
claims an atomic account snapshot, or computes spendable buying power. Preserve
all returned positions and open-query orders, including fractional/notional,
sell, replacement and unfamiliar status records. Execution reconciliation owns
their financial interpretation.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading
from uuid import UUID

from .alpaca_paper_identity import alpaca_paper_account_identity_sha256
from .broker_asset_inventory import _json, _row, _sha, _text

# Alpaca GetOrdersRequest documents a maximum of 500. This is an API response
# boundary, not a strategy count/window. A full page cannot prove completeness.
OPEN_ORDER_RESPONSE_LIMIT = 500
REASONS = ("held", "pending")


@dataclass(frozen=True)
class CoverageMember:
    identity: str
    asset_id: str
    asset_class: str
    broker_symbol: str
    metadata_json: str


@dataclass(frozen=True)
class CoverageRead:
    reason: str
    started_ns: int
    completed_ns: int
    members: tuple[CoverageMember, ...]
    complete: bool
    error: str | None
    response_count: int | None
    content_sha256: str
    bracket_reads: tuple[CoverageRead, ...] = ()


@dataclass(frozen=True)
class CoverageProbe:
    account_identity_sha256: str
    started_ns: int
    completed_ns: int
    reads: tuple[CoverageRead, ...]
    observation_sha256: str
    atomic_account_snapshot: bool = False
    order_authority: bool = False


def _uuid(value):
    value = _text(value)
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError("coverage_identity_invalid")
    return value


def coverage_read(reason, *, started_ns, completed_ns, response=None, error=None,
                  expected_account_id):
    """Retain usable identities from incomplete reads without claiming coverage.

    If the broker returns an unexpected account ID, discard the entire read.
    Other malformed members make the read incomplete but do not erase valid
    observed identities. A full orders page is retained as incomplete evidence.
    """
    if reason not in REASONS or type(started_ns) is not int or type(completed_ns) is not int or not 0 < started_ns <= completed_ns:
        raise ValueError("coverage_read_scope_invalid")
    alpaca_paper_account_identity_sha256(expected_account_id)
    members = {}
    count = len(response) if type(response) is list else None
    problem = error
    if type(response) is not list:
        problem = problem or "coverage_response_unavailable"
    else:
        if reason == "pending" and count >= OPEN_ORDER_RESPONSE_LIMIT:
            problem = problem or "open_order_response_bound_reached"
        for item in response:
            try:
                row = _row(item)
                if row.get("account_id") is not None and str(_text(row["account_id"])) != expected_account_id:
                    members.clear()
                    problem = "coverage_member_account_mismatch"
                    break
                asset_id = _uuid(row.get("asset_id"))
                identity = asset_id if reason == "held" else _uuid(row.get("id"))
                asset_class, symbol = _text(row.get("asset_class")), row.get("symbol")
                if (type(asset_class) is not str or not asset_class
                        or type(symbol) is not str or not symbol or symbol != symbol.strip().upper()):
                    raise ValueError("coverage_asset_identity_invalid")
                row.update(asset_id=asset_id, asset_class=asset_class)
                if reason == "pending":
                    row["id"] = identity
                metadata = _json(row)
                member = CoverageMember(identity, asset_id, asset_class, symbol, metadata)
                if identity in members:
                    problem = problem or "coverage_duplicate_identity"
                    # Conflicting identities must not pick an arbitrary alias.
                    if members[identity] != member:
                        members.clear()
                        problem = "coverage_conflicting_identity"
                        break
                else:
                    members[identity] = member
            except (TypeError, ValueError, AttributeError):
                problem = problem or "coverage_member_invalid"
    ordered = tuple(members[k] for k in sorted(members))
    digest = _sha(_json([reason, [m.metadata_json for m in ordered], count, problem]))
    return CoverageRead(reason, started_ns, completed_ns, ordered, problem is None,
                        problem, count, digest)


def coverage_probe(*, expected_account_id, started_ns, completed_ns, reads):
    account = alpaca_paper_account_identity_sha256(expected_account_id)
    if (type(started_ns) is not int or type(completed_ns) is not int or not 0 < started_ns <= completed_ns
            or type(reads) is not tuple or len(reads) != len(REASONS)
            or any(type(r) is not CoverageRead for r in reads)
            or tuple(r.reason for r in reads) != REASONS
            or any(not started_ns <= r.started_ns <= r.completed_ns <= completed_ns for r in reads)):
        raise ValueError("coverage_probe_scope_invalid")
    digest = _sha(_json(["broker_coverage_inventory_v1", account, started_ns, completed_ns,
        [[r.reason, r.started_ns, r.completed_ns, r.content_sha256] for r in reads]]))
    return CoverageProbe(account, started_ns, completed_ns, reads, digest)


def bracket_pending_reads(before, after):
    """A changing orders bracket cannot release demand around a position read.

    Preserve newly seen IDs from either side, with latest metadata for stable
    asset identity. This is observational race detection, not broker atomicity.
    """
    if (type(before) is not CoverageRead or type(after) is not CoverageRead
            or before.reason != "pending" or after.reason != "pending"
            or before.completed_ns > after.started_ns or before.bracket_reads or after.bracket_reads):
        raise ValueError("pending_bracket_invalid")
    complete = before.complete and after.complete and before.content_sha256 == after.content_sha256
    error = None if complete else "pending_bracket_changed_or_incomplete"
    members = {m.identity: m for m in before.members}
    for member in after.members:
        prior = members.get(member.identity)
        if prior is not None and (prior.asset_id, prior.asset_class) != (
                member.asset_id, member.asset_class):
            members.clear()
            error = "pending_bracket_identity_conflict"
            complete = False
            break
        members[member.identity] = member
    ordered = tuple(members[k] for k in sorted(members))
    digest = _sha(_json(["pending_bracket_v1",
        [[r.started_ns, r.completed_ns, r.content_sha256] for r in (before, after)],
        [m.metadata_json for m in ordered], complete, error]))
    # response_count=None because this is two responses; exact counts are in
    # bracket_reads. len(members) is the union count, never a single-page proof.
    return CoverageRead("pending", before.started_ns, after.completed_ns,
                        ordered, complete, error, None, digest, (before, after))


@dataclass(frozen=True)
class CoverageState:
    revision: int
    probe: CoverageProbe | None
    held: tuple[CoverageMember, ...]
    pending: tuple[CoverageMember, ...]
    unavailable_reasons: tuple[str, ...]

    @property
    def membership_replacement_complete(self):
        return self.probe is not None and not self.unavailable_reasons


class CoverageBook:
    """Incomplete reads may ADD coverage, never silently release prior demand.

    Release requires both sections complete, including the stable orders
    bracket. Otherwise a previously pending order could disappear after filling
    while the held read failed. Either section can still ADD observed members
    independently. Old IDs remain conservatively retained until a complete
    paired observation succeeds; this is coverage retention, not a risk ledger.
    """
    def __init__(self, *, expected_account_id):
        self._account = alpaca_paper_account_identity_sha256(expected_account_id)
        self._lock = threading.Lock()
        self.state = CoverageState(0, None, (), (), REASONS)

    def apply(self, probe):
        if type(probe) is not CoverageProbe or probe.account_identity_sha256 != self._account:
            raise ValueError("coverage_book_account_mismatch")
        with self._lock:
            prior = self.state
            if prior.probe is not None and probe.started_ns < prior.probe.completed_ns:
                raise ValueError("coverage_observation_regression")
            result, unavailable = {}, []
            replace_membership = all(r.complete for r in probe.reads)
            for read in probe.reads:
                members = {} if replace_membership else {m.identity: m for m in getattr(prior, read.reason)}
                if not read.complete:
                    unavailable.append(read.reason)
                for member in read.members:
                    previous = members.get(member.identity)
                    if previous is not None and (previous.asset_id, previous.asset_class) != (
                            member.asset_id, member.asset_class):
                        raise ValueError("coverage_retained_identity_conflict")
                    # Native UUID/class identify exposure. Symbol aliases may
                    # change across observations; retain the latest spelling
                    # without dropping the held/pending reason. Original
                    # spellings remain in the source/bracket observations.
                    members[member.identity] = member
                result[read.reason] = tuple(members[k] for k in sorted(members))
            self.state = CoverageState(prior.revision+1, probe, result["held"], result["pending"], tuple(unavailable))
            return self.state
