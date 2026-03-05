import 'package:flutter/material.dart';

import '../network/chili_api_client.dart';

class IntercomScreen extends StatefulWidget {
  const IntercomScreen({super.key});

  @override
  State<IntercomScreen> createState() => _IntercomScreenState();
}

class _IntercomScreenState extends State<IntercomScreen> {
  final _api = ChiliApiClient();
  List<dynamic> _housemates = [];
  bool _loading = true;
  String _myStatus = 'available';
  String? _error;

  @override
  void initState() {
    super.initState();
    _loadStatus();
  }

  Future<void> _loadStatus() async {
    setState(() => _loading = true);
    try {
      final data = await _api.fetchIntercomStatus();
      if (!mounted) return;
      if (data != null) {
        setState(() {
          _housemates = data['housemates'] ?? [];
          _myStatus = data['my_status']?['status'] ?? 'available';
          _loading = false;
          _error = null;
        });
      } else {
        setState(() {
          _loading = false;
          _error = 'Pair your device first to use the intercom.';
        });
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          _error = 'Could not reach server. Is it running?';
        });
      }
    }
  }

  Future<void> _setStatus(String status) async {
    try {
      await _api.setIntercomStatus(status);
      if (mounted) setState(() => _myStatus = status);
    } catch (_) {}
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Intercom',
              style: Theme.of(context).textTheme.headlineMedium),
          const SizedBox(height: 4),
          Text('Push-to-talk with your housemates',
              style: TextStyle(color: Colors.grey.shade600)),
          const SizedBox(height: 20),

          if (_error != null)
            Card(
              color: Colors.amber.shade50,
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Row(children: [
                  Icon(Icons.info_outline, color: Colors.amber.shade800),
                  const SizedBox(width: 12),
                  Expanded(
                      child: Text(_error!,
                          style: TextStyle(color: Colors.amber.shade900))),
                  IconButton(
                      icon: const Icon(Icons.refresh),
                      onPressed: _loadStatus),
                ]),
              ),
            )
          else if (_loading)
            const Center(child: CircularProgressIndicator())
          else ...[
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Row(children: [
                  const Text('My Status:',
                      style: TextStyle(fontWeight: FontWeight.w600)),
                  const SizedBox(width: 12),
                  ChoiceChip(
                    label: const Text('Available'),
                    selected: _myStatus == 'available',
                    selectedColor: Colors.green.shade100,
                    onSelected: (_) => _setStatus('available'),
                  ),
                  const SizedBox(width: 8),
                  ChoiceChip(
                    label: const Text('Do Not Disturb'),
                    selected: _myStatus == 'dnd',
                    selectedColor: Colors.red.shade100,
                    onSelected: (_) => _setStatus('dnd'),
                  ),
                ]),
              ),
            ),
            const SizedBox(height: 20),
            Text('Housemates',
                style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            if (_housemates.isEmpty)
              Card(
                child: Padding(
                  padding: const EdgeInsets.all(24),
                  child: Center(
                      child: Text('No housemates found. Pair devices first.',
                          style: TextStyle(color: Colors.grey.shade600))),
                ),
              )
            else
              ..._housemates.map(_housemateCard),
          ],
        ],
      ),
    );
  }

  Widget _housemateCard(dynamic h) {
    final name = h['user_name'] ?? h['name'] ?? 'Unknown';
    final online = h['online'] == true;
    final status = h['status'] ?? 'offline';
    Color dotColor = Colors.grey;
    if (online && status == 'available') dotColor = Colors.green;
    if (online && status == 'dnd') dotColor = Colors.red;

    return Card(
      child: ListTile(
        leading: CircleAvatar(
          backgroundColor: const Color(0xFFEF5350),
          child: Text(name[0].toUpperCase(),
              style: const TextStyle(
                  color: Colors.white, fontWeight: FontWeight.bold)),
        ),
        title: Text(name),
        subtitle: Text(online ? status.toUpperCase() : 'OFFLINE'),
        trailing: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 12,
              height: 12,
              decoration:
                  BoxDecoration(shape: BoxShape.circle, color: dotColor),
            ),
            if (online && status == 'available') ...[
              const SizedBox(width: 12),
              IconButton(
                icon: const Icon(Icons.mic),
                tooltip: 'Push to Talk',
                onPressed: () {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(
                        content: Text(
                            'Hold-to-talk coming soon with WebSocket PTT.')),
                  );
                },
              ),
            ],
          ],
        ),
      ),
    );
  }
}
