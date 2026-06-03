import 'dart:async';

import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';
import 'agent.dart';
import 'agent_persistence.dart';
import 'agent_registry.dart';
import 'agent_status_service.dart';

/// Signature of the live-status poller — injectable so tests can feed canned
/// readings without touching the network.
typedef AgentStatusPoller = Future<Map<String, AgentLiveStatus>> Function();

/// Mission control for CHILI's autonomous agents — check, configure and run the
/// trading / brain / coding / system fleet. AGT-1: master/detail with local
/// run/stop, enable toggles and editable config. AGT-2: live backend status for
/// the agents that expose it (read-only) with a connection indicator.
class AgentsScreen extends StatefulWidget {
  const AgentsScreen({
    super.key,
    AgentRegistry? registry,
    AgentStatusPoller? livePoller,
    bool livePolling = true,
  })  : _injectedRegistry = registry,
        _injectedPoller = livePoller,
        _livePolling = livePolling;

  /// Optional registry for tests; production builds seed a fresh one.
  final AgentRegistry? _injectedRegistry;

  /// Optional status poller for tests; production builds a real one.
  final AgentStatusPoller? _injectedPoller;

  /// When false, no live polling timer runs (used by tests / when offline use
  /// is undesirable).
  final bool _livePolling;

  @override
  State<AgentsScreen> createState() => _AgentsScreenState();
}

class _AgentsScreenState extends State<AgentsScreen> {
  late final AgentRegistry _registry;
  AgentStatusPoller? _poller;
  AgentStatusService? _ownedService;
  AgentKind? _filter; // null = All
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
    _restore();
    if (widget._livePolling) _startPolling();
  }

  Future<void> _restore() async {
    final List<Map<String, dynamic>> saved = await AgentPersistence.load();
    if (!mounted) return;
    _registry.applySaved(saved); // overlay before we start listening to saves
    _registry.addListener(_scheduleSave);
  }

  void _startPolling() {
    if (widget._injectedPoller != null) {
      _poller = widget._injectedPoller;
    } else {
      _ownedService = AgentStatusService(ChiliApiClient());
      _poller = _ownedService!.poll;
    }
    _poll();
    _pollTimer = Timer.periodic(_pollInterval, (_) => _poll());
  }

  Future<void> _poll() async {
    final AgentStatusPoller? poll = _poller;
    if (poll == null) return;
    Map<String, AgentLiveStatus> live;
    try {
      live = await poll();
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

  @override
  void dispose() {
    _saveTimer?.cancel();
    _pollTimer?.cancel();
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
          final List<Agent> list = _registry.byKind(_filter);
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
                    SizedBox(width: 300, child: _agentList(cs, list)),
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
              const Spacer(),
              if (widget._livePolling) ...<Widget>[
                _ConnPill(reachable: _reachable, polled: _polledOnce),
                IconButton(
                  tooltip: 'Refresh status',
                  visualDensity: VisualDensity.compact,
                  icon: const Icon(Icons.refresh, size: 18),
                  onPressed: _poll,
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

  // ── Master list ───────────────────────────────────────────────────────────
  Widget _agentList(ColorScheme cs, List<Agent> list) {
    if (list.isEmpty) {
      return Center(
        child: Text('No agents', style: TextStyle(color: cs.onSurfaceVariant)),
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
          ],
        ),
        const SizedBox(height: 12),
        Text(a.description, style: TextStyle(color: cs.onSurfaceVariant, height: 1.4)),
        const SizedBox(height: 20),
        // Controls. Live-backed agents reflect backend truth (read-only here);
        // real start/stop control lands in AGT-3.
        Wrap(
          spacing: 10,
          runSpacing: 10,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: <Widget>[
            FilledButton.icon(
              onPressed:
                  (!live && a.canStart && a.enabled && a.status != AgentStatus.running)
                      ? () => _registry.start(a.id)
                      : null,
              icon: const Icon(Icons.play_arrow, size: 18),
              label: const Text('Start'),
            ),
            OutlinedButton.icon(
              onPressed: (!live && a.canStop && a.status == AgentStatus.running)
                  ? () => _registry.stop(a.id)
                  : null,
              icon: const Icon(Icons.stop, size: 18),
              label: const Text('Stop'),
            ),
            OutlinedButton.icon(
              onPressed: (!live && a.canRunOnce && a.enabled)
                  ? () => _registry.runOnce(a.id)
                  : null,
              icon: const Icon(Icons.bolt, size: 18),
              label: const Text('Run once'),
            ),
            const SizedBox(width: 4),
            Row(
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                Switch(
                  value: a.enabled,
                  onChanged: live ? null : (bool v) => _registry.setEnabled(a.id, v),
                ),
                Text('Enabled', style: TextStyle(color: cs.onSurface)),
              ],
            ),
          ],
        ),
        const SizedBox(height: 8),
        live ? _liveNote(cs) : _localNote(cs),
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
        const SizedBox(height: 24),
      ],
    );
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
