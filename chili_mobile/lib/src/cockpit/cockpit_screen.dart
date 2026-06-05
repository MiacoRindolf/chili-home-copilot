import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';
import '../realtime/live_channel.dart';
import '../realtime/live_sources.dart';
import '../realtime/live_status.dart';
import '../ui/app_ui.dart';
import 'trading_models.dart';

/// Fetches a combined trading snapshot — injectable for tests.
typedef TradingSnapshotFetcher = Future<TradingSnapshot> Function();

/// Live Trading Cockpit (TC-1): equity / P&L, kill-switch + drawdown-breaker
/// state, and open positions — streamed through the RT-1 real-time layer
/// (adaptive 4s active / 15s idle + backoff) with a live connection indicator.
class CockpitScreen extends StatefulWidget {
  const CockpitScreen({super.key, TradingSnapshotFetcher? fetcher})
      : _injectedFetcher = fetcher;

  final TradingSnapshotFetcher? _injectedFetcher;

  @override
  State<CockpitScreen> createState() => _CockpitScreenState();
}

class _CockpitScreenState extends State<CockpitScreen> {
  ChiliApiClient? _api;
  late final TradingSnapshotFetcher _fetcher;
  late final LiveChannel<TradingSnapshot> _channel;

  @override
  void initState() {
    super.initState();
    _api = widget._injectedFetcher == null ? ChiliApiClient() : null;
    _fetcher = widget._injectedFetcher ?? _liveFetch;
    _channel = LiveChannel<TradingSnapshot>(
      () => PollingLiveSource<TradingSnapshot>(
        _fetcher,
        activeInterval: const Duration(seconds: 4),
        idleInterval: const Duration(seconds: 15),
      ),
      dedupe: false,
    );
    _channel.addListener(_onTick);
    _channel.start();
  }

  void _onTick() {
    if (mounted) setState(() {});
  }

  Future<TradingSnapshot> _liveFetch() async {
    final List<Map<String, dynamic>> r = await Future.wait(<Future<Map<String, dynamic>>>[
      _api!.getBrokerPositions(),
      _api!.getBrokerPortfolio(),
      _api!.getTradingGovernance(),
      _api!.getRiskBudget(),
    ]);
    // Empty portfolio → backend unreachable → surface as offline (channel error).
    if ((r[1]['portfolio'] as Map?)?.isEmpty ?? r[1].isEmpty) {
      throw Exception('trading backend unreachable');
    }
    return buildTradingSnapshot(
      positions: r[0],
      portfolio: r[1],
      governance: r[2],
      risk: r[3],
    );
  }

  @override
  void dispose() {
    _channel.removeListener(_onTick);
    _channel.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    final TradingSnapshot? snap = _channel.value;
    return Scaffold(
      backgroundColor: cs.surface,
      body: Column(
        children: <Widget>[
          _header(cs),
          const Divider(height: 1),
          Expanded(
            child: snap == null
                ? _firstLoad(cs)
                : _body(cs, snap),
          ),
        ],
      ),
    );
  }

  Widget _firstLoad(ColorScheme cs) {
    if (_channel.status == LiveStatus.error ||
        _channel.status == LiveStatus.offline) {
      return ApEmptyState(
        icon: Icons.cloud_off,
        message: 'Trading backend offline',
        detail: 'Reconnecting…',
        action: FilledButton.icon(
          onPressed: _channel.refresh,
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Retry now'),
        ),
      );
    }
    return const Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          CircularProgressIndicator(),
          SizedBox(height: 12),
          Text('Connecting to the trading desk…'),
        ],
      ),
    );
  }

  Widget _header(ColorScheme cs) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 14, 12, 12),
      child: Row(
        children: <Widget>[
          Icon(Icons.candlestick_chart, color: cs.primary),
          const SizedBox(width: 10),
          Text('Cockpit',
              style: Theme.of(context)
                  .textTheme
                  .headlineSmall
                  ?.copyWith(fontWeight: FontWeight.w700)),
          const Spacer(),
          _LivePill(status: _channel.status),
          IconButton(
            tooltip: 'Refresh now',
            icon: const Icon(Icons.refresh, size: 20),
            onPressed: _channel.refresh,
          ),
        ],
      ),
    );
  }

  Widget _body(ColorScheme cs, TradingSnapshot s) {
    return ListView(
      padding: const EdgeInsets.all(20),
      children: <Widget>[
        // Equity / P&L tiles.
        Row(
          children: <Widget>[
            Expanded(
              child: ApStatCard(
                label: 'Total equity',
                value: _money(s.totalEquity),
                icon: Icons.account_balance_wallet_outlined,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: ApStatCard(
                label: 'Day P&L',
                value: _signed(s.dayPnl),
                icon: s.dayPnl >= 0 ? Icons.trending_up : Icons.trending_down,
                color: _pnlColor(s.dayPnl, cs),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: ApStatCard(
                label: 'Unrealized P&L',
                value: _signed(s.unrealizedPnl),
                icon: Icons.show_chart,
                color: _pnlColor(s.unrealizedPnl, cs),
              ),
            ),
          ],
        ),
        const SizedBox(height: 14),
        // Risk / governance pills.
        Wrap(
          spacing: 10,
          runSpacing: 8,
          children: <Widget>[
            ApStatusPill(
              s.killSwitchActive ? 'Kill switch ON' : 'Kill switch off',
              color: s.killSwitchActive ? Colors.red : Colors.green,
              icon: s.killSwitchActive ? Icons.block : Icons.shield_outlined,
            ),
            ApStatusPill(
              s.breakerTripped ? 'Breaker TRIPPED' : 'Breaker ok',
              color: s.breakerTripped ? Colors.red : Colors.green,
              icon: s.breakerTripped
                  ? Icons.dangerous_outlined
                  : Icons.verified_outlined,
            ),
            ApStatusPill(
              s.automationEnabled ? 'Automation on' : 'Automation off',
              color: s.automationEnabled ? Colors.green : cs.onSurfaceVariant,
              icon: Icons.bolt,
            ),
            if (s.ensembleMode.isNotEmpty)
              ApStatusPill('Ensemble: ${s.ensembleMode}', color: cs.secondary),
            ApStatusPill('Heat ${s.totalHeatPct.toStringAsFixed(1)}%',
                color: s.totalHeatPct > 80 ? Colors.red : cs.onSurfaceVariant),
          ],
        ),
        if (s.killSwitchActive && s.killSwitchReason.isNotEmpty) ...<Widget>[
          const SizedBox(height: 8),
          Text('Kill switch: ${s.killSwitchReason}',
              style: TextStyle(color: cs.error, fontSize: 12)),
        ],
        if (s.breakerTripped && s.breakerReason.isNotEmpty) ...<Widget>[
          const SizedBox(height: 4),
          Text('Breaker: ${s.breakerReason}',
              style: TextStyle(color: cs.error, fontSize: 12)),
        ],
        const SizedBox(height: 20),
        ApSectionHeader('Open positions · ${s.positions.length}',
            icon: Icons.list_alt),
        const SizedBox(height: 6),
        if (s.positions.isEmpty)
          Text('No open positions.',
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant))
        else
          ApPanel(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
            child: Column(
              children: <Widget>[
                for (int i = 0; i < s.positions.length; i++) ...<Widget>[
                  if (i > 0) Divider(height: 14, color: cs.outlineVariant),
                  _positionRow(cs, s.positions[i]),
                ],
              ],
            ),
          ),
        const SizedBox(height: 24),
      ],
    );
  }

  Widget _positionRow(ColorScheme cs, Position p) {
    final Color pnlColor = _pnlColor(p.unrealizedPnl, cs);
    return Row(
      children: <Widget>[
        SizedBox(
          width: 78,
          child: Text(p.ticker,
              style: TextStyle(
                  fontWeight: FontWeight.w700, color: cs.onSurface)),
        ),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(
                '${_qty(p.qty)} @ ${_money(p.entryPrice)} → ${_money(p.currentPrice)}',
                style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant),
              ),
              if (p.venue.isNotEmpty)
                Text(p.venue,
                    style: TextStyle(fontSize: 10, color: cs.onSurfaceVariant)),
            ],
          ),
        ),
        Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: <Widget>[
            Text(_signed(p.unrealizedPnl),
                style:
                    TextStyle(fontWeight: FontWeight.w700, color: pnlColor)),
            Text('${p.unrealizedPnlPct >= 0 ? '+' : ''}${p.unrealizedPnlPct.toStringAsFixed(2)}%',
                style: TextStyle(fontSize: 11, color: pnlColor)),
          ],
        ),
      ],
    );
  }

  // ── formatting ────────────────────────────────────────────────────────────
  static Color _pnlColor(double v, ColorScheme cs) {
    if (v > 0) return Colors.green;
    if (v < 0) return cs.error;
    return cs.onSurfaceVariant;
  }

  static String _signed(double v) =>
      '${v >= 0 ? '+' : '-'}${_money(v.abs())}';

  static String _money(double v) {
    final bool neg = v < 0;
    final String fixed = v.abs().toStringAsFixed(2);
    final List<String> parts = fixed.split('.');
    final String intPart = parts[0];
    final StringBuffer buf = StringBuffer();
    for (int i = 0; i < intPart.length; i++) {
      if (i > 0 && (intPart.length - i) % 3 == 0) buf.write(',');
      buf.write(intPart[i]);
    }
    return '${neg ? '-' : ''}\$$buf.${parts[1]}';
  }

  static String _qty(double q) =>
      q == q.roundToDouble() ? q.toStringAsFixed(0) : q.toStringAsFixed(4);
}

class _LivePill extends StatelessWidget {
  const _LivePill({required this.status});
  final LiveStatus status;

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    late final Color c;
    switch (status) {
      case LiveStatus.live:
        c = Colors.green;
      case LiveStatus.connecting:
      case LiveStatus.error:
        c = Colors.amber;
      case LiveStatus.offline:
      case LiveStatus.idle:
        c = cs.onSurfaceVariant;
    }
    return Padding(
      padding: const EdgeInsets.only(right: 4),
      child: ApStatusPill(status.label,
          color: c, icon: status.isHealthy ? Icons.circle : Icons.circle_outlined),
    );
  }
}
