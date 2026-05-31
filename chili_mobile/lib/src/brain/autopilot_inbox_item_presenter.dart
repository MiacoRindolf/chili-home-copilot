class AutopilotInboxReportEvidence {
  const AutopilotInboxReportEvidence({
    this.sourceLabel = '',
    this.supersessionLabel = '',
    this.detail = '',
    this.openActionLabel = '',
    this.isStateSnapshot = false,
  });

  final String sourceLabel;
  final String supersessionLabel;
  final String detail;
  final String openActionLabel;
  final bool isStateSnapshot;

  bool get hasSource => sourceLabel.isNotEmpty;
  bool get hasSupersession => supersessionLabel.isNotEmpty;
  bool get hasDetail => detail.isNotEmpty;
  bool get hasOpenActionLabel => openActionLabel.isNotEmpty;
}

class AutopilotInboxRuntimePressureEvidence {
  const AutopilotInboxRuntimePressureEvidence({
    required this.label,
    required this.detail,
  });

  final String label;
  final String detail;

  bool get hasDetail => detail.isNotEmpty;
}

class AutopilotInboxRuntimePressurePresentation {
  const AutopilotInboxRuntimePressurePresentation({
    this.blockerLabel = '',
    this.warningLabel = '',
    this.evidenceCountLabel = '',
    this.evidenceDetails = const <AutopilotInboxRuntimePressureEvidence>[],
    this.handoffLabel = '',
    this.handoffCopy = '',
  });

  final String blockerLabel;
  final String warningLabel;
  final String evidenceCountLabel;
  final List<AutopilotInboxRuntimePressureEvidence> evidenceDetails;
  final String handoffLabel;
  final String handoffCopy;

  bool get hasBlockers => blockerLabel.isNotEmpty;
  bool get hasWarnings => warningLabel.isNotEmpty;
  bool get hasEvidenceCount => evidenceCountLabel.isNotEmpty;
  bool get hasEvidenceDetails => evidenceDetails.isNotEmpty;
  bool get hasHandoff => handoffCopy.isNotEmpty;
  bool get hasEvidence =>
      hasBlockers ||
      hasWarnings ||
      hasEvidenceCount ||
      hasEvidenceDetails ||
      hasHandoff;
}

class AutopilotInboxItemPresenter {
  static AutopilotInboxReportEvidence reportEvidence(
    Map<String, dynamic> item,
  ) {
    final sourceKind = _clean(item['report_source_kind']);
    final createdAt = _clean(item['created_at']);
    final path = _clean(item['path']);
    final supersedes = _asInt(item['report_supersedes_older_count']);
    final hasFreshness =
        sourceKind.isNotEmpty || createdAt.isNotEmpty || supersedes > 0;
    if (!hasFreshness) return const AutopilotInboxReportEvidence();

    final cleanSource = _sourceLabel(sourceKind);
    final detailParts = <String>[];
    if (createdAt.isNotEmpty) detailParts.add('seen $createdAt');
    if (cleanSource.isNotEmpty) detailParts.add('source $cleanSource');
    if (supersedes > 0) {
      detailParts.add(
        'supersedes $supersedes older snapshot${supersedes == 1 ? '' : 's'}',
      );
    }
    if (path.isNotEmpty) detailParts.add(_tailPath(path));

    return AutopilotInboxReportEvidence(
      sourceLabel: cleanSource.isEmpty ? '' : 'source $cleanSource',
      supersessionLabel: supersedes > 0 ? 'supersedes $supersedes' : '',
      detail: 'Evidence ${detailParts.join(', ')}.',
      openActionLabel: _openActionLabel(sourceKind, supersedes),
      isStateSnapshot: sourceKind == 'state',
    );
  }

  static String openActionLabel(Map<String, dynamic> item) {
    final sourceKind = _clean(item['report_source_kind']);
    final sourceLabel = _sourceLabel(sourceKind);
    final supersedes = _asInt(item['report_supersedes_older_count']);
    final label = _openActionLabel(sourceKind, supersedes);
    if (label.isNotEmpty) return label;

    final path = _clean(item['open_path']).isNotEmpty
        ? _clean(item['open_path'])
        : _clean(item['path']);
    if (path.isEmpty) return '';
    if (sourceLabel.isNotEmpty) return 'Open $sourceLabel report';
    return 'Open report';
  }

  static AutopilotInboxRuntimePressurePresentation runtimePressure(
    Map<String, dynamic> item,
  ) {
    final blockers = _asInt(item['runtime_pressure_blocker_count']);
    final warnings = _asInt(item['runtime_pressure_warning_count']);
    final evidenceItems = _asMapList(item['runtime_pressure_items']);
    final handoffLabel = _clean(item['runtime_pressure_handoff_label']);
    final handoffCopy = _clean(item['runtime_pressure_handoff_copy']);
    if (blockers <= 0 &&
        warnings <= 0 &&
        evidenceItems.isEmpty &&
        handoffCopy.isEmpty) {
      return const AutopilotInboxRuntimePressurePresentation();
    }

    final evidenceDetails = <AutopilotInboxRuntimePressureEvidence>[];
    for (final evidence in evidenceItems.take(3)) {
      final label = _clean(evidence['label']).isNotEmpty
          ? _clean(evidence['label'])
          : _clean(evidence['kind']).replaceAll('_', ' ');
      final path = _clean(evidence['path']);
      final detail = _clean(evidence['detail']).isNotEmpty
          ? _clean(evidence['detail'])
          : label;
      final parts = <String>[
        detail,
        if (path.isNotEmpty) _tailPath(path),
      ];
      evidenceDetails.add(
        AutopilotInboxRuntimePressureEvidence(
          label: label,
          detail: parts.join(' | '),
        ),
      );
    }

    return AutopilotInboxRuntimePressurePresentation(
      blockerLabel: blockers > 0
          ? '$blockers runtime blocker${blockers == 1 ? '' : 's'}'
          : '',
      warningLabel: warnings > 0
          ? '$warnings runtime warning${warnings == 1 ? '' : 's'}'
          : '',
      evidenceCountLabel: evidenceItems.isNotEmpty
          ? '${evidenceItems.length} evidence item${evidenceItems.length == 1 ? '' : 's'}'
          : '',
      evidenceDetails: evidenceDetails,
      handoffLabel:
          handoffLabel.isEmpty ? 'Copy runtime handoff' : handoffLabel,
      handoffCopy: handoffCopy,
    );
  }

  static int _asInt(Object? value) {
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse(value?.toString() ?? '') ?? 0;
  }

  static List<Map<String, dynamic>> _asMapList(Object? value) {
    if (value is! Iterable) return <Map<String, dynamic>>[];
    return [
      for (final entry in value)
        if (entry is Map)
          entry.map((key, item) => MapEntry(key.toString(), item)),
    ];
  }

  static String _tailPath(String path) {
    final segments = path
        .replaceAll('\\', '/')
        .split('/')
        .where((segment) => segment.trim().isNotEmpty)
        .toList();
    if (segments.length <= 3) return path;
    return segments.skip(segments.length - 3).join('/');
  }

  static String _openActionLabel(String sourceKind, int supersedes) {
    if (sourceKind == 'evidence') {
      return supersedes > 0 ? 'Open current evidence' : 'Open evidence';
    }
    if (sourceKind == 'state') return 'Open state snapshot';
    return '';
  }

  static String _sourceLabel(String sourceKind) {
    if (sourceKind == 'state') return 'state snapshot';
    return sourceKind.replaceAll('_', ' ');
  }

  static String _clean(Object? value) => value?.toString().trim() ?? '';
}
