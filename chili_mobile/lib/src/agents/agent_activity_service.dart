import '../network/chili_api_client.dart';

/// One backend-reported run/decision for an agent (AGT-8).
class AgentRun {
  const AgentRun({required this.when, required this.title, this.outcome});

  /// ISO-8601 timestamp (or raw string) of the run, '' if unknown.
  final String when;
  final String title;
  final String? outcome;

  @override
  bool operator ==(Object other) =>
      other is AgentRun &&
      other.when == when &&
      other.title == title &&
      other.outcome == outcome;

  @override
  int get hashCode => Object.hash(when, title, outcome);
}

/// Endpoint that exposes recent runs/decisions for an agent, plus the JSON key
/// the list lives under. Only agents with a real endpoint appear here.
class _RunsEndpoint {
  const _RunsEndpoint(this.path, this.itemsKey);
  final String path;
  final String itemsKey;
}

const Map<String, _RunsEndpoint> _runsEndpoints = <String, _RunsEndpoint>{
  'learning-cycle':
      _RunsEndpoint('/api/brain/context/gateway/learn/runs?limit=10', 'runs'),
  'coding-autopilot':
      _RunsEndpoint('/api/brain/dispatch/runs?limit=10', 'runs'),
  'momentum-live-runner': _RunsEndpoint(
      '/api/trading/momentum/automation/decisions/recent?limit=10', 'decisions'),
  'position-monitor':
      _RunsEndpoint('/api/trading/monitor/decisions?limit=10', 'decisions'),
};

/// Agent ids that have a backend recent-runs feed (AGT-8). The UI shows a
/// "Backend runs" section only for these.
Set<String> get backendRunsAgentIds => _runsEndpoints.keys.toSet();

bool agentHasBackendRuns(String id) => _runsEndpoints.containsKey(id);

/// Generic, tolerant parser: pull a readable (when, title, outcome) out of one
/// backend record across the (varied) run/decision endpoint shapes. Pure.
List<AgentRun> parseAgentRuns(List<Map<String, dynamic>> raw) {
  final List<AgentRun> out = <AgentRun>[];
  for (final Map<String, dynamic> r in raw) {
    final String when = _firstStr(r, const <String>[
      'ended_at',
      'finished_at',
      'decided_at',
      'completed_at',
      'created_at',
      'started_at',
      'updated_at',
      'at',
      'timestamp',
    ]);
    String title = _firstStr(r, const <String>[
      'title',
      'label',
      'decision',
      'decision_type',
      'phase',
      'action',
      'event_type',
      'category',
      'summary',
    ]);
    // Add a salient subject when present (ticker / task).
    final String subject = _firstStr(r, const <String>[
      'chosen_ticker',
      'ticker',
      'symbol',
      'task_title',
      'pattern',
    ]);
    if (subject.isNotEmpty) {
      title = title.isEmpty ? subject : '$title · $subject';
    }
    if (title.isEmpty) title = 'run';

    String? outcome = _firstStrOrNull(r, const <String>[
      'outcome_status',
      'outcome',
      'status',
      'result',
      'verdict',
    ]);
    // `success: true/false` → ok / failed, when no explicit outcome string.
    if (outcome == null && r.containsKey('success')) {
      outcome = r['success'] == true ? 'ok' : 'failed';
    }
    final int? touched = _asIntOrNull(r['patterns_touched']);
    if (touched != null) {
      outcome = outcome == null
          ? '$touched patterns'
          : '$outcome · $touched patterns';
    }

    out.add(AgentRun(when: when, title: title, outcome: outcome));
  }
  return out;
}

String _firstStr(Map<String, dynamic> m, List<String> keys) =>
    _firstStrOrNull(m, keys) ?? '';

String? _firstStrOrNull(Map<String, dynamic> m, List<String> keys) {
  for (final String k in keys) {
    final Object? v = m[k];
    if (v == null) continue;
    final String s = v.toString().trim();
    if (s.isNotEmpty && s != 'null') return s;
  }
  return null;
}

int? _asIntOrNull(Object? v) {
  if (v is int) return v;
  if (v is num) return v.toInt();
  if (v is String) return int.tryParse(v);
  return null;
}

/// Fetches an agent's recent backend runs (tolerant: offline/invalid → []).
class AgentActivityService {
  AgentActivityService(this._api);
  final ChiliApiClient _api;

  Future<List<AgentRun>> fetch(String agentId) async {
    final _RunsEndpoint? ep = _runsEndpoints[agentId];
    if (ep == null) return const <AgentRun>[];
    final List<Map<String, dynamic>> raw =
        await _api.getJsonListSafe(ep.path, itemsKey: ep.itemsKey);
    return parseAgentRuns(raw);
  }
}
