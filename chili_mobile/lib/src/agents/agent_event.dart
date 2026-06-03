import 'package:flutter/foundation.dart';

/// What kind of thing happened to an agent — drives the icon/accent in the
/// activity log.
enum AgentEventKind { action, status, config }

/// One entry in an agent's session activity log (AGT-5). Ephemeral: the log
/// lives in memory for the session and is not persisted.
@immutable
class AgentEvent {
  const AgentEvent({
    required this.kind,
    required this.message,
    required this.timestamp,
  });

  final AgentEventKind kind;
  final String message;
  final String timestamp; // ISO-8601

  @override
  bool operator ==(Object other) =>
      other is AgentEvent &&
      other.kind == kind &&
      other.message == message &&
      other.timestamp == timestamp;

  @override
  int get hashCode => Object.hash(kind, message, timestamp);
}
