# Task [10]: remove repeated opinion dwell and measure the exit handoff tail

The breakout-failure and lost-VWAP opinion sites could repeatedly return while waiting for a wall-clock dwell, despite having no independent authority to sell. The recovered task [10] removes that repeated dwell and its four settings. Opinions still arm the print verdict. The existing one-pass pre-emption when an opinion is newly armed remains; this patch does not claim every early return or exit delay is gone.

Exit-tail receipts retain the first observation of an exit at the submit seam, record successful broker-response observations and release/BBO block counts, and attach price/quantity tail accounting to subsequent fill confirmations. Broker partial executions retain the exit episode; terminal completion and existing recycle/scale-out boundaries retire it. The metadata uses `exit_tail_` keys so it does not acquire pending-exit reconciliation identity.

Clock provenance is explicit: the decision stamp is first local submit-seam entry, submission is the local observation after a successful adapter response, and fill is the runner confirmation observation. None is asserted to be an authenticated upstream signal time or broker execution time. Legacy pending stamps retain an unspecified-phase label; missing clocks remain missing.

| Binding | Value / derivation |
| --- | --- |
| Opinion dwell | Removed; no replacement seconds window or print-count wait |
| Exit authority | Existing print verdict; opinions arm, rather than independently sell |
| Tail mark | `(fill_price - bid_at_decision) * filled_quantity`, when inputs are present |
| Timing | Differences between named local observations, diagnostic only |
| Episode | First seam/post preserved across retries and actual partial executions |

Recovered historical measurements in planner [10] report decision-to-local-fill p50 21.16 seconds (22 observations), decision-to-freeze 0.82 seconds and 23 missing identities. The 21.16-second value is not a decision-to-submit benchmark. These are prior Claude measurements, not re-queried or newly validated profitability claims. This patch improves future attribution and removes the redundant opinion dwell; it does not establish a latency bound.

Validation on main base `4f75f0a0203083ac096d35a3519fa2348c87e438`, isolated PostgreSQL `chili_rossbench26_test`:

- Recovered baseline: 52 passed, 317.02 seconds.
- Three new clock-contract tests failed before the metadata amendment, then passed with it.
- Amended focused and direct-neighbor verification: **180 passed**, 13 warnings, 159.86 seconds. Files: `test_exit_tail_receipt.py`, `test_exit_tail_clock_provenance.py`, `test_opinion_sites_do_not_dwell.py`, `test_momentum_lost_vwap_flatten.py`, `test_session_tick_wake.py`, `test_opinion_exits_ask_the_tape.py`, `test_exit_verdict_f_state_machine.py`, `test_exit_verdict_f_source_pins.py`, `test_exit_pre_place_handback.py`.
- Warnings concern an existing configuration escape sequence and SQLAlchemy table cycles. Three DB-using test modules are registered for the existing targeted trading cleanup; this changes test setup only.

Two independent adversarial reviews and program verification remain required. Combined task [9]/[10] verification is also pending because both touch the runner. No PAPER lane, bridge, broker configuration or live deployment was changed. Ordinary G's 255-print window and nested-wave entry/exit integration remain open in [21]/[29].

Provenance: Claude source commit `8003f893c6703d5e3e79e424bd6523ff5a9e567f`, recovered as `742103f24f3daaf31a322a291e35b5fc2cb01a28`, followed by Astra/Codex clock-provenance correction and isolated verification. Original Claude worktree preserved.
