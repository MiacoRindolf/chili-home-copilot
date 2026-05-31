enum AutopilotSafetySeverity { clear, warning, blocked }

class AutopilotSafetyState {
  const AutopilotSafetyState({
    required this.severity,
    required this.releaseLabel,
    required this.releaseDetail,
    required this.releaseBlockerCount,
    required this.releaseBlocked,
    required this.runtimeLabel,
    required this.runtimeDetail,
    required this.runtimeBlockerCount,
    required this.runtimeBlocked,
    required this.runtimePath,
    required this.runtimeOpenPath,
    required this.controlLabel,
    required this.controlDetail,
    required this.controlBlockerCount,
    required this.controlHighRiskCount,
    required this.controlBlocked,
    this.controlTargetLabel = '',
    this.controlTargetDetail = '',
    this.controlTargetPath = '',
    this.controlTargetOpenPath = '',
    this.controlTargetHandoffLabel = '',
    this.controlTargetHandoffCopy = '',
    required this.evidenceLabel,
    required this.evidenceDetail,
    required this.evidencePath,
    required this.evidenceOpenPath,
    required this.nextActionLabel,
    required this.nextActionDetail,
    required this.nextActionAgent,
    required this.nextActionKind,
    required this.nextActionRunId,
    required this.nextActionPath,
    required this.nextActionOpenPath,
    required this.nextActionRecoveryAction,
    required this.nextActionButtonLabel,
    required this.nextActionHandoffLabel,
    required this.nextActionHandoffCopy,
    required this.inboxActionCount,
    required this.nonAuthorizationDetail,
  });

  final AutopilotSafetySeverity severity;
  final String releaseLabel;
  final String releaseDetail;
  final int releaseBlockerCount;
  final bool releaseBlocked;
  final String runtimeLabel;
  final String runtimeDetail;
  final int runtimeBlockerCount;
  final bool runtimeBlocked;
  final String runtimePath;
  final String runtimeOpenPath;
  final String controlLabel;
  final String controlDetail;
  final int controlBlockerCount;
  final int controlHighRiskCount;
  final bool controlBlocked;
  final String controlTargetLabel;
  final String controlTargetDetail;
  final String controlTargetPath;
  final String controlTargetOpenPath;
  final String controlTargetHandoffLabel;
  final String controlTargetHandoffCopy;
  final String evidenceLabel;
  final String evidenceDetail;
  final String evidencePath;
  final String evidenceOpenPath;
  final String nextActionLabel;
  final String nextActionDetail;
  final String nextActionAgent;
  final String nextActionKind;
  final String nextActionRunId;
  final String nextActionPath;
  final String nextActionOpenPath;
  final String nextActionRecoveryAction;
  final String nextActionButtonLabel;
  final String nextActionHandoffLabel;
  final String nextActionHandoffCopy;
  final int inboxActionCount;
  final String nonAuthorizationDetail;

  bool get blocked => severity == AutopilotSafetySeverity.blocked;
  bool get warning => severity == AutopilotSafetySeverity.warning;
  bool get hasAction =>
      nextActionLabel != AutopilotSafetyStatePresenter.noOperatorActionLabel ||
      nextActionDetail.isNotEmpty ||
      nextActionAgent.isNotEmpty ||
      nextActionKind.isNotEmpty;
  bool get hasOpenTarget =>
      nextActionRunId.isNotEmpty ||
      nextActionPath.isNotEmpty ||
      nextActionOpenPath.isNotEmpty ||
      runtimePath.isNotEmpty ||
      runtimeOpenPath.isNotEmpty ||
      controlTargetPath.isNotEmpty ||
      controlTargetOpenPath.isNotEmpty ||
      evidencePath.isNotEmpty ||
      evidenceOpenPath.isNotEmpty;
  bool get hasHandoff => nextActionHandoffCopy.isNotEmpty;
  bool get hasControlTargetOpen =>
      controlTargetPath.isNotEmpty || controlTargetOpenPath.isNotEmpty;
  bool get hasControlTargetHandoff => controlTargetHandoffCopy.isNotEmpty;
  String get safeActionButtonLabel {
    if (nextActionButtonLabel.isNotEmpty) return nextActionButtonLabel;
    if (hasAction && hasOpenTarget) return 'Review';
    if (hasOpenTarget) return 'Evidence';
    return '';
  }
}

class AutopilotSafetyStatePresenter {
  static const noOperatorActionLabel = 'No operator action';

  static AutopilotSafetyState fromReadiness(Map<String, dynamic> readiness) {
    final operatorInbox = _asMap(readiness['operator_inbox']);
    final releaseTrust = _asMap(operatorInbox['release_trust_summary']);
    final runtimeQueue = _asMap(readiness['runtime_queue']);
    final runtimePressure = _asMap(
      readiness['runtime_pressure'] ?? runtimeQueue['runtime_pressure'],
    );
    final agentFlow = _asMap(readiness['agent_flow']);
    final controlTrust = _asMap(
      agentFlow['control_plane_trust'] ??
          operatorInbox['control_plane_trust_summary'],
    );

    final releaseBlockers = _asInt(releaseTrust['blocker_count']);
    final releaseBlocked = releaseBlockers > 0;
    final runtimeBlockers = _asInt(runtimePressure['blocker_count']);
    final runtimeWarnings = _asInt(runtimePressure['warning_count']);
    final runtimeStatus = _clean(runtimePressure['status']);
    final runtimeBlocked = runtimeBlockers > 0 ||
        runtimeWarnings > 0 ||
        runtimeStatus == 'warning' ||
        runtimeStatus == 'failed';
    final controlBlockers = _asInt(controlTrust['blocker_count']);
    final highRiskControl = _asInt(controlTrust['high_risk_count']);
    final activeQuarantines =
        _asInt(agentFlow['quarantined_target_active_count']);
    final pausedUncovered =
        _asInt(agentFlow['paused_automation_uncovered_count']);
    final controlBlocked = controlBlockers > 0 ||
        highRiskControl > 0 ||
        activeQuarantines > 0 ||
        pausedUncovered > 0;
    final inboxActions = _asInt(operatorInbox['total_action_count']);
    final nextAction = _nextAction(
      operatorInbox: operatorInbox,
      runtimePressure: runtimePressure,
      runtimeQueue: runtimeQueue,
      releaseTrust: releaseTrust,
      controlTrust: controlTrust,
    );
    final controlTarget = _controlTarget(
      controlTrust: controlTrust,
      agentFlow: agentFlow,
    );
    final evidence = _evidence(
      operatorInbox: operatorInbox,
      agentFlow: agentFlow,
      runtimePressure: runtimePressure,
      releaseTrust: releaseTrust,
    );
    final severity = releaseBlocked || runtimeBlocked || controlBlocked
        ? AutopilotSafetySeverity.blocked
        : inboxActions > 0
            ? AutopilotSafetySeverity.warning
            : AutopilotSafetySeverity.clear;

    return AutopilotSafetyState(
      severity: severity,
      releaseLabel: releaseBlocked ? 'Release blocked' : 'Release guarded',
      releaseDetail: _releaseDetail(releaseTrust, releaseBlockers),
      releaseBlockerCount: releaseBlockers,
      releaseBlocked: releaseBlocked,
      runtimeLabel: runtimeBlocked ? 'Runtime blocked' : 'Runtime guarded',
      runtimeDetail: _runtimeDetail(
        runtimePressure,
        runtimeBlockers,
        runtimeWarnings,
      ),
      runtimeBlockerCount: runtimeBlockers,
      runtimeBlocked: runtimeBlocked,
      runtimePath: _clean(runtimePressure['path']),
      runtimeOpenPath: _clean(runtimePressure['open_path']),
      controlLabel:
          controlBlocked ? 'Control plane blocked' : 'Control plane clear',
      controlDetail: _controlDetail(
        controlTrust: controlTrust,
        blockers: controlBlockers,
        highRisk: highRiskControl,
        activeQuarantines: activeQuarantines,
        pausedUncovered: pausedUncovered,
      ),
      controlBlockerCount: controlBlockers,
      controlHighRiskCount: highRiskControl,
      controlBlocked: controlBlocked,
      controlTargetLabel: controlTarget.label,
      controlTargetDetail: controlTarget.detail,
      controlTargetPath: controlTarget.path,
      controlTargetOpenPath: controlTarget.openPath,
      controlTargetHandoffLabel: controlTarget.handoffLabel,
      controlTargetHandoffCopy: controlTarget.handoffCopy,
      evidenceLabel: evidence.label,
      evidenceDetail: evidence.detail,
      evidencePath: evidence.path,
      evidenceOpenPath: evidence.openPath,
      nextActionLabel: nextAction.label,
      nextActionDetail: nextAction.detail,
      nextActionAgent: nextAction.agent,
      nextActionKind: nextAction.kind,
      nextActionRunId: nextAction.runId,
      nextActionPath: nextAction.path,
      nextActionOpenPath: nextAction.openPath,
      nextActionRecoveryAction: nextAction.recoveryAction,
      nextActionButtonLabel: nextAction.buttonLabel,
      nextActionHandoffLabel: nextAction.handoffLabel,
      nextActionHandoffCopy: nextAction.handoffCopy,
      inboxActionCount: inboxActions,
      nonAuthorizationDetail: _nonAuthorizationDetail(severity),
    );
  }

  static String _releaseDetail(
    Map<String, dynamic> releaseTrust,
    int blockers,
  ) {
    if (blockers <= 0) return 'No release-trust blocker is surfaced.';
    final groups = _asMap(releaseTrust['group_counts']);
    final categories = _asMap(releaseTrust['category_counts']);
    final parts = <String>[];
    _addCount(parts, categories['source_trust'], 'source');
    _addCount(parts, categories['ci_blocked'], 'CI');
    _addCount(parts, categories['pr_health'], 'PR');
    _addCount(parts, categories['review_conflict'], 'review conflict');
    _addCount(parts, categories['report_quality'], 'report quality');
    if (parts.isEmpty) {
      _addCount(parts, groups['release_trust'], 'release');
      _addCount(parts, groups['pr_health'], 'PR');
      _addCount(parts, groups['evidence_quality'], 'evidence');
    }
    if (parts.isEmpty) {
      return '$blockers blocker${_plural(blockers)} requires release review.';
    }
    final itemDetail = _releaseTrustItemDetail(releaseTrust);
    final freshnessDetail = _releaseTrustFreshnessDetail(releaseTrust);
    final base = '$blockers blocker${_plural(blockers)}: ${parts.join(', ')}.';
    return [
      base,
      if (itemDetail.isNotEmpty) itemDetail,
      if (freshnessDetail.isNotEmpty) freshnessDetail,
    ].join(' ');
  }

  static String _releaseTrustItemDetail(Map<String, dynamic> releaseTrust) {
    final items = _asMapList(releaseTrust['items']);
    if (items.isEmpty) return '';
    final item = items.first;
    final prNumber = _clean(item['pr_number']);
    final branch = _clean(item['pr_branch']);
    final ciState = _clean(item['ci_state']);
    final ciSummary = _clean(item['ci_summary']);
    final blocker = _clean(item['blocker_kind']);
    final category = _clean(item['category']);
    final label = _clean(item['label']);
    if (prNumber.isEmpty &&
        branch.isEmpty &&
        ciState.isEmpty &&
        blocker.isEmpty) {
      return '';
    }
    final subject = prNumber.isNotEmpty ? 'PR #$prNumber' : 'Current head';
    final details = <String>[];
    if (blocker.isNotEmpty) details.add(blocker.replaceAll('_', ' '));
    if (ciState.isNotEmpty) details.add(ciState.replaceAll('_', ' '));
    if (ciSummary.isNotEmpty &&
        ciSummary.toLowerCase().replaceAll(' ', '_') !=
            ciState.toLowerCase().replaceAll(' ', '_')) {
      details.add(ciSummary);
    }
    final branchTail = branch.isEmpty ? '' : ' on ${_tailPath(branch)}';
    final reason = details.isNotEmpty
        ? details.join(', ')
        : label.isNotEmpty
            ? label
            : category.replaceAll('_', ' ');
    return '$subject current-head blocker$branchTail: $reason.';
  }

  static String _releaseTrustFreshnessDetail(
    Map<String, dynamic> releaseTrust,
  ) {
    final items = _asMapList(releaseTrust['items']);
    if (items.isEmpty) return '';
    final item = items.first;
    final createdAt = _clean(item['created_at']);
    final sourceKind = _clean(item['source_kind']);
    final path = _clean(item['path']);
    final supersedes = _asInt(item['supersedes_older_count']);
    if (createdAt.isEmpty && sourceKind.isEmpty && supersedes <= 0) {
      return '';
    }
    final parts = <String>[];
    if (createdAt.isNotEmpty) parts.add('seen $createdAt');
    if (sourceKind.isNotEmpty) {
      parts.add('from ${sourceKind.replaceAll('_', ' ')}');
    }
    if (path.isNotEmpty) parts.add(_tailPath(path));
    if (supersedes > 0) {
      parts.add('supersedes $supersedes older snapshot${_plural(supersedes)}');
    }
    return 'Evidence ${parts.join(', ')}.';
  }

  static String _controlDetail({
    required Map<String, dynamic> controlTrust,
    required int blockers,
    required int highRisk,
    required int activeQuarantines,
    required int pausedUncovered,
  }) {
    if (blockers <= 0 &&
        highRisk <= 0 &&
        activeQuarantines <= 0 &&
        pausedUncovered <= 0) {
      return 'No control-plane blocker is surfaced.';
    }
    final parts = <String>[];
    final categories = _asMap(controlTrust['category_counts']);
    final quarantineCount = _asInt(categories['quarantine']);
    _addCount(parts, quarantineCount > 0 ? quarantineCount : activeQuarantines,
        'quarantine');
    _addCount(parts, categories['paused_automation'], 'paused automation');
    _addCount(parts, categories['detached_worktree'], 'detached worktree');
    _addCount(parts, categories['dirty_worktree'], 'dirty worktree');
    _addCount(parts, categories['agent_lock'], 'agent lock');
    if (parts.isEmpty) _addCount(parts, blockers, 'blocker');
    _addCount(parts, highRisk, 'high-risk');
    if (pausedUncovered > 0 && _asInt(categories['paused_automation']) <= 0) {
      _addCount(parts, pausedUncovered, 'paused automation');
    }
    final detail = _clean(controlTrust['next_action_detail']);
    if (detail.isNotEmpty) {
      return '${parts.join(', ')}. $detail';
    }
    return '${parts.join(', ')} needs operator review.';
  }

  static _SafetyNextAction _nextAction({
    required Map<String, dynamic> operatorInbox,
    required Map<String, dynamic> runtimePressure,
    required Map<String, dynamic> runtimeQueue,
    required Map<String, dynamic> releaseTrust,
    required Map<String, dynamic> controlTrust,
  }) {
    final inboxAction = _clean(operatorInbox['next_action']);
    var label = _clean(operatorInbox['next_action_label']);
    var detail = _clean(operatorInbox['next_action_detail']);
    var agent = _clean(operatorInbox['next_action_agent']);
    var kind = _clean(operatorInbox['next_action_kind']);
    if (label.isNotEmpty && inboxAction != 'keep_monitoring') {
      return _SafetyNextAction(
        label: label,
        detail: detail,
        agent: agent,
        kind: kind,
        runId: _clean(operatorInbox['next_action_run_id']),
        path: _clean(operatorInbox['next_action_path']),
        openPath: _clean(operatorInbox['next_action_open_path']),
        recoveryAction: _clean(operatorInbox['next_action_recovery_action']),
        buttonLabel: _clean(operatorInbox['next_action_button_label']),
        handoffLabel: _clean(operatorInbox['next_action_handoff_label']),
        handoffCopy: _clean(operatorInbox['next_action_handoff_copy']),
      );
    }

    final runtimeStatus = _clean(runtimePressure['status']);
    final runtimeNeedsReview = _asInt(runtimePressure['blocker_count']) > 0 ||
        _asInt(runtimePressure['warning_count']) > 0 ||
        runtimeStatus == 'warning' ||
        runtimeStatus == 'failed';
    label = _firstClean([
      runtimePressure['next_action_label'],
      runtimeQueue['next_action_label'],
    ]);
    detail = _firstClean([
      runtimePressure['next_action_detail'],
      runtimePressure['detail'],
      runtimeQueue['next_action_detail'],
    ]);
    kind = _firstClean([
      runtimePressure['next_action'],
      runtimeQueue['next_action'],
    ]);
    if (runtimeNeedsReview && label.isNotEmpty && label != 'Keep monitoring') {
      return _SafetyNextAction(
        label: label,
        detail: detail,
        kind: kind,
        path: _firstClean([
          runtimePressure['path'],
          runtimeQueue['next_action_path'],
        ]),
        openPath: _firstClean([
          runtimePressure['open_path'],
          runtimeQueue['next_action_open_path'],
        ]),
        buttonLabel: 'Review runtime',
        handoffLabel: _firstClean([
          runtimePressure['next_action_handoff_label'],
          runtimeQueue['next_action_handoff_label'],
        ]),
        handoffCopy: _firstClean([
          runtimePressure['next_action_handoff_copy'],
          runtimeQueue['next_action_handoff_copy'],
        ]),
      );
    }

    label = _clean(controlTrust['next_action_label']);
    detail = _clean(controlTrust['next_action_detail']);
    kind = _clean(controlTrust['next_action_kind']);
    if (label.isNotEmpty) {
      return _SafetyNextAction(
        label: label,
        detail: detail,
        kind: kind,
        path: _clean(controlTrust['next_action_path']),
        openPath: _clean(controlTrust['next_action_open_path']),
        handoffLabel: _clean(controlTrust['next_action_handoff_label']),
        handoffCopy: _clean(controlTrust['next_action_handoff_copy']),
      );
    }

    label = _clean(releaseTrust['next_action_label']);
    detail = _clean(releaseTrust['next_action_detail']);
    kind = _clean(releaseTrust['next_action_kind']);
    if (label.isNotEmpty) {
      final releaseItems = _asMapList(releaseTrust['items']);
      final releaseItem =
          releaseItems.isEmpty ? <String, dynamic>{} : releaseItems.first;
      return _SafetyNextAction(
        label: label,
        detail: detail,
        agent: _clean(releaseTrust['next_action_agent']),
        kind: kind,
        path: _firstClean([
          releaseTrust['next_action_path'],
          releaseItem['path'],
        ]),
        openPath: _firstClean([
          releaseTrust['next_action_open_path'],
          releaseItem['open_path'],
        ]),
      );
    }

    return const _SafetyNextAction(
      label: noOperatorActionLabel,
    );
  }

  static _SafetyControlTarget _controlTarget({
    required Map<String, dynamic> controlTrust,
    required Map<String, dynamic> agentFlow,
  }) {
    final controlItems = _asMapList(controlTrust['items']);
    final quarantineItem = controlItems.firstWhere(
      (item) => _clean(item['kind']) == 'quarantined_target',
      orElse: () => <String, dynamic>{},
    );
    if (quarantineItem.isNotEmpty) {
      return _quarantineControlTarget(quarantineItem);
    }

    final flowItems = _asMapList(agentFlow['items']);
    final flowQuarantine = flowItems.firstWhere(
      (item) =>
          _clean(item['status']) == 'quarantined_target_active' ||
          _clean(item['quarantine_thread_id']).isNotEmpty,
      orElse: () => <String, dynamic>{},
    );
    if (flowQuarantine.isNotEmpty) {
      return _quarantineControlTarget(flowQuarantine);
    }

    final targets = _asMapList(agentFlow['quarantined_targets']);
    final activeTarget = targets.firstWhere(
      (item) => item['active'] == true,
      orElse: () => <String, dynamic>{},
    );
    if (activeTarget.isNotEmpty) {
      return _quarantineControlTarget(activeTarget);
    }
    return const _SafetyControlTarget();
  }

  static _SafetyControlTarget _quarantineControlTarget(
    Map<String, dynamic> item,
  ) {
    final threadId = _firstClean([
      item['thread_id'],
      item['quarantine_thread_id'],
    ]);
    final status = _firstClean([
      item['status'],
      item['quarantine_status'],
    ]);
    final label = _firstClean([
      item['label'],
      item['operator_label'],
      item['quarantine_operator_label'],
    ]);
    final proofRemaining = _asIntNullable(_firstClean([
      item['proof_remaining_minutes'],
      item['proof_window_remaining_minutes'],
      item['quarantine_proof_remaining_minutes'],
    ]));
    final proofWindow = _asIntNullable(_firstClean([
      item['proof_window_minutes'],
      item['quarantine_proof_window_minutes'],
    ]));
    final activity = _firstClean([
      item['activity_state'],
      item['quarantine_activity_state'],
    ]);
    final goalStatus = _firstClean([
      item['target_session_goal_status'],
      item['quarantine_session_goal_status'],
    ]);
    final path = _firstClean([
      item['source_path'],
      item['registry_path'],
      item['path'],
    ]);
    final openPath = _firstClean([
      item['open_path'],
      item['registry_open_path'],
    ]);

    final parts = <String>[];
    if (threadId.isNotEmpty) {
      parts.add('target ${_shortId(threadId)}');
    }
    if (status.isNotEmpty) {
      parts.add(status.toLowerCase().replaceAll('_', ' '));
    }
    if (label.isNotEmpty) {
      parts.add(label.toLowerCase());
    }
    if (activity.isNotEmpty) {
      parts.add(activity.toLowerCase().replaceAll('_', ' '));
    }
    if (goalStatus.isNotEmpty) {
      parts.add('goal $goalStatus');
    }
    if (proofRemaining != null) {
      parts.add(
        proofRemaining > 0
            ? '$proofRemaining min proof left'
            : 'proof window met',
      );
    } else if (proofWindow != null && proofWindow > 0) {
      parts.add('$proofWindow min proof required');
    }

    final title = label.isNotEmpty
        ? 'Quarantine: $label'
        : status.contains('TERMINATION')
            ? 'Quarantine: Needs operator stop'
            : 'Quarantine target';
    final detail = parts.isEmpty
        ? 'Active quarantined target needs control-plane review.'
        : parts.join(' | ');
    return _SafetyControlTarget(
      label: title,
      detail: detail,
      path: path,
      openPath: openPath,
      handoffLabel: _firstClean([
        item['handoff_label'],
        item['operator_handoff_label'],
        item['quarantine_operator_handoff_label'],
      ]),
      handoffCopy: _firstClean([
        item['handoff_copy'],
        item['operator_handoff_copy'],
        item['quarantine_operator_handoff_copy'],
      ]),
    );
  }

  static _SafetyEvidence _evidence({
    required Map<String, dynamic> operatorInbox,
    required Map<String, dynamic> agentFlow,
    required Map<String, dynamic> runtimePressure,
    required Map<String, dynamic> releaseTrust,
  }) {
    final inboxItems = _asMapList(operatorInbox['items']);
    final flowItems = _asMapList(agentFlow['items']);
    final releaseItems = _asMapList(releaseTrust['items']);
    final nextActionPath = _clean(operatorInbox['next_action_path']);
    final nextInboxItem = _matchingItem(inboxItems, nextActionPath);
    final nextFlowItem = _matchingItem(flowItems, nextActionPath);
    final runtimeItems = _asMapList(runtimePressure['items']);
    final runtimeItem =
        runtimeItems.isEmpty ? <String, dynamic>{} : runtimeItems.first;
    final releaseItem =
        releaseItems.isEmpty ? <String, dynamic>{} : releaseItems.first;
    final reviewPacket = _asMap(agentFlow['review_packet_summary']);
    final reviewItems = _asMapList(reviewPacket['items']);
    final reviewItem =
        reviewItems.isEmpty ? <String, dynamic>{} : reviewItems.first;

    final timestamp = _firstClean([
      nextInboxItem['created_at'],
      nextInboxItem['updated_at'],
      nextFlowItem['created_at'],
      nextFlowItem['updated_at'],
      runtimePressure['created_at'],
      runtimeItem['created_at'],
      releaseItem['created_at'],
      releaseItem['updated_at'],
      reviewItem['created_at'],
    ]);
    final sourceKind = _firstClean([
      releaseItem['source_kind'],
      runtimeItem['kind'],
      nextInboxItem['source_kind'],
      nextFlowItem['source_kind'],
    ]);
    final supersedes = _asIntNullable(_firstClean([
      releaseItem['supersedes_older_count'],
      nextInboxItem['supersedes_older_count'],
      nextFlowItem['supersedes_older_count'],
    ]));
    final head = _shortSha(
      _firstClean([
        nextInboxItem['worktree_head_short'],
        nextInboxItem['head_short'],
        nextInboxItem['review_request_worktree_head'],
        nextFlowItem['worktree_head_short'],
        nextFlowItem['head_short'],
        runtimeItem['head_short'],
        releaseItem['head_short'],
        releaseItem['worktree_head_short'],
        reviewItem['worktree_head'],
      ]),
    );
    final path = _firstClean([
      nextActionPath,
      nextInboxItem['path'],
      nextFlowItem['path'],
      runtimePressure['path'],
      runtimeItem['path'],
      releaseItem['path'],
      reviewItem['path'],
    ]);
    final openPath = _firstClean([
      operatorInbox['next_action_open_path'],
      nextInboxItem['open_path'],
      nextFlowItem['open_path'],
      runtimePressure['open_path'],
      runtimeItem['open_path'],
      releaseItem['open_path'],
      reviewItem['open_path'],
    ]);
    final parts = <String>[];
    if (timestamp.isNotEmpty) parts.add('Seen $timestamp');
    if (sourceKind.isNotEmpty) {
      parts.add('source ${sourceKind.replaceAll('_', ' ')}');
    }
    if (supersedes != null && supersedes > 0) {
      parts.add('supersedes $supersedes older snapshot${_plural(supersedes)}');
    }
    if (head.isNotEmpty) parts.add('head $head');
    if (path.isNotEmpty || openPath.isNotEmpty) {
      parts.add(_tailPath(path.isNotEmpty ? path : openPath));
    }
    return _SafetyEvidence(
      label: 'Evidence',
      detail: parts.isEmpty ? 'No evidence path surfaced.' : parts.join(' | '),
      path: path,
      openPath: openPath,
    );
  }

  static String _runtimeDetail(
    Map<String, dynamic> runtimePressure,
    int blockers,
    int warnings,
  ) {
    if (runtimePressure.isEmpty) {
      return 'No runtime pressure evidence is surfaced.';
    }
    final detail = _clean(runtimePressure['detail']);
    if (detail.isNotEmpty) return detail;
    if (blockers > 0 || warnings > 0) {
      final parts = <String>[];
      _addCount(parts, blockers, 'blocker');
      _addCount(parts, warnings, 'warning');
      return 'Runtime pressure has ${parts.join(', ')}.';
    }
    return 'Runtime pressure evidence is inside the current guardrails.';
  }

  static Map<String, dynamic> _matchingItem(
    List<Map<String, dynamic>> items,
    String path,
  ) {
    if (items.isEmpty) return <String, dynamic>{};
    if (path.isNotEmpty) {
      for (final item in items) {
        if (_clean(item['path']) == path || _clean(item['open_path']) == path) {
          return item;
        }
      }
    }
    return items.first;
  }

  static String _nonAuthorizationDetail(AutopilotSafetySeverity severity) {
    if (severity == AutopilotSafetySeverity.clear) {
      return 'The strip does not grant release, runtime, PR, or live-behavior permission.';
    }
    return 'Evidence only: no release, runtime, PR ready/merge, route cutover, or live behavior is authorized.';
  }

  static Map<String, dynamic> _asMap(Object? value) {
    if (value is Map) {
      return value.map((key, entry) => MapEntry(key.toString(), entry));
    }
    return <String, dynamic>{};
  }

  static List<Map<String, dynamic>> _asMapList(Object? value) {
    if (value is! Iterable) return <Map<String, dynamic>>[];
    return [
      for (final entry in value)
        if (entry is Map)
          entry.map((key, item) => MapEntry(key.toString(), item)),
    ];
  }

  static void _addCount(List<String> parts, Object? value, String label) {
    final count = _asInt(value);
    if (count <= 0) return;
    parts.add('$count $label${_plural(count)}');
  }

  static String _plural(int count) => count == 1 ? '' : 's';

  static String _firstClean(Iterable<Object?> values) {
    for (final value in values) {
      final clean = _clean(value);
      if (clean.isNotEmpty) return clean;
    }
    return '';
  }

  static String _shortSha(String value) {
    final clean = value.trim();
    if (clean.length <= 12) return clean;
    return clean.substring(0, 12);
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

  static int _asInt(Object? value) {
    if (value is int) return value;
    if (value is num) return value.toInt();
    return int.tryParse(value?.toString() ?? '') ?? 0;
  }

  static int? _asIntNullable(Object? value) {
    if (value is int) return value;
    if (value is num) return value.toInt();
    final clean = value?.toString().trim() ?? '';
    if (clean.isEmpty) return null;
    return int.tryParse(clean);
  }

  static String _shortId(String value) {
    final clean = value.trim();
    if (clean.length <= 8) return clean;
    return clean.substring(0, 8);
  }

  static String _clean(Object? value) => value?.toString().trim() ?? '';
}

class _SafetyNextAction {
  const _SafetyNextAction({
    required this.label,
    this.detail = '',
    this.agent = '',
    this.kind = '',
    this.runId = '',
    this.path = '',
    this.openPath = '',
    this.recoveryAction = '',
    this.buttonLabel = '',
    this.handoffLabel = '',
    this.handoffCopy = '',
  });

  final String label;
  final String detail;
  final String agent;
  final String kind;
  final String runId;
  final String path;
  final String openPath;
  final String recoveryAction;
  final String buttonLabel;
  final String handoffLabel;
  final String handoffCopy;
}

class _SafetyEvidence {
  const _SafetyEvidence({
    required this.label,
    required this.detail,
    this.path = '',
    this.openPath = '',
  });

  final String label;
  final String detail;
  final String path;
  final String openPath;
}

class _SafetyControlTarget {
  const _SafetyControlTarget({
    this.label = '',
    this.detail = '',
    this.path = '',
    this.openPath = '',
    this.handoffLabel = '',
    this.handoffCopy = '',
  });

  final String label;
  final String detail;
  final String path;
  final String openPath;
  final String handoffLabel;
  final String handoffCopy;
}
