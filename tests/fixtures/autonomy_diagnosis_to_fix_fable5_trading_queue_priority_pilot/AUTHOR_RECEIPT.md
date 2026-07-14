# Historical Fable 5 Queue-Priority Trading Replay Author Receipt

- Source implementation was frozen at `9d4663778bce6ed17f78f7c617874e12e22044f7` before this fixture was authored.
- Historical reference: commit `b04ade0678b16daa74fe3491314c185088ea2933`, `fix: protect mesh refresh under queue pressure`.
- The user identifies the source conversation as Fable 5 work. This repository commit is user-attested historical
  reference evidence, not provider-authenticated same-task Fable 5 output.
- The fixture is a self-contained extraction of the pre-fix queue-starvation mechanism. It keeps the queue
  repository as the sole causal owner and includes a telemetry summary as a distractor.
- Public tests cover ordinary enqueue, full-queue rejection, and correlation limits. Feedback tests encode exact
  capacity, protected causes, oldest/id ordering, audit preservation, and unprotected rejection. The separate final
  oracle covers fresh and wrong-cause rows, over-cap state, correlation-gate ordering, locked-row skipping, and
  timezone-safe auditing.
- CHILI source must not be changed between this fixture commit and the first protocol run.
- The fixture was authored by the current coding agent after source freeze, not by an independent context-isolated
  author. Its first run is post-freeze transfer evidence but does not satisfy the independent-author promotion gate.
