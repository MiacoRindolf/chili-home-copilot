import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';
import '../ui/app_ui.dart';
import 'mcp_models.dart';

/// Fetches MCP status JSON — injectable for tests.
typedef McpStatusFetcher = Future<Map<String, dynamic>> Function();

/// Fetches the flat MCP tool list — injectable for tests (MC-2).
typedef McpToolsFetcher = Future<List<Map<String, dynamic>>> Function();

/// MCP Tools — surfaces CHILI's external Model-Context-Protocol tool servers
/// (salvaged from Odysseus): configured servers, live connection status, the
/// permitted tools each exposes (MC-2), and the allowlist/denylist policy.
/// Read-only.
class McpScreen extends StatefulWidget {
  const McpScreen({super.key, McpStatusFetcher? fetcher, McpToolsFetcher? toolsFetcher})
      : _injectedFetcher = fetcher,
        _injectedToolsFetcher = toolsFetcher;

  final McpStatusFetcher? _injectedFetcher;
  final McpToolsFetcher? _injectedToolsFetcher;

  @override
  State<McpScreen> createState() => _McpScreenState();
}

class _McpScreenState extends State<McpScreen> {
  late final McpStatusFetcher _fetcher;
  late final McpToolsFetcher _toolsFetcher;
  McpStatus? _status;
  Map<String, List<McpTool>> _tools = const <String, List<McpTool>>{};
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    final ChiliApiClient? api =
        (widget._injectedFetcher == null || widget._injectedToolsFetcher == null)
            ? ChiliApiClient()
            : null;
    _fetcher = widget._injectedFetcher ?? api!.getMcpStatus;
    _toolsFetcher = widget._injectedToolsFetcher ?? api!.getMcpTools;
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final List<Object> r = await Future.wait(<Future<Object>>[
        _fetcher(),
        _toolsFetcher(),
      ]);
      if (!mounted) return;
      setState(() {
        _status = parseMcpStatus(r[0] as Map<String, dynamic>);
        _tools = groupToolsByServer(
            parseMcpTools(r[1] as List<Map<String, dynamic>>));
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    return Scaffold(
      backgroundColor: cs.surface,
      body: Column(
        children: <Widget>[
          _header(cs),
          const Divider(height: 1),
          Expanded(child: _body(cs)),
        ],
      ),
    );
  }

  Widget _header(ColorScheme cs) {
    final McpStatus? s = _status;
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 14, 12, 12),
      child: Row(
        children: <Widget>[
          Icon(Icons.hub_outlined, color: cs.primary),
          const SizedBox(width: 10),
          Text('MCP Tools',
              style: Theme.of(context)
                  .textTheme
                  .headlineSmall
                  ?.copyWith(fontWeight: FontWeight.w700)),
          const SizedBox(width: 12),
          if (s != null)
            ApStatusPill(
              s.enabled ? 'Enabled' : 'Disabled',
              color: s.enabled ? Colors.green : cs.onSurfaceVariant,
            ),
          if (s != null && s.enabled) ...<Widget>[
            const SizedBox(width: 6),
            ApStatusPill('${s.connectedCount}/${s.servers.length} connected',
                color: s.connectedCount > 0 ? Colors.green : cs.onSurfaceVariant),
          ],
          const Spacer(),
          IconButton(
            tooltip: 'Refresh',
            icon: const Icon(Icons.refresh, size: 20),
            onPressed: _loading ? null : _load,
          ),
        ],
      ),
    );
  }

  Widget _body(ColorScheme cs) {
    if (_loading) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            CircularProgressIndicator(),
            SizedBox(height: 12),
            Text('Loading MCP status…'),
          ],
        ),
      );
    }
    if (_error != null) {
      return ApEmptyState(
        icon: Icons.cloud_off,
        message: 'Couldn’t load MCP status',
        detail: _error,
        action: FilledButton.icon(
          onPressed: _load,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Retry'),
        ),
      );
    }
    final McpStatus s = _status ?? McpStatus.empty;
    if (!s.enabled) {
      return const ApEmptyState(
        icon: Icons.extension_off_outlined,
        message: 'MCP is disabled',
        detail:
            'External tool servers are off. Enable with CHILI_MCP_ENABLED=1 and configure servers.',
      );
    }
    if (!s.hasServers) {
      return const ApEmptyState(
        icon: Icons.hub_outlined,
        message: 'No MCP servers configured',
        detail: 'Add servers via mcp_servers_json to connect external tools.',
      );
    }
    return ListView(
      padding: const EdgeInsets.all(20),
      children: <Widget>[
        if (!s.sdkPresent)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: ApPanel(
              color: Colors.amber.withValues(alpha: 0.10),
              padding: const EdgeInsets.all(12),
              child: Row(children: <Widget>[
                Icon(Icons.warning_amber_rounded,
                    size: 18, color: Colors.amber.shade700),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    'The MCP SDK is not installed — servers are configured but cannot connect.',
                    style: TextStyle(color: cs.onSurface),
                  ),
                ),
              ]),
            ),
          ),
        for (final McpServer srv in s.servers) _serverCard(cs, srv),
        const SizedBox(height: 24),
      ],
    );
  }

  Widget _serverCard(ColorScheme cs, McpServer srv) {
    final Color statusColor = _statusColor(srv.status, cs);
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: ApPanel(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Icon(srv.transport == 'sse' ? Icons.cloud_outlined : Icons.terminal,
                    size: 18, color: cs.onSurfaceVariant),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(srv.name,
                      style: TextStyle(
                          fontSize: 15,
                          fontWeight: FontWeight.w700,
                          color: cs.onSurface)),
                ),
                if (srv.transport.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ApStatusPill(srv.transport, color: cs.secondary),
                  ),
                ApStatusPill(srv.status, color: statusColor),
              ],
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 16,
              runSpacing: 4,
              children: <Widget>[
                _stat(cs, Icons.build_outlined, '${srv.toolCount} tools',
                    cs.onSurface),
                if (srv.blockedCount > 0)
                  _stat(cs, Icons.shield_outlined,
                      '${srv.blockedCount} blocked by safety', cs.error),
              ],
            ),
            if (srv.error != null) ...<Widget>[
              const SizedBox(height: 8),
              Text(srv.error!, style: TextStyle(color: cs.error, fontSize: 12)),
            ],
            // MC-2 — live tools this server exposes (name + description).
            if ((_tools[srv.id] ?? const <McpTool>[]).isNotEmpty) ...<Widget>[
              const SizedBox(height: 12),
              const ApSectionHeader('Tools', icon: Icons.build_outlined),
              const SizedBox(height: 4),
              for (final McpTool t in _tools[srv.id]!)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 3),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Icon(Icons.bolt, size: 14, color: cs.secondary),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: <Widget>[
                            Text(t.name,
                                style: TextStyle(
                                    fontSize: 13,
                                    fontWeight: FontWeight.w600,
                                    color: cs.onSurface)),
                            if (t.description.isNotEmpty)
                              Text(t.description,
                                  style: TextStyle(
                                      fontSize: 12,
                                      color: cs.onSurfaceVariant)),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
            ],
            if (srv.allowedTools.isNotEmpty) ...<Widget>[
              const SizedBox(height: 12),
              const ApSectionHeader('Allowed tools'),
              const SizedBox(height: 4),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: <Widget>[
                  for (final String t in srv.allowedTools)
                    ApStatusPill(t, color: cs.secondary),
                ],
              ),
            ],
            if (srv.denylistedTools.isNotEmpty) ...<Widget>[
              const SizedBox(height: 12),
              const ApSectionHeader('Blocked by safety denylist'),
              const SizedBox(height: 4),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children: <Widget>[
                  for (final String t in srv.denylistedTools)
                    ApStatusPill(t, color: cs.error, icon: Icons.block),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _stat(ColorScheme cs, IconData icon, String text, Color color) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: <Widget>[
        Icon(icon, size: 14, color: color),
        const SizedBox(width: 5),
        Text(text, style: TextStyle(fontSize: 12, color: color)),
      ],
    );
  }

  static Color _statusColor(String status, ColorScheme cs) {
    switch (status) {
      case 'connected':
        return Colors.green;
      case 'error':
        return cs.error;
      case 'disconnected':
        return cs.onSurfaceVariant;
      default:
        return Colors.amber.shade700;
    }
  }
}
