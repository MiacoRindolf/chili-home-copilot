// Pure scheduling helpers for the real-time layer (RT-1). No I/O, no state —
// trivially unit-testable.

/// Exponential backoff with a cap, for reconnect/retry after [attempt] failures
/// (attempt starts at 1 for the first failure). 1→base, 2→2·base, 3→4·base, …
Duration backoffDelay(
  int attempt, {
  Duration base = const Duration(seconds: 1),
  Duration max = const Duration(seconds: 30),
}) {
  if (attempt <= 0) return Duration.zero;
  final int shift = attempt - 1 > 16 ? 16 : attempt - 1; // avoid overflow
  final int ms = base.inMilliseconds * (1 << shift);
  final int capped = ms > max.inMilliseconds ? max.inMilliseconds : ms;
  return Duration(milliseconds: capped);
}

/// Adaptive poll cadence: poll quickly while the surface is [active]
/// (foreground / window focused), slowly when idle to save resources.
Duration adaptiveInterval({
  required bool active,
  Duration activeInterval = const Duration(seconds: 5),
  Duration idleInterval = const Duration(seconds: 30),
}) =>
    active ? activeInterval : idleInterval;
