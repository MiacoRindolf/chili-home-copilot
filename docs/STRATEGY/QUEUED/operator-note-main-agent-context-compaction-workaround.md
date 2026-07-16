# operator-note-main-agent-context-compaction-workaround

STATUS: QUEUED
PRIORITY: P0 PROCESS NOTE
PROPOSED: 2026-06-30
REQUESTED_BY: operator
SCOPE: main-agent resume behavior after automatic context compaction

## Operator Note

The operator is seeing redundant work and repeated investigations after automatic context compaction.

When resuming after compaction, do not rely only on the event/context summary. Treat the summary as orientation, then re-anchor on the most recent visible user and assistant messages before executing more work.

Required resume protocol:

1. Read the latest visible messages first.
2. Identify the exact last stopping point before compaction.
3. Check what was already verified, queued, patched, deployed, or ruled out.
4. Continue from that point instead of restarting the same investigation.
5. If the summary conflicts with the most recent messages, trust the most recent messages and explicitly reconcile the conflict.

Reason:

The compaction summary can lose the exact final handoff point, which causes duplicate DB/code audits and delays the active repair work.

