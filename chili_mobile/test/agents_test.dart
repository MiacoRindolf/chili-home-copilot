import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:chili_mobile/src/agents/agent.dart';
import 'package:chili_mobile/src/agents/agent_persistence.dart';
import 'package:chili_mobile/src/agents/agent_registry.dart';
import 'package:chili_mobile/src/agents/agents_screen.dart';

void main() {
  group('defaultAgents seed', () {
    final List<Agent> seed = defaultAgents();

    test('is non-empty and covers every real category', () {
      expect(seed, isNotEmpty);
      for (final AgentKind k in <AgentKind>[
        AgentKind.trading,
        AgentKind.brain,
        AgentKind.coding,
        AgentKind.system,
      ]) {
        expect(seed.any((Agent a) => a.kind == k), isTrue, reason: 'missing $k');
      }
    });

    test('ids are unique and include the headline agents', () {
      final Set<String> ids = seed.map((Agent a) => a.id).toSet();
      expect(ids.length, seed.length, reason: 'duplicate agent id');
      expect(ids, containsAll(<String>['auto-trader', 'learning-cycle', 'coding-autopilot']));
    });

    test('all seeded agents are builtin', () {
      expect(seed.every((Agent a) => a.builtin), isTrue);
    });
  });

  group('Agent json', () {
    test('roundtrips all fields', () {
      const Agent a = Agent(
        id: 'x',
        name: 'X',
        kind: AgentKind.coding,
        description: 'desc',
        status: AgentStatus.running,
        enabled: false,
        builtin: false,
        schedule: 'every 9s',
        config: <String, String>{'k': 'v'},
        killSwitch: 'flag=0',
        lastRun: '2026-06-03T10:00:00.000',
        lastResult: 'ok',
      );
      final Agent b = Agent.fromJson(a.toJson());
      expect(b.id, 'x');
      expect(b.kind, AgentKind.coding);
      expect(b.status, AgentStatus.running);
      expect(b.enabled, isFalse);
      expect(b.builtin, isFalse);
      expect(b.schedule, 'every 9s');
      expect(b.config['k'], 'v');
      expect(b.killSwitch, 'flag=0');
      expect(b.lastRun, '2026-06-03T10:00:00.000');
      expect(b.lastResult, 'ok');
    });

    test('copyWith can null out lastRun via sentinel', () {
      const Agent a = Agent(
        id: 'x',
        name: 'X',
        kind: AgentKind.system,
        description: '',
        lastRun: '2026-01-01',
      );
      expect(a.copyWith().lastRun, '2026-01-01'); // unchanged when omitted
      expect(a.copyWith(lastRun: null).lastRun, isNull); // explicit clear
    });
  });

  group('AgentRegistry', () {
    test('start sets running + stamps lastRun; stop reverts', () {
      final AgentRegistry r = AgentRegistry();
      const String id = 'auto-trader';
      expect(r.byId(id)!.status, AgentStatus.unknown);
      r.start(id);
      expect(r.byId(id)!.status, AgentStatus.running);
      expect(r.byId(id)!.lastRun, isNotNull);
      r.stop(id);
      expect(r.byId(id)!.status, AgentStatus.stopped);
    });

    test('disabled agent cannot start', () {
      final AgentRegistry r = AgentRegistry();
      const String id = 'momentum-live-runner'; // seeded disabled
      expect(r.byId(id)!.enabled, isFalse);
      r.start(id);
      expect(r.byId(id)!.status, AgentStatus.unknown);
    });

    test('disabling a running agent stops it', () {
      final AgentRegistry r = AgentRegistry();
      const String id = 'learning-cycle';
      r.start(id);
      expect(r.byId(id)!.status, AgentStatus.running);
      r.setEnabled(id, false);
      expect(r.byId(id)!.status, AgentStatus.stopped);
      expect(r.byId(id)!.enabled, isFalse);
    });

    test('runningCount reflects state', () {
      final AgentRegistry r = AgentRegistry();
      expect(r.runningCount, 0);
      r.start('auto-trader');
      r.start('learning-cycle');
      expect(r.runningCount, 2);
    });

    test('byKind filters', () {
      final AgentRegistry r = AgentRegistry();
      final List<Agent> trading = r.byKind(AgentKind.trading);
      expect(trading, isNotEmpty);
      expect(trading.every((Agent a) => a.kind == AgentKind.trading), isTrue);
      expect(r.byKind(null).length, r.agents.length);
    });

    test('setConfig updates a single knob', () {
      final AgentRegistry r = AgentRegistry();
      r.setConfig('auto-trader', 'tick_interval_s', '5');
      expect(r.byId('auto-trader')!.config['tick_interval_s'], '5');
    });

    test('notifies listeners on mutation', () {
      final AgentRegistry r = AgentRegistry();
      int n = 0;
      r.addListener(() => n++);
      r.start('auto-trader');
      r.setConfig('auto-trader', 'tick_interval_s', '7');
      expect(n, greaterThanOrEqualTo(2));
    });

    test('addCustom / remove protect built-ins', () {
      final AgentRegistry r = AgentRegistry();
      r.remove('auto-trader'); // builtin → protected
      expect(r.byId('auto-trader'), isNotNull);
      r.addCustom(const Agent(
        id: 'my-agent',
        name: 'My Agent',
        kind: AgentKind.custom,
        description: 'mine',
      ));
      expect(r.byId('my-agent'), isNotNull);
      expect(r.byId('my-agent')!.builtin, isFalse);
      r.remove('my-agent');
      expect(r.byId('my-agent'), isNull);
    });

    test('applySaved overlays user state but keeps fresh seed description', () {
      final AgentRegistry r = AgentRegistry();
      final String freshDesc = r.byId('auto-trader')!.description;
      r.applySaved(<Map<String, dynamic>>[
        <String, dynamic>{
          'id': 'auto-trader',
          'name': 'STALE NAME',
          'description': 'stale description',
          'kind': 'trading',
          'status': 'running',
          'enabled': false,
          'config': <String, String>{'tick_interval_s': '99'},
        },
        // unknown id → restored as custom
        <String, dynamic>{
          'id': 'ghost',
          'name': 'Ghost',
          'kind': 'custom',
          'description': 'restored',
        },
      ]);
      final Agent at = r.byId('auto-trader')!;
      expect(at.status, AgentStatus.running, reason: 'saved status applied');
      expect(at.enabled, isFalse, reason: 'saved enabled applied');
      expect(at.config['tick_interval_s'], '99', reason: 'saved config applied');
      expect(at.description, freshDesc, reason: 'seed description preserved');
      expect(r.byId('ghost'), isNotNull, reason: 'unknown id restored as custom');
      expect(r.byId('ghost')!.builtin, isFalse);
    });
  });

  group('AgentPersistence', () {
    setUp(() => SharedPreferences.setMockInitialValues(<String, Object>{}));

    test('save then load roundtrips', () async {
      final AgentRegistry r = AgentRegistry();
      r.start('auto-trader');
      await AgentPersistence.save(r.toJson());
      final List<Map<String, dynamic>> loaded = await AgentPersistence.load();
      expect(loaded, isNotEmpty);
      final AgentRegistry r2 = AgentRegistry();
      r2.applySaved(loaded);
      expect(r2.byId('auto-trader')!.status, AgentStatus.running);
    });

    test('empty list clears the key', () async {
      await AgentPersistence.save(<Map<String, dynamic>>[]);
      expect(await AgentPersistence.load(), isEmpty);
    });
  });

  group('AgentsScreen widget', () {
    setUp(() => SharedPreferences.setMockInitialValues(<String, Object>{}));

    testWidgets('renders header, list and a working Start control', (WidgetTester tester) async {
      final AgentRegistry r = AgentRegistry();
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(registry: r),
      ));
      await tester.pumpAndSettle();

      expect(find.text('Agents'), findsOneWidget);
      expect(find.text('Auto-Trader'), findsWidgets);
      expect(find.text('0 running'), findsOneWidget);

      // Auto-Trader is first → selected by default. Start it.
      await tester.tap(find.widgetWithText(FilledButton, 'Start'));
      await tester.pumpAndSettle();
      expect(r.byId('auto-trader')!.status, AgentStatus.running);
      expect(find.text('1 running'), findsOneWidget);
    });

    testWidgets('kind filter narrows the list', (WidgetTester tester) async {
      await tester.pumpWidget(const MaterialApp(home: AgentsScreen()));
      await tester.pumpAndSettle();

      // Coding agents exist; System agents exist. Filter to Coding and confirm
      // a coding agent shows while a system-only agent does not.
      await tester.tap(find.widgetWithText(ChoiceChip, 'Coding'));
      await tester.pumpAndSettle();
      expect(find.text('Coding Autopilot'), findsWidgets);
      expect(find.text('Broker Sync'), findsNothing);
    });
  });
}
