import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:chili_mobile/src/agents/agent.dart';
import 'package:chili_mobile/src/agents/agent_control_service.dart';
import 'package:chili_mobile/src/agents/agent_persistence.dart';
import 'package:chili_mobile/src/agents/agent_registry.dart';
import 'package:chili_mobile/src/agents/agent_status_service.dart';
import 'package:chili_mobile/src/agents/agents_screen.dart';
import 'package:chili_mobile/src/network/chili_api_client.dart';

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

    testWidgets('renders header, list and a working local Start control', (WidgetTester tester) async {
      final AgentRegistry r = AgentRegistry();
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(registry: r, livePolling: false),
      ));
      await tester.pumpAndSettle();

      expect(find.text('Agents'), findsOneWidget);
      expect(find.text('Auto-Trader'), findsWidgets);
      expect(find.text('0 running'), findsOneWidget);

      // Pick a local-only agent (not control-backed, not live) and Start it.
      await tester.tap(find.text('Crypto Stop Monitor').first);
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Start'));
      await tester.pumpAndSettle();
      expect(r.byId('crypto-stop-monitor')!.status, AgentStatus.running);
      expect(find.text('1 running'), findsOneWidget);
    });

    testWidgets('kind filter narrows the list', (WidgetTester tester) async {
      await tester.pumpWidget(const MaterialApp(
        home: AgentsScreen(livePolling: false),
      ));
      await tester.pumpAndSettle();

      // Coding agents exist; System agents exist. Filter to Coding and confirm
      // a coding agent shows while a system-only agent does not.
      await tester.tap(find.widgetWithText(ChoiceChip, 'Coding'));
      await tester.pumpAndSettle();
      expect(find.text('Coding Autopilot'), findsWidgets);
      expect(find.text('Broker Sync'), findsNothing);
    });

    testWidgets('injected live poller drives status + connection pill', (WidgetTester tester) async {
      final AgentRegistry r = AgentRegistry();
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(
          registry: r,
          livePoller: () async => <String, AgentLiveStatus>{
            'learning-cycle': const AgentLiveStatus(AgentStatus.running,
                detail: '12 patterns touched'),
          },
        ),
      ));
      await tester.pumpAndSettle();

      expect(r.byId('learning-cycle')!.status, AgentStatus.running);
      expect(find.text('Live'), findsOneWidget); // connection pill
      expect(find.text('1 running'), findsOneWidget);
    });

    testWidgets('read-only live agent (no control endpoint) has disabled Start', (WidgetTester tester) async {
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(
          livePoller: () async => const <String, AgentLiveStatus>{},
        ),
      ));
      await tester.pumpAndSettle();

      // Position Monitor is live-backed but has no control endpoint → read-only.
      await tester.tap(find.text('Position Monitor').first);
      await tester.pumpAndSettle();
      final FilledButton start = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Start'),
      );
      expect(start.onPressed, isNull, reason: 'no control endpoint → read-only');
      expect(find.text('LIVE'), findsWidgets);
    });

    testWidgets('compute-only control invokes backend without a dialog', (WidgetTester tester) async {
      final List<String> calls = <String>[];
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(
          livePoller: () async => const <String, AgentLiveStatus>{},
          controlInvoker: (String id, AgentAction action) async {
            calls.add('$id:${action.name}');
          },
        ),
      ));
      await tester.pumpAndSettle();

      // Filter to Coding so the (lazily-built) tile is on-screen, then select it.
      await tester.tap(find.widgetWithText(ChoiceChip, 'Coding'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Coding Autopilot').first);
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Start'));
      await tester.pumpAndSettle();

      expect(calls, <String>['coding-autopilot:start']);
      // No confirm dialog for compute-only agents.
      expect(find.text('Resume live trading'), findsNothing);
    });

    testWidgets('trading control requires confirm; Cancel aborts, Resume fires', (WidgetTester tester) async {
      final List<String> calls = <String>[];
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(
          livePoller: () async => const <String, AgentLiveStatus>{},
          controlInvoker: (String id, AgentAction action) async {
            calls.add('$id:${action.name}');
          },
        ),
      ));
      await tester.pumpAndSettle();

      // Auto-Trader is control-backed AND trading-gated (default selected first).
      await tester.tap(find.text('Auto-Trader').first);
      await tester.pumpAndSettle();

      // Tap Start → confirm dialog appears; Cancel → no backend call.
      await tester.tap(find.widgetWithText(FilledButton, 'Start'));
      await tester.pumpAndSettle();
      expect(find.text('Resume live trading'), findsOneWidget);
      await tester.tap(find.widgetWithText(TextButton, 'Cancel'));
      await tester.pumpAndSettle();
      expect(calls, isEmpty, reason: 'cancel must not call the backend');

      // Tap Start again → confirm → backend call fires.
      await tester.tap(find.widgetWithText(FilledButton, 'Start'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Resume live trading'));
      await tester.pumpAndSettle();
      expect(calls, <String>['auto-trader:start']);
    });
  });

  group('AgentControlService mapping (AGT-3)', () {
    test('control-backed / run-once / trading-confirm ids are all real agents', () {
      final Set<String> seeded = defaultAgents().map((Agent a) => a.id).toSet();
      expect(seeded.containsAll(controlBackedAgentIds), isTrue);
      expect(controlBackedAgentIds.containsAll(runOnceBackedAgentIds), isTrue);
      expect(controlBackedAgentIds.containsAll(tradingConfirmAgentIds), isTrue);
    });

    test('unsupported (id, action) pairs throw before any network call', () async {
      // Unsupported pairs short-circuit in the switch, so the client is never
      // touched — a real (unused) client is fine here.
      final AgentControlService svc = AgentControlService(ChiliApiClient());
      await expectLater(
        svc.invoke('auto-trader', AgentAction.runOnce),
        throwsUnsupportedError,
      );
      await expectLater(
        svc.invoke('no-such-agent', AgentAction.start),
        throwsUnsupportedError,
      );
    });
  });

  group('deriveAgentStatuses (AGT-2)', () {
    test('code-brain mode maps autopilot + watcher', () {
      final Map<String, AgentLiveStatus> s = deriveAgentStatuses(
        const AgentStatusInputs(codeStatus: <String, dynamic>{
          'runtime_state': <String, dynamic>{'mode': 'reactive'},
          'queue_depth': 3,
        }),
      );
      expect(s['coding-autopilot']!.status, AgentStatus.running);
      expect(s['coding-autopilot']!.detail, contains('reactive'));
      expect(s['task-watcher']!.status, AgentStatus.running);
    });

    test('paused code-brain mode → stopped', () {
      final Map<String, AgentLiveStatus> s = deriveAgentStatuses(
        const AgentStatusInputs(codeStatus: <String, dynamic>{
          'runtime_state': <String, dynamic>{'mode': 'paused'},
        }),
      );
      expect(s['coding-autopilot']!.status, AgentStatus.stopped);
    });

    test('learning cycle from context flag + last run', () {
      final Map<String, AgentLiveStatus> s = deriveAgentStatuses(
        const AgentStatusInputs(
          contextStatus: <String, dynamic>{
            'runtime_state': <String, dynamic>{'learning_enabled': true},
          },
          learnRuns: <Map<String, dynamic>>[
            <String, dynamic>{
              'ended_at': '2026-06-03T12:00:00.000',
              'success': true,
              'patterns_touched': 7,
            },
          ],
        ),
      );
      final AgentLiveStatus lc = s['learning-cycle']!;
      expect(lc.status, AgentStatus.running);
      expect(lc.lastRun, '2026-06-03T12:00:00.000');
      expect(lc.detail, contains('7'));
    });

    test('momentum runner: active sessions → running', () {
      final Map<String, AgentLiveStatus> s = deriveAgentStatuses(
        const AgentStatusInputs(
          momentumSummary: <String, dynamic>{'active': 2},
        ),
      );
      expect(s['momentum-live-runner']!.status, AgentStatus.running);
      expect(s['momentum-live-runner']!.detail, contains('2'));
    });

    test('position monitor reachable → running with last check', () {
      final Map<String, AgentLiveStatus> s = deriveAgentStatuses(
        const AgentStatusInputs(
          monitorActive: <String, dynamic>{
            'summary': <String, dynamic>{
              'active_count': 4,
              'last_check': '2026-06-03T11:59:00.000',
            },
          },
        ),
      );
      expect(s['position-monitor']!.status, AgentStatus.running);
      expect(s['position-monitor']!.lastRun, '2026-06-03T11:59:00.000');
    });

    test('empty inputs → no entries (agents stay unknown)', () {
      final Map<String, AgentLiveStatus> s =
          deriveAgentStatuses(const AgentStatusInputs());
      expect(s, isEmpty);
    });

    test('all live-backed ids are real seeded agents', () {
      final Set<String> seeded =
          defaultAgents().map((Agent a) => a.id).toSet();
      expect(seeded.containsAll(liveBackedAgentIds), isTrue);
    });
  });

  group('AgentRegistry.applyLiveStatus', () {
    test('applies status + lastRun + detail', () {
      final AgentRegistry r = AgentRegistry();
      r.applyLiveStatus('learning-cycle', AgentStatus.running,
          lastRun: '2026-06-03T12:00:00.000', lastResult: '7 patterns');
      final Agent a = r.byId('learning-cycle')!;
      expect(a.status, AgentStatus.running);
      expect(a.lastRun, '2026-06-03T12:00:00.000');
      expect(a.lastResult, '7 patterns');
    });

    test('is a no-op when nothing changed (no extra notifications)', () {
      final AgentRegistry r = AgentRegistry();
      r.applyLiveStatus('learning-cycle', AgentStatus.running,
          lastRun: '2026-06-03T12:00:00.000');
      int n = 0;
      r.addListener(() => n++);
      r.applyLiveStatus('learning-cycle', AgentStatus.running,
          lastRun: '2026-06-03T12:00:00.000');
      expect(n, 0, reason: 'identical reading should not notify');
    });
  });

  group('makeAgentId (AGT-4)', () {
    test('kebab-cases and strips punctuation', () {
      expect(makeAgentId('My Cool Bot', <String>{}), 'my-cool-bot');
      expect(makeAgentId('  Auto-Trader!!!  ', <String>{}), 'auto-trader');
    });

    test('dedups against existing ids', () {
      expect(makeAgentId('x', <String>{'x'}), 'x-2');
      expect(makeAgentId('x', <String>{'x', 'x-2'}), 'x-3');
    });

    test('falls back to "agent" when name has no usable chars', () {
      expect(makeAgentId('!!!', <String>{}), 'agent');
    });
  });

  group('AgentRegistry.upsertCustom (AGT-4)', () {
    test('adds a new custom agent (forced non-builtin)', () {
      final AgentRegistry r = AgentRegistry();
      r.upsertCustom(const Agent(
        id: 'c1',
        name: 'C1',
        kind: AgentKind.custom,
        description: 'd',
        builtin: true, // should be coerced to false
      ));
      expect(r.byId('c1'), isNotNull);
      expect(r.byId('c1')!.builtin, isFalse);
    });

    test('updates editable fields but preserves runtime state', () {
      final AgentRegistry r = AgentRegistry();
      r.upsertCustom(const Agent(
          id: 'c1', name: 'C1', kind: AgentKind.custom, description: 'd'));
      r.start('c1');
      expect(r.byId('c1')!.status, AgentStatus.running);
      r.upsertCustom(const Agent(
        id: 'c1',
        name: 'C1 renamed',
        kind: AgentKind.brain,
        description: 'd2',
        schedule: 'daily',
        config: <String, String>{'k': 'v'},
      ));
      final Agent a = r.byId('c1')!;
      expect(a.name, 'C1 renamed');
      expect(a.kind, AgentKind.brain);
      expect(a.schedule, 'daily');
      expect(a.config['k'], 'v');
      expect(a.status, AgentStatus.running, reason: 'runtime state preserved');
    });

    test('never modifies a built-in agent', () {
      final AgentRegistry r = AgentRegistry();
      r.upsertCustom(const Agent(
          id: 'auto-trader',
          name: 'HACKED',
          kind: AgentKind.custom,
          description: 'x'));
      expect(r.byId('auto-trader')!.name, 'Auto-Trader');
    });
  });

  group('Custom agent UI (AGT-4)', () {
    setUp(() => SharedPreferences.setMockInitialValues(<String, Object>{}));

    testWidgets('New → fill → Create adds a selectable agent', (WidgetTester tester) async {
      final AgentRegistry r = AgentRegistry();
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(registry: r, livePolling: false),
      ));
      await tester.pumpAndSettle();
      final int before = r.agents.length;

      await tester.tap(find.widgetWithText(TextButton, 'New'));
      await tester.pumpAndSettle();
      final Finder name = find
          .descendant(of: find.byType(AlertDialog), matching: find.byType(TextField))
          .first;
      await tester.enterText(name, 'My Bot');
      await tester.tap(find.widgetWithText(FilledButton, 'Create'));
      await tester.pumpAndSettle();

      expect(r.agents.length, before + 1);
      expect(r.byId('my-bot'), isNotNull);
      expect(r.byId('my-bot')!.builtin, isFalse);
    });

    testWidgets('empty name is rejected', (WidgetTester tester) async {
      final AgentRegistry r = AgentRegistry();
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(registry: r, livePolling: false),
      ));
      await tester.pumpAndSettle();
      final int before = r.agents.length;

      await tester.tap(find.widgetWithText(TextButton, 'New'));
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Create'));
      await tester.pumpAndSettle();

      expect(find.text('Name is required'), findsOneWidget);
      expect(r.agents.length, before, reason: 'nothing created');
    });

    testWidgets('delete a custom agent via its menu', (WidgetTester tester) async {
      final AgentRegistry r = AgentRegistry(seed: <Agent>[
        ...defaultAgents(),
        const Agent(
          id: 'my-bot',
          name: 'My Bot',
          kind: AgentKind.custom,
          description: 'mine',
          builtin: false,
        ),
      ]);
      await tester.pumpWidget(MaterialApp(
        home: AgentsScreen(registry: r, livePolling: false),
      ));
      await tester.pumpAndSettle();

      // Filter to Custom so the lone custom agent is on-screen, then select it.
      await tester.tap(find.widgetWithText(ChoiceChip, 'Custom'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('My Bot').first); // select it
      await tester.pumpAndSettle();
      await tester.tap(find.byIcon(Icons.more_vert));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Delete').last);
      await tester.pumpAndSettle();
      await tester.tap(find.widgetWithText(FilledButton, 'Delete'));
      await tester.pumpAndSettle();

      expect(r.byId('my-bot'), isNull);
    });
  });
}
