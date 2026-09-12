# Application-owned PAPER shared tick observation

The dedicated `momentum_exec_only` PAPER application starts native observation
and structural publication workers after durable risk restoration and scheduler
startup. Catalog parsing and broker HTTP stay outside the publication worker.
The host has no order methods and does not change entry/exit policy.

`CHILI_MOMENTUM_SHARED_TICK_HOST_CONFIG_PATH` supplies a `paper_shared_tick_host_v1`
JSON resource/catalog receipt. It is a required dependency, not an enable flag.
The exact schema is validated by `paper_context_host_config.load_config` and
demonstrated in `tests/test_paper_context_host.py`. There are no default strategy
windows. Resource bounds reject the complete operation rather than truncating a
successful universe. The config hash is exposed by `/healthz/shared-tick`.

The native worker owns a stable account-scoped PostgreSQL session fence and a
dedicated literal PAPER GET transport. Every inventory/exposure probe brackets
its reads with the pinned account identity. Pending orders bracket positions.
Redirects, oversized responses and failed reads cannot become complete empties.
Both active asset classes are retained; native crypto remains an explicit source
gap until its own adapter is connected. Provider catalog matching does not prove
effective freshness, live subscriptions, real-time entitlement or tick coverage.

The durable latest native checkpoint precedes mapping delivery. A single-slot
acknowledged handoff prevents the host from silently discarding observations it
has made. It backpressures broker refresh while publication cannot accept demand.
This checkpoint is not a historical broker event stream: orders/fills occurring
between REST observations are not certified as captured. Status distinguishes
native observation, broker-read completeness and the reference actually committed
to shared context. Source receipts remain event/print based.

The publisher owns a separate LISTEN connection for `iqfeed_print_publications`.
Notifications and local handoff signals wake it; actual contiguous journal reads
determine every release. The idle fallback and native refresh waits schedule I/O
only. Missing source/schema is visible. A competing owner, corrupt recovery or
other worker failure terminates this host; it cannot silently cold-reset a stream.

Shutdown signals both workers before a bounded join. Still-running workers are
reported as stopping. The lifecycle lock prevents deferred startup from starting
workers after shutdown. Worker-owned sessions are closed by those workers, and
LISTEN/advisory-lock sessions are invalidated rather than returned to the pool.
No endpoint read triggers database or broker requests.

Rollout requires coordinated source instrumentation and migrations381–384, the
pinned resource/catalog input, the PAPER account pin and the existing process
ownership checks. The running lane and bridges have not been changed by adding
this host. `/healthz` remains liveness only; `/healthz/shared-tick` reports actual
dependencies and revisions, not trading readiness.

Verification used the actual PAPER inventory and full pinned provider archive in
an isolated database:13,426 enrolled tradable equities;9 equity/73 crypto demand
mapping gaps. Cold enrollment35.97s; complete native/context warm recovery36.02s;
one synthetic three-print AAPL release woke and published in0.258s. These are
diagnostic cases, not production throughput, quote-timing or profitability claims.
No broker order was submitted and the temporary schema was removed. Targeted
worker, transport, startup, publisher and checkpoint tests passed.
