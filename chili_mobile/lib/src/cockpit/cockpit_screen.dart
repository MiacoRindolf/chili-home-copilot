import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';
import '../notifications/notification_center.dart';
import '../realtime/live_channel.dart';
import '../realtime/live_sources.dart';
import '../realtime/live_status.dart';
import '../ui/app_ui.dart';
import 'sparkline.dart';
import 'trading_models.dart';

/// Fetches a combined trading snapshot — injectable for tests.
typedef TradingSnapshotFetcher = Future<TradingSnapshot> Function();

/// Live Trading Cockpit (TC-1): equity / P&L, kill-switch + drawdown-breaker
/// state, and open positions — streamed through the RT-1 real-time layer
/// (adaptive 4s active / 15s idle + backoff) with a live connection indicator.
class CockpitScreen extends StatefulWidget {
  const CockpitScreen({
    super.key,
    TradingSnapshotFetcher? fetcher,
    this.notifications,
  }) : _injectedFetcher = fetcher;

  final TradingSnapshotFetcher? _injectedFetcher;

  /// NC-2 — optional shared notification center for kill-switch / breaker alerts.
  final NotificationCenter? notifications;

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

  // NC-2 — previous risk state for transition detection (null = not yet seen).
  bool? _prevKill;
  bool? _prevBreaker;

  // TC-2 — rolling session equity buffer for the sparkline (capped).
  final List<double> _equityHistory = <double>[];
  static const int _equityHistoryMax = 180; // ~12 min at 4s

  // TC-3 — open-positions sort order (default: biggest P/L first).
  PositionSort _sort = PositionSort.pnl;

  // TC-4 — venue filter for the open-positions list (null = all venues).
  String? _venue;

  void _onTick() {
    _emitRiskAlerts();
    final TradingSnapshot? s = _channel.value;
    if (s != null && s.totalEquity > 0) {
      _equityHistory.add(s.totalEquity);
      if (_equityHistory.length > _equityHistoryMax) {
        _equityHistory.removeAt(0);
      }
    }
    if (mounted) setState(() {});
  }

  /// Push a notification when the kill switch or breaker transitions (not on the
  /// first snapshot, which only establishes the baseline).
  void _emitRiskAlerts() {
    final NotificationCenter? nc = widget.notifications;
    final TradingSnapshot? s = _channel.value;
    if (nc == null || s == null) return;
    if (_prevKill != null && s.killSwitchActive != _prevKill) {
      if (s.killSwitchActive) {
        nc.add(NotifKind.error, 'Kill switch activated',
            detail: s.killSwitchReason.isEmpty
                ? 'Automated trading is halted.'
                : s.killSwitchReason,
            source: 'Cockpit');
      } else {
        nc.add(NotifKind.success, 'Kill switch cleared',
            detail: 'Automated trading may resume.', source: 'Cockpit');
      }
    }
    if (_prevBreaker != null && s.breakerTripped != _prevBreaker) {
      if (s.breakerTripped) {
        nc.add(NotifKind.error, 'Drawdown breaker tripped',
            detail: s.breakerReason.isEmpty
                ? 'Trading blocked until reset.'
                : s.breakerReason,
            source: 'Cockpit');
      } else {
        nc.add(NotifKind.success, 'Drawdown breaker reset',
            source: 'Cockpit');
      }
    }
    _prevKill = s.killSwitchActive;
    _prevBreaker = s.breakerTripped;
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
        const SizedBox(height: 12),
        // TC-6 — cash / buying-power / cash-weight tiles.
        Row(
          children: <Widget>[
            Expanded(
              child: ApStatCard(
                label: 'Cash',
                value: _money(s.cash),
                icon: Icons.payments_outlined,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: ApStatCard(
                label: 'Buying power',
                value: _money(s.buyingPower),
                icon: Icons.add_card_outlined,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: ApStatCard(
                label: 'Cash weight',
                value:
                    '${(cashFractionOfEquity(s.cash, s.totalEquity) * 100).round()}%',
                icon: Icons.pie_chart_outline,
              ),
            ),
          ],
        ),
        // TC-2 — live session equity curve.
        if (_equityHistory.length >= 2) ...<Widget>[
          const SizedBox(height: 14),
          ApPanel(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const ApSectionHeader('Equity · session',
                    icon: Icons.show_chart),
                const SizedBox(height: 8),
                Sparkline(values: _equityHistory),
              ],
            ),
          ),
        ],
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
        // TC-5 — per-venue exposure breakdown (only when spread across brokers).
        ...() {
          final List<VenueExposure> exp = venueExposures(s.positions);
          if (exp.length < 2) return const <Widget>[];
          return <Widget>[
            const ApSectionHeader('Exposure by venue', icon: Icons.pie_chart_outline),
            const SizedBox(height: 6),
            ApPanel(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              child: Column(
                children: <Widget>[
                  for (int i = 0; i < exp.length; i++) ...<Widget>[
                    if (i > 0) const SizedBox(height: 10),
                    _venueExposureRow(cs, exp[i]),
                  ],
                ],
              ),
            ),
            const SizedBox(height: 20),
          ];
        }(),
        Row(
          children: <Widget>[
            Expanded(
              child: ApSectionHeader('Open positions · ${s.positions.length}',
                  icon: Icons.list_alt),
            ),
            if (s.positions.length > 1) _sortSelector(cs),
          ],
        ),
        const SizedBox(height: 6),
        if (s.positions.isEmpty)
          Text('No open positions.',
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant))
        else
          Builder(builder: (BuildContext _) {
            final List<String> venues = venuesOf(s.positions);
            final List<Position> shown = sortPositions(
                filterPositionsByVenue(s.positions, _venue), _sort);
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                if (venues.length > 1) ...<Widget>[
                  _venueFilter(cs, venues),
                  const SizedBox(height: 8),
                ],
                if (shown.isEmpty)
                  Text('No positions on $_venue.',
                      style:
                          TextStyle(fontSize: 12, color: cs.onSurfaceVariant))
                else
                  ApPanel(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 14, vertical: 6),
                    child: Column(
                      children: <Widget>[
                        for (int i = 0; i < shown.length; i++) ...<Widget>[
                          if (i > 0)
                            Divider(height: 14, color: cs.outlineVariant),
                          _positionRow(cs, shown[i]),
                        ],
                      ],
                    ),
                  ),
              ],
            );
          }),
        const SizedBox(height: 24),
      ],
    );
  }

  // TC-5 — one venue's exposure: share bar + market value + unrealized P/L.
  Widget _venueExposureRow(ColorScheme cs, VenueExposure e) {
    final Color pnlColor = _pnlColor(e.unrealizedPnl, cs);
    final int pct = (e.share * 100).round();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Row(
          children: <Widget>[
            Expanded(
              child: Text(
                '${e.venue} · ${e.count} pos',
                style: TextStyle(
                    fontWeight: FontWeight.w700, color: cs.onSurface),
              ),
            ),
            Text('${_money(e.marketValue)}  ',
                style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
            Text(_signed(e.unrealizedPnl),
                style:
                    TextStyle(fontWeight: FontWeight.w700, color: pnlColor)),
          ],
        ),
        const SizedBox(height: 4),
        Row(
          children: <Widget>[
            Expanded(
              child: ClipRRect(
                borderRadius: BorderRadius.circular(3),
                child: LinearProgressIndicator(
                  value: e.share.clamp(0.0, 1.0),
                  minHeight: 6,
                  backgroundColor: cs.surfaceContainerHighest,
                  valueColor: AlwaysStoppedAnimation<Color>(cs.primary),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Text('$pct%',
                style: TextStyle(fontSize: 11, color: cs.onSurfaceVariant)),
          ],
        ),
      ],
    );
  }

  // TC-4 — venue filter chips ("All" + one per broker present).
  Widget _venueFilter(ColorScheme cs, List<String> venues) {
    Widget chip(String label, String? value) {
      final bool selected = _venue == value;
      return Padding(
        padding: const EdgeInsets.only(right: 6),
        child: ChoiceChip(
          label: Text(label),
          selected: selected,
          onSelected: (_) => setState(() => _venue = value),
          visualDensity: VisualDensity.compact,
        ),
      );
    }

    return Wrap(
      children: <Widget>[
        chip('All', null),
        for (final String v in venues) chip(v, v),
      ],
    );
  }

  // TC-3 — compact sort menu for the open-positions list.
  Widget _sortSelector(ColorScheme cs) {
    return PopupMenuButton<PositionSort>(
      tooltip: 'Sort positions',
      initialValue: _sort,
      onSelected: (PositionSort v) => setState(() => _sort = v),
      itemBuilder: (BuildContext _) => <PopupMenuEntry<PositionSort>>[
        for (final PositionSort s in PositionSort.values)
          PopupMenuItem<PositionSort>(
            value: s,
            child: Text(positionSortLabel(s)),
          ),
      ],
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          Icon(Icons.sort, size: 14, color: cs.onSurfaceVariant),
          const SizedBox(width: 4),
          Text(positionSortLabel(_sort),
              style: TextStyle(fontSize: 12, color: cs.onSurfaceVariant)),
          Icon(Icons.arrow_drop_down, size: 16, color: cs.onSurfaceVariant),
        ],
      ),
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
