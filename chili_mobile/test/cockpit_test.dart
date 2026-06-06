import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:chili_mobile/src/cockpit/cockpit_screen.dart';
import 'package:chili_mobile/src/cockpit/sparkline.dart';
import 'package:chili_mobile/src/cockpit/trading_models.dart';
import 'package:chili_mobile/src/notifications/notification_center.dart';

void main() {
  group('sparklinePoints (TC-2)', () {
    test('maps a rising series across the box, inverting y', () {
      final List<Offset> p =
          sparklinePoints(<double>[0, 5, 10], const Size(100, 50));
      expect(p.length, 3);
      expect(p.first.dx, 0);
      expect(p.last.dx, 100);
      expect(p.first.dy, 50); // lowest value → bottom
      expect(p.last.dy, 0); // highest value → top
      expect(p[1].dy, closeTo(25, 1e-9)); // midpoint
    });

    test('flat series sits mid-height; <2 points → empty', () {
      final List<Offset> flat =
          sparklinePoints(<double>[7, 7, 7], const Size(60, 40));
      expect(flat.every((Offset o) => o.dy == 20), isTrue);
      expect(sparklinePoints(<double>[1], const Size(60, 40)), isEmpty);
      expect(sparklinePoints(<double>[], const Size(60, 40)), isEmpty);
    });
  });

  group('sortPositions (TC-3)', () {
    Position pos(String ticker,
            {double pnl = 0, double pct = 0, double value = 0}) =>
        Position(
          ticker: ticker,
          qty: 1,
          entryPrice: 1,
          currentPrice: 1,
          marketValue: value,
          unrealizedPnl: pnl,
          unrealizedPnlPct: pct,
          venue: '',
        );

    final List<Position> sample = <Position>[
      pos('AAPL', pnl: 150, pct: 10, value: 1650),
      pos('TSLA', pnl: -300, pct: -5, value: 9000),
      pos('NVDA', pnl: 500, pct: 2, value: 4000),
    ];

    test('P/L sorts biggest-first', () {
      expect(
          sortPositions(sample, PositionSort.pnl)
              .map((Position p) => p.ticker),
          <String>['NVDA', 'AAPL', 'TSLA']);
    });

    test('P/L % sorts biggest-first', () {
      expect(
          sortPositions(sample, PositionSort.pnlPct)
              .map((Position p) => p.ticker),
          <String>['AAPL', 'NVDA', 'TSLA']);
    });

    test('Value sorts biggest-first', () {
      expect(
          sortPositions(sample, PositionSort.value)
              .map((Position p) => p.ticker),
          <String>['TSLA', 'NVDA', 'AAPL']);
    });

    test('Ticker sorts A→Z, case-insensitive', () {
      expect(
          sortPositions(sample, PositionSort.ticker)
              .map((Position p) => p.ticker),
          <String>['AAPL', 'NVDA', 'TSLA']);
    });

    test('does not mutate the input list and breaks ties by ticker', () {
      final List<Position> input = <Position>[
        pos('ZZZ', pnl: 100),
        pos('AAA', pnl: 100), // tie on P/L → ticker decides
      ];
      final List<Position> sorted = sortPositions(input, PositionSort.pnl);
      expect(sorted.map((Position p) => p.ticker), <String>['AAA', 'ZZZ']);
      // original order preserved (purity).
      expect(input.map((Position p) => p.ticker), <String>['ZZZ', 'AAA']);
    });

    test('positionSortLabel covers every variant', () {
      for (final PositionSort s in PositionSort.values) {
        expect(positionSortLabel(s), isNotEmpty);
      }
    });
  });

  group('venue filter (TC-4)', () {
    Position pos(String ticker, String venue) => Position(
          ticker: ticker,
          qty: 1,
          entryPrice: 1,
          currentPrice: 1,
          marketValue: 1,
          unrealizedPnl: 0,
          unrealizedPnlPct: 0,
          venue: venue,
        );

    final List<Position> sample = <Position>[
      pos('AAPL', 'robinhood'),
      pos('BTC', 'coinbase'),
      pos('TSLA', 'robinhood'),
      pos('NOVENUE', ''),
    ];

    test('venuesOf returns distinct non-empty venues, sorted', () {
      expect(venuesOf(sample), <String>['coinbase', 'robinhood']);
      expect(venuesOf(const <Position>[]), isEmpty);
    });

    test('filterPositionsByVenue is case-insensitive', () {
      expect(
          filterPositionsByVenue(sample, 'ROBINHOOD')
              .map((Position p) => p.ticker),
          <String>['AAPL', 'TSLA']);
      expect(
          filterPositionsByVenue(sample, 'coinbase')
              .map((Position p) => p.ticker),
          <String>['BTC']);
    });

    test('null / empty venue returns all (and does not mutate input)', () {
      expect(filterPositionsByVenue(sample, null).length, sample.length);
      expect(filterPositionsByVenue(sample, '  ').length, sample.length);
      final List<Position> copy = filterPositionsByVenue(sample, null);
      copy.clear();
      expect(sample.length, 4); // original untouched
    });
  });

  group('cashFractionOfEquity (TC-6)', () {
    test('cash / equity, clamped, divide-by-zero safe', () {
      expect(cashFractionOfEquity(2500, 10000), closeTo(0.25, 1e-9));
      expect(cashFractionOfEquity(0, 10000), 0);
      expect(cashFractionOfEquity(5000, 0), 0); // no equity → 0, not NaN/inf
      expect(cashFractionOfEquity(-100, 10000), 0); // clamped low
      expect(cashFractionOfEquity(20000, 10000), 1); // clamped high
    });
  });

  group('venueExposures (TC-5)', () {
    Position pos(String ticker, String venue, double mv, double pnl) =>
        Position(
          ticker: ticker,
          qty: 1,
          entryPrice: 1,
          currentPrice: 1,
          marketValue: mv,
          unrealizedPnl: pnl,
          unrealizedPnlPct: 0,
          venue: venue,
        );

    test('aggregates market value + P/L per venue, sorted by value desc', () {
      final List<VenueExposure> exp = venueExposures(<Position>[
        pos('AAPL', 'robinhood', 1000, 50),
        pos('TSLA', 'robinhood', 2000, -100),
        pos('BTC', 'coinbase', 6000, 300),
      ]);
      expect(exp.length, 2);
      // coinbase (6000) sorts before robinhood (3000).
      expect(exp.first.venue, 'coinbase');
      expect(exp.first.marketValue, 6000);
      expect(exp.first.unrealizedPnl, 300);
      expect(exp.first.count, 1);
      expect(exp.first.share, closeTo(6000 / 9000, 1e-9));

      final VenueExposure rh = exp[1];
      expect(rh.venue, 'robinhood');
      expect(rh.marketValue, 3000);
      expect(rh.unrealizedPnl, -50);
      expect(rh.count, 2);
      expect(rh.share, closeTo(3000 / 9000, 1e-9));
    });

    test('blank venue groups under "other"; zero total → zero shares', () {
      final List<VenueExposure> exp = venueExposures(<Position>[
        pos('X', '', 0, 0),
        pos('Y', 'coinbase', 0, 0),
      ]);
      expect(exp.map((VenueExposure e) => e.venue), containsAll(<String>['other', 'coinbase']));
      expect(exp.every((VenueExposure e) => e.share == 0), isTrue);
    });

    test('empty positions → empty', () {
      expect(venueExposures(const <Position>[]), isEmpty);
    });
  });

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
      // TC-6 — cash / buying-power / cash-weight tiles.
      expect(find.text('Cash'), findsOneWidget);
      expect(find.text('Buying power'), findsOneWidget);
      expect(find.text('\$5,000.00'), findsOneWidget); // cash
      expect(find.text('Cash weight'), findsOneWidget);
      expect(find.text('20%'), findsOneWidget); // 5000 / 25000
      // TC-8 — realized / unrealized / total P&L breakdown.
      expect(find.text('REALIZED'), findsOneWidget);
      expect(find.text('TOTAL'), findsOneWidget);
      expect(find.text('+\$600.00'), findsOneWidget); // realized
      expect(find.text('+\$1,000.00'), findsOneWidget); // total
    });

    testWidgets('TC-3: positions render in default P/L-desc order with a sorter',
        (WidgetTester tester) async {
      tester.view.physicalSize = const Size(1100, 900);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      Position pos(String t, double pnl) => Position(
            ticker: t,
            qty: 1,
            entryPrice: 1,
            currentPrice: 1,
            marketValue: 100,
            unrealizedPnl: pnl,
            unrealizedPnlPct: pnl,
            venue: '',
          );
      await tester.pumpWidget(MaterialApp(
        home: CockpitScreen(
          fetcher: () async => TradingSnapshot(
            totalEquity: 1000,
            cash: 0,
            buyingPower: 0,
            dayPnl: 0,
            totalPnl: 0,
            realizedPnl: 0,
            unrealizedPnl: 0,
            killSwitchActive: false,
            killSwitchReason: '',
            automationEnabled: true,
            ensembleMode: '',
            breakerTripped: false,
            breakerReason: '',
            totalHeatPct: 0,
            positions: <Position>[pos('LOSS', -50), pos('WIN', 200)],
          ),
        ),
      ));
      await tester.pumpAndSettle();
      // Sort selector chip is visible (>1 position).
      expect(find.byTooltip('Sort positions'), findsOneWidget);
      // Default P/L-desc → WIN row sits above LOSS row.
      final double winY = tester.getTopLeft(find.text('WIN')).dy;
      final double lossY = tester.getTopLeft(find.text('LOSS')).dy;
      expect(winY, lessThan(lossY));
    });

    testWidgets('TC-5: exposure-by-venue panel renders with ≥2 venues',
        (WidgetTester tester) async {
      tester.view.physicalSize = const Size(1100, 1000);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      Position pos(String t, String venue, double mv) => Position(
            ticker: t,
            qty: 1,
            entryPrice: 1,
            currentPrice: 1,
            marketValue: mv,
            unrealizedPnl: 1,
            unrealizedPnlPct: 1,
            venue: venue,
          );
      await tester.pumpWidget(MaterialApp(
        home: CockpitScreen(
          fetcher: () async => TradingSnapshot(
            totalEquity: 1000,
            cash: 0,
            buyingPower: 0,
            dayPnl: 0,
            totalPnl: 0,
            realizedPnl: 0,
            unrealizedPnl: 0,
            killSwitchActive: false,
            killSwitchReason: '',
            automationEnabled: true,
            ensembleMode: '',
            breakerTripped: false,
            breakerReason: '',
            totalHeatPct: 0,
            positions: <Position>[
              pos('AAPL', 'robinhood', 3000),
              pos('BTC', 'coinbase', 1000),
            ],
          ),
        ),
      ));
      await tester.pumpAndSettle();
      expect(find.text('EXPOSURE BY VENUE'), findsOneWidget); // ApSectionHeader uppercases
      // robinhood 3000/4000 = 75%, coinbase 1000/4000 = 25%.
      expect(find.text('75%'), findsOneWidget);
      expect(find.text('25%'), findsOneWidget);
      expect(find.text('robinhood · 1 pos'), findsOneWidget);
    });

    testWidgets('TC-7: tapping a position fires onDiscussPosition',
        (WidgetTester tester) async {
      tester.view.physicalSize = const Size(1100, 900);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      Position? discussed;
      await tester.pumpWidget(MaterialApp(
        home: CockpitScreen(
          onDiscussPosition: (Position p) => discussed = p,
          fetcher: () async => const TradingSnapshot(
            totalEquity: 1000,
            cash: 0,
            buyingPower: 0,
            dayPnl: 0,
            totalPnl: 0,
            realizedPnl: 0,
            unrealizedPnl: 0,
            killSwitchActive: false,
            killSwitchReason: '',
            automationEnabled: true,
            ensembleMode: '',
            breakerTripped: false,
            breakerReason: '',
            totalHeatPct: 0,
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
      await tester.tap(find.text('AAPL'));
      await tester.pump();
      expect(discussed, isNotNull);
      expect(discussed!.ticker, 'AAPL');
    });

    testWidgets('TC-4: venue chips filter the open-positions list',
        (WidgetTester tester) async {
      tester.view.physicalSize = const Size(1100, 900);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      Position pos(String t, String venue) => Position(
            ticker: t,
            qty: 1,
            entryPrice: 1,
            currentPrice: 1,
            marketValue: 100,
            unrealizedPnl: 1,
            unrealizedPnlPct: 1,
            venue: venue,
          );
      await tester.pumpWidget(MaterialApp(
        home: CockpitScreen(
          fetcher: () async => TradingSnapshot(
            totalEquity: 1000,
            cash: 0,
            buyingPower: 0,
            dayPnl: 0,
            totalPnl: 0,
            realizedPnl: 0,
            unrealizedPnl: 0,
            killSwitchActive: false,
            killSwitchReason: '',
            automationEnabled: true,
            ensembleMode: '',
            breakerTripped: false,
            breakerReason: '',
            totalHeatPct: 0,
            positions: <Position>[
              pos('AAPL', 'robinhood'),
              pos('BTC', 'coinbase'),
            ],
          ),
        ),
      ));
      await tester.pumpAndSettle();
      // Both venues present → filter chips show, both tickers visible.
      expect(find.widgetWithText(ChoiceChip, 'All'), findsOneWidget);
      expect(find.widgetWithText(ChoiceChip, 'coinbase'), findsOneWidget);
      expect(find.text('AAPL'), findsOneWidget);
      expect(find.text('BTC'), findsOneWidget);

      // Tap "coinbase" → only BTC remains.
      await tester.tap(find.widgetWithText(ChoiceChip, 'coinbase'));
      await tester.pumpAndSettle();
      expect(find.text('BTC'), findsOneWidget);
      expect(find.text('AAPL'), findsNothing);
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
