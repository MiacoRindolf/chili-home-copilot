# Selected crypto PAPER routes cannot fall back to LIVE

When `chili_momentum_crypto_execution_via_alpaca_paper` is selected, missing
PAPER configuration or an unavailable Alpaca listing now raises a typed routing
refusal. The previous resolver returned `coinbase_spot` in those cases and relied
on a separate auto-arm filter to prevent a LIVE order. Routing failure is no
longer successful auto-arm broker readiness, even with cached ready families.

Native Alpaca slash identifiers (including non-USD quotes) follow the crypto
routing and auto-arm posture checks. Legacy USD pairs and the known numeric base
remain supported; a dashed equity/warrant ticker is not reclassified as crypto.
This is identifier routing, not certification of non-USD funding or execution.

A dedicated `momentum_exec_only` process with the crypto PAPER route selected
also skips generic Robinhood/Coinbase startup restoration, including direct
calls to the restore function. This holds when the equity PAPER flag is off.

The existing equity-only order quarantine remains in force. These corrections
do not enable crypto trading, alter owned positions' explicit execution families,
or establish fractional order/fill/fee/protection accounting. Those integration
steps must be implemented before the crypto lane can make PAPER trades.

Validation covers missing PAPER fields, unavailable/raising listing probes,
classifier failures, native pairs, legacy identifiers, dashed equities, cached
auto-arm readiness, full-pass refusal without Coinbase connect, and direct
startup restore without broker/vault access. The historical
`test_market_closed_equity_skipped` failure was reproduced on unchanged main
`0b0dec7c740529285c9bc52244f079131d800edf`; it is not reported as passing.
