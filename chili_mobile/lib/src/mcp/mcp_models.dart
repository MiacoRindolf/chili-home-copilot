// Parsed shape of GET /api/brain/mcp/status (MC-1). Merges each configured
// server with its live runtime status. Pure + tolerant → unit-testable.

class McpServer {
  const McpServer({
    required this.id,
    required this.name,
    required this.transport,
    required this.allowedTools,
    required this.denylistedTools,
    required this.status,
    required this.toolCount,
    required this.blockedCount,
    this.error,
  });

  final String id;
  final String name;
  final String transport; // stdio | sse
  final List<String> allowedTools; // config allowlist
  final List<String> denylistedTools; // allowlisted BUT blocked by hard denylist
  final String status; // connected | error | disconnected | unknown
  final int toolCount; // live permitted tools
  final int blockedCount; // live blocked-by-safety tools
  final String? error;

  bool get isConnected => status == 'connected';
}

class McpStatus {
  const McpStatus({
    required this.enabled,
    required this.sdkPresent,
    required this.supervisorRunning,
    required this.configuredServers,
    required this.servers,
  });

  final bool enabled;
  final bool sdkPresent;
  final bool supervisorRunning;
  final int configuredServers;
  final List<McpServer> servers;

  bool get hasServers => servers.isNotEmpty;
  int get connectedCount => servers.where((McpServer s) => s.isConnected).length;

  static const McpStatus empty = McpStatus(
    enabled: false,
    sdkPresent: false,
    supervisorRunning: false,
    configuredServers: 0,
    servers: <McpServer>[],
  );
}

McpStatus parseMcpStatus(Map<String, dynamic> json) {
  final Map<String, dynamic> live = _map(json['live_status']);
  final List<McpServer> servers = <McpServer>[
    for (final Object? raw in (json['servers'] as List? ?? const <Object?>[]))
      if (raw is Map) _server(Map<String, dynamic>.from(raw), live),
  ];
  return McpStatus(
    enabled: json['enabled'] == true,
    sdkPresent: json['sdk_present'] == true,
    supervisorRunning: json['supervisor_running'] == true,
    configuredServers:
        (json['configured_servers'] as num?)?.toInt() ?? servers.length,
    servers: servers,
  );
}

McpServer _server(Map<String, dynamic> cfg, Map<String, dynamic> liveAll) {
  final String id = _str(cfg['id']);
  final Map<String, dynamic> live = _map(liveAll[id]);
  final String name = _str(live['name']).isNotEmpty
      ? _str(live['name'])
      : (_str(cfg['name']).isNotEmpty ? _str(cfg['name']) : id);
  final String transport =
      _str(live['transport']).isNotEmpty ? _str(live['transport']) : _str(cfg['transport']);
  return McpServer(
    id: id,
    name: name,
    transport: transport,
    allowedTools: _strList(cfg['allowed_tools']),
    denylistedTools: _strList(cfg['allowlist_blocked_by_denylist']),
    status: _str(live['status']).isEmpty ? 'unknown' : _str(live['status']),
    toolCount: (live['tool_count'] as num?)?.toInt() ?? 0,
    blockedCount: (live['blocked_count'] as num?)?.toInt() ?? 0,
    error: _str(live['error']).isEmpty ? null : _str(live['error']),
  );
}

Map<String, dynamic> _map(Object? v) =>
    v is Map ? Map<String, dynamic>.from(v) : <String, dynamic>{};

List<String> _strList(Object? v) => v is List
    ? v.map(_str).where((String s) => s.isNotEmpty).toList()
    : <String>[];

String _str(Object? v) => v?.toString().trim() ?? '';
