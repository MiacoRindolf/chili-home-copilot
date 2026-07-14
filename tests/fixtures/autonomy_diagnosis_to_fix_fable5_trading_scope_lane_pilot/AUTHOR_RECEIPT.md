# Historical Fable 5 Candidate-Scope Trading Replay Author Receipt

- Source implementation was frozen at `33a4038ce85be35f8d6a4e6e1a0597a9e5cf949d` before this fixture was authored.
- Historical reference: commit `b7afb8f3cd3eb0b86730c8ff73d200164ed51092`, `fix: split autotrader candidate scope lanes`.
- The user identifies the source conversation as Fable 5 work. This repository commit is user-attested historical
  reference evidence, not provider-authenticated same-task Fable 5 output.
- The fixture is a self-contained extraction of the broad-OR query-shape failure. It keeps AutoTrader selection as
  the sole causal owner and includes the query provider as a distractor/measurement boundary.
- Public tests cover accepted-set equivalence, global cap, and zero-limit behavior. Feedback tests expose the two
  scope-pure query requirement. The separate final oracle covers id-first merge order, per-lane capacity, identity
  deduplication, and mixed timestamp normalization.
- CHILI source must not be changed between this fixture commit and the first protocol run.
- The fixture was authored by the current coding agent after source freeze, not by an independent context-isolated
  author. Its first run is post-freeze transfer evidence but does not satisfy the independent-author promotion gate.
