import 'package:flutter/material.dart';

/// One launchable entry in the command palette.
class PaletteItem {
  final String id;
  final String title;
  final IconData icon;
  const PaletteItem(this.id, this.title, this.icon);
}

/// Subsequence ("fuzzy") match: do [q]'s chars appear in order in [label]?
/// ("intr" matches "Intercom"). Case-insensitive; empty query matches all.
bool paletteFuzzy(String label, String query) {
  final String q = query.trim().toLowerCase();
  if (q.isEmpty) return true;
  final String l = label.toLowerCase();
  int i = 0;
  for (int j = 0; j < l.length && i < q.length; j++) {
    if (l[j] == q[i]) i++;
  }
  return i == q.length;
}

List<PaletteItem> paletteFilter(List<PaletteItem> items, String query) =>
    items.where((PaletteItem it) => paletteFuzzy(it.title, query)).toList();

/// Command-palette overlay (Ctrl+K): a search field over the app list. Type to
/// fuzzy-filter; Enter opens the top match; tap opens any; the scrim/Esc close.
class WorkspacePalette extends StatefulWidget {
  final List<PaletteItem> items;
  final void Function(String id) onOpen;
  final VoidCallback onClose;

  const WorkspacePalette({
    super.key,
    required this.items,
    required this.onOpen,
    required this.onClose,
  });

  @override
  State<WorkspacePalette> createState() => _WorkspacePaletteState();
}

class _WorkspacePaletteState extends State<WorkspacePalette> {
  final TextEditingController _ctrl = TextEditingController();
  String _query = '';

  List<PaletteItem> get _filtered => paletteFilter(widget.items, _query);

  void _openTop() {
    final List<PaletteItem> f = _filtered;
    if (f.isNotEmpty) widget.onOpen(f.first.id);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    final List<PaletteItem> items = _filtered;
    return Positioned.fill(
      child: Stack(
        children: <Widget>[
          Positioned.fill(
            child: GestureDetector(
              onTap: widget.onClose,
              child: const ColoredBox(color: Colors.black54),
            ),
          ),
          Align(
            alignment: const Alignment(0, -0.35),
            child: Material(
              elevation: 18,
              color: cs.surface,
              borderRadius: BorderRadius.circular(14),
              clipBehavior: Clip.antiAlias,
              child: SizedBox(
                width: 460,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: <Widget>[
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                      child: TextField(
                        controller: _ctrl,
                        autofocus: true,
                        decoration: const InputDecoration(
                          hintText: 'Search apps…',
                          prefixIcon: Icon(Icons.search),
                          border: InputBorder.none,
                        ),
                        onChanged: (String v) => setState(() => _query = v),
                        onSubmitted: (_) => _openTop(),
                      ),
                    ),
                    Divider(height: 1, color: cs.outlineVariant),
                    Flexible(
                      child: items.isEmpty
                          ? const Padding(
                              padding: EdgeInsets.all(20),
                              child: Text('No matches.'),
                            )
                          : ListView.builder(
                              shrinkWrap: true,
                              padding: const EdgeInsets.symmetric(vertical: 4),
                              itemCount: items.length,
                              itemBuilder: (BuildContext context, int i) {
                                final PaletteItem it = items[i];
                                return ListTile(
                                  dense: true,
                                  leading: Icon(it.icon, color: cs.primary),
                                  title: Text(it.title),
                                  onTap: () => widget.onOpen(it.id),
                                );
                              },
                            ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
