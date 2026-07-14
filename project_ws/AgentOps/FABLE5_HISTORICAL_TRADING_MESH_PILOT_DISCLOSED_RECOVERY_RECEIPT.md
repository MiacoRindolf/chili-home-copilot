# Historical Fable 5 Mesh Pressure Disclosed Recovery Receipt

## Status

The failure mechanism from the untouched 40/100 run was disclosed only after its report, JSON result, and receipt
were committed. Generic remediation was then implemented in commit `42ed0615`, `fix: bound optional local work
under pressure`. The sealed fixture and its four SHA-256-bound artifacts were not changed.

Historical reference commit: `d5ee0e92798398d6617a31ac092ec4757cfd4187`, `fix: cap mesh teacher under queue
pressure`. The Fable 5 attribution remains user-attested historical evidence, not provider-authenticated same-task
output.

## Result

- Score: **100/100**
- Sealed-final functional solve: **1/1**
- Diagnosis family: expected and retained `config`
- Changed files: exact expected owners, `mesh/settings.py` and `mesh/aggregator.py`
- Queue repository changed: no
- Public tests: **3/3 passed**
- Public plus repair-feedback tests: **6/6 passed**
- Fresh isolated sealed-final tests: **12/12 passed**
- Patch retained: true
- Deterministic diagnosis fast path: true
- Deterministic-only: true
- Live-reasoning-qualified: false
- Local model calls: **0**
- Premium calls: **0**
- Wall time: **5.2 seconds**
- Verdict: `needs_improvement`
- Git-normalized Markdown SHA-256: `b7159605a1f192446a901366083d5ae5e5caca002dec1ca21cfdfd56aa61e20f`
- Git-normalized results SHA-256: `b03cb8945d237d0850bf336b834b93e1f17a2e1ebe0dd5462243a76dc1035ee0`

## Generic Capability Added

1. Prompt-derived invariants now model bounded optional teacher work as one effective-policy family: settings own
   limits, a repository measures queue state, and the consumer owns admission while mechanical work continues.
2. Cross-file structural recognition distinguishes those three roles and proposes only the settings and consumer
   owners. It fails closed when the queue provider or settings owner is ambiguous.
3. Settings repair supports dataclass policy objects and Pydantic-style settings. It installs a 50-call default and
   0.8 pressure threshold, preserves zero overrides, and retains available environment-alias conventions.
4. Consumer repair supports both a single teacher assignment and a multi-stage `use_teacher` gate. It reads live
   queue pressure, sheds at the exact threshold, fails open when the database is absent or the bounded probe fails,
   and leaves the deterministic result/state path intact.
5. The existing daily-cap check remains independent of pressure admission, so below-threshold calls still obey the
   configured budget.

The operator transfers to alternate policy, queue, handler, and field names. A read-only probe against the complete
pre-fix parent sources of `d5ee0e92` selected exactly `app/config.py` and
`trade_context_aggregator.py`, left `repository.py` unchanged, produced valid Python for both owners, and closed all
contract warnings. No historical source, runtime service, database, broker, or live trading state was modified.

## Validation

- Full affected suites: **325 passed**, with the two existing SQLAlchemy/deprecation warnings.
- Alternate-name policy/consumer transfer: passed.
- Global `BaseSettings` plus two-stage-gate source shape: passed.
- Ambiguous queue-provider fail-closed guard: passed.
- Exact disclosed fixture operator and no-model benchmark paths: passed.
- Sealed final opened only after the zero-call model ledger was frozen.

## Interpretation

This is useful non-wrapper behavior: CHILI diagnoses a recognized source structure, repairs two owners, validates
the result, and opens the fresh final without invoking any model. It is also disclosed development evidence. The
authoritative first encounter remains 40/100, and the recovery earns no live-model reasoning credit. It does not
establish unknown-mechanism transfer, authenticated Fable 5 parity, or superiority.

Artifacts:

- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_MESH_PILOT_DISCLOSED_RECOVERY.md`
- `project_ws/AgentOps/fable5_historical_trading_mesh_pilot_disclosed_recovery.json`
- `project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_MESH_PILOT_UNTOUCHED_RECEIPT.md`
- `tests/fixtures/autonomy_diagnosis_to_fix_fable5_trading_mesh_pilot/`
