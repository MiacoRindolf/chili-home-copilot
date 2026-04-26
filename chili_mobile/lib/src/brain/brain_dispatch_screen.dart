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

  // Phase F — Context Brain tab state
  Map<String, dynamic>? _ctxStatus;
  List<Map<String, dynamic>> _ctxAssemblies = [];
  List<Map<String, dynamic>> _ctxSources = [];
  bool _ctxLoading = false;
  String? _ctxError;

  // History tab filter state
  bool _hideRouterEscalate = true;  // hide noisy auto-escalations by default
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
    _tabs = TabController(length: 4, vsync: this);
    _tabs.addListener(_onTabChanged);
    unawaited(_boot());
  }

  void _onTabChanged() {
    if (_tabs.indexIsChanging) return;
    if (_tabs.index == 2) {
      unawaited(_loadRuns());
    } else if (_tabs.index == 3) {
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
    await _refreshStatus();
    _statusTimer?.cancel();
    _statusTimer = Timer.periodic(const Duration(seconds: 30), (_) {
      if (mounted && _paired) unawaited(_refreshStatus());
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
      final msg = data['message']?.toString() ?? 'Check your email for the code.';
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
        sourceInput: _sourceCtrl.text.trim().isEmpty ? null : _sourceCtrl.text.trim(),
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
        title: Text(targetActive ? 'Enable kill switch?' : 'Disable kill switch?'),
        content: Text(
          targetActive
              ? 'Code dispatch will stop until you turn this off.'
              : 'Resume normal dispatch?',
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Confirm')),
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
    _tabs.removeListener(_onTabChanged);
    _tabs.dispose();
    _titleCtrl.dispose();
    _descCtrl.dispose();
    _sourceCtrl.dispose();
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
                              Icon(Icons.error_outline, color: Colors.red.shade800, size: 20),
                              const SizedBox(width: 8),
                              Expanded(
                                child: Text(
                                  _pairError!,
                                  style: TextStyle(color: Colors.red.shade900, fontSize: 13),
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
                      style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
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
                        style: TextStyle(color: Colors.grey.shade700, fontSize: 13),
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
          child: Text('Brain', style: Theme.of(context).textTheme.headlineMedium),
        ),
        TabBar(
          controller: _tabs,
          tabs: const [
            Tab(text: 'Status'),
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
              child: Text(_statusError!, style: TextStyle(color: Colors.red.shade900)),
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
                Text('Kill switch', style: Theme.of(context).textTheme.titleMedium),
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
        Text('Counters (5 min)', style: Theme.of(context).textTheme.titleMedium),
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
            child: Text('No runs in the last 5 minutes', style: TextStyle(color: Colors.grey.shade600)),
          ),
        const SizedBox(height: 20),
        Text('Spend today', style: Theme.of(context).textTheme.titleMedium),
        const SizedBox(height: 8),
        if (spendToday.isEmpty)
          Text('No LLM spend recorded today', style: TextStyle(color: Colors.grey.shade600))
        else
          ...spendToday.map((row) {
            final m = Map<String, dynamic>.from(row as Map<dynamic, dynamic>);
            final prov = m['provider'] ?? '';
            final calls = m['calls'] ?? 0;
            final usd = (m['spend_usd'] is num) ? (m['spend_usd'] as num).toDouble() : 0.0;
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
                final m = Map<String, dynamic>.from(row as Map<dynamic, dynamic>);
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
          FilledButton(onPressed: _loadProjectsPicker, child: const Text('Reload projects')),
        ],
      );
    }
    return ListView(
      padding: const EdgeInsets.all(24),
      children: [
        if (_queueError != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Text(_queueError!, style: TextStyle(color: Colors.red.shade800)),
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
            helperText:
                'C:\\dev\\some-project · /workspace · '
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
            Text('Failed to load Context Brain status', style: TextStyle(color: Colors.red.shade800)),
            const SizedBox(height: 8),
            Text(_ctxError!, style: TextStyle(color: Colors.grey.shade700)),
            const SizedBox(height: 16),
            FilledButton(onPressed: _loadContextBrain, child: const Text('Retry')),
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
                      Icon(Icons.psychology_outlined, color: Theme.of(context).colorScheme.primary),
                      const SizedBox(width: 8),
                      Text('Runtime', style: Theme.of(context).textTheme.titleMedium),
                      const Spacer(),
                      Chip(
                        label: Text(state['mode']?.toString() ?? 'unknown'),
                        backgroundColor: Colors.green.shade50,
                      ),
                    ],
                  ),
                  const Divider(),
                  _kv('Token budget / request', '${state['token_budget_per_request'] ?? '-'}'),
                  _kv('Distillation threshold', '${state['distillation_threshold_tokens'] ?? '-'} tokens'),
                  _kv(
                    'Distillation spend today',
                    '\$${state['spent_today_distillation_usd'] ?? '0'} / \$${state['daily_distillation_usd_cap'] ?? '0'}',
                  ),
                  _kv('Strategy version', '${state['learned_strategy_version'] ?? '-'}'),
                  _kv('Learning enabled', '${state['learning_enabled'] ?? '-'}'),
                  _kv('Last learning cycle', '${state['last_learning_cycle_at'] ?? 'never'}'),
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
                    Text('Intents (last 24h)', style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    ...intentDist.entries.map((e) => _kv('${e.key}', '${e.value}')),
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
                    Text('Last assembly', style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    _kv('Intent', '${lastAssembly['intent']} (${lastAssembly['intent_confidence']})'),
                    _kv('Tokens used', '${lastAssembly['total_tokens_input']} / ${lastAssembly['budget_token_cap']} (${lastAssembly['budget_used_pct']}%)'),
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
                    Text('Source contribution (24h)', style: Theme.of(context).textTheme.titleMedium),
                    const Divider(),
                    ..._ctxSources.map((s) {
                      final id = s['source_id']?.toString() ?? '?';
                      final sel = s['total_selected'] ?? 0;
                      final tot = s['total_returned'] ?? 0;
                      final rate = ((s['selection_rate'] ?? 0.0) as num).toDouble();
                      return _kv('$id', '$sel/$tot (${(rate * 100).toStringAsFixed(0)}% selected)');
                    }),
                  ],
                ),
              ),
            ),
          ],
          if (_ctxAssemblies.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text('Recent assemblies', style: Theme.of(context).textTheme.titleMedium),
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
                    trailing: Text('#${a['id']}', style: TextStyle(color: Colors.grey.shade600)),
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
      if (_hideRouterEscalate && step == 'router_escalate' && !notify) return false;
      if (_onlyOperatorReview && !notify) return false;
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
          color: Theme.of(context).colorScheme.surfaceContainerHighest.withOpacity(0.4),
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
                  Icon(Icons.check_circle_outline, size: 48, color: Colors.green.shade300),
                  const SizedBox(height: 8),
                  Text(
                    'No runs match the current filter',
                    style: TextStyle(color: Colors.grey.shade700),
                  ),
                  if (hiddenCount > 0)
                    Padding(
                      padding: const EdgeInsets.only(top: 8),
                      child: TextButton(
                        onPressed: () => setState(() => _hideRouterEscalate = false),
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
    final diffSummary = r['diff_summary'] is Map ? r['diff_summary'] as Map : null;
    final pushed = diffSummary?['pushed'] == true;
    final pushUrl = diffSummary?['push_url']?.toString();
    final filesCount = (diffSummary?['files'] is List)
        ? (diffSummary!['files'] as List).length
        : 0;
    final loc = diffSummary?['loc'];
    final highlight = _watchTaskId != null && tid == _watchTaskId;

    // Choose a color + icon based on decision so the eye scans quickly
    final (Color, IconData, String) _decisionVisual = switch (decision) {
      'passed' => (Colors.green.shade600, Icons.check_circle, 'passed'),
      'merged' => (Colors.green.shade700, Icons.merge_type, 'merged'),
      'applied' => (Colors.blue.shade700, Icons.task_alt, 'applied'),
      'validation_failed' => (Colors.orange.shade700, Icons.warning_amber, 'validation failed'),
      'failed' => (Colors.red.shade700, Icons.cancel, 'failed'),
      'escalated' => (Colors.purple.shade600, Icons.outlined_flag, 'escalated'),
      _ => (Colors.grey.shade700, Icons.circle, decision),
    };
    final (color, decIcon, decLabel) = _decisionVisual;

    final shortTime = started.length >= 19 ? started.substring(11, 19) : started;
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
              style: TextStyle(color: color, fontWeight: FontWeight.w500, fontSize: 13),
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
                _miniChip(step, Colors.blueGrey.shade50, Colors.blueGrey.shade800),
              if (filesCount > 0)
                _miniChip('$filesCount files', Colors.indigo.shade50, Colors.indigo.shade800),
              if (loc != null)
                _miniChip('$loc loc', Colors.indigo.shade50, Colors.indigo.shade800),
              if (pushed)
                _miniChip('pushed', Colors.green.shade50, Colors.green.shade800),
              if (branchName != null && branchName.isNotEmpty)
                _miniChip(branchName, Colors.purple.shade50, Colors.purple.shade800),
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
            child: Text(k, style: TextStyle(color: Colors.grey.shade700, fontSize: 12)),
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
            child: Text(k, style: TextStyle(color: Colors.grey.shade700, fontSize: 12)),
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
