# Explicit continuous PAPER ownership

The prior timed supervisor stops at a UTC HHMM cutoff. Its legacy cleanup
uses integer quantities and stock DAY orders, which cannot reconcile native
fractional crypto cycles. `scripts/timeshare_supervisor.py window
--continuous-native-paper --operator-authorized` selects an explicit lifetime
without a daily cutoff. Timed equity windows retain their existing behavior;
invalid times such as 2400 are rejected.

Continuous admission requires both PAPER environment bindings, matching account
identity, native crypto configuration and its environment digest. Existing
broker/producer census, exclusive lease, receipt hashes and JobObject fencing
remain mandatory. Transition only at a verified broker-flat boundary with no
active native cycle intents under the account locks. Configure the native host
authority to hash the new supervisor path before launch.

Lease loss or app death still stops the child. Continuous mode never invokes
the legacy stock cleanup or claims positions are flat without broker evidence.
An unexpected stop requires exact native journal/order reconciliation; this
change does not implement automatic crash recovery or permit a new owner to
skip the flat admission checks. It also does not speed up source restoration.

Validation: targeted supervision tests cover midnight crossing, lease/app
failure, PAPER/account/config binding failures and absence of legacy cleanup.
