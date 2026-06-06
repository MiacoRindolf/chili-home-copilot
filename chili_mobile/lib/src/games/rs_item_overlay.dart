import 'package:flutter/material.dart';

import 'runescape_prices.dart';

/// A modern, animated RuneScape item-price card (GAME-14). Pure Flutter — this
/// is the rich overlay that replaces the native GDI one: a dark search field,
/// item image, the GE price as the hero, volume, and a brief wiki blurb, with
/// smooth fade/slide transitions between states.
///
/// Self-contained and testable: pass a [RuneScapePrices] with a fake HTTP
/// getter to drive it in widget tests without the network.
class RsItemOverlay extends StatefulWidget {
  const RsItemOverlay({
    super.key,
    RuneScapePrices? prices,
    this.onClose,
    this.autofocus = false,
  }) : _prices = prices;

  final RuneScapePrices? _prices;
  final VoidCallback? onClose;

  /// Autofocus the search field (true for a floating on-game overlay; false
  /// when embedded in CHILI so it doesn't steal focus).
  final bool autofocus;

  @override
  State<RsItemOverlay> createState() => _RsItemOverlayState();
}

enum _Phase { idle, loading, ok, empty, error }

class _RsItemOverlayState extends State<RsItemOverlay> {
  late final RuneScapePrices _prices;
  final TextEditingController _ctrl = TextEditingController();
  final FocusNode _focus = FocusNode();

  _Phase _phase = _Phase.idle;
  ItemPrice? _price;
  ItemInfo? _info;
  String _message = '';
  int _seq = 0; // guards against out-of-order async results

  @override
  void initState() {
    super.initState();
    _prices = widget._prices ?? RuneScapePrices();
  }

  @override
  void dispose() {
    _ctrl.dispose();
    _focus.dispose();
    super.dispose();
  }

  Future<void> _search() async {
    final String q = _ctrl.text.trim();
    if (q.isEmpty) return;
    final int seq = ++_seq;
    setState(() {
      _phase = _Phase.loading;
      _price = null;
      _info = null;
    });
    try {
      final ItemPrice? p = await _prices.lookup(q);
      if (!mounted || seq != _seq) return;
      if (p == null) {
        setState(() {
          _phase = _Phase.empty;
          _message = 'No GE price for “$q”';
        });
        return;
      }
      setState(() {
        _price = p;
        _phase = _Phase.ok;
      });
      // Enrich with the wiki blurb + image (best-effort).
      try {
        final ItemInfo info = await _prices.wikiInfo(p.name);
        if (mounted && seq == _seq) setState(() => _info = info);
      } catch (_) {
        // price still shows without the blurb/image
      }
    } catch (_) {
      if (!mounted || seq != _seq) return;
      setState(() {
        _phase = _Phase.error;
        _message = 'Lookup failed — check your connection';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    const Color bg = Color(0xFF14181C);
    const Color accent = Color(0xFF35D08A);
    return Container(
      width: 340,
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Colors.white.withValues(alpha: 0.06)),
        boxShadow: <BoxShadow>[
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.45),
            blurRadius: 24,
            offset: const Offset(0, 8),
          ),
        ],
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          // Accent header line.
          Container(height: 3, color: accent),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 6),
            child: Row(
              children: <Widget>[
                Expanded(child: _searchField(accent)),
                if (widget.onClose != null)
                  IconButton(
                    visualDensity: VisualDensity.compact,
                    iconSize: 18,
                    color: Colors.white54,
                    onPressed: widget.onClose,
                    icon: const Icon(Icons.close),
                  ),
              ],
            ),
          ),
          AnimatedSize(
            duration: const Duration(milliseconds: 220),
            curve: Curves.easeOutCubic,
            alignment: Alignment.topCenter,
            child: AnimatedSwitcher(
              duration: const Duration(milliseconds: 240),
              switchInCurve: Curves.easeOutCubic,
              transitionBuilder: (Widget child, Animation<double> a) =>
                  FadeTransition(
                opacity: a,
                child: SlideTransition(
                  position: Tween<Offset>(
                          begin: const Offset(0, 0.06), end: Offset.zero)
                      .animate(a),
                  child: child,
                ),
              ),
              child: _body(accent),
            ),
          ),
        ],
      ),
    );
  }

  Widget _searchField(Color accent) {
    return TextField(
      controller: _ctrl,
      focusNode: _focus,
      autofocus: widget.autofocus,
      textInputAction: TextInputAction.search,
      onSubmitted: (_) => _search(),
      style: const TextStyle(color: Color(0xFFECEFF1), fontSize: 14),
      cursorColor: accent,
      decoration: InputDecoration(
        isDense: true,
        filled: true,
        fillColor: const Color(0xFF20262D),
        hintText: 'Search RuneScape item price…',
        hintStyle: const TextStyle(color: Color(0xFF7A838B), fontSize: 13),
        prefixIcon: const Icon(Icons.search, size: 18, color: Color(0xFF7A838B)),
        contentPadding: const EdgeInsets.symmetric(vertical: 10, horizontal: 8),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: BorderSide.none,
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(10),
          borderSide: BorderSide(color: accent.withValues(alpha: 0.6)),
        ),
      ),
    );
  }

  Widget _body(Color accent) {
    switch (_phase) {
      case _Phase.idle:
        return _hint('Type an item and press Enter for its GE price');
      case _Phase.loading:
        return _loading(accent);
      case _Phase.empty:
      case _Phase.error:
        return _hint(_message, key: const ValueKey<String>('msg'));
      case _Phase.ok:
        return _result(accent);
    }
  }

  Widget _hint(String text, {Key? key}) => Padding(
        key: key ?? const ValueKey<String>('idle'),
        padding: const EdgeInsets.fromLTRB(14, 6, 14, 16),
        child: Align(
          alignment: Alignment.centerLeft,
          child: Text(text,
              style: const TextStyle(color: Color(0xFF8A929A), fontSize: 13)),
        ),
      );

  Widget _loading(Color accent) => Padding(
        key: const ValueKey<String>('loading'),
        padding: const EdgeInsets.fromLTRB(14, 8, 14, 18),
        child: Row(
          children: <Widget>[
            SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(strokeWidth: 2, color: accent),
            ),
            const SizedBox(width: 12),
            const Text('Searching…',
                style: TextStyle(color: Color(0xFF8A929A), fontSize: 13)),
          ],
        ),
      );

  Widget _result(Color accent) {
    final ItemPrice p = _price!;
    final String? thumb = _info?.thumbUrl;
    return Padding(
      key: ValueKey<String>('ok:${p.id}'),
      padding: const EdgeInsets.fromLTRB(14, 4, 14, 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _thumb(thumb),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Text(p.name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                            color: Color(0xFFB9C0C7),
                            fontSize: 13,
                            fontWeight: FontWeight.w600)),
                    const SizedBox(height: 2),
                    Text('${formatGpFull(p.price)} gp',
                        style: TextStyle(
                            color: accent,
                            fontSize: 24,
                            fontWeight: FontWeight.w700,
                            letterSpacing: -0.5)),
                    Text('Vol ${formatGpFull(p.volume)}/day',
                        style: const TextStyle(
                            color: Color(0xFF7A838B), fontSize: 11)),
                  ],
                ),
              ),
            ],
          ),
          if ((_info?.extract ?? '').isNotEmpty) ...<Widget>[
            const SizedBox(height: 10),
            Text(
              briefBlurb(_info!.extract),
              style: const TextStyle(
                  color: Color(0xFF9AA2AA), fontSize: 12, height: 1.35),
            ),
          ],
        ],
      ),
    );
  }

  Widget _thumb(String? url) {
    const double size = 56;
    final Widget placeholder = Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        color: const Color(0xFF20262D),
        borderRadius: BorderRadius.circular(8),
      ),
      child: const Icon(Icons.inventory_2_outlined,
          size: 24, color: Color(0xFF55606A)),
    );
    if (url == null || url.isEmpty) return placeholder;
    return ClipRRect(
      borderRadius: BorderRadius.circular(8),
      child: Image.network(
        url,
        width: size,
        height: size,
        fit: BoxFit.contain,
        errorBuilder: (_, __, ___) => placeholder,
        loadingBuilder: (BuildContext _, Widget child, ImageChunkEvent? p) =>
            p == null ? child : placeholder,
      ),
    );
  }
}
