# Native crypto application host

The execution-only PAPER app can now own the native source, selection, atomic
multi-symbol reservation and full-sale cycle workers. It starts from the normal
application startup path and shuts down with that application. The local
`/healthz/native-crypto` route reports each worker without making broker calls.

The host requires an explicit absolute configuration file through
`CHILI_MOMENTUM_NATIVE_CRYPTO_HOST_CONFIG_PATH`, the existing PAPER account pin
and crypto PAPER routing posture. It validates the actual accepted supervisor
receipt, parent process generation, code/environment and PostgreSQL ownership
lease. It cannot start a launcher or acquire another process's ownership.
Configuring this host excludes crypto from the legacy execution-family route,
including when the native configuration fails, to prevent a second producer or
fallback to another venue. Equity routing is unchanged.

Source acquisition, candidate admission and held-cycle reconciliation have
separate workers. Every eligible USD-quoted native candidate participates in the
locked allocation. Every active owned cycle is revisited, including reservations
from an earlier app process; an error in one does not skip the others. Current
selection receipts precede reservations and order decisions. A recovered source
prefix cannot authorize entry until this app run observes a new publication.
Transport scheduling and provider rate-reset evidence are operational clocks;
they do not select a print window or reset wave history.

Validation uses isolated PostgreSQL DB26 and simulated literal-PAPER transport.
The host tests cover two simultaneous eligible reservations, replay idempotence,
evidence-write failure, whole-balance exits after partial fills, source outage
with continued position reconciliation, recovery, process lifecycle, bootstrap
assembly, provider backoff and exclusion of the legacy crypto producer. Actual
Alpaca fills and production deployment are separate evidence, not test outcomes.

The implementation is an experimental PAPER path. It does not establish optimal
profit capture, full instantaneous coverage or continuous weekend capacity. The
REST source still revisits the acquisition prefix to recover late events, and
its work/journal size grow. Resource exhaustion stops new source observations
visibly while broker reconciliation remains alive; an unavailable source cannot
invent an exit quote. Published fee scenarios are not actual account fee proof.
Non-USD quote funding, live membership transitions, sustainable source retention,
and ownership across the existing supervisor window remain follow-up work.
