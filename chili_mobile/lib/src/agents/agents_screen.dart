import 'dart:async';

import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';
import '../ui/app_ui.dart';
import 'agent.dart';
import 'agent_activity_service.dart';
import 'agent_control_service.dart';
import 'agent_event.dart';
import 'agent_filter.dart';
import 'agent_persistence.dart';
import 'agent_registry.dart';
import 'agent_status_service.dart';
import 'run_health.dart';

/// Signature of the live-status poller — injectable so tests can feed canned
/// readings without touching the network.
typedef AgentStatusPoller = Future<Map<String, AgentLiveStatus>> Function();

/// Signature of a control invoker — injectable so tests can record actions
/// without touching the network.
typedef AgentControlInvoker = Future<void> Function(String id, AgentAction action);

/// Signature of a backend recent-runs fetcher — injectable for tests (AGT-8).
typedef AgentRunsFetcher = Future<List<AgentRun>> Function(String id);

/// Mission control for CHILI's autonomous agents — check, configure and run the
/// trading / brain / coding / system fleet. AGT-1: master/detail with local
/// run/stop, enable toggles and editable config. AGT-2: live backend status for
/// the agents that expose it (read-only) with a connection indicator.
class AgentsScreen extends StatefulWidget {
  const AgentsScreen({
    super.key,
    AgentRegistry? registry,
    AgentStatusPoller? livePoller,
    AgentControlInvoker? controlInvoker,
    AgentRunsFetcher? runsFetcher,
    bool livePolling = true,
    this.onDiscussRun,
  })  : _injectedRegistry = registry,
        _injectedPoller = livePoller,
        _injectedControl = controlInvoker,
        _injectedRunsFetcher = runsFetcher,
        _livePolling = livePolling;

  /// AG-2 — tap a backend run to discuss it in Chat (reuses the UK-2 ask inbox).
  /// (agentLabel, run) so the prompt can name which agent the run belongs to.
  final void Function(String agentLabel, AgentRun run)? onDiscussRun;

  /// Optional registry for tests; production builds seed a fresh one.
  final AgentRegistry? _injectedRegistry;

  /// Optional status poller for tests; production builds a real one.
  final AgentStatusPoller? _injectedPoller;

  /// Optional control invoker for tests; production builds a real one.
  final AgentControlInvoker? _injectedControl;

  /// Optional backend recent-runs fetcher for tests; production builds a real one.
  final AgentRunsFetcher? _injectedRunsFetcher;

  /// When false, no live polling timer runs (used by tests / when offline use
  /// is undesirable).
  final bool _livePolling;

  @override
  State<AgentsScreen> createState() => _AgentsScreenState();
}

class _AgentsScreenState extends State<AgentsScreen> {
  late final AgentRegistry _registry;
  late final AgentStatusPoller _poller;
  late final AgentControlInvoker _invoker;
  late final AgentRunsFetcher _runsFetcher;
  AgentStatusService? _ownedService;
  // Cached backend-runs futures per agent (AGT-8); cleared on manual refresh.
  final Map<String, Future<List<AgentRun>>> _runsFutures =
      <String, Future<List<AgentRun>>>{};
  AgentKind? _filter; // null = All
  String _query = ''; // search box
  AgentSort _sort = AgentSort.defaultOrder;
  final TextEditingController _search = TextEditingController();
  String? _selectedId;
  Timer? _saveTimer;
  Timer? _pollTimer;
  bool _reachable = false; // backend responded on the last poll
  bool _polledOnce = false;

  static const Duration _pollInterval = Duration(seconds: 25);

  @override
  void initState() {
    super.initState();
    _registry = widget._injectedRegistry ?? AgentRegistry();
    _selectedId = _registry.agents.isNotEmpty ? _registry.agents.first.id : null;

    // A single shared client backs the status poll, control actions and the
    // backend-runs feed.
    final bool needClient = widget._injectedPoller == null ||
        widget._injectedControl == null ||
        widget._injectedRunsFetcher == null;
    final ChiliApiClient? api = needClient ? ChiliApiClient() : null;
    if (widget._injectedPoller != null) {
      _poller = widget._injectedPoller!;
    } else {
      _ownedService = AgentStatusService(api!);
      _poller = _ownedService!.poll;
    }
    _invoker = widget._injectedControl ?? AgentControlService(api!).invoke;
    _runsFetcher =
        widget._injectedRunsFetcher ?? AgentActivityService(api!).fetch;

    // When the registry is injected (e.g. shared by the workspace in AGT-7),
    // the owner handles persistence + polling; we're just a view.
    if (widget._injectedRegistry == null) _restore();
    if (widget._livePolling) {
      _poll();
      _pollTimer = Timer.periodic(_pollInterval, (_) => _poll());
    }
  }

  Future<void> _restore() async {
    final List<Map<String, dynamic>> saved = await AgentPersistence.load();
    if (!mounted) return;
    _registry.applySaved(saved); // overlay before we start listening to saves
    _registry.addListener(_scheduleSave);
  }

  /// Cached per-agent backend-runs future (AGT-8) — fetched once until a manual
  /// refresh clears the cache.
  Future<List<AgentRun>> _runsFuture(String id) =>
      _runsFutures.putIfAbsent(id, () => _runsFetcher(id));

  /// Header refresh: re-pull live status AND re-fetch backend runs.
  void _manualRefresh() {
    setState(_runsFutures.clear);
    _poll();
  }

  Future<void> _poll() async {
    Map<String, AgentLiveStatus> live;
    try {
      live = await _poller();
    } catch (_) {
      live = const <String, AgentLiveStatus>{};
    }
    if (!mounted) return;
    live.forEach((String id, AgentLiveStatus s) {
      _registry.applyLiveStatus(id, s.status,
          lastRun: s.lastRun, lastResult: s.detail);
    });
    setState(() {
      _reachable = _ownedService?.reachable ?? live.isNotEmpty;
      _polledOnce = true;
    });
  }

  // ── Real backend control (AGT-3) ─────────────────────────────────────────
  Future<void> _control(Agent a, AgentAction action) async {
    if (tradingConfirmAgentIds.contains(a.id)) {
      final bool ok = await _confirmTrading(a, action);
      if (ok != true || !mounted) return;
    }
    try {
      await _invoker(a.id, action);
      if (!mounted) return;
      _snack('${a.name}: ${_actionVerb(action)} sent to backend');
      _poll(); // pull the real status back
    } catch (e) {
      if (!mounted) return;
      _snack('${a.name}: ${_actionVerb(action)} failed — ${_short(e)}',
          isError: true);
    }
  }

  Future<bool> _confirmTrading(Agent a, AgentAction action) async {
    final bool resuming = action == AgentAction.start;
    final String title = resuming ? 'Resume ${a.name}?' : 'Pause ${a.name}?';
    final String body = resuming
        ? 'This re-enables live trading — the auto-trader can place REAL orders. '
            'Continue?'
        : 'This halts new auto-trader entries. Open positions are unaffected.';
    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) {
        final ColorScheme cs = Theme.of(ctx).colorScheme;
        return AlertDialog(
          icon: Icon(resuming ? Icons.warning_amber_rounded : Icons.pause_circle,
              color: resuming ? cs.error : cs.primary),
          title: Text(title),
          content: Text(body),
          actions: <Widget>[
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              style: resuming
                  ? FilledButton.styleFrom(backgroundColor: cs.error)
                  : null,
              onPressed: () => Navigator.of(ctx).pop(true),
              child: Text(resuming ? 'Resume live trading' : 'Pause'),
            ),
          ],
        );
      },
    );
    return ok ?? false;
  }

  void _snack(String msg, {bool isError = false}) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(msg),
      backgroundColor: isError ? cs.errorContainer : null,
      behavior: SnackBarBehavior.floating,
      duration: const Duration(seconds: 3),
    ));
  }

  static String _actionVerb(AgentAction a) {
    switch (a) {
      case AgentAction.start:
        return 'Start';
      case AgentAction.stop:
        return 'Stop';
      case AgentAction.runOnce:
        return 'Run';
    }
  }

  static String _short(Object e) {
    final String s = e.toString().replaceFirst('Exception: ', '');
    return s.length > 80 ? '${s.substring(0, 80)}…' : s;
  }

  // ── Custom agents (AGT-4) ────────────────────────────────────────────────
  Future<void> _showAgentEditor({Agent? initial}) async {
    final Set<String> existing = _registry.agents.map((Agent a) => a.id).toSet();
    final Agent? result = await showDialog<Agent>(
      context: context,
      builder: (BuildContext _) =>
          _AgentEditorDialog(initial: initial, existingIds: existing),
    );
    if (result == null || !mounted) return;
    _registry.upsertCustom(result);
    setState(() => _selectedId = result.id);
  }

  Future<void> _deleteCustom(Agent a) async {
    final bool? ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        title: Text('Delete ${a.name}?'),
        content: const Text('This removes the custom agent. It cannot be undone.'),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(
                backgroundColor: Theme.of(ctx).colorScheme.error),
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (ok != true || !mounted) return;
    _registry.remove(a.id);
    setState(() {
      if (_selectedId == a.id) {
        _selectedId =
            _registry.agents.isNotEmpty ? _registry.agents.first.id : null;
      }
    });
  }

  @override
  void dispose() {
    _saveTimer?.cancel();
    _pollTimer?.cancel();
    _search.dispose();
    _registry.removeListener(_scheduleSave);
    // Only dispose registries we created.
    if (widget._injectedRegistry == null) _registry.dispose();
    super.dispose();
  }

  void _scheduleSave() {
    _saveTimer?.cancel();
    _saveTimer = Timer(const Duration(milliseconds: 600), () {
      AgentPersistence.save(_registry.toJson());
    });
  }

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Scaffold(
      backgroundColor: cs.surface,
      body: AnimatedBuilder(
        animation: _registry,
        builder: (BuildContext context, _) {
          final List<Agent> list = sortAgents(
            filterAgents(_registry.byKind(_filter), _query),
            _sort,
          );
          final Agent? selected =
              _selectedId == null ? null : _registry.byId(_selectedId!);
          return Column(
            children: <Widget>[
              _header(cs),
              const Divider(height: 1),
              Expanded(
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: <Widget>[
                    SizedBox(width: 300, child: _listColumn(cs, list)),
                    VerticalDivider(width: 1, thickness: 1, color: cs.outlineVariant),
                    Expanded(child: _detail(cs, selected)),
                  ],
                ),
              ),
            ],
          );
        },
      ),
    );
  }

  // ── Header: title + running count, then a scrollable filter row ──────────
  Widget _header(ColorScheme cs) {
    final int running = _registry.runningCount;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 12, 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              Icon(Icons.smart_toy, color: cs.primary),
              const SizedBox(width: 10),
              Text(
                'Agents',
                style: TextStyle(
                  fontSize: 18,
                  fontWeight: FontWeight.w700,
                  color: cs.onSurface,
                ),
              ),
              const SizedBox(width: 12),
              _CountPill(
                label: '$running running',
                color: running > 0 ? Colors.green : cs.onSurfaceVariant,
              ),
              const SizedBox(width: 6),
              _CountPill(
                label: '${_registry.agents.length} total',
                color: cs.onSurfaceVariant,
              ),
              const SizedBox(width: 8),
              TextButton.icon(
                onPressed: () => _showAgentEditor(),
                icon: const Icon(Icons.add, size: 18),
                label: const Text('New'),
                style: TextButton.styleFrom(visualDensity: VisualDensity.compact),
              ),
              const Spacer(),
              if (widget._livePolling) ...<Widget>[
                _ConnPill(reachable: _reachable, polled: _polledOnce),
                IconButton(
                  tooltip: 'Refresh status & runs',
                  visualDensity: VisualDensity.compact,
                  icon: const Icon(Icons.refresh, size: 18),
                  onPressed: _manualRefresh,
                ),
              ],
            ],
          ),
          const SizedBox(height: 8),
          // Horizontally scrollable so the chips never overflow a narrow window.
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: <Widget>[
                _filterChip(cs, 'All', null),
                for (final AgentKind k in AgentKind.values)
                  if (k != AgentKind.custom || _registry.byKind(k).isNotEmpty)
                    _filterChip(cs, k.label, k),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _filterChip(ColorScheme cs, String label, AgentKind? kind) {
    final bool sel = _filter == kind;
    return Padding(
      padding: const EdgeInsets.only(left: 6),
      child: ChoiceChip(
        label: Text(label),
        selected: sel,
        visualDensity: VisualDensity.compact,
        onSelected: (_) => setState(() => _filter = kind),
      ),
    );
  }

  // ── Master list column: search + sort toolbar, then the list ─────────────
  Widget _listColumn(ColorScheme cs, List<Agent> list) {
    return Column(
      children: <Widget>[
        _listToolbar(cs),
        Divider(height: 1, color: cs.outlineVariant),
        Expanded(child: _agentList(cs, list)),
      ],
    );
  }

  Widget _listToolbar(ColorScheme cs) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(8, 6, 4, 6),
      child: Row(
        children: <Widget>[
          Expanded(
            child: SizedBox(
              height: 36,
              child: TextField(
                controller: _search,
                style: const TextStyle(fontSize: 13),
                decoration: InputDecoration(
                  isDense: true,
                  hintText: 'Search agents',
                  prefixIcon: const Icon(Icons.search, size: 18),
                  suffixIcon: _query.isEmpty
                      ? null
                      : IconButton(
                          icon: const Icon(Icons.close, size: 16),
                          onPressed: () {
                            _search.clear();
                            setState(() => _query = '');
                          },
                        ),
                  contentPadding: const EdgeInsets.symmetric(vertical: 0),
                  border: const OutlineInputBorder(),
                ),
                onChanged: (String v) => setState(() => _query = v),
              ),
            ),
          ),
          PopupMenuButton<AgentSort>(
            tooltip: 'Sort',
            icon: const Icon(Icons.sort, size: 18),
            initialValue: _sort,
            onSelected: (AgentSort s) => setState(() => _sort = s),
            itemBuilder: (BuildContext _) => <PopupMenuEntry<AgentSort>>[
              for (final AgentSort s in AgentSort.values)
                PopupMenuItem<AgentSort>(
                  value: s,
                  child: Row(
                    children: <Widget>[
                      Icon(
                        s == _sort ? Icons.check : Icons.sort,
                        size: 16,
                        color: s == _sort ? cs.primary : cs.onSurfaceVariant,
                      ),
                      const SizedBox(width: 8),
                      Text('Sort by ${s.label}'),
                    ],
                  ),
                ),
            ],
          ),
        ],
      ),
    );
  }

  // ── Master list ───────────────────────────────────────────────────────────
  Widget _agentList(ColorScheme cs, List<Agent> list) {
    if (list.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Text(
            _query.isNotEmpty ? 'No agents match “$_query”' : 'No agents',
            textAlign: TextAlign.center,
            style: TextStyle(color: cs.onSurfaceVariant),
          ),
        ),
      );
    }
    return ListView.builder(
      padding: const EdgeInsets.symmetric(vertical: 4),
      itemCount: list.length,
      itemBuilder: (BuildContext context, int i) {
        final Agent a = list[i];
        final bool sel = a.id == _selectedId;
        return InkWell(
          onTap: () => setState(() => _selectedId = a.id),
          child: Container(
            color: sel ? cs.primary.withValues(alpha: 0.10) : null,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            child: Row(
              children: <Widget>[
                _StatusDot(status: a.status),
                const SizedBox(width: 10),
                Icon(_kindIcon(a.kind), size: 18, color: cs.onSurfaceVariant),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(
                        a.name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontWeight: FontWeight.w600,
                          color: cs.onSurface,
                        ),
                      ),
                      Text(
                        a.schedule.isEmpty ? a.kind.label : a.schedule,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant),
                      ),
                    ],
                  ),
                ),
                if (liveBackedAgentIds.contains(a.id))
                  const Padding(
                    padding: EdgeInsets.only(left: 4),
                    child: _LiveTag(),
                  ),
                if (!a.enabled)
                  Padding(
                    padding: const EdgeInsets.only(left: 4),
                    child: Icon(Icons.pause_circle_outline,
                        size: 16, color: cs.onSurfaceVariant),
                  ),
              ],
            ),
          ),
        );
      },
    );
  }

  // ── Detail pane ───────────────────────────────────────────────────────────
  Widget _detail(ColorScheme cs, Agent? a) {
    if (a == null) {
      return Center(
        child: Text('Select an agent',
            style: TextStyle(color: cs.onSurfaceVariant)),
      );
    }
    final bool live = liveBackedAgentIds.contains(a.id);
    final bool ctrl = controlBackedAgentIds.contains(a.id); // real backend control
    final bool gated = tradingConfirmAgentIds.contains(a.id); // trading → confirm
    final bool roLive = live && !ctrl; // live status but no control endpoint
    final bool runOnceBacked = runOnceBackedAgentIds.contains(a.id);
    return ListView(
      padding: const EdgeInsets.all(20),
      children: <Widget>[
        Row(
          children: <Widget>[
            _StatusDot(status: a.status, size: 12),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                a.name,
                style: TextStyle(
                  fontSize: 20,
                  fontWeight: FontWeight.w700,
                  color: cs.onSurface,
                ),
              ),
            ),
            _CountPill(label: a.kind.label, color: cs.onSurfaceVariant),
            const SizedBox(width: 8),
            _CountPill(label: a.status.label, color: _statusColor(a.status, cs)),
            if (live) ...<Widget>[
              const SizedBox(width: 8),
              const _LiveTag(),
            ],
            if (gated) ...<Widget>[
              const SizedBox(width: 8),
              _CountPill(label: 'LIVE TRADING', color: cs.error),
            ],
            if (!a.builtin) ...<Widget>[
              const SizedBox(width: 4),
              PopupMenuButton<String>(
                tooltip: 'Edit agent',
                icon: const Icon(Icons.more_vert, size: 18),
                onSelected: (String v) {
                  if (v == 'edit') _showAgentEditor(initial: a);
                  if (v == 'delete') _deleteCustom(a);
                },
                itemBuilder: (BuildContext _) => const <PopupMenuEntry<String>>[
                  PopupMenuItem<String>(value: 'edit', child: Text('Edit')),
                  PopupMenuItem<String>(value: 'delete', child: Text('Delete')),
                ],
              ),
            ],
          ],
        ),
        const SizedBox(height: 12),
        Text(a.description, style: TextStyle(color: cs.onSurfaceVariant, height: 1.4)),
        const SizedBox(height: 20),
        // Controls. ctrl agents act on the real backend (trading ones gated by a
        // confirm); roLive agents are read-only; the rest are local (AGT-1).
        Wrap(
          spacing: 10,
          runSpacing: 10,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: <Widget>[
            FilledButton.icon(
              onPressed: ctrl
                  ? (a.status != AgentStatus.running
                      ? () => _control(a, AgentAction.start)
                      : null)
                  : ((!roLive && a.canStart && a.enabled && a.status != AgentStatus.running)
                      ? () => _registry.start(a.id)
                      : null),
              icon: const Icon(Icons.play_arrow, size: 18),
              label: const Text('Start'),
            ),
            OutlinedButton.icon(
              onPressed: ctrl
                  ? (a.status == AgentStatus.running
                      ? () => _control(a, AgentAction.stop)
                      : null)
                  : ((!roLive && a.canStop && a.status == AgentStatus.running)
                      ? () => _registry.stop(a.id)
                      : null),
              icon: const Icon(Icons.stop, size: 18),
              label: const Text('Stop'),
            ),
            OutlinedButton.icon(
              onPressed: ctrl
                  ? (runOnceBacked ? () => _control(a, AgentAction.runOnce) : null)
                  : ((!roLive && a.canRunOnce && a.enabled)
                      ? () => _registry.runOnce(a.id)
                      : null),
              icon: const Icon(Icons.bolt, size: 18),
              label: const Text('Run once'),
            ),
            const SizedBox(width: 4),
            Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Switch(
                  value: a.enabled,
                  onChanged: (live || ctrl)
                      ? null
                      : (bool v) => _registry.setEnabled(a.id, v),
                ),
                Text('Enabled', style: TextStyle(color: cs.onSurface)),
              ],
            ),
          ],
        ),
        const SizedBox(height: 8),
        if (ctrl)
          _noteRow(
            cs,
            gated ? Icons.warning_amber_rounded : Icons.cloud_sync_outlined,
            gated
                ? 'Acts on the LIVE auto-trader. Resuming can place real orders — every action is confirmed first.'
                : 'Acts on the live backend (real start/stop/run).',
          )
        else if (roLive)
          _liveNote(cs)
        else
          _localNote(cs),
        const SizedBox(height: 18),
        _section(cs, 'Schedule'),
        Text(a.schedule.isEmpty ? '—' : a.schedule,
            style: TextStyle(color: cs.onSurface)),
        if (a.killSwitch != null) ...<Widget>[
          const SizedBox(height: 14),
          _section(cs, 'Kill switch'),
          Row(
            children: <Widget>[
              Icon(Icons.shield_outlined, size: 16, color: cs.error),
              const SizedBox(width: 6),
              Expanded(
                child: Text(a.killSwitch!, style: TextStyle(color: cs.onSurface)),
              ),
            ],
          ),
        ],
        const SizedBox(height: 14),
        _section(cs, 'Last run'),
        Text(
          a.lastRun == null ? 'Never (this session)' : _fmtRun(a.lastRun!, a.lastResult),
          style: TextStyle(color: cs.onSurface),
        ),
        if (a.config.isNotEmpty) ...<Widget>[
          const SizedBox(height: 18),
          _section(cs, 'Configuration'),
          const SizedBox(height: 6),
          for (final MapEntry<String, String> e in a.config.entries)
            _configRow(cs, a, e.key, e.value),
        ],
        if (agentHasBackendRuns(a.id)) ...<Widget>[
          const SizedBox(height: 18),
          _backendRunsSection(cs, a),
        ],
        const SizedBox(height: 18),
        _activitySection(cs, a),
        const SizedBox(height: 24),
      ],
    );
  }

  // ── Backend runs feed (AGT-8) ────────────────────────────────────────────
  Widget _backendRunsSection(ColorScheme cs, Agent a) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _section(cs, 'Backend runs'),
        const SizedBox(height: 4),
        FutureBuilder<List<AgentRun>>(
          future: _runsFuture(a.id),
          builder: (BuildContext context,
              AsyncSnapshot<List<AgentRun>> snap) {
            if (snap.connectionState == ConnectionState.waiting) {
              return Row(
                children: <Widget>[
                  const SizedBox(
                    width: 14,
                    height: 14,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                  const SizedBox(width: 8),
                  Text('Loading…',
                      style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
                ],
              );
            }
            final List<AgentRun> runs = snap.data ?? const <AgentRun>[];
            if (runs.isEmpty) {
              return Text(
                'No backend runs (offline or none recorded).',
                style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
              );
            }
            final RunHealth health = summarizeRuns(runs);
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                // AG-1 — run-health summary: outcome strip + success-rate pill.
                Row(
                  children: <Widget>[
                    Expanded(child: RunOutcomeStrip(runs)),
                    if (health.successLabel != null) ...<Widget>[
                      const SizedBox(width: 8),
                      ApStatusPill(
                        health.successLabel!,
                        color: (health.successRate ?? 0) >= 0.5
                            ? const Color(0xFF2E9E5B)
                            : cs.error,
                      ),
                    ],
                  ],
                ),
                const SizedBox(height: 6),
                for (final AgentRun r in runs) _runRow(cs, a, r),
              ],
            );
          },
        ),
      ],
    );
  }

  Widget _runRow(ColorScheme cs, Agent a, AgentRun r) {
    final DateTime? dt = DateTime.tryParse(r.when);
    final String when = dt == null
        ? r.when
        : '${dt.month.toString().padLeft(2, '0')}/${dt.day.toString().padLeft(2, '0')} '
            '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
    final bool tappable = widget.onDiscussRun != null; // AG-2
    final Widget row = Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Icon(Icons.cloud_outlined, size: 14, color: cs.secondary),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            r.outcome == null ? r.title : '${r.title} — ${r.outcome}',
            style: TextStyle(fontSize: 13, color: cs.onSurface),
          ),
        ),
        const SizedBox(width: 8),
        Text(when,
            style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant)),
        if (tappable) ...<Widget>[
          const SizedBox(width: 6),
          Icon(Icons.forum_outlined, size: 13, color: cs.onSurfaceVariant),
        ],
      ],
    );
    if (!tappable) {
      return Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: row,
      );
    }
    return InkWell(
      onTap: () => widget.onDiscussRun!(a.name, r),
      borderRadius: BorderRadius.circular(6),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 4, horizontal: 2),
        child: row,
      ),
    );
  }

  // ── Activity log (AGT-5) ─────────────────────────────────────────────────
  Widget _activitySection(ColorScheme cs, Agent a) {
    final List<AgentEvent> log = _registry.events(a.id);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        _section(cs, 'Recent activity'),
        const SizedBox(height: 4),
        if (log.isEmpty)
          Text('No activity yet this session.',
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant))
        else
          for (final AgentEvent e in log) _activityRow(cs, e),
      ],
    );
  }

  Widget _activityRow(ColorScheme cs, AgentEvent e) {
    final DateTime? dt = DateTime.tryParse(e.timestamp);
    final String when = dt == null
        ? ''
        : '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}:${dt.second.toString().padLeft(2, '0')}';
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Icon(_eventIcon(e.kind), size: 14, color: _eventColor(e.kind, cs)),
          const SizedBox(width: 8),
          Expanded(
            child: Text(e.message,
                style: TextStyle(fontSize: 13, color: cs.onSurface)),
          ),
          const SizedBox(width: 8),
          Text(when,
              style: TextStyle(
                  fontSize: 11,
                  color: cs.onSurfaceVariant,
                  fontFeatures: const <FontFeature>[
                    FontFeature.tabularFigures()
                  ])),
        ],
      ),
    );
  }

  static IconData _eventIcon(AgentEventKind k) {
    switch (k) {
      case AgentEventKind.action:
        return Icons.play_circle_outline;
      case AgentEventKind.status:
        return Icons.sync;
      case AgentEventKind.config:
        return Icons.tune;
    }
  }

  static Color _eventColor(AgentEventKind k, ColorScheme cs) {
    switch (k) {
      case AgentEventKind.action:
        return cs.primary;
      case AgentEventKind.status:
        return Colors.green;
      case AgentEventKind.config:
        return cs.secondary;
    }
  }

  Widget _localNote(ColorScheme cs) {
    return _noteRow(
      cs,
      Icons.info_outline,
      'Controls update local state. Live backend status & control arrive in a later update.',
    );
  }

  Widget _liveNote(ColorScheme cs) {
    return _noteRow(
      cs,
      Icons.cloud_done_outlined,
      'Live status from the backend (read-only). Start/stop control lands in a later update.',
    );
  }

  Widget _noteRow(ColorScheme cs, IconData icon, String text) {
    return Row(
      children: <Widget>[
        Icon(icon, size: 14, color: cs.onSurfaceVariant),
        const SizedBox(width: 6),
        Expanded(
          child: Text(
            text,
            style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant),
          ),
        ),
      ],
    );
  }

  Widget _section(ColorScheme cs, String title) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Text(
        title.toUpperCase(),
        style: TextStyle(
          fontSize: 11,
          fontWeight: FontWeight.w700,
          letterSpacing: 0.6,
          color: cs.onSurfaceVariant,
        ),
      ),
    );
  }

  Widget _configRow(ColorScheme cs, Agent a, String key, String value) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: <Widget>[
          SizedBox(
            width: 150,
            child: Text(key, style: TextStyle(color: cs.onSurfaceVariant, fontSize: 13)),
          ),
          Expanded(
            child: TextField(
              key: ValueKey<String>('cfg-${a.id}-$key'),
              controller: TextEditingController(text: value),
              enabled: a.canConfigure,
              style: const TextStyle(fontSize: 13),
              decoration: const InputDecoration(
                isDense: true,
                contentPadding: EdgeInsets.symmetric(horizontal: 8, vertical: 6),
                border: OutlineInputBorder(),
              ),
              onSubmitted: (String v) => _registry.setConfig(a.id, key, v),
            ),
          ),
        ],
      ),
    );
  }

  // ── helpers ─────────────────────────────────────────────────────────────
  String _fmtRun(String iso, String? result) {
    final DateTime? dt = DateTime.tryParse(iso);
    final String when = dt == null
        ? iso
        : '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}:${dt.second.toString().padLeft(2, '0')}';
    return result == null ? when : '$when · $result';
  }

  static IconData _kindIcon(AgentKind k) {
    switch (k) {
      case AgentKind.trading:
        return Icons.trending_up;
      case AgentKind.brain:
        return Icons.psychology;
      case AgentKind.coding:
        return Icons.terminal;
      case AgentKind.system:
        return Icons.settings_suggest;
      case AgentKind.custom:
        return Icons.smart_toy;
    }
  }

  static Color _statusColor(AgentStatus s, ColorScheme cs) {
    switch (s) {
      case AgentStatus.running:
        return Colors.green;
      case AgentStatus.error:
        return cs.error;
      case AgentStatus.idle:
        return cs.secondary;
      case AgentStatus.stopped:
        return cs.onSurfaceVariant;
      case AgentStatus.unknown:
        return Colors.amber;
    }
  }
}

class _StatusDot extends StatelessWidget {
  const _StatusDot({required this.status, this.size = 10});
  final AgentStatus status;
  final double size;

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: _AgentsScreenState._statusColor(status, cs),
        shape: BoxShape.circle,
      ),
    );
  }
}

class _CountPill extends StatelessWidget {
  const _CountPill({required this.label, required this.color});
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(20),
      ),
      child: Text(
        label,
        style: TextStyle(fontSize: 11, fontWeight: FontWeight.w600, color: color),
      ),
    );
  }
}

/// Small "LIVE" tag marking agents whose status comes from the backend.
class _LiveTag extends StatelessWidget {
  const _LiveTag();

  @override
  Widget build(BuildContext context) {
    const Color c = Colors.green;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: c.withValues(alpha: 0.14),
        borderRadius: BorderRadius.circular(4),
      ),
      child: const Text(
        'LIVE',
        style: TextStyle(
          fontSize: 9,
          fontWeight: FontWeight.w800,
          letterSpacing: 0.5,
          color: c,
        ),
      ),
    );
  }
}

/// Header connection indicator: green when the backend answered the last poll.
class _ConnPill extends StatelessWidget {
  const _ConnPill({required this.reachable, required this.polled});
  final bool reachable;
  final bool polled;

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    final Color c = !polled
        ? cs.onSurfaceVariant
        : (reachable ? Colors.green : cs.onSurfaceVariant);
    final String label =
        !polled ? 'Connecting…' : (reachable ? 'Live' : 'Offline');
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Icon(reachable && polled ? Icons.circle : Icons.circle_outlined,
            size: 9, color: c),
        const SizedBox(width: 5),
        Text(label, style: TextStyle(fontSize: 11, color: c)),
      ],
    );
  }
}

/// Create / edit a custom agent. Returns the built [Agent] via Navigator.pop,
/// or null on cancel. For a new agent it generates a unique id; for an edit it
/// keeps the existing id (the registry preserves runtime state).
class _AgentEditorDialog extends StatefulWidget {
  const _AgentEditorDialog({this.initial, required this.existingIds});
  final Agent? initial;
  final Set<String> existingIds;

  @override
  State<_AgentEditorDialog> createState() => _AgentEditorDialogState();
}

class _AgentEditorDialogState extends State<_AgentEditorDialog> {
  late final TextEditingController _name;
  late final TextEditingController _desc;
  late final TextEditingController _schedule;
  late AgentKind _kind;
  late final List<_ConfigRowControllers> _config;
  String? _nameError;

  @override
  void initState() {
    super.initState();
    final Agent? a = widget.initial;
    _name = TextEditingController(text: a?.name ?? '');
    _desc = TextEditingController(text: a?.description ?? '');
    _schedule = TextEditingController(text: a?.schedule ?? '');
    _kind = a?.kind ?? AgentKind.custom;
    _config = <_ConfigRowControllers>[
      for (final MapEntry<String, String> e in (a?.config ?? const <String, String>{}).entries)
        _ConfigRowControllers(e.key, e.value),
    ];
  }

  @override
  void dispose() {
    _name.dispose();
    _desc.dispose();
    _schedule.dispose();
    for (final _ConfigRowControllers c in _config) {
      c.dispose();
    }
    super.dispose();
  }

  void _save() {
    final String name = _name.text.trim();
    if (name.isEmpty) {
      setState(() => _nameError = 'Name is required');
      return;
    }
    final Map<String, String> config = <String, String>{};
    for (final _ConfigRowControllers c in _config) {
      final String k = c.key.text.trim();
      if (k.isEmpty) continue;
      config[k] = c.value.text.trim();
    }
    final String id = widget.initial?.id ??
        makeAgentId(name, widget.existingIds);
    Navigator.of(context).pop(Agent(
      id: id,
      name: name,
      kind: _kind,
      description: _desc.text.trim(),
      schedule: _schedule.text.trim(),
      config: config,
      builtin: false,
      status: widget.initial?.status ?? AgentStatus.unknown,
      enabled: widget.initial?.enabled ?? true,
    ));
  }

  @override
  Widget build(BuildContext context) {
    final bool editing = widget.initial != null;
    return AlertDialog(
      title: Text(editing ? 'Edit agent' : 'New agent'),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              TextField(
                controller: _name,
                autofocus: true,
                decoration: InputDecoration(
                  labelText: 'Name',
                  errorText: _nameError,
                  border: const OutlineInputBorder(),
                ),
                onChanged: (_) {
                  if (_nameError != null) setState(() => _nameError = null);
                },
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<AgentKind>(
                initialValue: _kind,
                decoration: const InputDecoration(
                  labelText: 'Kind',
                  border: OutlineInputBorder(),
                ),
                items: <DropdownMenuItem<AgentKind>>[
                  for (final AgentKind k in AgentKind.values)
                    DropdownMenuItem<AgentKind>(value: k, child: Text(k.label)),
                ],
                onChanged: (AgentKind? v) =>
                    setState(() => _kind = v ?? AgentKind.custom),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _desc,
                maxLines: 2,
                decoration: const InputDecoration(
                  labelText: 'Description',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _schedule,
                decoration: const InputDecoration(
                  labelText: 'Schedule',
                  hintText: 'e.g. every 5min, daily, on demand',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 16),
              Row(
                children: <Widget>[
                  Text('Config', style: Theme.of(context).textTheme.labelLarge),
                  const Spacer(),
                  TextButton.icon(
                    onPressed: () => setState(
                        () => _config.add(_ConfigRowControllers('', ''))),
                    icon: const Icon(Icons.add, size: 16),
                    label: const Text('Add field'),
                  ),
                ],
              ),
              for (int i = 0; i < _config.length; i++)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Row(
                    children: <Widget>[
                      Expanded(
                        child: TextField(
                          controller: _config[i].key,
                          decoration: const InputDecoration(
                            isDense: true,
                            labelText: 'key',
                            border: OutlineInputBorder(),
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: TextField(
                          controller: _config[i].value,
                          decoration: const InputDecoration(
                            isDense: true,
                            labelText: 'value',
                            border: OutlineInputBorder(),
                          ),
                        ),
                      ),
                      IconButton(
                        tooltip: 'Remove field',
                        icon: const Icon(Icons.close, size: 18),
                        onPressed: () => setState(() {
                          _config.removeAt(i).dispose();
                        }),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
      actions: <Widget>[
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _save,
          child: Text(editing ? 'Save' : 'Create'),
        ),
      ],
    );
  }
}

class _ConfigRowControllers {
  _ConfigRowControllers(String k, String v)
      : key = TextEditingController(text: k),
        value = TextEditingController(text: v);
  final TextEditingController key;
  final TextEditingController value;
  void dispose() {
    key.dispose();
    value.dispose();
  }
}
