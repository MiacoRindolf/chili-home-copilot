# Scripts

## Test runners

- `run-tests.ps1` — full test suite. **Always use this in agent sessions instead of bare pytest.** Has 30-minute hard timeout, writes `tests-summary.txt` for quick parsing, kills leftover pytest processes from prior runs.
- `run-tests-quick.ps1 [path]` — fast iteration. Stops on first failure (`-x`), 60-second per-test timeout, optional path argument.

### Convention for agents

Read `tests-summary.txt` first. Only `cat tests-output.log` if summary indicates failures or timeouts. Never invoke `pytest` directly in agent sessions — the foreground tracking is unreliable on Windows + PowerShell.

### Exit codes

- `0` — all tests passed (skips OK)
- `1` — one or more tests failed
- `124` — wrapper hit 30-minute hard timeout
- other — pytest internal error
