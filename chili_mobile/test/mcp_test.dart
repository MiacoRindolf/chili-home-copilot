import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/mcp/mcp_models.dart';
import 'package:chili_mobile/src/mcp/mcp_screen.dart';

void main() {
  group('parseMcpStatus', () {
    test('merges configured servers with live status', () {
      final McpStatus s = parseMcpStatus(<String, dynamic>{
        'enabled': true,
        'sdk_present': true,
        'supervisor_running': true,
        'configured_servers': 2,
        'servers': <Map<String, dynamic>>[
          <String, dynamic>{
            'id': 'sec',
            'name': 'SEC Filings',
            'transport': 'sse',
            'allowed_tools': <String>['search_filings', 'get_filing'],
            'allowlist_blocked_by_denylist': <String>['place_order'],
          },
          <String, dynamic>{'id': 'news', 'transport': 'stdio'},
        ],
        'live_status': <String, dynamic>{
          'sec': <String, dynamic>{
            'status': 'connected',
            'name': 'SEC Filings',
            'transport': 'sse',
            'tool_count': 2,
            'blocked_count': 1,
          },
          'news': <String, dynamic>{
            'status': 'error',
            'error': 'spawn failed',
          },
        },
      });
      expect(s.enabled, isTrue);
      expect(s.sdkPresent, isTrue);
      expect(s.supervisorRunning, isTrue);
      expect(s.servers.length, 2);
      expect(s.connectedCount, 1);

      final McpServer sec = s.servers.firstWhere((McpServer x) => x.id == 'sec');
      expect(sec.name, 'SEC Filings');
      expect(sec.transport, 'sse');
      expect(sec.status, 'connected');
      expect(sec.isConnected, isTrue);
      expect(sec.toolCount, 2);
      expect(sec.blockedCount, 1);
      expect(sec.allowedTools, contains('search_filings'));
      expect(sec.denylistedTools, contains('place_order'));

      final McpServer news =
          s.servers.firstWhere((McpServer x) => x.id == 'news');
      expect(news.name, 'news'); // falls back to id
      expect(news.status, 'error');
      expect(news.error, 'spawn failed');
    });

    test('tolerates an empty / disabled payload', () {
      final McpStatus s = parseMcpStatus(<String, dynamic>{});
      expect(s.enabled, isFalse);
      expect(s.hasServers, isFalse);
      expect(s.servers, isEmpty);

      final McpStatus s2 = parseMcpStatus(<String, dynamic>{
        'enabled': true,
        'servers': <Object?>['garbage', 42],
      });
      expect(s2.servers, isEmpty);
    });

    test('server with no live status defaults to unknown', () {
      final McpStatus s = parseMcpStatus(<String, dynamic>{
        'enabled': true,
        'servers': <Map<String, dynamic>>[
          <String, dynamic>{'id': 'x', 'name': 'X', 'transport': 'stdio'},
        ],
      });
      expect(s.servers.single.status, 'unknown');
      expect(s.servers.single.toolCount, 0);
    });
  });

  group('parseMcpTools / groupToolsByServer', () {
    test('parses a flat tool list, skipping nameless entries', () {
      final List<McpTool> tools = parseMcpTools(<Map<String, dynamic>>[
        <String, dynamic>{
          'server_id': 'sec',
          'server_name': 'SEC',
          'name': 'search_filings',
          'description': 'Full-text search of EDGAR filings',
        },
        <String, dynamic>{'server_id': 'sec', 'name': ''}, // skipped
        <String, dynamic>{
          'server_id': 'news',
          'server_name': 'News',
          'name': 'headlines',
        },
      ]);
      expect(tools.length, 2);
      expect(tools.first.name, 'search_filings');
      expect(tools.first.description, 'Full-text search of EDGAR filings');
      expect(tools.last.description, ''); // tolerant of missing description
    });

    test('groups tools by their server id', () {
      final Map<String, List<McpTool>> g =
          groupToolsByServer(parseMcpTools(<Map<String, dynamic>>[
        <String, dynamic>{'server_id': 'sec', 'name': 'a'},
        <String, dynamic>{'server_id': 'sec', 'name': 'b'},
        <String, dynamic>{'server_id': 'news', 'name': 'c'},
      ]));
      expect(g.keys, containsAll(<String>['sec', 'news']));
      expect(g['sec']!.map((McpTool t) => t.name), <String>['a', 'b']);
      expect(g['news']!.single.name, 'c');
    });
  });

  group('McpScreen widget', () {
    testWidgets('renders a connected server with its live tools (MC-2)',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: McpScreen(
          fetcher: () async => <String, dynamic>{
            'enabled': true,
            'sdk_present': true,
            'servers': <Map<String, dynamic>>[
              <String, dynamic>{
                'id': 'sec',
                'name': 'SEC Filings',
                'transport': 'sse',
                'allowed_tools': <String>['search_filings'],
              },
            ],
            'live_status': <String, dynamic>{
              'sec': <String, dynamic>{'status': 'connected', 'tool_count': 1},
            },
          },
          toolsFetcher: () async => <Map<String, dynamic>>[
            <String, dynamic>{
              'server_id': 'sec',
              'server_name': 'SEC Filings',
              'name': 'search_filings',
              'description': 'Full-text search of EDGAR filings',
            },
          ],
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('MCP Tools'), findsOneWidget);
      expect(find.text('SEC Filings'), findsOneWidget);
      expect(find.text('connected'), findsOneWidget);
      // The live tool name + description both render.
      expect(find.text('search_filings'), findsWidgets);
      expect(find.text('Full-text search of EDGAR filings'), findsOneWidget);
    });

    testWidgets('shows disabled empty state when MCP is off',
        (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: McpScreen(
          fetcher: () async => <String, dynamic>{'enabled': false},
          toolsFetcher: () async => <Map<String, dynamic>>[],
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('MCP is disabled'), findsOneWidget);
    });
  });
}
