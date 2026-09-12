# Native crypto funding and standalone probe imports

The actual PAPER account read at2026-09-12T13:25:11Z reported ordinary buying
power40531.04USD and non-marginable buying power10132.76USD, a fourfold difference.
Account and crypto status were ACTIVE; native blocked fields were false. The
ordinary account snapshot did not expose the latter funding field. Native crypto
funding must not substitute equity margin buying power for this broker value.

`get_crypto_account_truth` now returns exact native funding/readiness fields,
the bound account UUID, a unique read ID, and local request/receive nanoseconds.
It preserves signed cash, missing values and accrued-fee observations without
declaring fee completeness. Account/crypto status and native boolean readiness
must be known; unsupported/missing account currency is explicit. These clocks
describe a REST observation, not a tick strategy window or provider event time.

The pure residual calculation is:

    remaining bound = max(0, observed non-marginable BP
                           - sum(local debit upper bounds - proven reflected parts))

Every claim must match the account and quote currency, and a nonzero reflected
part must match this exact read ID. Reflection is not inferred from broker order
presence, status or elapsed time. If reflection is unknown, its proven part is
zero; the possible conservative overlap remains visible. This arithmetic does
not itself establish reflection, hold a lock, reserve money or authenticate an
input. Durable account-wide ledger integration remains necessary before it can
admit concurrent execution. The current production caller is the isolated
mechanical probe, which first proves a flat/no-pending account and uses no claims.

The account endpoint supplies USD funding only. BTC/USDC/USDT-quoted pairs need
actual quote-asset balance/ownership/reservation evidence; this function reports
that missing source rather than pretending a stablecoin equals USD or silently
spending leveraged USD. The full native source universe remains73 pairs.

Actual read-only integration exposed a regression in the earlier full-close
probe: importing a pure crypto helper through `app.services.trading` eagerly
loaded the application and required DATABASE_URL. The new funding import caught
that before any order; the prior position helper could otherwise have encountered
it after entry. No crypto order has been submitted. Pure truth/funding code now
lives in `app.crypto_execution`; the previous venue path re-exports truth types
for compatibility. The probe imports all pure helpers before broker operations.

117 targeted funding/probe/fractional-truth/fill checks passed5.12s, including a
fresh isolated interpreter with no database/app settings. It exercises funding
and full-close position validation and asserts that app.config, trading startup,
SQLAlchemy and Alpaca SDK were not loaded. Real PAPER GETs verified the native
funding reader, then a standalone read-only plan completed with broker-derived
BTC quantity0.000012941 and limit77301.400000000USD: notional ceiling
1.000357417400000000USD. This is a mechanical minimum-size plan, not a signal or
order. The initial failed and corrected captures are retained separately.

Remaining project9 work: atomic ledger/reflection binding, non-USD quote funding,
native crypto shared tick/quote ingestion, supported protection, ambiguous-submit
reconciliation and entry/full-sale/re-entry wiring. This change does not activate
crypto strategy orders or relax the ordinary equity order quarantine.

Primary contract: [Alpaca crypto orders, fees and non-marginable funding](https://docs.alpaca.markets/us/docs/crypto-trading).
