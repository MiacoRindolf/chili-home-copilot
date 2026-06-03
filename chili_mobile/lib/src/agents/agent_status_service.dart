import '../network/chili_api_client.dart';
import 'agent.dart';

/// A live status reading derived from the backend for one agent.
class AgentLiveStatus {
  const AgentLiveStatus(this.status, {this.lastRun, this.detail});
  final AgentStatus status;
  final String? lastRun; // ISO-8601 if known
  final String? detail; // short human note, e.g. "mode: reactive · queue 3"

  @override
  bool operator ==(Object other) =>
      other is AgentLiveStatus &&
      other.status == status &&
      other.lastRun == lastRun &&
      other.detail == detail;

  @override
  int get hashCode => Object.hash(status, lastRun, detail);
}

/// Agent ids that have a real backend status signal (AGT-2). Everything else
/// stays `unknown` until an endpoint exists. Kept here so the UI can badge
/// "live" agents and disable local toggles that the backend owns.
const Set<String> liveBackedAgentIds = <String>{
  'learning-cycle',
  'coding-autopilot',
  'task-watcher',
  'momentum-live-runner',
  'position-monitor',
};

/// Raw endpoint payloads, grouped — passed to [deriveAgentStatuses]. Kept as a
/// plain bag so the derivation is a pure function over decoded JSON (testable
/// without HTTP).
class AgentStatusInputs {
  const AgentStatusInputs({
    this.dispatch = const <String, dynamic>{},
    this.codeStatus = const <String, dynamic>{},
    this.contextStatus = const <String, dynamic>{},
    this.learnRuns = const <Map<String, dynamic>>[],
    this.momentumSummary = const <String, dynamic>{},
    this.monitorActive = const <String, dynamic>{},
  });

  final Map<String, dynamic> dispatch;
  final Map<String, dynamic> codeStatus;
  final Map<String, dynamic> contextStatus;
  final List<Map<String, dynamic>> learnRuns;
  final Map<String, dynamic> momentumSummary;
  final Map<String, dynamic> monitorActive;

  /// True when at least one endpoint returned data — used as a coarse
  /// "backend reachable" signal.
  bool get anyData =>
      dispatch.isNotEmpty ||
      codeStatus.isNotEmpty ||
      contextStatus.isNotEmpty ||
      learnRuns.isNotEmpty ||
      momentumSummary.isNotEmpty ||
      monitorActive.isNotEmpty;
}

/// Pure: map decoded backend payloads → per-agent live status. Only emits an
/// entry when the backing payload actually carries the signal, so a missing /
/// offline endpoint leaves that agent untouched (caller keeps `unknown`).
Map<String, AgentLiveStatus> deriveAgentStatuses(AgentStatusInputs i) {
  final Map<String, AgentLiveStatus> out = <String, AgentLiveStatus>{};

  // ── Code-brain runtime (mode: reactive/paused/legacy_60s) → autopilot + watcher.
  final Map<String, dynamic>? runtime =
      _asMap(i.codeStatus['runtime_state']);
  final String? mode = runtime?['mode'] as String?;
  if (mode != null) {
    final AgentStatus s = mode == 'paused'
        ? AgentStatus.stopped
        : (mode.startsWith('reactive') || mode.startsWith('legacy'))
            ? AgentStatus.running
            : AgentStatus.idle;
    final int queue = _asInt(i.codeStatus['queue_depth']) ?? 0;
    out['coding-autopilot'] =
        AgentLiveStatus(s, detail: 'mode: $mode · queue $queue');
    out['task-watcher'] = AgentLiveStatus(s, detail: 'mode: $mode');
  }

  // ── Learning cycle: context-brain learning flag + last learn run.
  final Map<String, dynamic>? ctx = _asMap(i.contextStatus['runtime_state']);
  final bool? learningEnabled =
      (ctx?['learning_enabled'] ?? i.contextStatus['learning_enabled']) as bool?;
  if (learningEnabled != null) {
    String? lastRun;
    String? result;
    if (i.learnRuns.isNotEmpty) {
      final Map<String, dynamic> r = i.learnRuns.first;
      lastRun = (r['ended_at'] ?? r['started_at']) as String?;
      final int touched = _asInt(r['patterns_touched']) ?? 0;
      final bool ok = r['success'] as bool? ?? false;
      result = ok ? '$touched patterns touched' : 'last run failed';
    }
    out['learning-cycle'] = AgentLiveStatus(
      learningEnabled ? AgentStatus.running : AgentStatus.stopped,
      lastRun: lastRun,
      detail: result,
    );
  }

  // ── Momentum live runner: active automation sessions → running.
  if (i.momentumSummary.isNotEmpty) {
    final int active = _activeMomentumSessions(i.momentumSummary);
    out['momentum-live-runner'] = AgentLiveStatus(
      active > 0 ? AgentStatus.running : AgentStatus.idle,
      detail: active > 0 ? '$active active session(s)' : 'no active sessions',
    );
  }

  // ── Position monitor: monitor/active reachable → running; carry last check.
  if (i.monitorActive.isNotEmpty) {
    final Map<String, dynamic>? summary = _asMap(i.monitorActive['summary']);
    final int activeCount = _asInt(summary?['active_count']) ?? 0;
    final String? lastCheck = summary?['last_check'] as String?;
    out['position-monitor'] = AgentLiveStatus(
      AgentStatus.running,
      lastRun: lastCheck,
      detail: '$activeCount active setup(s)',
    );
  }

  return out;
}

int _activeMomentumSessions(Map<String, dynamic> summary) {
  // Tolerate a few likely shapes: explicit active count, by-state map, or list.
  final int? direct = _asInt(summary['active'] ?? summary['active_count']);
  if (direct != null) return direct;
  final Map<String, dynamic>? byState =
      _asMap(summary['by_state'] ?? summary['states'] ?? summary['counts']);
  if (byState != null) {
    int n = 0;
    for (final String key in const <String>['active', 'running', 'live']) {
      n += _asInt(byState[key]) ?? 0;
    }
    return n;
  }
  return 0;
}

Map<String, dynamic>? _asMap(Object? v) =>
    v is Map ? Map<String, dynamic>.from(v) : null;

int? _asInt(Object? v) {
  if (v is int) return v;
  if (v is num) return v.toInt();
  if (v is String) return int.tryParse(v);
  return null;
}

/// Fetches all backing endpoints (tolerantly) and derives per-agent live
/// status. Network-touching; the derivation itself is the pure function above.
class AgentStatusService {
  AgentStatusService(this._api);
  final ChiliApiClient _api;

  /// True after the last [poll] if any endpoint returned data.
  bool reachable = false;

  Future<Map<String, AgentLiveStatus>> poll() async {
    final List<Object> results = await Future.wait(<Future<Object>>[
      _api.getCodeBrainStatus(),
      _api.getContextStatusSafe(),
      _api.getLearnRuns(),
      _api.getMomentumAutomationSummary(),
      _api.getActiveSetupsSummary(),
      _api.getDispatchStatusSafe(),
    ]);
    final AgentStatusInputs inputs = AgentStatusInputs(
      codeStatus: results[0] as Map<String, dynamic>,
      contextStatus: results[1] as Map<String, dynamic>,
      learnRuns: results[2] as List<Map<String, dynamic>>,
      momentumSummary: results[3] as Map<String, dynamic>,
      monitorActive: results[4] as Map<String, dynamic>,
      dispatch: results[5] as Map<String, dynamic>,
    );
    reachable = inputs.anyData;
    return deriveAgentStatuses(inputs);
  }
}
