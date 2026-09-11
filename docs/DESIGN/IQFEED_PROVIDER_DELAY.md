# IQFeed provider Delay retention — planner [39]

Every exact selected-field Q trade now retains `Delay` as
`iqfeed_trade_ticks.provider_delay_minutes`. The behavior is unconditional.
DTN documents the field in **minutes** in its
[FIDs reference](https://iqhelp.dtn.com/fids/) and
[Snap Quote reference](https://iqhelp.dtn.com/snap-quote-overview/).

Explicit ASCII whole numbers from zero through PostgreSQL INTEGER's maximum
are stored unchanged. Blank, malformed, negative, non-integer, and out-of-range
values become NULL without discarding the trade. Historical rows and legacy
producers remain NULL. NULL is unknown, not a declaration of real-time data.
There is no inferred value, cached last-known value, historical backfill, or
new strategy cutoff. The storage bound is the signed 32-bit SQL type limit.

The column accompanies the existing source-frame identity and causal clocks.
The bridge configuration records its field, unit, column, and null policy.
COPY, execute_values, and the SQLAlchemy VALUES fallback preserve the same
value; both tape benchmarks include NULL, zero, and positive-value fixtures.
The captured-paper replay envelope is a separate contract and is unchanged.

Migration `378_iqfeed_provider_delay_minutes` adds the nullable column without
a default or index. Apply it through the app's migration owner **before**
starting the new bridge build. The bridge's read-only startup schema gate
requires the column before opening its provider connection. Existing bridges
remain compatible with the additive schema. Bridge source changes also change
the content-addressed build pin; use the existing reviewed deployment/pin flow.
The lane and bridge must not be restarted during an active trading window.

After the permitted deployment, verify the new migration/build/run and the
first captured rows with queries bounded by symbol and an actual provider-time
interval. Report counts of NULL, zero, and positive Delay with the run id and
row clocks; do not label NULLs real time or require zero NULLs without observing
the provider's wire behavior. The [38] arm and [32] frontier policies remain
unchanged: provider Delay does not replace transport freshness, because a
real-time entitlement can still reach the database late.
