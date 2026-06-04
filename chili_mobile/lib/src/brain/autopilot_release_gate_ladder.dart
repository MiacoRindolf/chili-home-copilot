/// Presentation model for the Project-Autopilot "release gate ladder".
///
/// Encodes the operating principle that a green CI run is *evidence*, not
/// release *authority*: draft-readiness, the merge decision, and the
/// release/runtime gate are tracked as distinct gates. Built from an operator
/// inbox `release_trust_summary` map.
///
/// Reconstructed to the contract pinned by
/// `test/autopilot_agent_bench_activity_presenter_test.dart` (the original file
/// was referenced by the presenter but never committed, breaking the build).
class AutopilotReleaseGateLadderStep {
  const AutopilotReleaseGateLadderStep({
    required this.label,
    required this.stateLabel,
    required this.detail,
    required this.blocked,
  });

  final String label;
  final String stateLabel;
  final String detail;
  final bool blocked;
}

class AutopilotReleaseGateLadderPresentation {
  const AutopilotReleaseGateLadderPresentation({
    required this.hasSignal,
    required this.summaryLabel,
    required this.steps,
  });

  final bool hasSignal;
  final String summaryLabel;
  final List<AutopilotReleaseGateLadderStep> steps;

  static const AutopilotReleaseGateLadderPresentation empty =
      AutopilotReleaseGateLadderPresentation(
    hasSignal: false,
    summaryLabel: '',
    steps: <AutopilotReleaseGateLadderStep>[],
  );

  /// Build the ladder from a `release_trust_summary` map.
  ///
  /// - A PR-publication packet (an `items[]` entry carrying a
  ///   `pr_publish_gate_state`) blocks every gate, since publication is held
  ///   until the review-ready packet clears; the required evidence is surfaced
  ///   on the draft gate.
  /// - Otherwise a release-trust blocker blocks only the release/runtime gate;
  ///   draft and merge stay separate gates.
  static AutopilotReleaseGateLadderPresentation fromReleaseTrust(
    Map<String, dynamic> releaseTrust,
  ) {
    final int blockerCount = _asInt(releaseTrust['blocker_count']);
    final Map<String, dynamic> groups = _asMap(releaseTrust['group_counts']);
    final bool anyGroupBlocker =
        groups.values.any((Object? v) => _asInt(v) > 0);
    final List<Map<String, dynamic>> items =
        _asMapList(releaseTrust['items']);

    final bool prPublishActive = items.any(
      (Map<String, dynamic> it) => _clean(it['pr_publish_gate_state']).isNotEmpty,
    );

    final bool hasSignal =
        blockerCount > 0 || prPublishActive || anyGroupBlocker;
    if (!hasSignal) return empty;

    final List<String> needs = <String>[
      for (final Map<String, dynamic> it in items)
        ..._asStringList(it['pr_publish_required_evidence'])
            .map(_humanize)
            .map((String e) => 'Needs $e'),
    ];

    final String draftState = prPublishActive ? 'blocked' : 'separate gate';
    final String draftDetail = prPublishActive
        ? (needs.isEmpty ? 'Needs the review-ready packet' : needs.join(', '))
        : 'Draft readiness is tracked apart from CI';
    final String mergeState = prPublishActive ? 'blocked' : 'separate gate';
    final String mergeDetail = prPublishActive
        ? 'Merge decision waits on the review-ready packet'
        : 'Merge decision is a separate gate from CI';

    final List<AutopilotReleaseGateLadderStep> steps =
        <AutopilotReleaseGateLadderStep>[
      AutopilotReleaseGateLadderStep(
        label: 'Draft ready',
        stateLabel: draftState,
        detail: draftDetail,
        blocked: draftState == 'blocked',
      ),
      AutopilotReleaseGateLadderStep(
        label: 'Merge decision',
        stateLabel: mergeState,
        detail: mergeDetail,
        blocked: mergeState == 'blocked',
      ),
      const AutopilotReleaseGateLadderStep(
        label: 'Release/runtime',
        stateLabel: 'blocked',
        detail: 'Main green CI is not release authority',
        blocked: true,
      ),
    ];

    return AutopilotReleaseGateLadderPresentation(
      hasSignal: true,
      summaryLabel: 'Green CI is evidence, not authority',
      steps: steps,
    );
  }
}

// ── local helpers (kept private to this file) ────────────────────────────────

int _asInt(Object? value) {
  if (value is int) return value;
  if (value is num) return value.toInt();
  if (value is String) return int.tryParse(value) ?? 0;
  return 0;
}

Map<String, dynamic> _asMap(Object? value) =>
    value is Map ? Map<String, dynamic>.from(value) : <String, dynamic>{};

List<Map<String, dynamic>> _asMapList(Object? value) => value is List
    ? value
        .whereType<Map>()
        .map((Map e) => Map<String, dynamic>.from(e))
        .toList()
    : <Map<String, dynamic>>[];

List<String> _asStringList(Object? value) => value is List
    ? value
        .map((Object? e) => e?.toString().trim() ?? '')
        .where((String e) => e.isNotEmpty)
        .toList()
    : <String>[];

String _clean(Object? value) => value?.toString().trim() ?? '';

/// `exact_current_head_sha` → `Exact current head sha`.
String _humanize(String token) {
  final String words = token.replaceAll('_', ' ').replaceAll('-', ' ').trim();
  if (words.isEmpty) return words;
  return words[0].toUpperCase() + words.substring(1);
}
