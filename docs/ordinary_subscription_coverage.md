# Ordinary feed coverage: independent demand before benchmarks

The operator's target is full opportunity coverage and concurrent tick evaluation,
not a ranked shortlist of symbols to consider. Inventory, actual valid tick
coverage, evaluated setups and funded positions are different measured sets. A
feed resource gap is not a negative trading signal. If the joint account and
execution constraints admit all qualified opportunities, a top1/topN rule must
not discard some of them.

## Actual shared L1/L2 policy correction

Both ordinary bridges call the same `resolve_subscription_target`. Ross-only
comparison symbols now use spare capacity after independent active, hint,
eligible and retained demand. Ross membership cannot promote an overlapping
symbol; changing its order, removing it or failing its query cannot freeze or
reorder the independent set. Ross is no longer a mandatory dynamic source.
Benchmark errors remain diagnostic records. This does not yet remove Ross from
the legacy eligibility builder, auto-arm or sizing code.

An active-session query failure now preserves all previously known ACTIVE names
before reserving space for new hints. Previously the generic retained-roster
branch could reserve the only slot for a hint and remove the old ACTIVE name.
When protected demand exceeds provider capacity, the resolver retains the demand
and emits `protected_targets_exceed_capacity`. It does not pretend that the feed
can serve it; actual provider admission/coverage still needs verification.

Other retained independent symbols displaced under capacity pressure now appear
in explicit capacity-gap records. Prior Ross-only coverage cannot reserve slots
against independent demand. A successful authoritative empty ACTIVE result can
release that source's old membership; an error cannot. ACTIVE here is still the
existing execution-session source, not a newly complete broker position inventory.
Actual held/pending account truth remains required in shared-owner integration.

The cached Ross reader now distinguishes a successful empty result from an
unavailable refresh. A real provider exception preserves its prior successful
cache and its known source retry interval. These clocks govern provider cache
transport, not strategy windows. The actual writer test verifies that a blocked
benchmark refresh does not block tape drain or unwatch a currently ACTIVE symbol.

## Evidence and scope

The new regression scenarios produced12 failures and3 passes before the fix.
After the policy correction, the first policy batch had86 passes and6 failures:
all six were old assertions explicitly requiring Ross to displace eligible or
onset coverage. They were rewritten to test the operator's new requirement,
preserving cause metadata, deterministic output and explicit capacity loss.
The next policy/provider batch had110 passes and2 pre-existing neighbor failures:
a test modeled provider failure with an authoritative empty result, and a drain
fixture lacked the current byte/collapse arguments. The failure test now raises
an actual provider exception; authoritative empty results have a separate test.
The drain test retains a currently ACTIVE name while a benchmark load is blocked.
The final policy/provider/print-journal/owner batch passed142 tests in62.61s.
Counts overlap and are not a whole-project suite result.

This is an actual bridge policy correction in an isolated worktree. No running
bridge, lane, subscription socket or broker order was changed during the build.
The existing `_eligible_symbols_read` still reads viability/age-filtered rows and
truncates by its upstream limit. It is not the full broker-tradable universe.
The shared tick owner is not yet its subscription/lifecycle authority. Real
selection/entry/exit consumer wiring, tick-native setup qualification, simultaneous
account allocation, full inventory/coverage accounting, crypto integration and
coordinated reviewed deployment remain required. No complete opportunity capture
or profitability claim follows from these tests.
