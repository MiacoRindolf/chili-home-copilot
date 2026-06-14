import 'package:flutter/material.dart';

/// One parsed line of a unified diff.
enum DiffLineKind { add, remove, hunk, fileHeader, context }

class DiffLine {
  const DiffLine(this.kind, this.text);
  final DiffLineKind kind;
  final String text;
}

/// One file's section of a unified diff.
class DiffFile {
  DiffFile(this.path, this.lines);
  final String path;
  final List<DiffLine> lines;

  int get added => lines.where((l) => l.kind == DiffLineKind.add).length;
  int get removed => lines.where((l) => l.kind == DiffLineKind.remove).length;
}

/// Pure parser: split a unified diff into per-file sections with typed lines.
/// Dependency-free and testable without a widget tree.
List<DiffFile> parseUnifiedDiff(String diff) {
  final files = <DiffFile>[];
  DiffFile? current;
  String? pendingPath;

  void flush() {
    final c = current;
    if (c != null && c.lines.isNotEmpty) files.add(c);
  }

  for (final raw in diff.replaceAll('\r\n', '\n').split('\n')) {
    if (raw.startsWith('diff --git')) {
      flush();
      current = null;
      pendingPath = null;
      continue;
    }
    if (raw.startsWith('--- ')) {
      // old-file header; the new-file header (+++) names the target.
      continue;
    }
    if (raw.startsWith('+++ ')) {
      flush();
      pendingPath = _cleanPath(raw.substring(4));
      current = DiffFile(pendingPath ?? '(file)', <DiffLine>[]);
      continue;
    }
    if (raw.startsWith('@@')) {
      final c = current ??= DiffFile(pendingPath ?? '(file)', <DiffLine>[]);
      c.lines.add(DiffLine(DiffLineKind.hunk, raw));
      continue;
    }
    final c = current;
    if (c == null) continue; // preamble before the first +++
    if (raw.startsWith('+')) {
      c.lines.add(DiffLine(DiffLineKind.add, raw));
    } else if (raw.startsWith('-')) {
      c.lines.add(DiffLine(DiffLineKind.remove, raw));
    } else {
      c.lines.add(DiffLine(DiffLineKind.context, raw));
    }
  }
  flush();
  return files;
}

String? _cleanPath(String s) {
  var p = s.trim();
  if (p == '/dev/null') return p;
  if (p.startsWith('a/') || p.startsWith('b/')) p = p.substring(2);
  final tab = p.indexOf('\t');
  if (tab >= 0) p = p.substring(0, tab);
  return p.isEmpty ? null : p;
}

/// A per-file collapsible unified-diff viewer with +/- coloring. The whole
/// reason the operator can review what the autopilot wrote before merging.
class DiffView extends StatefulWidget {
  const DiffView(this.diff, {super.key, this.initiallyExpanded = false});
  final String diff;
  final bool initiallyExpanded;

  @override
  State<DiffView> createState() => _DiffViewState();
}

class _DiffViewState extends State<DiffView> {
  late final List<DiffFile> _files = parseUnifiedDiff(widget.diff);
  late final Set<int> _expanded = widget.initiallyExpanded
      ? Set<int>.from(Iterable<int>.generate(_files.length))
      : <int>{};

  @override
  Widget build(BuildContext context) {
    if (_files.isEmpty) {
      return SelectableText(
        widget.diff,
        style: const TextStyle(fontFamily: 'monospace', fontSize: 11.5),
      );
    }
    final cs = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        for (var i = 0; i < _files.length; i++)
          _fileBlock(context, cs, i, _files[i]),
      ],
    );
  }

  Widget _fileBlock(BuildContext context, ColorScheme cs, int i, DiffFile f) {
    final open = _expanded.contains(i);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      decoration: BoxDecoration(
        border: Border.all(color: cs.outlineVariant),
        borderRadius: BorderRadius.circular(6),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          InkWell(
            onTap: () => setState(() {
              open ? _expanded.remove(i) : _expanded.add(i);
            }),
            child: Container(
              color: cs.surfaceContainerHighest.withValues(alpha: 0.5),
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
              child: Row(
                children: <Widget>[
                  Icon(open ? Icons.expand_more : Icons.chevron_right,
                      size: 18, color: cs.onSurfaceVariant),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      f.path,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          fontFamily: 'monospace',
                          fontSize: 12,
                          fontWeight: FontWeight.w600),
                    ),
                  ),
                  if (f.added > 0) ...<Widget>[
                    Text('+${f.added}',
                        style: const TextStyle(
                            color: Color(0xFF2E7D32),
                            fontSize: 12,
                            fontWeight: FontWeight.w700)),
                    const SizedBox(width: 6),
                  ],
                  if (f.removed > 0)
                    Text('-${f.removed}',
                        style: const TextStyle(
                            color: Color(0xFFC62828),
                            fontSize: 12,
                            fontWeight: FontWeight.w700)),
                ],
              ),
            ),
          ),
          if (open)
            Container(
              width: double.infinity,
              color: cs.surface,
              child: SingleChildScrollView(
                scrollDirection: Axis.horizontal,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    for (final line in f.lines) _diffLine(cs, line),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _diffLine(ColorScheme cs, DiffLine line) {
    Color? bg;
    Color fg;
    switch (line.kind) {
      case DiffLineKind.add:
        bg = const Color(0x1A2E7D32);
        fg = const Color(0xFF1B5E20);
        break;
      case DiffLineKind.remove:
        bg = const Color(0x1AC62828);
        fg = const Color(0xFFB71C1C);
        break;
      case DiffLineKind.hunk:
        bg = cs.primary.withValues(alpha: 0.08);
        fg = cs.primary;
        break;
      case DiffLineKind.fileHeader:
        fg = cs.onSurfaceVariant;
        break;
      case DiffLineKind.context:
        fg = cs.onSurface.withValues(alpha: 0.85);
        break;
    }
    // Dark-mode legibility: lighten the +/- text on dark surfaces.
    final isDark = cs.brightness == Brightness.dark;
    if (isDark && line.kind == DiffLineKind.add) fg = const Color(0xFF81C784);
    if (isDark && line.kind == DiffLineKind.remove) fg = const Color(0xFFE57373);
    return Container(
      color: bg,
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 1),
      child: Text(
        line.text.isEmpty ? ' ' : line.text,
        style: TextStyle(
            fontFamily: 'monospace', fontSize: 11.5, height: 1.35, color: fg),
      ),
    );
  }
}
