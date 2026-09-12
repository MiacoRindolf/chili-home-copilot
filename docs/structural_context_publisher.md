# Supervised ordinary PAPER context publisher

`StructuralContextPublisher` owns the recoverable reducer for the same canonical
account-identity stream read by ordinary PAPER selection. This lifecycle does not
place broker orders. Its hosting process must supply provider-bound demand
observations and own its thread/stop event. It is not yet installed in the running
application. Native broker UUID inventory now has a separate durable checkpoint
service (see broker_native_demand_checkpoint.md). Typed provider binding now feeds
durable enrollment and native consumer lookup (native_tick_enrollment.md); actual
application lifecycle wiring is now implemented (paper_shared_tick_host.md).
Coordinated source/schema rollout and actual market throughput remain unverified.

Startup checks every required source/output/input table under a read-only
repeatable-read transaction with a20s statement timeout. Missing migration tables
return `waiting_schema` without creating anything. With schemas present but no
actual source release, startup returns `waiting_source`; it does not manufacture
an empty source frontier. A pre-existing context stream always takes fenced exact
recovery. Missing history, incompatible code or changed reducer resource limits
cannot silently create a replacement cold generation. Competing writers fail
without disturbing the existing owner.

The supervisor callback receives immutable demand checkpoints, including restored
source revisions and retained members after incomplete observations. It supplies
only newer, provider-bound observations. None means no new observation; a failed
read must either explicitly be incomplete or terminate the worker. It can never
erase held/pending observation demand by masquerading as a complete empty list.
For native membership the callback returns `NativeTickEnrollment`, including its
mapping reference and explicit gaps. Legacy dictionaries remain for independent
watch/ranking observations. After native binding, legacy dictionaries cannot
overwrite native inventory/held/pending roles.

Each step commits one whole source publication. The loop catches up without a
clock wait between advancing releases. Its explicit idle wait schedules database
I/O only; it never forms a strategy window. No-op source reads append no recovery
records. An unresolved release is durably reported and the worker releases its
writer fence. It cannot skip that release or silently cold-start past it. Graceful
stop, failed demand reads and failed startup likewise close only the worker's own
database session. No bridge process or broker position is altered.

`PublisherBudget` contains caller-supplied allocation/replay work bounds. They are
not empirical trading constants, rank cutoffs or selected wave horizons. Increasing
a budget is an explicit recovery/configuration change, not a reason to reset
history invisibly. `writer_present` continues to mean only database fence presence;
it does not certify completed recovery, provider health, complete symbol coverage,
fresh quotes or order authority.

## Remaining coordinated release sequence

1. Connect the publisher host to the durable native-demand/provider-binding input.
   Changed-symbol publications now replace repeated whole-universe serialization;
   use the measured full-catalog results in changed_symbol_context_publications.md
   when configuring the host, then verify actual source lag under load.
   The native checkpoint service is implemented; run its full catalog refresh and
   recovery outside the per-print path (measured about5s each for14,381 members).
   Retain unbound assets explicitly; no slash/hyphen stripping or inferred crypto
   IQFeed binding. Complete the measured policy consumer independently of this
   descriptive publisher.
2. Compose and verify the exact release against current main. Migrations381–383
   and native checkpoint migration384 are registered in this branch;379/380 belong to other pending work and are not
   registered here. Normal application startup runs registered migrations under
   its existing schema lock; the publisher and bridge never create these schemas.
3. With an account-pinned flat PAPER boundary and the owned application/supervisor
   stopped, coordinate all actual source writers and their restart ownership before
   changing the lane checkout. The existing generic post-close bridge helper kills
   every matching Python command and is not an adequate exact-process rollout
   gate. Do not invoke it merely because the bridge source hash changed.
4. Apply the exact application release/migrations, then start the instrumented
   trade producer and publisher under their owners. Verify actual new release rows,
   process/build identity and the same canonical stream seen by selection. Missing
   weekend prints remain `waiting_source`; source absence cannot pass as coverage.
5. Verify cold/warm status, native demand coverage, current-source lag and consumer
   revisions before claiming that new entry/exit behavior is active. Continue PAPER
   integration and deeper studies after each coherently executable release.

The running lane was last verified at491ea62 with source migrations absent. This
document is deployment preparation, not a deployment receipt or a trading-edge
claim. The operator's paper-first release authorization remains in force.
