import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  final _api = ChiliApiClient();
  List<dynamic> _chores = [];
  List<dynamic> _birthdays = [];
  List<dynamic> _activity = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _loadData();
  }

  Future<void> _loadData() async {
    setState(() => _loading = true);
    try {
      final results = await Future.wait([
        _api.fetchChores(),
        _api.fetchBirthdays(),
        _api.fetchActivity(),
      ]);
      if (!mounted) return;
      setState(() {
        _chores = results[0];
        _birthdays = results[1];
        _activity = results[2];
        _loading = false;
      });
    } catch (e) {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());

    return RefreshIndicator(
      onRefresh: _loadData,
      child: SingleChildScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Dashboard',
                style: Theme.of(context).textTheme.headlineMedium),
            const SizedBox(height: 20),
            _buildStatsRow(),
            const SizedBox(height: 20),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(child: _buildChoresCard()),
                const SizedBox(width: 16),
                Expanded(child: _buildBirthdaysCard()),
              ],
            ),
            const SizedBox(height: 16),
            _buildActivityCard(),
          ],
        ),
      ),
    );
  }

  Widget _buildStatsRow() {
    final pending = _chores.where((c) => !(c['done'] ?? false)).length;
    final today = DateTime.now().toIso8601String().substring(0, 10);
    final overdue = _chores.where((c) {
      if (c['done'] == true) return false;
      final d = c['due_date'];
      return d != null && d.compareTo(today) < 0;
    }).length;
    final done = _chores.where((c) => c['done'] == true).length;

    return Row(children: [
      _statCard('Pending', '$pending', const Color(0xFFEF5350), Icons.list_alt),
      const SizedBox(width: 12),
      _statCard('Overdue', '$overdue',
          overdue > 0 ? Colors.red.shade700 : Colors.green, Icons.warning_amber),
      const SizedBox(width: 12),
      _statCard('Done', '$done', Colors.green, Icons.check_circle),
      const SizedBox(width: 12),
      _statCard('Birthdays', '${_birthdays.length}', Colors.purple, Icons.cake),
    ]);
  }

  Widget _statCard(String label, String value, Color color, IconData icon) {
    return Expanded(
      child: Card(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(children: [
            Icon(icon, color: color, size: 28),
            const SizedBox(height: 8),
            Text(value,
                style: TextStyle(
                    fontSize: 28, fontWeight: FontWeight.bold, color: color)),
            const SizedBox(height: 4),
            Text(label,
                style: TextStyle(fontSize: 12, color: Colors.grey.shade600)),
          ]),
        ),
      ),
    );
  }

  Widget _buildChoresCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              const Icon(Icons.list_alt, size: 20),
              const SizedBox(width: 8),
              Text('Chores', style: Theme.of(context).textTheme.titleMedium),
            ]),
            const Divider(),
            if (_chores.isEmpty)
              const Padding(
                  padding: EdgeInsets.all(12),
                  child: Text('No chores yet',
                      style: TextStyle(color: Colors.grey)))
            else
              ..._chores.take(10).map(_choreRow),
          ],
        ),
      ),
    );
  }

  Widget _choreRow(dynamic c) {
    final done = c['done'] == true;
    final title = c['title'] ?? '';
    final priority = c['priority'] ?? 'medium';
    final dueDate = c['due_date'];
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(children: [
        Icon(done ? Icons.check_circle : Icons.radio_button_unchecked,
            color: done ? Colors.green : Colors.grey, size: 20),
        const SizedBox(width: 8),
        Expanded(
            child: Text(title,
                style: TextStyle(
                    fontSize: 13,
                    decoration: done ? TextDecoration.lineThrough : null,
                    color: done ? Colors.grey : null))),
        if (priority == 'high')
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
            decoration: BoxDecoration(
                color: Colors.red.shade50,
                borderRadius: BorderRadius.circular(4)),
            child: Text('High',
                style: TextStyle(fontSize: 10, color: Colors.red.shade700)),
          ),
        if (dueDate != null) ...[
          const SizedBox(width: 6),
          Text(dueDate,
              style: TextStyle(fontSize: 11, color: Colors.grey.shade600)),
        ],
      ]),
    );
  }

  Widget _buildBirthdaysCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              const Icon(Icons.cake, size: 20),
              const SizedBox(width: 8),
              Text('Birthdays',
                  style: Theme.of(context).textTheme.titleMedium),
            ]),
            const Divider(),
            if (_birthdays.isEmpty)
              const Padding(
                  padding: EdgeInsets.all(12),
                  child: Text('No birthdays yet',
                      style: TextStyle(color: Colors.grey)))
            else
              ..._birthdays.take(8).map(_birthdayRow),
          ],
        ),
      ),
    );
  }

  Widget _birthdayRow(dynamic b) {
    final name = b['name'] ?? '';
    final daysUntil = b['days_until'] ?? 999;
    final date = b['date'] ?? '';
    Color bg = Colors.grey.shade200;
    Color fg = Colors.grey.shade700;
    String label = '${daysUntil}d';
    if (daysUntil == 0) {
      bg = const Color(0xFFEF5350);
      fg = Colors.white;
      label = 'Today!';
    } else if (daysUntil <= 7) {
      bg = Colors.amber.shade100;
      fg = Colors.amber.shade800;
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
          decoration:
              BoxDecoration(color: bg, borderRadius: BorderRadius.circular(10)),
          child: Text(label,
              style: TextStyle(
                  fontSize: 11, fontWeight: FontWeight.w600, color: fg)),
        ),
        const SizedBox(width: 10),
        Expanded(child: Text(name, style: const TextStyle(fontSize: 13))),
        Text(date,
            style: TextStyle(fontSize: 11, color: Colors.grey.shade500)),
      ]),
    );
  }

  Widget _buildActivityCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              const Icon(Icons.timeline, size: 20),
              const SizedBox(width: 8),
              Text('Recent Activity',
                  style: Theme.of(context).textTheme.titleMedium),
              const Spacer(),
              IconButton(
                  icon: const Icon(Icons.refresh, size: 18),
                  onPressed: _loadData),
            ]),
            const Divider(),
            if (_activity.isEmpty)
              const Padding(
                  padding: EdgeInsets.all(12),
                  child: Text('No activity yet',
                      style: TextStyle(color: Colors.grey)))
            else
              ..._activity.take(10).map(_activityRow),
          ],
        ),
      ),
    );
  }

  Widget _activityRow(dynamic a) {
    final desc = a['description'] ?? '';
    final userName = a['user_name'] ?? '';
    final type = a['event_type'] ?? '';
    IconData icon = Icons.info_outline;
    if (type.contains('chore')) {
      icon = type.contains('done') ? Icons.check : Icons.add;
    }
    if (type.contains('birthday')) icon = Icons.cake;
    if (type.contains('chat')) icon = Icons.chat_bubble_outline;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(children: [
        Icon(icon, size: 18, color: Colors.grey.shade600),
        const SizedBox(width: 10),
        Expanded(
          child: RichText(
            text: TextSpan(
              style: const TextStyle(fontSize: 13, color: Colors.black87),
              children: [
                if (userName.isNotEmpty)
                  TextSpan(
                      text: '$userName ',
                      style: const TextStyle(fontWeight: FontWeight.w600)),
                TextSpan(text: desc),
              ],
            ),
          ),
        ),
      ]),
    );
  }
}
