import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';
import '../network/network_error_message.dart';
import 'device_auth_store.dart';

/// Dispatch monitor: status, queue task, run history.
class BrainDispatchScreen extends StatefulWidget {
  const BrainDispatchScreen({super.key, required this.onOpenSettings});

  final VoidCallback onOpenSettings;

  @override
  State<BrainDispatchScreen> createState() => _BrainDispatchScreenState();
}

class _BrainDispatchScreenState extends State<BrainDispatchScreen>
    with SingleTickerProviderStateMixin {
  late TabController _tabs;
  final ChiliApiClient _api = ChiliApiClient();
  Timer? _statusTimer;

  bool _loadingBoot = true;
  bool _paired = false;

  Map<String, dynamic>? _status;
  String? _statusError;

  final TextEditingController _titleCtrl = TextEditingController();
  final TextEditingController _descCtrl = TextEditingController();
  // Phase E.2 — optional dynamic source for the Queue tab. The backend
  // resolver accepts: local Windows path, container path, GitHub URL,
  // USER/REPO shorthand, or a bare repo name.
  final TextEditingController _sourceCtrl = TextEditingController();
  List<Map<String, dynamic>> _projects = [];
  int? _projectId;
  bool _queueBusy = false;
  String? _queueError;

  List<Map<String, dynamic>> _runs = [];
  String? _runsError;
  bool _runsLoading = false;

  final TextEditingController _autopilotPromptCtrl = TextEditingController();
  final ScrollController _autopilotChatScroll = ScrollController();
  List<Map<String, dynamic>> _codeRepos = [];
  int? _autonomyRepoId;
  List<Map<String, dynamic>> _autonomyRuns = [];
  Map<String, dynamic>? _activeAutonomyRun;
  bool _autonomyLoading = false;
  bool _autonomyBusy = false;
  String? _autonomyError;
  Timer? _autonomyTimer;

  // Phase F — Context Brain tab state
  Map<String, dynamic>? _ctxStatus;
  List<Map<String, dynamic>> _ctxAssemblies = [];
  List<Map<String, dynamic>> _ctxSources = [];
  bool _ctxLoading = false;
  String? _ctxError;

  // History tab filter state
  bool _hideRouterEscalate = true; // hide noisy auto-escalations by default
  bool _onlyOperatorReview = false; // toggle to show ONLY notify_user=true
  int? _watchTaskId;

  final TextEditingController _pairEmailCtrl = TextEditingController();
  final TextEditingController _pairCodeCtrl = TextEditingController();
  final TextEditingController _pairLabelCtrl = TextEditingController();
  bool _pairBusy = false;
  String? _pairError;

  /// After a successful "Send code" request; mirrors chat.html pairing steps.
  bool _pairShowCodeStep = false;
  String? _pairCodeMessage;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 5, vsync: this);
    _tabs.addListener(_onTabChanged);
    unawaited(_boot());
  }

  void _onTabChanged() {
    if (_tabs.indexIsChanging) return;
    if (_tabs.index == 1) {
      unawaited(_loadAutonomyRuns());
    } else if (_tabs.index == 3) {
      unawaited(_loadRuns());
    } else if (_tabs.index == 4) {
      unawaited(_loadContextBrain());
    }
  }

  Future<void> _loadContextBrain() async {
    if (!mounted) return;
    setState(() {
      _ctxLoading = true;
      _ctxError = null;
    });
    try {
      final status = await _api.getContextBrainStatus();
      final assemblies = await _api.getContextBrainAssemblies(limit: 25);
      final sources = await _api.getContextBrainSources();
      if (!mounted) return;
      setState(() {
        _ctxStatus = status;
        _ctxAssemblies = assemblies;
        _ctxSources = sources;
        _ctxLoading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _ctxError = '$e';
        _ctxLoading = false;
      });
    }
  }

  Future<void> _boot() async {
    await _api.initFromStore();
    final token = await DeviceAuthStore.getToken();
    if (!mounted) return;
    if (token == null || token.isEmpty) {
      setState(() {
        _paired = false;
        _loadingBoot = false;
      });
      return;
    }
    await _startPairedSession();
    if (mounted) setState(() => _loadingBoot = false);
  }

  Future<void> _startPairedSession() async {
    setState(() => _paired = true);
    await _loadProjectsPicker();
    await _loadCodeRepos();
    await _refreshStatus();
    await _loadAutonomyRuns(silent: true);
    _statusTimer?.cancel();
    _statusTimer = Timer.periodic(const Duration(seconds: 30), (_) {
      if (mounted && _paired) unawaited(_refreshStatus());
    });
    _autonomyTimer?.cancel();
    _autonomyTimer = Timer.periodic(const Duration(seconds: 5), (_) {
      if (mounted && _paired && _tabs.index == 1) {
        unawaited(_refreshActiveAutonomyRun());
      }
    });
  }

  Future<void> _requestPairCode() async {
    final email = _pairEmailCtrl.text.trim();
    if (email.isEmpty) {
      setState(() => _pairError = 'Enter your email.');
      return;
    }
    setState(() {
      _pairBusy = true;
      _pairError = null;
    });
    try {
      final data = await _api.pairRequest(email: email);
      if (!mounted) return;
      final ok = data['ok'] == true;
      if (!ok) {
        setState(() {
          _pairError = '${data['error'] ?? 'Request failed'}';
          _pairBusy = false;
        });
        return;
      }
      final devCode = data['dev_code']?.toString();
      final msg =
          data['message']?.toString() ?? 'Check your email for the code.';
      if (devCode != null && devCode.isNotEmpty) {
        _pairCodeCtrl.text = devCode;
      }
      setState(() {
        _pairShowCodeStep = true;
        _pairCodeMessage = msg;
        _pairBusy = false;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _pairError = userVisibleNetworkError(e);
          _pairBusy = false;
        });
      }
    }
  }

  void _pairBackToEmail() {
    setState(() {
      _pairShowCodeStep = false;
      _pairCodeMessage = null;
      _pairError = null;
    });
  }

  Future<void> _verifyPairCode() async {
    final code = _pairCodeCtrl.text.trim();
    if (code.isEmpty) {
      setState(() => _pairError = 'Enter the code.');
      return;
    }
    setState(() {
      _pairBusy = true;
      _pairError = null;
    });
    try {
      final data = await _api.pairVerify(
        code: code,
        label: _pairLabelCtrl.text.trim().isEmpty
            ? 'CHILI Desktop Companion'
            : _pairLabelCtrl.text.trim(),
      );
      if (!mounted) return;
      if (data['ok'] != true) {
        setState(() {
          _pairError = '${data['error'] ?? 'Verification failed'}';
          _pairBusy = false;
        });
        return;
      }
      final token = data['token'] as String?;
      if (token == null || token.isEmpty) {
        setState(() {
          _pairError = 'Server did not return a token.';
          _pairBusy = false;
        });
        return;
      }
      await DeviceAuthStore.setToken(token);
      _api.token = token;
      _pairCodeCtrl.clear();
      setState(() {
        _pairBusy = false;
        _pairError = null;
        _pairShowCodeStep = false;
        _pairCodeMessage = null;
      });
      await _startPairedSession();
    } catch (e) {
      if (mounted) {
        setState(() {
          _pairError = userVisibleNetworkError(e);
          _pairBusy = false;
        });
      }
    }
  }

  Future<void> _loadProjectsPicker() async {
    try {
      final list = await _api.listProjects();
      final last = await DeviceAuthStore.getLastProjectId();
      int? pick;
      if (last != null) {
        final has = list.any((p) => p['id'] == last);
        if (has) pick = last;
      }
      pick ??= list.isNotEmpty ? (list.first['id'] as int?) : null;
      if (mounted) {
        setState(() {
          _projects = list;
          _projectId = pick;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() => _queueError = 'Could not load projects: $e');
      }
    }
  }

  Future<void> _loadCodeRepos() async {
    try {
      final list = await _api.getCodeBrainRepos();
      int? pick = _autonomyRepoId;
      if (pick != null && !list.any((repo) => _asInt(repo['id']) == pick)) {
        pick = null;
      }
      if (pick == null) {
        for (final repo in list) {
          if (repo['reachable_in_current_runtime'] == true) {
            pick = _asInt(repo['id']);
            if (pick != null) break;
          }
        }
      }
      if (pick == null && list.isNotEmpty) {
        pick = _asInt(list.first['id']);
      }
      if (mounted) {
        setState(() {
          _codeRepos = list;
          _autonomyRepoId = pick;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() => _autonomyError =
            'Could not load local repos: ${userVisibleNetworkError(e)}');
      }
    }
  }

  Future<void> _refreshStatus() async {
    try {
      final data = await _api.getDispatchStatus();
      if (mounted) {
        setState(() {
          _status = data;
          _statusError = null;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() => _statusError = '$e');
      }
    }
  }

  Future<void> _loadRuns() async {
    setState(() {
      _runsLoading = true;
      _runsError = null;
    });
    try {
      final list = await _api.getDispatchRuns(limit: 20, taskId: _watchTaskId);
      if (mounted) {
        setState(() {
          _runs = list;
          _runsLoading = false;
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _runsError = '$e';
          _runsLoading = false;
        });
      }
    }
  }

  bool _autonomyTerminal(Map<String, dynamic>? run) {
    final status = run?['status']?.toString() ?? '';
    return const {'merged', 'completed', 'blocked', 'failed', 'cancelled'}
        .contains(status);
  }

  Future<void> _loadAutonomyRuns({bool silent = false}) async {
    if (!mounted) return;
    if (!silent) {
      setState(() {
        _autonomyLoading = true;
        _autonomyError = null;
      });
    }
    try {
      final list = await _api.getProjectAutonomyRuns(limit: 20);
      Map<String, dynamic>? active = _activeAutonomyRun;
      if (list.isNotEmpty) {
        final activeId = active?['run_id']?.toString();
        active = list.firstWhere(
          (r) => r['run_id']?.toString() == activeId,
          orElse: () => list.first,
        );
      }
      if (!mounted) return;
      setState(() {
        _autonomyRuns = list;
        _activeAutonomyRun = active;
        _autonomyLoading = false;
        _autonomyError = null;
      });
      await _refreshActiveAutonomyRun(silent: true, force: true);
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _autonomyError = userVisibleNetworkError(e);
        _autonomyLoading = false;
      });
    }
  }

  Future<void> _refreshActiveAutonomyRun({
    bool silent = true,
    bool force = false,
  }) async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    if (_autonomyTerminal(_activeAutonomyRun) && silent && !force) return;
    try {
      final run = await _api.getProjectAutonomyRun(runId);
      if (!mounted) return;
      setState(() {
        _activeAutonomyRun = run;
        _autonomyError = null;
      });
    } catch (e) {
      if (!mounted || silent) return;
      setState(() => _autonomyError = userVisibleNetworkError(e));
    }
  }

  Future<void> _startAutopilot() async {
    final prompt = _autopilotPromptCtrl.text.trim();
    if (prompt.isEmpty) {
      setState(() => _autonomyError = 'Enter a prompt for Project Autopilot.');
      return;
    }
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.createProjectAutonomyRun(
        prompt: prompt,
        repoId: _autonomyRepoId,
      );
      if (!mounted) return;
      setState(() {
        _activeAutonomyRun = run;
        _autopilotPromptCtrl.clear();
      });
      await _loadAutonomyRuns(silent: true);
      await _refreshActiveAutonomyRun(silent: true, force: true);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Autopilot started: ${run['run_id']}')),
        );
      }
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _cancelAutopilot() async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.cancelProjectAutonomyRun(runId);
      if (mounted) setState(() => _activeAutonomyRun = run);
      await _refreshActiveAutonomyRun(silent: true, force: true);
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _mergeAutopilot() async {
    final runId = _activeAutonomyRun?['run_id']?.toString();
    if (runId == null || runId.isEmpty) return;
    setState(() {
      _autonomyBusy = true;
      _autonomyError = null;
    });
    try {
      final run = await _api.mergeProjectAutonomyRun(runId);
      if (mounted) setState(() => _activeAutonomyRun = run);
      await _loadAutonomyRuns(silent: true);
    } catch (e) {
      if (mounted) setState(() => _autonomyError = userVisibleNetworkError(e));
    } finally {
      if (mounted) setState(() => _autonomyBusy = false);
    }
  }

  Future<void> _submitQueue() async {
    final pid = _projectId;
    if (pid == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Select a project')),
      );
      return;
    }
    setState(() {
      _queueBusy = true;
      _queueError = null;
    });
    try {
      final res = await _api.queueDispatchTask(
        title: _titleCtrl.text.trim(),
        description: _descCtrl.text.trim(),
        projectId: pid,
        sourceInput:
            _sourceCtrl.text.trim().isEmpty ? null : _sourceCtrl.text.trim(),
      );
      await DeviceAuthStore.setLastProjectId(pid);
      if (!mounted) return;
      final tid = res['task_id'];
      final watchId = tid is int ? tid : int.tryParse('$tid');
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('Queued task #$tid'),
          action: SnackBarAction(
            label: 'Watch this task',
            onPressed: () {
              setState(() => _watchTaskId = watchId);
              _tabs.animateTo(2);
              unawaited(_loadRuns());
            },
          ),
        ),
      );
      _titleCtrl.clear();
      _descCtrl.clear();
      _sourceCtrl.clear();
    } catch (e) {
      if (mounted) setState(() => _queueError = '$e');
    } finally {
      if (mounted) setState(() => _queueBusy = false);
    }
  }

  Future<void> _confirmKillSwitch(bool targetActive) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title:
            Text(targetActive ? 'Enable kill switch?' : 'Disable kill switch?'),
        content: Text(
          targetActive
              ? 'Code dispatch will stop until you turn this off.'
              : 'Resume normal dispatch?',
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('Cancel')),
          FilledButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('Confirm')),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    try {
      await _api.toggleKillSwitch(
        active: targetActive,
        reason: targetActive ? 'brain_ui' : null,
      );
      await _refreshStatus();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Kill switch updated')),
        );
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Kill switch failed: $e')),
        );
      }
    }
  }

  String _agoLabel(String? iso) {
    if (iso == null || iso.isEmpty) return 'No activity recorded';
    final t = DateTime.tryParse(iso);
    if (t == null) return iso;
    final s = DateTime.now().difference(t).inSeconds;
    if (s < 60) return '$s seconds ago';
    if (s < 3600) return '${s ~/ 60} minutes ago';
    return '${s ~/ 3600} hours ago';
  }

  String _snapshotPreview(dynamic snap) {
    if (snap == null) return '—';
    try {
      final s = const JsonEncoder.withIndent('  ').convert(snap);
      if (s.length > 800) return '${s.substring(0, 800)}…';
      return s;
    } catch (_) {
      return snap.toString();
    }
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _autonomyTimer?.cancel();
    _tabs.removeListener(_onTabChanged);
    _tabs.dispose();
    _titleCtrl.dispose();
    _descCtrl.dispose();
    _sourceCtrl.dispose();
    _autopilotPromptCtrl.dispose();
    _autopilotChatScroll.dispose();
    _pairEmailCtrl.dispose();
    _pairCodeCtrl.dispose();
    _pairLabelCtrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_loadingBoot) {
      return const Center(child: CircularProgressIndicator());
    }
    if (!_paired) {
      return Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 440),
          child: Card(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    'Pair Your Device',
                    textAlign: TextAlign.center,
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                  const SizedBox(height: 8),
                  if (_pairError != null)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 12),
                      child: Material(
                        color: Colors.red.shade50,
                        borderRadius: BorderRadius.circular(8),
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Row(
                            children: [
                              Icon(Icons.error_outline,
                                  color: Colors.red.shade800, size: 20),
                              const SizedBox(width: 8),
                              Expanded(
                                child: Text(
                                  _pairError!,
                                  style: TextStyle(
                                      color: Colors.red.shade900, fontSize: 13),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  if (!_pairShowCodeStep) ...[
                    Text(
                      'Enter the email your admin registered for you. '
                      "We'll send a 6-digit verification code.",
                      textAlign: TextAlign.center,
                      style:
                          TextStyle(color: Colors.grey.shade700, fontSize: 13),
                    ),
                    const SizedBox(height: 20),
                    TextField(
                      controller: _pairEmailCtrl,
                      keyboardType: TextInputType.emailAddress,
                      autocorrect: false,
                      decoration: const InputDecoration(
                        hintText: 'your-email@example.com',
                        border: OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: 12),
                    FilledButton(
                      onPressed: _pairBusy ? null : _requestPairCode,
                      child: _pairBusy
                          ? const SizedBox(
                              height: 22,
                              width: 22,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('Send Code'),
                    ),
                  ] else ...[
                    if (_pairCodeMessage != null)
                      Text(
                        _pairCodeMessage!,
                        textAlign: TextAlign.center,
                        style: TextStyle(
                            color: Colors.grey.shade700, fontSize: 13),
                      ),
                    const SizedBox(height: 16),
                    TextField(
                      controller: _pairCodeCtrl,
                      keyboardType: TextInputType.text,
                      decoration: const InputDecoration(
                        hintText: '6-digit code',
                        border: OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: 12),
                    TextField(
                      controller: _pairLabelCtrl,
                      decoration: const InputDecoration(
                        hintText: 'Device name (e.g. My Phone)',
                        border: OutlineInputBorder(),
                      ),
                    ),
                    const SizedBox(height: 12),
                    FilledButton(
                      onPressed: _pairBusy ? null : _verifyPairCode,
                      child: _pairBusy
                          ? const SizedBox(
                              height: 22,
                              width: 22,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('Verify & Pair'),
                    ),
                    TextButton(
                      onPressed: _pairBusy ? null : _pairBackToEmail,
                      child: const Text('Use a different email'),
                    ),
                  ],
                  const SizedBox(height: 20),
                  OutlinedButton.icon(
                    onPressed: widget.onOpenSettings,
                    icon: const Icon(Icons.settings, size: 18),
                    label: const Text('Open Settings'),
                  ),
                ],
              ),
            ),
          ),
        ),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(24, 16, 24, 0),
          child:
              Text('Brain', style: Theme.of(context).textTheme.headlineMedium),
        ),
        TabBar(
          controller: _tabs,
          tabs: const [
            Tab(text: 'Status'),
            Tab(text: 'Autopilot'),
            Tab(text: 'Queue'),
            Tab(text: 'History'),
            Tab(text: 'Context'),
          ],
        ),
        Expanded(
          child: TabBarView(
            controller: _tabs,
            children: [
              _buildStatusTab(),
              _buildAutopilotTab(),
              _buildQueueTab(),
              _buildHistoryTab(),
              _buildContextTab(),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildStatusTab() {
    if (_statusError != null) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: [
          Card(
            color: Colors.red.shade50,
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Text(_statusError!,
                  style: TextStyle(color: Colors.red.shade900)),
            ),
          ),
          const SizedBox(height: 12),
          FilledButton(onPressed: _refreshStatus, child: const Text('Retry')),
        ],
      );
    }
    final s = _status;
    if (s == null) {
      return const Center(child: CircularProgressIndicator());
    }
    final killRaw = s['kill_switch'];
    final kill = killRaw is Map
        ? Map<String, dynamic>.from(killRaw)
        : <String, dynamic>{};
    final active = kill['active'] == true;
    final reason = kill['reason']?.toString() ?? '';
    final counters = (s['counters_5min'] as Map?) ?? {};
    final spendToday = (s['spend_today'] as List?) ?? [];
    final lastIso = s['last_dispatch_activity_at'] as String?;

    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        Card(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Kill switch',
                    style: Theme.of(context).textTheme.titleMedium),
                const SizedBox(height: 8),
                Text(
                  active ? 'ON${reason.isNotEmpty ? ': $reason' : ''}' : 'Off',
                  style: TextStyle(
                    fontWeight: FontWeight.w600,
                    color: active ? Colors.red.shade700 : Colors.green.shade700,
                  ),
                ),
                const SizedBox(height: 12),
                Row(
                  children: [
                    if (!active)
                      FilledButton(
                        onPressed: () => _confirmKillSwitch(true),
                        child: const Text('Enable'),
                      )
                    else
                      OutlinedButton(
                        onPressed: () => _confirmKillSwitch(false),
                        child: const Text('Disable'),
                      ),
                  ],
                ),
              ],
            ),
          ),
        ),
        const SizedBox(height: 12),
        Text('Last activity: ${_agoLabel(lastIso)}',
            style: TextStyle(color: Colors.grey.shade700)),
        const SizedBox(height: 16),
        Text('Counters (5 min)',
            style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: counters.entries.map((e) {
            return Chip(label: Text('${e.key}: ${e.value}'));
          }).toList(),
        ),
        if (counters.isEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text('No runs in the last 5 minutes',
                style: TextStyle(color: Colors.grey.shade600)),
          ),
        const SizedBox(height: 20),
        Text('Spend today', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        if (spendToday.isEmpty)
          Text('No LLM spend recorded today',
              style: TextStyle(color: Colors.grey.shade600))
        else
          ...spendToday.map((row) {
            final m = Map<String, dynamic>.from(row as Map<dynamic, dynamic>);
            final prov = m['provider'] ?? '';
            final calls = m['calls'] ?? 0;
            final usd = (m['spend_usd'] is num)
                ? (m['spend_usd'] as num).toDouble()
                : 0.0;
            return ListTile(
              dense: true,
              title: Text('$prov'),
              subtitle: Text('$calls calls · \$${usd.toStringAsFixed(4)}'),
            );
          }),
        if (spendToday.isNotEmpty) ...[
          const Divider(),
          Builder(
            builder: (context) {
              double total = 0;
              for (final row in spendToday) {
                final m =
                    Map<String, dynamic>.from(row as Map<dynamic, dynamic>);
                final u = m['spend_usd'];
                if (u is num) total += u.toDouble();
              }
              return Text('Total today: \$${total.toStringAsFixed(4)}',
                  style: const TextStyle(fontWeight: FontWeight.bold));
            },
          ),
        ],
      ],
    );
  }

  Map<String, dynamic> _asMap(dynamic raw) {
    if (raw is Map<String, dynamic>) return raw;
    if (raw is Map) return Map<String, dynamic>.from(raw);
    return <String, dynamic>{};
  }

  List<Map<String, dynamic>> _asMapList(dynamic raw) {
    if (raw is! List) return const [];
    return raw
        .whereType<Map>()
        .map((item) => Map<String, dynamic>.from(item))
        .toList();
  }

  List<String> _asStringList(dynamic raw) {
    if (raw is! List) return const [];
    return raw
        .map((item) => item?.toString() ?? '')
        .where((item) => item.trim().isNotEmpty)
        .toList();
  }

  int? _asInt(dynamic raw) {
    if (raw is int) return raw;
    return int.tryParse(raw?.toString() ?? '');
  }

  String _shortStamp(dynamic raw) {
    final value = raw?.toString() ?? '';
    if (value.length >= 19) {
      return value.substring(0, 19).replaceFirst('T', ' ');
    }
    return value;
  }

  Color _autonomyStatusColor(String status) {
    switch (status) {
      case 'merged':
      case 'completed':
        return Colors.green.shade700;
      case 'blocked':
        return Colors.orange.shade800;
      case 'failed':
      case 'cancelled':
        return Colors.red.shade700;
      case 'validating':
      case 'merging':
        return Colors.indigo.shade700;
      case 'running':
        return Colors.blue.shade700;
      default:
        return Colors.grey.shade700;
    }
  }

  Widget _buildAutopilotTab() {
    return LayoutBuilder(
      builder: (context, constraints) {
        if (constraints.maxWidth < 980) {
          return _buildAutonomyStackedCockpit();
        }
        final rightWidth = constraints.maxWidth >= 1320 ? 380.0 : 330.0;
        return Column(
          children: [
            if (_autonomyError != null) _buildAutonomyErrorBanner(),
            Expanded(
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  SizedBox(width: 280, child: _buildAutonomyThreadSidebar()),
                  VerticalDivider(width: 1, color: Colors.grey.shade300),
                  Expanded(child: _buildAutonomyConversationPane()),
                  VerticalDivider(width: 1, color: Colors.grey.shade300),
                  SizedBox(
                    width: rightWidth,
                    child: _buildAutonomyTrackingSidebar(),
                  ),
                ],
              ),
            ),
          ],
        );
      },
    );
  }

  Widget _buildAutonomyStackedCockpit() {
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (_autonomyError != null) _buildAutonomyErrorBanner(compact: true),
        SizedBox(height: 360, child: _buildAutonomyConversationPane()),
        const SizedBox(height: 12),
        SizedBox(height: 260, child: _buildAutonomyThreadSidebar()),
        const SizedBox(height: 12),
        if (_activeAutonomyRun != null)
          _buildAutonomyActiveRun(_activeAutonomyRun!)
        else
          _emptyAutonomyState('No Project Autopilot run selected'),
      ],
    );
  }

  Widget _buildAutonomyErrorBanner({bool compact = false}) {
    return Container(
      width: double.infinity,
      padding: EdgeInsets.all(compact ? 10 : 12),
      color: Colors.red.shade50,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.error_outline, color: Colors.red.shade800, size: 20),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              _autonomyError!,
              style: TextStyle(color: Colors.red.shade900, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyThreadSidebar() {
    return Container(
      color: Colors.grey.shade50,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 14, 10, 10),
            child: Row(
              children: [
                Icon(Icons.forum_outlined,
                    color: Theme.of(context).colorScheme.primary, size: 20),
                const SizedBox(width: 8),
                Expanded(
                  child: Text('Autopilot',
                      style: Theme.of(context).textTheme.titleMedium),
                ),
                IconButton(
                  tooltip: 'Refresh runs',
                  onPressed: _autonomyBusy
                      ? null
                      : () async {
                          await _loadCodeRepos();
                          await _loadAutonomyRuns();
                        },
                  icon: const Icon(Icons.refresh, size: 20),
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 0, 14, 12),
            child: FilledButton.icon(
              onPressed: _autonomyBusy
                  ? null
                  : () {
                      _autopilotPromptCtrl.clear();
                      setState(() => _activeAutonomyRun = null);
                    },
              icon: const Icon(Icons.add, size: 18),
              label: const Text('New run'),
            ),
          ),
          Expanded(
            child: _autonomyLoading
                ? const Center(child: CircularProgressIndicator())
                : _autonomyRuns.isEmpty
                    ? _emptyAutonomyState('No run history yet')
                    : ListView.builder(
                        padding: const EdgeInsets.fromLTRB(8, 0, 8, 12),
                        itemCount: _autonomyRuns.length,
                        itemBuilder: (context, index) =>
                            _buildAutonomyThreadTile(_autonomyRuns[index]),
                      ),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyThreadTile(Map<String, dynamic> run) {
    final runId = run['run_id']?.toString() ?? '';
    final status = run['status']?.toString() ?? 'unknown';
    final stage = run['current_stage']?.toString() ?? '';
    final prompt = run['prompt']?.toString() ?? '';
    final selected = _activeAutonomyRun?['run_id']?.toString() == runId;
    final color = _autonomyStatusColor(status);
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Material(
        color: selected ? Colors.indigo.shade50 : Colors.transparent,
        borderRadius: BorderRadius.circular(8),
        child: InkWell(
          borderRadius: BorderRadius.circular(8),
          onTap: () {
            setState(() => _activeAutonomyRun = run);
            unawaited(_refreshActiveAutonomyRun(silent: false, force: true));
          },
          child: Padding(
            padding: const EdgeInsets.all(10),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Icon(_autonomyStatusIcon(status), color: color, size: 18),
                    const SizedBox(width: 6),
                    Expanded(
                      child: Text(
                        runId.isEmpty ? 'Autopilot run' : runId,
                        style: const TextStyle(
                          fontWeight: FontWeight.w700,
                          fontSize: 12,
                        ),
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  prompt.isEmpty ? '(no prompt recorded)' : prompt,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: Colors.grey.shade800, fontSize: 12),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: [
                    _miniChip(status, color.withValues(alpha: 0.12), color),
                    if (stage.isNotEmpty)
                      _miniChip(stage, Colors.blueGrey.shade50,
                          Colors.blueGrey.shade800),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  _shortStamp(run['updated_at'] ?? run['created_at']),
                  style: TextStyle(color: Colors.grey.shade600, fontSize: 11),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildAutonomyConversationPane() {
    final run = _activeAutonomyRun;
    return Container(
      color: Colors.white,
      child: Column(
        children: [
          _buildAutonomyConversationHeader(run),
          Expanded(
            child: run == null
                ? _emptyAutonomyState('Start or select an Autopilot run')
                : _buildAutonomyChatTimeline(run),
          ),
          _buildAutonomyComposer(),
        ],
      ),
    );
  }

  Widget _buildAutonomyConversationHeader(Map<String, dynamic>? run) {
    final status = run?['status']?.toString() ?? 'ready';
    final stage = run?['current_stage']?.toString() ?? '';
    final merge = run?['merge_status']?.toString() ?? '';
    final color = _autonomyStatusColor(status);
    final terminal = _autonomyTerminal(run);
    final branch = run?['integration_branch']?.toString() ?? '';
    final canMerge = terminal &&
        branch.isNotEmpty &&
        status != 'merged' &&
        merge != 'merged';
    return Container(
      padding: const EdgeInsets.fromLTRB(18, 12, 14, 12),
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: Colors.grey.shade300)),
      ),
      child: Row(
        children: [
          Icon(_autonomyStatusIcon(status), color: color, size: 22),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  run?['run_id']?.toString().isNotEmpty == true
                      ? run!['run_id'].toString()
                      : 'Project Autopilot',
                  style: Theme.of(context).textTheme.titleMedium,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 4),
                Wrap(
                  spacing: 6,
                  runSpacing: 4,
                  children: [
                    _miniChip(status, color.withValues(alpha: 0.12), color),
                    if (stage.isNotEmpty)
                      _miniChip(
                          stage, Colors.indigo.shade50, Colors.indigo.shade800),
                    if (merge.isNotEmpty)
                      _miniChip('merge: $merge', Colors.blueGrey.shade50,
                          Colors.blueGrey.shade800),
                  ],
                ),
              ],
            ),
          ),
          IconButton(
            tooltip: 'Refresh',
            onPressed: _autonomyBusy || run == null
                ? null
                : () => _refreshActiveAutonomyRun(
                      silent: false,
                      force: true,
                    ),
            icon: const Icon(Icons.refresh),
          ),
          IconButton(
            tooltip: 'Cancel run',
            onPressed: _autonomyBusy || run == null || terminal
                ? null
                : _cancelAutopilot,
            icon: const Icon(Icons.stop_circle_outlined),
          ),
          IconButton(
            tooltip: 'Merge validated branch',
            onPressed: _autonomyBusy || !canMerge ? null : _mergeAutopilot,
            icon: const Icon(Icons.merge_type),
          ),
        ],
      ),
    );
  }

  Widget _buildAutonomyChatTimeline(Map<String, dynamic> run) {
    final prompt = run['prompt']?.toString() ?? '';
    final plan = _asMap(run['plan']);
    final steps = _asMapList(run['steps']);
    final artifacts = _asMapList(run['artifacts']);
    final validation = _asMapList(run['validation']);
    final error = run['error_message']?.toString() ?? '';
    final merge = run['merge_message']?.toString() ?? '';
    final widgets = <Widget>[
      if (prompt.isNotEmpty)
        _buildChatBubble(
          icon: Icons.person_outline,
          title: 'Operator',
          body: prompt,
          alignRight: true,
          color: Colors.indigo.shade700,
          background: Colors.indigo.shade50,
        ),
      ...steps.map(_buildAutonomyStepBubble),
      if (plan.isNotEmpty) _buildAutonomyPlanBubble(plan),
      ...artifacts
          .where((artifact) => {'model_call', 'diff', 'diff_rejected', 'commit'}
              .contains(artifact['artifact_type']?.toString()))
          .map(_buildAutonomyArtifactBubble),
      if (validation.isNotEmpty) _buildAutonomyValidationBubble(validation),
      if (merge.isNotEmpty)
        _buildChatBubble(
          icon: Icons.merge_type,
          title: 'Merge gate',
          body: merge,
          color: Colors.blueGrey.shade700,
          background: Colors.blueGrey.shade50,
        ),
      if (error.isNotEmpty)
        _buildChatBubble(
          icon: Icons.warning_amber_outlined,
          title: 'Blocked reason',
          body: error,
          color: Colors.orange.shade900,
          background: Colors.orange.shade50,
        ),
    ];
    return ListView(
      controller: _autopilotChatScroll,
      padding: const EdgeInsets.fromLTRB(18, 16, 18, 24),
      children: widgets.isEmpty
          ? [_emptyAutonomyState('Waiting for run events')]
          : widgets,
    );
  }

  Widget _buildAutonomyStepBubble(Map<String, dynamic> step) {
    final stage = step['stage']?.toString() ?? '';
    final status = step['status']?.toString() ?? '';
    final title = step['title']?.toString() ?? stage;
    final detail = _detailPreview(step['detail']);
    final color = _autonomyStatusColor(status);
    return _buildChatBubble(
      icon: _autonomyStageIcon(stage),
      title: title,
      body: detail,
      meta: _shortStamp(step['created_at']),
      chips: [
        if (stage.isNotEmpty)
          _miniChip(stage, Colors.blueGrey.shade50, Colors.blueGrey.shade800),
        if (status.isNotEmpty)
          _miniChip(status, color.withValues(alpha: 0.12), color),
      ],
      color: color,
      background: Colors.grey.shade50,
    );
  }

  Widget _buildAutonomyPlanBubble(Map<String, dynamic> plan) {
    final analysis = plan['analysis']?.toString() ?? '';
    final notes = plan['notes']?.toString() ?? '';
    final files = _asMapList(plan['files'])
        .map((file) => file['path']?.toString() ?? '')
        .where((path) => path.isNotEmpty)
        .toList();
    return _buildChatBubble(
      icon: Icons.account_tree_outlined,
      title: 'Architect plan',
      body: [analysis, if (notes.isNotEmpty) notes].join('\n\n').trim(),
      chips: files
          .map((file) =>
              _miniChip(file, Colors.teal.shade50, Colors.teal.shade900))
          .toList(),
      color: Colors.teal.shade900,
      background: Colors.teal.shade50,
    );
  }

  Widget _buildAutonomyArtifactBubble(Map<String, dynamic> artifact) {
    final type = artifact['artifact_type']?.toString() ?? 'artifact';
    final name = artifact['name']?.toString() ?? type;
    final json = _asMap(artifact['content_json']);
    final ok = json['ok'];
    final body = artifact['content']?.toString().trim().isNotEmpty == true
        ? artifact['content'].toString()
        : _detailPreview(json);
    final color = ok == false ? Colors.orange.shade900 : Colors.blue.shade800;
    return _buildChatBubble(
      icon: _autonomyArtifactIcon(type),
      title: name,
      body: body,
      meta: _shortStamp(artifact['created_at']),
      chips: [
        _miniChip(type, Colors.blueGrey.shade50, Colors.blueGrey.shade800),
        if (ok != null)
          _miniChip(
              ok == true ? 'ok' : 'rejected',
              ok == true ? Colors.green.shade50 : Colors.orange.shade50,
              ok == true ? Colors.green.shade800 : Colors.orange.shade900),
      ],
      color: color,
      background: Colors.white,
    );
  }

  Widget _buildAutonomyValidationBubble(List<Map<String, dynamic>> validation) {
    final passed = validation.where((item) {
      final code = item['exit_code'];
      return code == 0 || code == '0' || item['passed'] == true;
    }).length;
    final failed = validation.length - passed;
    final body = validation
        .map((item) =>
            '${item['step_key'] ?? 'command'}: exit ${item['exit_code'] ?? '-'}')
        .join('\n');
    return _buildChatBubble(
      icon: failed == 0 ? Icons.verified_outlined : Icons.report_outlined,
      title: 'Validation',
      body: body,
      chips: [
        _miniChip(
            '$passed passed', Colors.green.shade50, Colors.green.shade800),
        if (failed > 0)
          _miniChip(
              '$failed failed', Colors.orange.shade50, Colors.orange.shade900),
      ],
      color: failed == 0 ? Colors.green.shade800 : Colors.orange.shade900,
      background: failed == 0 ? Colors.green.shade50 : Colors.orange.shade50,
    );
  }

  Widget _buildChatBubble({
    required IconData icon,
    required String title,
    required Color color,
    required Color background,
    String body = '',
    String meta = '',
    List<Widget> chips = const [],
    bool alignRight = false,
  }) {
    final content = ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: 720),
      child: Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: background,
          border: Border.all(color: color.withValues(alpha: 0.18)),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, color: color, size: 18),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(title,
                      style: const TextStyle(fontWeight: FontWeight.w700),
                      overflow: TextOverflow.ellipsis),
                ),
                if (meta.isNotEmpty)
                  Text(meta,
                      style:
                          TextStyle(color: Colors.grey.shade600, fontSize: 11)),
              ],
            ),
            if (body.trim().isNotEmpty) ...[
              const SizedBox(height: 8),
              SelectableText(
                body.length > 1400 ? '${body.substring(0, 1400)}...' : body,
                style: TextStyle(
                  color: Colors.grey.shade900,
                  fontSize: 13,
                  height: 1.35,
                ),
              ),
            ],
            if (chips.isNotEmpty) ...[
              const SizedBox(height: 8),
              Wrap(spacing: 6, runSpacing: 6, children: chips),
            ],
          ],
        ),
      ),
    );
    return Align(
      alignment: alignRight ? Alignment.centerRight : Alignment.centerLeft,
      child: content,
    );
  }

  Widget _buildAutonomyComposer() {
    return Container(
      padding: const EdgeInsets.fromLTRB(18, 12, 18, 14),
      decoration: BoxDecoration(
        color: Colors.grey.shade50,
        border: Border(top: BorderSide(color: Colors.grey.shade300)),
      ),
      child: Column(
        children: [
          Row(
            children: [
              Expanded(child: _buildAutonomyRepoPicker()),
              const SizedBox(width: 10),
              IconButton(
                tooltip: 'Refresh repos and runs',
                onPressed: _autonomyBusy
                    ? null
                    : () async {
                        await _loadCodeRepos();
                        await _loadAutonomyRuns();
                      },
                icon: const Icon(Icons.sync),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: TextField(
                  controller: _autopilotPromptCtrl,
                  minLines: 2,
                  maxLines: 5,
                  decoration: const InputDecoration(
                    labelText: 'Ask Project Autopilot to change this repo',
                    alignLabelWithHint: true,
                    border: OutlineInputBorder(),
                  ),
                ),
              ),
              const SizedBox(width: 10),
              SizedBox(
                height: 48,
                child: FilledButton.icon(
                  onPressed: _autonomyBusy || _codeRepos.isEmpty
                      ? null
                      : _startAutopilot,
                  icon: _autonomyBusy
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.send),
                  label: const Text('Run'),
                ),
              ),
            ],
          ),
          if (_codeRepos.isEmpty) ...[
            const SizedBox(height: 8),
            Text(
              'No registered local repos are visible to this desktop backend.',
              style: TextStyle(color: Colors.orange.shade900, fontSize: 13),
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildAutonomyRepoPicker() {
    return DropdownButtonFormField<int>(
      key: ValueKey<int?>(_autonomyRepoId),
      initialValue: _autonomyRepoId,
      decoration: const InputDecoration(
        labelText: 'Local repo',
        border: OutlineInputBorder(),
        isDense: true,
      ),
      items: [
        for (final repo in _codeRepos)
          if (_asInt(repo['id']) != null)
            DropdownMenuItem<int>(
              value: _asInt(repo['id']),
              child: Text(
                '${repo['name']?.toString() ?? 'repo ${repo['id']}'}'
                '${repo['reachable_in_current_runtime'] == true ? '' : ' (not reachable here)'}',
                overflow: TextOverflow.ellipsis,
              ),
            ),
      ],
      onChanged: _autonomyBusy
          ? null
          : (value) => setState(() => _autonomyRepoId = value),
    );
  }

  Widget _buildAutonomyTrackingSidebar() {
    final run = _activeAutonomyRun;
    if (run == null) {
      return Container(
        color: Colors.grey.shade50,
        child: _emptyAutonomyState('Tracking details appear here'),
      );
    }
    final plan = _asMap(run['plan']);
    final agents = _asMapList(run['agents']);
    final files = _asStringList(run['files']);
    final validation = _asMapList(run['validation']);
    final learning = _asMap(run['learning']);
    final branch = run['integration_branch']?.toString() ?? '';
    final worktree = run['worktree_path']?.toString() ?? '';
    final mergeMessage = run['merge_message']?.toString() ?? '';
    final errorMessage = run['error_message']?.toString() ?? '';
    return Container(
      color: Colors.grey.shade50,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text('Tracking', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 10),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _miniChip(
                  run['status']?.toString() ?? 'unknown',
                  _autonomyStatusColor(run['status']?.toString() ?? '')
                      .withValues(alpha: 0.12),
                  _autonomyStatusColor(run['status']?.toString() ?? '')),
              if (run['current_stage'] != null)
                _miniChip('${run['current_stage']}', Colors.indigo.shade50,
                    Colors.indigo.shade800),
              if (run['merge_status'] != null)
                _miniChip('merge: ${run['merge_status']}',
                    Colors.blueGrey.shade50, Colors.blueGrey.shade800),
            ],
          ),
          const SizedBox(height: 12),
          if (branch.isNotEmpty) _kvSelectable('Branch', branch),
          if (worktree.isNotEmpty) _kvSelectable('Worktree', worktree),
          if (mergeMessage.isNotEmpty) _kvSelectable('Merge', mergeMessage),
          if (errorMessage.isNotEmpty) _kvSelectable('Blocked', errorMessage),
          const Divider(height: 28),
          _buildAutonomyPlan(plan, files),
          const Divider(height: 28),
          _buildAutonomyAgents(agents),
          const Divider(height: 28),
          _buildAutonomyValidation(validation),
          const Divider(height: 28),
          _buildAutonomyLearning(learning),
        ],
      ),
    );
  }

  Widget _emptyAutonomyState(String label) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: Text(label, style: TextStyle(color: Colors.grey.shade700)),
      ),
    );
  }

  String _detailPreview(dynamic detail) {
    if (detail == null) return '';
    if (detail is Map && detail.isEmpty) return '';
    if (detail is List && detail.isEmpty) return '';
    if (detail is String) return detail;
    try {
      final text = const JsonEncoder.withIndent('  ').convert(detail);
      return text.length > 900 ? '${text.substring(0, 900)}...' : text;
    } catch (_) {
      return detail.toString();
    }
  }

  IconData _autonomyStatusIcon(String status) {
    switch (status) {
      case 'merged':
      case 'completed':
        return Icons.check_circle_outline;
      case 'blocked':
        return Icons.pause_circle_outline;
      case 'failed':
      case 'cancelled':
        return Icons.error_outline;
      case 'validating':
        return Icons.fact_check_outlined;
      case 'merging':
        return Icons.merge_type;
      case 'running':
        return Icons.autorenew;
      default:
        return Icons.radio_button_unchecked;
    }
  }

  IconData _autonomyStageIcon(String stage) {
    switch (stage) {
      case 'classify':
        return Icons.manage_search;
      case 'repo_scan':
        return Icons.folder_open;
      case 'plan':
        return Icons.account_tree_outlined;
      case 'assign_roles':
        return Icons.groups_outlined;
      case 'implement':
        return Icons.code;
      case 'integrate':
        return Icons.call_merge;
      case 'validate':
        return Icons.fact_check_outlined;
      case 'repair':
        return Icons.build_outlined;
      case 'merge':
        return Icons.merge_type;
      case 'learn':
        return Icons.school_outlined;
      default:
        return Icons.circle_outlined;
    }
  }

  IconData _autonomyArtifactIcon(String type) {
    switch (type) {
      case 'model_call':
        return Icons.memory;
      case 'diff':
        return Icons.difference_outlined;
      case 'diff_rejected':
        return Icons.warning_amber_outlined;
      case 'commit':
        return Icons.commit;
      default:
        return Icons.inventory_2_outlined;
    }
  }

  Widget _buildAutonomyActiveRun(Map<String, dynamic> run) {
    final runId = run['run_id']?.toString() ?? '';
    final status = run['status']?.toString() ?? 'unknown';
    final stage = run['current_stage']?.toString() ?? '';
    final merge = run['merge_status']?.toString() ?? 'pending';
    final statusColor = _autonomyStatusColor(status);
    final terminal = _autonomyTerminal(run);
    final plan = _asMap(run['plan']);
    final agents = _asMapList(run['agents']);
    final files = _asStringList(run['files']);
    final validation = _asMapList(run['validation']);
    final steps = _asMapList(run['steps']);
    final learning = _asMap(run['learning']);
    final branch = run['integration_branch']?.toString() ?? '';
    final worktree = run['worktree_path']?.toString() ?? '';
    final mergeMessage = run['merge_message']?.toString() ?? '';
    final errorMessage = run['error_message']?.toString() ?? '';
    final canMerge = terminal &&
        branch.isNotEmpty &&
        status != 'merged' &&
        merge != 'merged';

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.hub_outlined, color: statusColor),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    runId.isEmpty ? 'Active run' : runId,
                    style: Theme.of(context).textTheme.titleMedium,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
                _miniChip(
                    status, statusColor.withValues(alpha: 0.12), statusColor),
                const SizedBox(width: 6),
                _miniChip('merge: $merge', Colors.blueGrey.shade50,
                    Colors.blueGrey.shade800),
              ],
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 8,
              runSpacing: 6,
              children: [
                if (stage.isNotEmpty)
                  _miniChip(
                      stage, Colors.indigo.shade50, Colors.indigo.shade800),
                if (run['repo_id'] != null)
                  _miniChip('repo #${run['repo_id']}', Colors.grey.shade100,
                      Colors.grey.shade800),
                if (branch.isNotEmpty)
                  _miniChip(
                      branch, Colors.purple.shade50, Colors.purple.shade800),
              ],
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                OutlinedButton.icon(
                  onPressed:
                      _autonomyBusy || terminal ? null : _cancelAutopilot,
                  icon: const Icon(Icons.stop_circle_outlined, size: 18),
                  label: const Text('Cancel'),
                ),
                FilledButton.icon(
                  onPressed:
                      _autonomyBusy || !canMerge ? null : _mergeAutopilot,
                  icon: const Icon(Icons.merge_type, size: 18),
                  label: const Text('Merge'),
                ),
                OutlinedButton.icon(
                  onPressed: _autonomyBusy
                      ? null
                      : () => _refreshActiveAutonomyRun(silent: false),
                  icon: const Icon(Icons.refresh, size: 18),
                  label: const Text('Refresh'),
                ),
              ],
            ),
            if (worktree.isNotEmpty) ...[
              const SizedBox(height: 12),
              _kvSelectable('Worktree', worktree),
            ],
            if (mergeMessage.isNotEmpty) _kvSelectable('Merge', mergeMessage),
            if (errorMessage.isNotEmpty) _kvSelectable('Blocked', errorMessage),
            const Divider(height: 28),
            _buildAutonomyPlan(plan, files),
            const Divider(height: 28),
            _buildAutonomyAgents(agents),
            const Divider(height: 28),
            _buildAutonomyValidation(validation),
            const Divider(height: 28),
            _buildAutonomyLearning(learning),
            const Divider(height: 28),
            _buildAutonomySteps(steps),
          ],
        ),
      ),
    );
  }

  Widget _buildAutonomyPlan(Map<String, dynamic> plan, List<String> files) {
    final analysis = plan['analysis']?.toString() ?? '';
    final notes = plan['notes']?.toString() ?? '';
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Architect plan', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (analysis.isEmpty && notes.isEmpty && files.isEmpty)
          Text('Waiting for plan',
              style: TextStyle(color: Colors.grey.shade700))
        else ...[
          if (analysis.isNotEmpty)
            SelectableText(analysis, style: const TextStyle(fontSize: 13)),
          if (notes.isNotEmpty) ...[
            const SizedBox(height: 8),
            SelectableText(notes,
                style: TextStyle(fontSize: 13, color: Colors.grey.shade800)),
          ],
          if (files.isNotEmpty) ...[
            const SizedBox(height: 10),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: files
                  .map((file) => _miniChip(
                      file, Colors.teal.shade50, Colors.teal.shade900))
                  .toList(),
            ),
          ],
        ],
      ],
    );
  }

  Widget _buildAutonomyAgents(List<Map<String, dynamic>> agents) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Agent lanes', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (agents.isEmpty)
          Text('Waiting for lane assignment',
              style: TextStyle(color: Colors.grey.shade700))
        else
          ...agents.map((agent) {
            final name = agent['name']?.toString() ??
                agent['role']?.toString() ??
                'agent';
            final role = agent['role']?.toString() ?? '';
            final status = agent['status']?.toString() ?? '';
            final files = _asStringList(agent['files']);
            return Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Wrap(
                    spacing: 6,
                    runSpacing: 4,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      _miniChip(
                          name, Colors.indigo.shade50, Colors.indigo.shade800),
                      if (role.isNotEmpty && role != name)
                        _miniChip(role, Colors.blueGrey.shade50,
                            Colors.blueGrey.shade800),
                      if (status.isNotEmpty)
                        _miniChip(status, Colors.green.shade50,
                            Colors.green.shade800),
                    ],
                  ),
                  if (files.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    Text(files.join(', '),
                        style: TextStyle(
                            color: Colors.grey.shade700, fontSize: 12)),
                  ],
                ],
              ),
            );
          }),
      ],
    );
  }

  Widget _buildAutonomyValidation(List<Map<String, dynamic>> validation) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Validation', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (validation.isEmpty)
          Text('No validation results yet',
              style: TextStyle(color: Colors.grey.shade700))
        else
          ...validation.map((item) {
            final key = item['step_key']?.toString() ??
                item['command']?.toString() ??
                'command';
            final code = item['exit_code'];
            final ok = code == 0 || code == '0' || item['passed'] == true;
            final stderr = item['stderr']?.toString() ?? '';
            final stdout = item['stdout']?.toString() ?? '';
            final output = stderr.trim().isNotEmpty ? stderr : stdout;
            return Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Wrap(
                    spacing: 6,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      Icon(ok ? Icons.check_circle : Icons.warning_amber,
                          color: ok
                              ? Colors.green.shade700
                              : Colors.orange.shade800,
                          size: 18),
                      Text(key,
                          style: const TextStyle(fontWeight: FontWeight.w600)),
                      if (code != null)
                        _miniChip(
                            'exit $code',
                            ok ? Colors.green.shade50 : Colors.orange.shade50,
                            ok
                                ? Colors.green.shade800
                                : Colors.orange.shade900),
                    ],
                  ),
                  if (output.trim().isNotEmpty) ...[
                    const SizedBox(height: 4),
                    SelectableText(
                      output.length > 600
                          ? '${output.substring(0, 600)}...'
                          : output,
                      style: const TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 12,
                        color: Colors.black54,
                      ),
                    ),
                  ],
                ],
              ),
            );
          }),
      ],
    );
  }

  Widget _buildAutonomyLearning(Map<String, dynamic> learning) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Learning signals', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (learning.isEmpty)
          Text('No learning sample recorded yet',
              style: TextStyle(color: Colors.grey.shade700))
        else
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: learning.entries
                .map((entry) => _miniChip('${entry.key}: ${entry.value}',
                    Colors.cyan.shade50, Colors.cyan.shade900))
                .toList(),
          ),
      ],
    );
  }

  Widget _buildAutonomySteps(List<Map<String, dynamic>> steps) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Run steps', style: Theme.of(context).textTheme.titleSmall),
        const SizedBox(height: 8),
        if (steps.isEmpty)
          Text('Waiting for events',
              style: TextStyle(color: Colors.grey.shade700))
        else
          ...steps.map((step) {
            final stage = step['stage']?.toString() ?? '';
            final title = step['title']?.toString() ?? stage;
            final status = step['status']?.toString() ?? '';
            final agent = step['agent_name']?.toString() ?? '';
            final time = _shortStamp(step['created_at']);
            return ListTile(
              dense: true,
              contentPadding: EdgeInsets.zero,
              leading:
                  Icon(Icons.circle, size: 10, color: Colors.blueGrey.shade500),
              title: Text(title, overflow: TextOverflow.ellipsis),
              subtitle: Wrap(
                spacing: 6,
                runSpacing: 4,
                children: [
                  if (stage.isNotEmpty)
                    _miniChip(stage, Colors.blueGrey.shade50,
                        Colors.blueGrey.shade800),
                  if (status.isNotEmpty)
                    _miniChip(
                        status, Colors.green.shade50, Colors.green.shade800),
                  if (agent.isNotEmpty)
                    _miniChip(
                        agent, Colors.indigo.shade50, Colors.indigo.shade800),
                ],
              ),
              trailing: time.isEmpty
                  ? null
                  : Text(time,
                      style:
                          TextStyle(color: Colors.grey.shade600, fontSize: 11)),
            );
          }),
      ],
    );
  }

  Widget _buildQueueTab() {
    if (_projects.isEmpty) {
      return ListView(
        padding: const EdgeInsets.all(24),
        children: [
          Text(
            'No planner projects available. Create one in the web planner, then refresh.',
            style: TextStyle(color: Colors.grey.shade700),
          ),
          const SizedBox(height: 12),
          FilledButton(
              onPressed: _loadProjectsPicker,
              child: const Text('Reload projects')),
        ],
      );
    }
    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        if (_queueError != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Text(_queueError!,
                style: TextStyle(color: Colors.red.shade800)),
          ),
        TextField(
          controller: _titleCtrl,
          decoration: const InputDecoration(
            labelText: 'Title',
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 12),
        TextField(
          controller: _descCtrl,
          minLines: 4,
          maxLines: 8,
          decoration: const InputDecoration(
            labelText: 'Description',
            alignLabelWithHint: true,
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 12),
        // Phase E.2 — optional source. The brain auto-detects what the
        // input is and registers/clones as needed. Examples in helperText.
        TextField(
          controller: _sourceCtrl,
          decoration: const InputDecoration(
            labelText: 'Source (optional)',
            helperText: 'C:\\dev\\some-project · /workspace · '
                'https://github.com/USER/REPO · USER/REPO · repo-name',
            helperMaxLines: 2,
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 16),
        DropdownButtonFormField<int>(
          // ignore: deprecated_member_use
          value: _projectId,
          decoration: const InputDecoration(
            labelText: 'Project',
            border: OutlineInputBorder(),
          ),
          items: _projects
              .map(
                (p) => DropdownMenuItem<int>(
                  value: p['id'] as int,
                  child: Text('${p['name'] ?? p['id']}'),
                ),
              )
              .toList(),
          onChanged: (v) => setState(() => _projectId = v),
        ),
        const SizedBox(height: 20),
        FilledButton.icon(
          onPressed: _queueBusy ? null : _submitQueue,
          icon: _queueBusy
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : const Icon(Icons.send),
          label: const Text('Queue task'),
        ),
      ],
    );
  }

  Widget _buildContextTab() {
    if (_ctxLoading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_ctxError != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('Failed to load Context Brain status',
                style: TextStyle(color: Colors.red.shade800)),
            const SizedBox(height: 8),
            Text(_ctxError!, style: TextStyle(color: Colors.grey.shade700)),
            const SizedBox(height: 16),
            FilledButton(
                onPressed: _loadContextBrain, child: const Text('Retry')),
          ],
        ),
      );
    }
    final state = (_ctxStatus?['runtime_state'] as Map?) ?? {};
    final intentDist = (_ctxStatus?['intent_distribution_24h'] as Map?) ?? {};
    final lastAssembly = _ctxStatus?['last_assembly'] as Map?;

    return RefreshIndicator(
      onRefresh: _loadContextBrain,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Icon(Icons.psychology_outlined,
                          color: Theme.of(context).colorScheme.primary),
                      const SizedBox(width: 8),
                      Text('Runtime',
                          style: Theme.of(context).textTheme.titleMedium),
                      const Spacer(),
                      Chip(
                        label: Text(state['mode']?.toString() ?? 'unknown'),
                        backgroundColor: Colors.green.shade50,
                      ),
                    ],
                  ),
                  const Divider(),
                  _kv('Token budget / request',
                      '${state['token_budget_per_request'] ?? '-'}'),
                  _kv('Distillation threshold',
                      '${state['distillation_threshold_tokens'] ?? '-'} tokens'),
                  _kv(
                    'Distillation spend today',
                    '\$${state['spent_today_distillation_usd'] ?? '0'} / \$${state['daily_distillation_usd_cap'] ?? '0'}',
                  ),
                  _kv('Strategy version',
                      '${state['learned_strategy_version'] ?? '-'}'),
                  _kv('Learning enabled',
                      '${state['learning_enabled'] ?? '-'}'),
                  _kv('Last learning cycle',
                      '${state['last_learning_cycle_at'] ?? 'never'}'),
                ],
              ),
            ),
          ),
          if (intentDist.isNotEmpty) ...[
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Intents (last 24h)',
                        style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    ...intentDist.entries
                        .map((e) => _kv('${e.key}', '${e.value}')),
                  ],
                ),
              ),
            ),
          ],
          if (lastAssembly != null) ...[
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Last assembly',
                        style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    _kv('Intent',
                        '${lastAssembly['intent']} (${lastAssembly['intent_confidence']})'),
                    _kv('Tokens used',
                        '${lastAssembly['total_tokens_input']} / ${lastAssembly['budget_token_cap']} (${lastAssembly['budget_used_pct']}%)'),
                    _kv('Elapsed', '${lastAssembly['elapsed_ms']} ms'),
                    _kv('Distilled', '${lastAssembly['distilled']}'),
                    _kv('Sources used', '${lastAssembly['sources_used']}'),
                  ],
                ),
              ),
            ),
          ],
          if (_ctxSources.isNotEmpty) ...[
            const SizedBox(height: 12),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text('Source contribution (24h)',
                        style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    ..._ctxSources.map((s) {
                      final id = s['source_id']?.toString() ?? '?';
                      final sel = s['total_selected'] ?? 0;
                      final tot = s['total_returned'] ?? 0;
                      final rate =
                          ((s['selection_rate'] ?? 0.0) as num).toDouble();
                      return _kv(id,
                          '$sel/$tot (${(rate * 100).toStringAsFixed(0)}% selected)');
                    }),
                  ],
                ),
              ),
            ),
          ],
          if (_ctxAssemblies.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text('Recent assemblies',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            ..._ctxAssemblies.take(15).map((a) => Card(
                  child: ListTile(
                    dense: true,
                    title: Text('${a['intent']} (${a['intent_confidence']})'),
                    subtitle: Text(
                      '${a['total_tokens_input']}/${a['budget_token_cap']} tok · '
                      '${a['elapsed_ms']}ms · '
                      '${(a['sources_used'] is Map) ? (a['sources_used'] as Map).keys.join(',') : '-'}',
                    ),
                    trailing: Text('#${a['id']}',
                        style: TextStyle(color: Colors.grey.shade600)),
                  ),
                )),
          ],
        ],
      ),
    );
  }

  Widget _kv(String k, String v) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        children: [
          SizedBox(
            width: 200,
            child: Text(k, style: TextStyle(color: Colors.grey.shade700)),
          ),
          Expanded(
            child: Text(v, style: const TextStyle(fontWeight: FontWeight.w500)),
          ),
        ],
      ),
    );
  }

  Widget _buildHistoryTab() {
    if (_runsLoading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_runsError != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_runsError!),
            const SizedBox(height: 12),
            FilledButton(onPressed: _loadRuns, child: const Text('Retry')),
          ],
        ),
      );
    }

    // Build a filtered + grouped view.
    final filtered = _runs.where((r) {
      final step = r['cycle_step']?.toString() ?? '';
      final notify = r['notify_user'] == true;
      if (_hideRouterEscalate && step == 'router_escalate' && !notify) {
        return false;
      }
      if (_onlyOperatorReview && !notify) {
        return false;
      }
      return true;
    }).toList();

    // Count how many runs we hid so the filter toggle is contextual.
    final hiddenCount = _runs.length - filtered.length;
    final notifyCount = _runs.where((r) => r['notify_user'] == true).length;

    if (filtered.isEmpty && _runs.isEmpty) {
      return const Center(child: Text('No runs yet'));
    }

    return Column(
      children: [
        // Filter / summary bar
        Container(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
          color: Theme.of(context)
              .colorScheme
              .surfaceContainerHighest
              .withValues(alpha: 0.4),
          child: Row(
            children: [
              if (_watchTaskId != null) ...[
                FilterChip(
                  label: Text('Task #$_watchTaskId'),
                  selected: true,
                  onSelected: (_) {
                    setState(() => _watchTaskId = null);
                    unawaited(_loadRuns());
                  },
                  showCheckmark: false,
                  avatar: const Icon(Icons.close, size: 16),
                ),
                const SizedBox(width: 8),
              ],
              FilterChip(
                label: Text(
                  _hideRouterEscalate
                      ? 'Hide auto-escalates ($hiddenCount)'
                      : 'Hide auto-escalates',
                ),
                selected: _hideRouterEscalate,
                onSelected: (v) => setState(() => _hideRouterEscalate = v),
              ),
              const SizedBox(width: 8),
              FilterChip(
                label: Text('Needs review ($notifyCount)'),
                selected: _onlyOperatorReview,
                onSelected: (v) => setState(() => _onlyOperatorReview = v),
                avatar: notifyCount > 0
                    ? const Icon(Icons.flag, size: 16, color: Colors.red)
                    : null,
              ),
              const Spacer(),
              IconButton(
                icon: const Icon(Icons.refresh, size: 20),
                onPressed: _loadRuns,
                tooltip: 'Refresh',
              ),
            ],
          ),
        ),
        if (filtered.isEmpty)
          Expanded(
            child: Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.check_circle_outline,
                      size: 48, color: Colors.green.shade300),
                  const SizedBox(height: 8),
                  Text(
                    'No runs match the current filter',
                    style: TextStyle(color: Colors.grey.shade700),
                  ),
                  if (hiddenCount > 0)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: TextButton(
                        onPressed: () =>
                            setState(() => _hideRouterEscalate = false),
                        child: Text('Show $hiddenCount auto-escalations'),
                      ),
                    ),
                ],
              ),
            ),
          )
        else
          Expanded(
            child: ListView.separated(
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 16),
              itemCount: filtered.length,
              separatorBuilder: (_, __) => const SizedBox(height: 4),
              itemBuilder: (context, i) => _buildRunCard(filtered[i]),
            ),
          ),
      ],
    );
  }

  Widget _buildRunCard(Map<String, dynamic> r) {
    final id = r['id'];
    final tid = r['task_id'];
    final started = r['started_at']?.toString() ?? '';
    final step = r['cycle_step']?.toString() ?? '';
    final decision = r['decision']?.toString() ?? '';
    final esc = r['escalation_reason']?.toString();
    final vid = r['validation_run_id'];
    final snap = r['llm_snapshot'];
    final notify = r['notify_user'] == true;
    final branchName = r['branch_name']?.toString();
    final commitSha = r['commit_sha']?.toString();
    final diffSummary =
        r['diff_summary'] is Map ? r['diff_summary'] as Map : null;
    final pushed = diffSummary?['pushed'] == true;
    final pushUrl = diffSummary?['push_url']?.toString();
    final filesCount = (diffSummary?['files'] is List)
        ? (diffSummary!['files'] as List).length
        : 0;
    final loc = diffSummary?['loc'];
    final highlight = _watchTaskId != null && tid == _watchTaskId;

    // Choose a color + icon based on decision so the eye scans quickly
    final (Color, IconData, String) decisionVisual = switch (decision) {
      'passed' => (Colors.green.shade600, Icons.check_circle, 'passed'),
      'merged' => (Colors.green.shade700, Icons.merge_type, 'merged'),
      'applied' => (Colors.blue.shade700, Icons.task_alt, 'applied'),
      'validation_failed' => (
          Colors.orange.shade700,
          Icons.warning_amber,
          'validation failed'
        ),
      'failed' => (Colors.red.shade700, Icons.cancel, 'failed'),
      'escalated' => (Colors.purple.shade600, Icons.outlined_flag, 'escalated'),
      _ => (Colors.grey.shade700, Icons.circle, decision),
    };
    final (color, decIcon, decLabel) = decisionVisual;

    final shortTime =
        started.length >= 19 ? started.substring(11, 19) : started;
    final shortDate = started.length >= 10 ? started.substring(0, 10) : '';

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 4, vertical: 2),
      color: highlight
          ? Colors.amber.shade50
          : (notify ? Colors.red.shade50 : null),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(8),
        side: BorderSide(
          color: notify ? Colors.red.shade200 : Colors.grey.shade200,
          width: 1,
        ),
      ),
      child: ExpansionTile(
        leading: Icon(decIcon, color: color),
        title: Row(
          children: [
            Text(
              'Task #$tid',
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
            const SizedBox(width: 8),
            Text(
              decLabel,
              style: TextStyle(
                  color: color, fontWeight: FontWeight.w500, fontSize: 13),
            ),
            if (notify) ...[
              const SizedBox(width: 8),
              const Icon(Icons.flag, size: 14, color: Colors.red),
            ],
            const Spacer(),
            Text(
              '$shortDate $shortTime',
              style: TextStyle(color: Colors.grey.shade600, fontSize: 11),
            ),
          ],
        ),
        subtitle: Padding(
          padding: const EdgeInsets.only(top: 4),
          child: Wrap(
            spacing: 6,
            runSpacing: 4,
            children: [
              if (step.isNotEmpty)
                _miniChip(
                    step, Colors.blueGrey.shade50, Colors.blueGrey.shade800),
              if (filesCount > 0)
                _miniChip('$filesCount files', Colors.indigo.shade50,
                    Colors.indigo.shade800),
              if (loc != null)
                _miniChip(
                    '$loc loc', Colors.indigo.shade50, Colors.indigo.shade800),
              if (pushed)
                _miniChip(
                    'pushed', Colors.green.shade50, Colors.green.shade800),
              if (branchName != null && branchName.isNotEmpty)
                _miniChip(
                    branchName, Colors.purple.shade50, Colors.purple.shade800),
            ],
          ),
        ),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _hr('Run #$id'),
                if (esc != null && esc.isNotEmpty)
                  _kvSelectable('Escalation', esc),
                if (vid != null) _kv('Validation run id', '$vid'),
                if (commitSha != null && commitSha.isNotEmpty) ...[
                  _kvSelectable('Commit SHA', commitSha),
                  if (branchName != null && branchName.isNotEmpty)
                    _kvLink(
                      'GitHub branch',
                      'https://github.com/MiacoRindolf/chili-home-copilot/tree/$branchName',
                      branchName,
                    ),
                ],
                if (pushUrl != null && pushUrl.isNotEmpty)
                  _kvSelectable('Push URL', pushUrl),
                if (snap != null) ...[
                  const SizedBox(height: 8),
                  Text('llm_snapshot',
                      style: Theme.of(context).textTheme.labelLarge),
                  const SizedBox(height: 4),
                  SelectableText(
                    _snapshotPreview(snap),
                    style: const TextStyle(fontSize: 12, color: Colors.black54),
                  ),
                ],
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _miniChip(String label, Color bg, Color fg) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Text(
        label,
        style: TextStyle(color: fg, fontSize: 11, fontWeight: FontWeight.w500),
      ),
    );
  }

  Widget _hr(String label) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        label,
        style: TextStyle(
          color: Colors.grey.shade600,
          fontSize: 11,
          fontWeight: FontWeight.w500,
        ),
      ),
    );
  }

  Widget _kvSelectable(String k, String v) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 130,
            child: Text(k,
                style: TextStyle(color: Colors.grey.shade700, fontSize: 12)),
          ),
          Expanded(
            child: SelectableText(v,
                style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
          ),
        ],
      ),
    );
  }

  Widget _kvLink(String k, String url, String displayLabel) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 130,
            child: Text(k,
                style: TextStyle(color: Colors.grey.shade700, fontSize: 12)),
          ),
          Expanded(
            child: SelectableText.rich(
              TextSpan(
                text: displayLabel,
                style: const TextStyle(
                  color: Colors.blue,
                  decoration: TextDecoration.underline,
                  fontFamily: 'monospace',
                  fontSize: 12,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
