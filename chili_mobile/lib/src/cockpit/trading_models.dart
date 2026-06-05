// Live Trading Cockpit data model (TC-1). Combines four read-only backend
// endpoints into one snapshot. Pure + tolerant → unit-testable without network.

class Position {
  const Position({
    required this.ticker,
    required this.qty,
    required this.entryPrice,
    required this.currentPrice,
    required this.marketValue,
    required this.unrealizedPnl,
    required this.unrealizedPnlPct,
    required this.venue,
  });

  final String ticker;
  final double qty;
  final double entryPrice;
  final double currentPrice;
  final double marketValue;
  final double unrealizedPnl;
  final double unrealizedPnlPct;
  final String venue; // robinhood | coinbase
}

class TradingSnapshot {
  const TradingSnapshot({
    required this.totalEquity,
    required this.cash,
    required this.buyingPower,
    required this.dayPnl,
    required this.totalPnl,
    required this.realizedPnl,
    required this.unrealizedPnl,
    required this.killSwitchActive,
    required this.killSwitchReason,
    required this.automationEnabled,
    required this.ensembleMode,
    required this.breakerTripped,
    required this.breakerReason,
    required this.totalHeatPct,
    required this.positions,
  });

  final double totalEquity;
  final double cash;
  final double buyingPower;
  final double dayPnl;
  final double totalPnl;
  final double realizedPnl;
  final double unrealizedPnl;

  final bool killSwitchActive;
  final String killSwitchReason;
  final bool automationEnabled;
  final String ensembleMode;

  final bool breakerTripped;
  final String breakerReason;
  final double totalHeatPct;

  final List<Position> positions;

  static const TradingSnapshot empty = TradingSnapshot(
    totalEquity: 0,
    cash: 0,
    buyingPower: 0,
    dayPnl: 0,
    totalPnl: 0,
    realizedPnl: 0,
    unrealizedPnl: 0,
    killSwitchActive: false,
    killSwitchReason: '',
    automationEnabled: false,
    ensembleMode: '',
    breakerTripped: false,
    breakerReason: '',
    totalHeatPct: 0,
    positions: <Position>[],
  );
}

/// Combine the four endpoint payloads into a snapshot.
/// - positions: GET /api/trading/broker/positions  → {positions:[...]}
/// - portfolio: GET /api/trading/broker/portfolio  → {portfolio:{...}}
/// - governance: GET /api/trading/brain/governance → {kill_switch_active,...}
/// - risk: GET /api/trading/risk/budget            → {circuit_breaker:{...},...}
TradingSnapshot buildTradingSnapshot({
  Map<String, dynamic> positions = const <String, dynamic>{},
  Map<String, dynamic> portfolio = const <String, dynamic>{},
  Map<String, dynamic> governance = const <String, dynamic>{},
  Map<String, dynamic> risk = const <String, dynamic>{},
}) {
  final Map<String, dynamic> p = _map(portfolio['portfolio']);
  final Map<String, dynamic> breaker = _map(risk['circuit_breaker']);
  return TradingSnapshot(
    totalEquity: _d(p['total_equity']),
    cash: _d(p['cash']),
    buyingPower: _d(p['buying_power']),
    dayPnl: _d(p['day_pnl']),
    totalPnl: _d(p['total_pnl']),
    realizedPnl: _d(p['realized_pnl']),
    unrealizedPnl: _d(p['unrealized_pnl']),
    killSwitchActive: governance['kill_switch_active'] == true,
    killSwitchReason: _s(governance['kill_switch_reason']),
    automationEnabled: governance['automation_enabled'] == true,
    ensembleMode: _s(governance['ensemble_mode']),
    breakerTripped: breaker['tripped'] == true,
    breakerReason: _s(breaker['reason']),
    totalHeatPct: _d(risk['total_heat_pct']),
    positions: <Position>[
      for (final Object? raw
          in (positions['positions'] as List? ?? const <Object?>[]))
        if (raw is Map) _position(Map<String, dynamic>.from(raw)),
    ],
  );
}

Position _position(Map<String, dynamic> j) => Position(
      ticker: _s(j['ticker']).isEmpty ? _s(j['symbol']) : _s(j['ticker']),
      qty: _d(j['qty'] ?? j['quantity']),
      entryPrice: _d(j['entry_price']),
      currentPrice: _d(j['current_price']),
      marketValue: _d(j['market_value']),
      unrealizedPnl: _d(j['unrealized_pnl']),
      unrealizedPnlPct: _d(j['unrealized_pnl_pct']),
      venue: _s(j['venue'] ?? j['broker_source']),
    );

Map<String, dynamic> _map(Object? v) =>
    v is Map ? Map<String, dynamic>.from(v) : <String, dynamic>{};

double _d(Object? v) {
  if (v is num) return v.toDouble();
  if (v is String) return double.tryParse(v) ?? 0;
  return 0;
}

String _s(Object? v) => v?.toString().trim() ?? '';
