class AutonomyRunPresenter {
  static const interruptedWorkerMessage =
      'This run stopped because the backend restarted during implementation. '
      'No changes were merged. Start a fresh run from the same prompt.';

  static bool isInterruptedWorkerRun(Map<String, dynamic>? run) {
    if (run == null) return false;
    final status = _text(run['status']).toLowerCase();
    final message = [
      _text(run['error_message']),
      _text(run['merge_message']),
    ].join(' ').toLowerCase();
    return status == 'blocked' &&
        message.contains('interrupted') &&
        (message.contains('api restart') ||
            message.contains('durable run') ||
            message.contains('worktree'));
  }

  static bool canRerun(Map<String, dynamic>? run) {
    if (run == null) return false;
    final prompt = _text(run['prompt']).trim();
    if (prompt.isEmpty) return false;
    final status = _text(run['status']).toLowerCase();
    return const {'blocked', 'failed', 'cancelled'}.contains(status);
  }

  static String blockedRunMessage(Map<String, dynamic> run) {
    if (isInterruptedWorkerRun(run)) return interruptedWorkerMessage;
    final error = _text(run['error_message']);
    if (_mentionsRepairedRerun(error)) {
      return 'This run ended before the backend repair could help it. '
          'Start a fresh run from the same prompt to use the fixed worker.';
    }
    final friendlyError = _friendlyBlockedMessage(error);
    if (friendlyError.isNotEmpty) return friendlyError;
    if (error.isNotEmpty) return error;
    final merge = _text(run['merge_message']);
    if (_mentionsRepairedRerun(merge)) {
      return 'This run ended before the backend repair could help it. '
          'Start a fresh run from the same prompt to use the fixed worker.';
    }
    final friendlyMerge = _friendlyBlockedMessage(merge);
    if (friendlyMerge.isNotEmpty) return friendlyMerge;
    if (merge.isNotEmpty) return merge;
    return 'This run is blocked. Review the tracking panel for the safe next step.';
  }

  static String stepBody(Map<String, dynamic> step) {
    final stage = _text(step['stage']);
    final detail = _map(step['detail']);
    final rawDetail = step['detail'];
    final status = _text(step['status']);
    final title = _text(step['title']);

    if (_mentionsInterruption(title) ||
        _mentionsInterruption(compact(rawDetail))) {
      return interruptedWorkerMessage;
    }

    switch (stage) {
      case 'queued':
        final repoName = _firstText(detail, ['repo_name', 'repo']);
        final repoId = _text(detail['repo_id']);
        if (repoName.isNotEmpty && repoId.isNotEmpty) {
          return 'Queued for $repoName (#$repoId).';
        }
        if (repoName.isNotEmpty) {
          return 'Queued for $repoName.';
        }
        return 'Queued and waiting for the local Autopilot worker.';
      case 'classify':
        final preview = _firstText(detail, ['prompt_preview', 'prompt']);
        if (preview.isNotEmpty) {
          return 'Read the request: "$preview"';
        }
        return 'Classifying the request and choosing the safest path.';
      case 'repo_scan':
        final repo = _firstText(detail, ['repo', 'repo_name']);
        final path = _firstText(detail, ['path', 'repo_path']);
        if (repo.isNotEmpty && path.isNotEmpty) {
          return 'Scanned $repo at $path.';
        }
        if (repo.isNotEmpty) {
          return 'Scanned $repo for relevant project context.';
        }
        return 'Scanned repository context for this request.';
      case 'plan':
        final files = _stringList(detail['files']);
        if (files.isNotEmpty) {
          return 'Drafted an implementation plan around ${_listSummary(files)}.';
        }
        return 'Architect is drafting the implementation plan.';
      case 'assign_roles':
        final agents = _mapList(detail['agents']);
        if (agents.isNotEmpty) {
          final names = agents
              .map((agent) => _firstText(agent, ['name', 'role']))
              .where((name) => name.isNotEmpty)
              .toList();
          if (names.isNotEmpty) {
            return 'Assigned lanes for ${_listSummary(names, limit: 4)}.';
          }
        }
        return 'Assigned agent lanes and file ownership.';
      case 'implement':
        final files = _stringList(detail['files']);
        if (files.isNotEmpty) {
          return 'Generating changes for ${_listSummary(files)}.';
        }
        return status == 'completed'
            ? 'Implementation finished.'
            : 'Implementation is in progress.';
      case 'integrate':
        final branch = _firstText(detail, ['branch', 'integration_branch']);
        if (branch.isNotEmpty) return 'Integrating agent work on $branch.';
        return 'Integrating the agent work into the run branch.';
      case 'validate':
        final command = _firstText(detail, ['command', 'step_key']);
        final exitCode = _text(detail['exit_code']);
        if (command.isNotEmpty && exitCode.isNotEmpty) {
          return 'Ran $command with exit code $exitCode.';
        }
        if (command.isNotEmpty) return 'Running validation: $command.';
        return 'Running validation checks.';
      case 'repair':
        return 'Repair loop is applying feedback from validation.';
      case 'merge':
        final message = _firstText(detail, ['message', 'merge_message']);
        final friendly = _friendlyBlockedMessage(message);
        if (friendly.isNotEmpty) return friendly;
        if (message.isNotEmpty) return message;
        return 'Checking whether the validated branch can merge safely.';
      case 'learn':
        return 'Recording the run outcome as a local learning signal.';
      default:
        final fallback = compact(rawDetail);
        if (fallback.isNotEmpty) return fallback;
        return title.isNotEmpty ? title : 'Autopilot updated this run.';
    }
  }

  static String artifactBody(Map<String, dynamic> artifact) {
    final type = _text(artifact['artifact_type']);
    final name = _text(artifact['name']);
    final json = _map(artifact['content_json']);
    final content = _text(artifact['content']).trim();

    switch (type) {
      case 'model_call':
        return _modelCallBody(json, name);
      case 'worktree':
        final branch = _firstText(json, ['branch', 'integration_branch']);
        final path = _firstText(json, ['path', 'worktree', 'worktree_path']);
        if (branch.isNotEmpty && path.isNotEmpty) {
          return 'Created isolated worktree $branch at $path.';
        }
        if (path.isNotEmpty) return 'Created isolated worktree at $path.';
        break;
      case 'diff':
        final files = _stringList(json['files']);
        if (files.isNotEmpty) {
          return 'Generated a patch for ${_listSummary(files)}.';
        }
        return 'Generated a patch for review.';
      case 'diff_rejected':
        final reason = _firstText(json, ['stderr', 'error', 'reason']);
        if (reason.isNotEmpty) {
          return 'Rejected the generated patch because it did not apply cleanly: '
              '${_truncate(reason, 260)}';
        }
        return 'Rejected the generated patch because it did not apply cleanly.';
      case 'commit':
        final sha = _firstText(json, ['sha', 'commit_sha']);
        final message = _firstText(json, ['message', 'commit_message']);
        if (sha.isNotEmpty && message.isNotEmpty) {
          return 'Created commit ${_shortSha(sha)}: $message';
        }
        if (sha.isNotEmpty) return 'Created commit ${_shortSha(sha)}.';
        break;
      case 'visual_screenshot':
        final path = _firstText(json, ['path', 'url']);
        if (path.isNotEmpty) return 'Attached screenshot evidence: $path';
        return 'Requested screenshot evidence for UI/UX validation.';
      case 'visual_video':
        if (json['skipped'] == true) {
          final reason = _firstText(json, ['skip_reason', 'reason']);
          return reason.isEmpty
              ? 'Video validation was skipped.'
              : 'Video validation was skipped: $reason';
        }
        final path = _firstText(json, ['path', 'url']);
        if (path.isNotEmpty) return 'Attached video evidence: $path';
        return 'Requested video evidence for UI/UX validation.';
      case 'ui_review':
      case 'ux_review':
        final summary = _firstText(json, ['summary', 'message']);
        if (summary.isNotEmpty) return summary;
        break;
    }

    if (content.isNotEmpty && !_looksStructured(content)) return content;
    final fallback = compact(json);
    if (fallback.isNotEmpty) return fallback;
    return content;
  }

  static String validationBody(List<Map<String, dynamic>> validation) {
    if (validation.isEmpty) return '';
    final skipped = validation.where(_validationSkipped).length;
    final passed = validation
        .where((item) => !_validationSkipped(item) && _validationPassed(item))
        .length;
    final failed = validation.length - passed - skipped;
    final lines = validation.take(6).map((item) {
      final key = _firstText(item, ['step_key', 'command', 'name']);
      final exitCode = _text(item['exit_code']);
      final label = key.isEmpty ? 'command' : key;
      if (_validationSkipped(item)) {
        final reason = _text(item['skip_reason']).trim();
        return reason.isEmpty ? '$label skipped' : '$label skipped: $reason';
      }
      final ok = _validationPassed(item);
      final status = ok ? 'passed' : 'failed';
      return exitCode.isEmpty
          ? '$label $status'
          : '$label $status with exit code $exitCode';
    }).toList();
    String summary;
    if (failed == 0 && skipped == 0) {
      summary =
          '$passed validation ${passed == 1 ? 'check' : 'checks'} passed.';
    } else if (failed == 0) {
      summary =
          '$passed validation ${passed == 1 ? 'check' : 'checks'} passed; '
          '$skipped skipped.';
    } else {
      summary = '$failed of ${validation.length} validation checks failed.';
    }
    return [summary, ...lines].join('\n');
  }

  static String planBody(Map<String, dynamic> plan) {
    if (plan.isEmpty) return '';
    final analysis = _text(plan['analysis']).trim();
    final notes = _text(plan['notes']).trim();
    final files = _mapList(plan['files'])
        .map((file) => _firstText(file, ['path', 'file']))
        .where((path) => path.isNotEmpty)
        .toList();
    final parts = <String>[];
    if (analysis.isNotEmpty) parts.add(analysis);
    if (files.isNotEmpty) {
      parts.add('Files: ${_listSummary(files, limit: 6)}.');
    }
    if (notes.isNotEmpty) parts.add(notes);
    return parts.join('\n\n');
  }

  static String compact(dynamic value) {
    if (value == null) return '';
    if (value is String) return _truncate(value.trim(), 900);
    if (value is num || value is bool) return value.toString();
    if (value is List) {
      if (value.isEmpty) return '';
      final scalar =
          value.map(_scalarText).where((item) => item.isNotEmpty).toList();
      if (scalar.isNotEmpty) return _listSummary(scalar, limit: 5);
      return '${value.length} ${value.length == 1 ? 'item' : 'items'}';
    }
    if (value is Map) {
      final entries = Map<String, dynamic>.from(value)
          .entries
          .where((entry) =>
              _text(entry.value).isNotEmpty ||
              entry.value is Map ||
              entry.value is List)
          .take(6)
          .map((entry) => '${_label(entry.key)}: ${_compactValue(entry.value)}')
          .where((entry) => !entry.endsWith(': '))
          .toList();
      return _truncate(entries.join('; '), 900);
    }
    return _truncate(value.toString(), 900);
  }

  static String _modelCallBody(Map<String, dynamic> json, String name) {
    final ok = json['ok'];
    final model = _firstText(json, ['model', 'model_name']);
    final purpose = _firstText(json, ['purpose', 'stage', 'task']);
    final latency = _formatDurationMs(json['latency_ms']);
    final isChatModelCall = name == 'chat_model_call' ||
        purpose == 'brainstorm_chat' ||
        purpose == 'chat';
    final target =
        purpose.isNotEmpty ? purpose : (name.isNotEmpty ? name : 'planning');
    final modelText = model.isEmpty ? '' : ' with $model';
    final latencyText = latency.isEmpty ? '' : ' in $latency';
    final error = _firstText(json, ['error', 'error_message', 'message']);

    if (isChatModelCall) {
      if (ok == false) {
        return 'The local brainstorm chat model did not answer$latencyText. '
            'No repo scan or code changes were started.';
      }
      return 'The local brainstorm chat model answered$latencyText.';
    }
    if (ok == false) {
      final friendlyError = _friendlyModelError(error);
      final reason = friendlyError.isEmpty ? '' : ': $friendlyError';
      return 'Model call for $target$modelText did not complete$latencyText$reason';
    }
    return 'Model call for $target$modelText completed$latencyText.';
  }

  static String _friendlyModelError(String error) {
    final trimmed = error.trim();
    if (trimmed.isEmpty) return '';
    final lower = trimmed.toLowerCase();
    if (lower.contains('timed out') || lower.contains('timeouterror')) {
      return 'the local model timed out';
    }
    if (lower.contains('connection refused') ||
        lower.contains('urlopen error') ||
        lower.contains('failed to establish a new connection')) {
      return 'the local model service was not reachable';
    }
    return _truncate(trimmed, 260);
  }

  static bool _validationPassed(Map<String, dynamic> item) {
    final code = item['exit_code'];
    return code == 0 || code == '0' || item['passed'] == true;
  }

  static bool _validationSkipped(Map<String, dynamic> item) =>
      item['skipped'] == true || _text(item['skipped']).toLowerCase() == 'true';

  static String _friendlyBlockedMessage(String message) {
    final lower = message.toLowerCase();
    if (lower.contains('target checkout has dirty changes') ||
        (lower.contains('dirty changes') &&
            lower.contains('autopilot scope'))) {
      return 'Autopilot finished the work and created a validated branch, '
          'but it did not merge because your current checkout already has '
          'local edits in the same files. Commit or stash those edits, then '
          'rerun the prompt or merge the validated branch.';
    }
    if (lower.contains('no implementation diffs were generated') ||
        lower.contains('no usable implementation diffs were produced')) {
      return 'Autopilot could not turn this run into a usable code patch. '
          'No changes were merged. Start a fresh run with the same prompt; '
          'if it happens again, make the requested file or behavior more specific.';
    }
    return '';
  }

  static bool _mentionsInterruption(String text) {
    final lower = text.toLowerCase();
    return lower.contains('interrupted') &&
        (lower.contains('api restart') ||
            lower.contains('durable run') ||
            lower.contains('worktree'));
  }

  static bool _mentionsRepairedRerun(String text) {
    final lower = text.toLowerCase();
    return lower.contains('repaired') && lower.contains('rerun');
  }

  static String _formatDurationMs(dynamic raw) {
    final ms = raw is num ? raw.toDouble() : double.tryParse(_text(raw));
    if (ms == null || ms <= 0) return '';
    if (ms >= 1000) return '${(ms / 1000).toStringAsFixed(1)}s';
    return '${ms.round()}ms';
  }

  static String _firstText(Map<String, dynamic> map, List<String> keys) {
    for (final key in keys) {
      final value = _text(map[key]).trim();
      if (value.isNotEmpty) return value;
    }
    return '';
  }

  static Map<String, dynamic> _map(dynamic raw) {
    if (raw is Map<String, dynamic>) return raw;
    if (raw is Map) return Map<String, dynamic>.from(raw);
    return <String, dynamic>{};
  }

  static List<Map<String, dynamic>> _mapList(dynamic raw) {
    if (raw is! List) return const [];
    return raw
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  static List<String> _stringList(dynamic raw) {
    if (raw is! List) return const [];
    return raw
        .map(_scalarText)
        .where((item) => item.trim().isNotEmpty)
        .toList();
  }

  static String _compactValue(dynamic value) {
    if (value is Map) {
      final name = _firstText(
          Map<String, dynamic>.from(value), ['name', 'path', 'role']);
      return name.isEmpty ? '${value.length} fields' : name;
    }
    if (value is List) {
      if (value.isEmpty) return '';
      final scalar =
          value.map(_scalarText).where((item) => item.isNotEmpty).toList();
      if (scalar.isNotEmpty) return _listSummary(scalar, limit: 3);
      return '${value.length} ${value.length == 1 ? 'item' : 'items'}';
    }
    return _truncate(_scalarText(value), 220);
  }

  static String _scalarText(dynamic value) {
    if (value == null) return '';
    if (value is String) return value.trim();
    if (value is num || value is bool) return value.toString();
    return '';
  }

  static String _text(dynamic value) => value?.toString() ?? '';

  static String _label(String key) {
    final words = key.replaceAll('_', ' ').trim();
    if (words.isEmpty) return key;
    return words[0].toUpperCase() + words.substring(1);
  }

  static String _listSummary(List<String> items, {int limit = 3}) {
    final cleaned = items.where((item) => item.trim().isNotEmpty).toList();
    if (cleaned.isEmpty) return '';
    final shown = cleaned.take(limit).join(', ');
    final remaining = cleaned.length - limit;
    return remaining > 0 ? '$shown, and $remaining more' : shown;
  }

  static String _truncate(String text, int max) {
    if (text.length <= max) return text;
    return '${text.substring(0, max)}...';
  }

  static String _shortSha(String sha) {
    return sha.length > 10 ? sha.substring(0, 10) : sha;
  }

  static bool _looksStructured(String text) {
    final trimmed = text.trimLeft();
    return trimmed.startsWith('{') || trimmed.startsWith('[');
  }
}
