import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/cockpit/cockpit_screen.dart';
import 'package:chili_mobile/src/cockpit/trading_models.dart';
import 'package:chili_mobile/src/notifications/notification_center.dart';

void main() {
  group('buildTradingSnapshot', () {
    test('combines positions / portfolio / governance / risk', () {
      final TradingSnapshot s = buildTradingSnapshot(
        positions: <String, dynamic>{
          'positions': <Map<String, dynamic>>[
            <String, dynamic>{
              'ticker': 'AAPL',
              'qty': 10,
              'entry_price': 150.0,
              'current_price': 165.5,
              'market_value': 1655.0,
              'unrealized_pnl': 155.0,
              'unrealized_pnl_pct': 10.33,
              'venue': 'robinhood',
            },
          ],
        },
        portfolio: <String, dynamic>{
          'portfolio': <String, dynamic>{
            'total_equity': 25000.0,
            'cash': 5000.0,
            'buying_power': 10000.0,
            'day_pnl': -123.45,
            'total_pnl': 1200.0,
            'realized_pnl': 800.0,
            'unrealized_pnl': 400.0,
          },
        },
        governance: <String, dynamic>{
          'kill_switch_active': true,
          'kill_switch_reason': 'manual halt',
          'automation_enabled': false,
          'ensemble_mode': 'shadow',
        },
        risk: <String, dynamic>{
          'total_heat_pct': 42.5,
          'circuit_breaker': <String, dynamic>{
            'tripped': false,
            'reason': '',
          },
        },
      );
      expect(s.totalEquity, 25000.0);
      expect(s.dayPnl, -123.45);
      expect(s.killSwitchActive, isTrue);
      expect(s.killSwitchReason, 'manual halt');
      expect(s.automationEnabled, isFalse);
      expect(s.ensembleMode, 'shadow');
      expect(s.breakerTripped, isFalse);
      expect(s.totalHeatPct, 42.5);
      expect(s.positions.length, 1);
      final Position p = s.positions.single;
      expect(p.ticker, 'AAPL');
      expect(p.qty, 10.0);
      expect(p.unrealizedPnl, 155.0);
      expect(p.venue, 'robinhood');
    });

    test('tolerates empty payloads', () {
      final TradingSnapshot s = buildTradingSnapshot();
      expect(s.totalEquity, 0);
      expect(s.positions, isEmpty);
      expect(s.killSwitchActive, isFalse);
      expect(s.breakerTripped, isFalse);
    });

    test('reads tripped breaker + symbol/broker_source fallbacks', () {
      final TradingSnapshot s = buildTradingSnapshot(
        positions: <String, dynamic>{
          'positions': <Map<String, dynamic>>[
            <String, dynamic>{'symbol': 'BTC-USD', 'quantity': 0.5, 'broker_source': 'coinbase'},
          ],
        },
        risk: <String, dynamic>{
          'circuit_breaker': <String, dynamic>{'tripped': true, 'reason': '5d dd'},
        },
      );
      expect(s.breakerTripped, isTrue);
      expect(s.breakerReason, '5d dd');
      expect(s.positions.single.ticker, 'BTC-USD'); // symbol fallback
      expect(s.positions.single.qty, 0.5); // quantity fallback
      expect(s.positions.single.venue, 'coinbase'); // broker_source fallback
    });
  });

  group('CockpitScreen widget', () {
    testWidgets('renders equity, P&L, risk pills and a position',
        (WidgetTester tester) async {
      // Cockpit's default window is ~900px wide; give the test similar room.
      tester.view.physicalSize = const Size(1100, 800);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(MaterialApp(
        home: CockpitScreen(
          fetcher: () async => const TradingSnapshot(
            totalEquity: 25000,
            cash: 5000,
            buyingPower: 10000,
            dayPnl: 250.5,
            totalPnl: 1000,
            realizedPnl: 600,
            unrealizedPnl: 400,
            killSwitchActive: false,
            killSwitchReason: '',
            automationEnabled: true,
            ensembleMode: 'live',
            breakerTripped: false,
            breakerReason: '',
            totalHeatPct: 30,
            positions: <Position>[
              Position(
                ticker: 'AAPL',
                qty: 10,
                entryPrice: 150,
                currentPrice: 165,
                marketValue: 1650,
                unrealizedPnl: 150,
                unrealizedPnlPct: 10,
                venue: 'robinhood',
              ),
            ],
          ),
        ),
      ));
      await tester.pumpAndSettle();

      expect(find.text('Cockpit'), findsOneWidget);
      expect(find.text('Total equity'), findsOneWidget);
      expect(find.text('\$25,000.00'), findsOneWidget); // grouped money
      expect(find.text('+\$250.50'), findsOneWidget); // signed day P&L
      expect(find.text('Kill switch off'), findsOneWidget);
      expect(find.text('Breaker ok'), findsOneWidget);
      expect(find.text('AAPL'), findsOneWidget);
    });

    testWidgets('NC-2: a kill-switch transition pushes a notification',
        (WidgetTester tester) async {
      final NotificationCenter nc = NotificationCenter();
      int call = 0;
      TradingSnapshot snap({required bool kill}) => TradingSnapshot(
            totalEquity: 1000,
            cash: 0,
            buyingPower: 0,
            dayPnl: 0,
            totalPnl: 0,
            realizedPnl: 0,
            unrealizedPnl: 0,
            killSwitchActive: kill,
            killSwitchReason: kill ? 'manual halt' : '',
            automationEnabled: true,
            ensembleMode: '',
            breakerTripped: false,
            breakerReason: '',
            totalHeatPct: 0,
            positions: const <Position>[],
          );

      await tester.pumpWidget(MaterialApp(
        home: CockpitScreen(
          notifications: nc,
          // 1st poll = baseline (off), subsequent polls = kill ON.
          fetcher: () async => snap(kill: (call++) >= 1),
        ),
      ));
      await tester.pumpAndSettle();
      expect(nc.unreadCount, 0, reason: 'first snapshot is baseline, no alert');

      // Advance past the 4s active poll interval → second snapshot (kill ON).
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();

      expect(nc.unreadCount, greaterThanOrEqualTo(1));
      expect(nc.items.first.title, 'Kill switch activated');
      expect(nc.items.first.kind, NotifKind.error);
    });
  });
}
