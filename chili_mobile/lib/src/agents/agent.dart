import 'package:flutter/foundation.dart';

/// Broad family an agent belongs to. Drives grouping, filtering and the
/// default icon/accent in the Agents app.
enum AgentKind { trading, brain, coding, system, custom }

/// Last-known run state. `unknown` is the honest default for built-in agents
/// until AGT-2 wires real backend status — local controls (AGT-1) move it
/// between running/stopped.
enum AgentStatus { running, idle, stopped, error, unknown }

extension AgentKindLabel on AgentKind {
  String get label {
    switch (this) {
      case AgentKind.trading:
        return 'Trading';
      case AgentKind.brain:
        return 'Brain';
      case AgentKind.coding:
        return 'Coding';
      case AgentKind.system:
        return 'System';
      case AgentKind.custom:
        return 'Custom';
    }
  }
}

extension AgentStatusLabel on AgentStatus {
  String get label {
    switch (this) {
      case AgentStatus.running:
        return 'Running';
      case AgentStatus.idle:
        return 'Idle';
      case AgentStatus.stopped:
        return 'Stopped';
      case AgentStatus.error:
        return 'Error';
      case AgentStatus.unknown:
        return 'Unknown';
    }
  }
}

/// An autonomous agent / background worker that CHILI can run. Built-in agents
/// mirror the real backend fleet (trading scheduler jobs, brain workers, the
/// coding-autonomy loop); custom agents (AGT-4) are user-defined. Immutable —
/// the registry swaps instances via [copyWith] and notifies listeners.
@immutable
class Agent {
  /// Stable kebab-case identifier (also the persistence key).
  final String id;

  /// Human title shown in the list and detail header.
  final String name;
  final AgentKind kind;

  /// One-line description of what the agent does.
  final String description;
  final AgentStatus status;

  /// Whether the agent is allowed to run. Distinct from [status]: a disabled
  /// agent stays stopped; an enabled one may still be idle between ticks.
  final bool enabled;

  /// True for the seeded CHILI fleet, false for user-created agents.
  final bool builtin;

  /// Human cadence, e.g. "every 10s", "5:30 AM PT daily", "continuous".
  final String schedule;

  /// Config knobs (label → value) — real backend settings/intervals. Editable
  /// locally in AGT-1; wired to the backend in a later increment.
  final Map<String, String> config;

  /// How the agent is gated/killed, e.g. "kill switch + circuit breaker".
  /// Informational in AGT-1.
  final String? killSwitch;

  /// ISO-8601 timestamp of the last (local) run, or null if never run here.
  final String? lastRun;

  /// Short outcome summary of the last run, or null.
  final String? lastResult;

  final bool canStart;
  final bool canStop;
  final bool canRunOnce;
  final bool canConfigure;

  const Agent({
    required this.id,
    required this.name,
    required this.kind,
    required this.description,
    this.status = AgentStatus.unknown,
    this.enabled = true,
    this.builtin = true,
    this.schedule = '',
    this.config = const <String, String>{},
    this.killSwitch,
    this.lastRun,
    this.lastResult,
    this.canStart = true,
    this.canStop = true,
    this.canRunOnce = true,
    this.canConfigure = true,
  });

  Agent copyWith({
    String? name,
    AgentKind? kind,
    String? description,
    AgentStatus? status,
    bool? enabled,
    bool? builtin,
    String? schedule,
    Map<String, String>? config,
    String? killSwitch,
    Object? lastRun = _unset,
    Object? lastResult = _unset,
    bool? canStart,
    bool? canStop,
    bool? canRunOnce,
    bool? canConfigure,
  }) {
    return Agent(
      id: id,
      name: name ?? this.name,
      kind: kind ?? this.kind,
      description: description ?? this.description,
      status: status ?? this.status,
      enabled: enabled ?? this.enabled,
      builtin: builtin ?? this.builtin,
      schedule: schedule ?? this.schedule,
      config: config ?? this.config,
      killSwitch: killSwitch ?? this.killSwitch,
      lastRun: identical(lastRun, _unset) ? this.lastRun : lastRun as String?,
      lastResult:
          identical(lastResult, _unset) ? this.lastResult : lastResult as String?,
      canStart: canStart ?? this.canStart,
      canStop: canStop ?? this.canStop,
      canRunOnce: canRunOnce ?? this.canRunOnce,
      canConfigure: canConfigure ?? this.canConfigure,
    );
  }

  Map<String, dynamic> toJson() => <String, dynamic>{
        'id': id,
        'name': name,
        'kind': kind.name,
        'description': description,
        'status': status.name,
        'enabled': enabled,
        'builtin': builtin,
        'schedule': schedule,
        'config': config,
        if (killSwitch != null) 'killSwitch': killSwitch,
        if (lastRun != null) 'lastRun': lastRun,
        if (lastResult != null) 'lastResult': lastResult,
        'canStart': canStart,
        'canStop': canStop,
        'canRunOnce': canRunOnce,
        'canConfigure': canConfigure,
      };

  static Agent fromJson(Map<String, dynamic> j) {
    return Agent(
      id: j['id'] as String,
      name: j['name'] as String? ?? j['id'] as String,
      kind: _kindFrom(j['kind'] as String?),
      description: j['description'] as String? ?? '',
      status: _statusFrom(j['status'] as String?),
      enabled: j['enabled'] as bool? ?? true,
      builtin: j['builtin'] as bool? ?? false,
      schedule: j['schedule'] as String? ?? '',
      config: _stringMap(j['config']),
      killSwitch: j['killSwitch'] as String?,
      lastRun: j['lastRun'] as String?,
      lastResult: j['lastResult'] as String?,
      canStart: j['canStart'] as bool? ?? true,
      canStop: j['canStop'] as bool? ?? true,
      canRunOnce: j['canRunOnce'] as bool? ?? true,
      canConfigure: j['canConfigure'] as bool? ?? true,
    );
  }

  static AgentKind _kindFrom(String? s) {
    return AgentKind.values.firstWhere(
      (AgentKind k) => k.name == s,
      orElse: () => AgentKind.custom,
    );
  }

  static AgentStatus _statusFrom(String? s) {
    return AgentStatus.values.firstWhere(
      (AgentStatus k) => k.name == s,
      orElse: () => AgentStatus.unknown,
    );
  }

  static Map<String, String> _stringMap(Object? v) {
    if (v is! Map) return const <String, String>{};
    return v.map((Object? k, Object? val) =>
        MapEntry<String, String>(k.toString(), val?.toString() ?? ''));
  }

  static const Object _unset = Object();
}
