import 'package:flutter/material.dart';
import 'package:lottie/lottie.dart';
import 'package:window_manager/window_manager.dart';
import 'package:desktop_drop/desktop_drop.dart';

import '../network/chili_api_client.dart';
import '../voice/voice_input.dart';

/// The small floating avatar with an expandable chat bubble.
///
/// - Drag the red circle to move the window.
/// - Single tap to toggle the chat bubble.
/// - Double-tap to open the full app.
/// - Long-press the mic button to record voice.
/// - Drop files onto the avatar to send them to CHILI.
class AvatarView extends StatefulWidget {
  final VoidCallback onOpenFullApp;
  const AvatarView({super.key, required this.onOpenFullApp});

  @override
  State<AvatarView> createState() => _AvatarViewState();
}

class _AvatarViewState extends State<AvatarView> {
  final _client = ChiliApiClient();
  final _controller = TextEditingController();
  bool _isSending = false;
  String? _reply;
  bool _showChat = false;
  bool _isDraggingFile = false;
  String _avatarAnim = 'assets/animations/chili_idle.json';

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _toggleChat() async {
    final willShow = !_showChat;
    if (willShow) {
      await windowManager.setSize(const Size(300, 480));
    } else {
      await windowManager.setSize(const Size(200, 200));
    }
    if (mounted) setState(() => _showChat = willShow);
  }

  Future<void> _sendMessage() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _isSending) return;

    setState(() {
      _isSending = true;
      _reply = null;
      _avatarAnim = 'assets/animations/chili_thinking.json';
    });

    try {
      final reply = await _client.sendMessage(text);
      if (!mounted) return;
      setState(() {
        _reply = reply;
        _avatarAnim = 'assets/animations/chili_speaking.json';
      });
      await Future.delayed(const Duration(seconds: 2));
      if (mounted) {
        setState(() => _avatarAnim = 'assets/animations/chili_idle.json');
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _reply = 'Could not reach CHILI. Is the server running?';
        _avatarAnim = 'assets/animations/chili_idle.json';
      });
    } finally {
      if (mounted) {
        setState(() {
          _isSending = false;
          _controller.clear();
        });
      }
    }
  }

  void _onFilesDropped(DropDoneDetails details) {
    if (details.files.isEmpty) return;
    final names = details.files.map((f) => f.name).join(', ');
    setState(() {
      _isDraggingFile = false;
      _reply = 'Received files: $names\n(File analysis coming soon!)';
      if (!_showChat) _showChat = true;
    });
    // Expand window to show the reply if collapsed.
    windowManager.setSize(const Size(300, 480));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: DropTarget(
        onDragEntered: (_) => setState(() => _isDraggingFile = true),
        onDragExited: (_) => setState(() => _isDraggingFile = false),
        onDragDone: _onFilesDropped,
        child: SingleChildScrollView(
          child: Center(
            child: Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  // ── Avatar circle ──
                  GestureDetector(
                    onPanStart: (_) => windowManager.startDragging(),
                    onTap: _toggleChat,
                    onDoubleTap: widget.onOpenFullApp,
                    child: AnimatedContainer(
                      duration: const Duration(milliseconds: 200),
                      height: 150,
                      width: 150,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: _isDraggingFile
                            ? const Color(0xFF42A5F5)
                            : const Color(0xFFEF5350),
                        boxShadow: [
                          BoxShadow(
                            color: _isDraggingFile
                                ? Colors.blue.withOpacity(0.6)
                                : Colors.black.withOpacity(0.4),
                            blurRadius: _isDraggingFile ? 20 : 12,
                            offset: const Offset(0, 4),
                          ),
                        ],
                      ),
                      child: Padding(
                        padding: const EdgeInsets.all(10),
                        child: _isDraggingFile
                            ? const Icon(Icons.file_present,
                                size: 60, color: Colors.white)
                            : Lottie.asset(
                                _avatarAnim,
                                repeat: true,
                                fit: BoxFit.contain,
                              ),
                      ),
                    ),
                  ),

                  // ── Chat bubble ──
                  if (_showChat) ...[
                    const SizedBox(height: 10),
                    _buildChatBubble(),
                  ],
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildChatBubble() {
    return Container(
      width: 275,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(16),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withOpacity(0.2),
            blurRadius: 12,
            offset: const Offset(0, 4),
          ),
        ],
      ),
      child: Material(
        color: Colors.transparent,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // Input row with mic + text + send
            Row(
              children: [
                // Mic button (hold to record)
                VoiceInputButton(
                  onResult: (text) {
                    if (mounted) {
                      setState(() {
                        _reply = text;
                        _avatarAnim = 'assets/animations/chili_idle.json';
                      });
                    }
                  },
                  onRecordingStateChanged: (recording) {
                    if (mounted) {
                      setState(() {
                        _avatarAnim = recording
                            ? 'assets/animations/chili_listening.json'
                            : 'assets/animations/chili_idle.json';
                      });
                    }
                  },
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: TextField(
                    controller: _controller,
                    onSubmitted: (_) => _sendMessage(),
                    style: const TextStyle(fontSize: 13),
                    decoration: InputDecoration(
                      isDense: true,
                      contentPadding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 10,
                      ),
                      hintText: 'Ask CHILI…',
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(20),
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                IconButton(
                  onPressed: _isSending ? null : _sendMessage,
                  icon: _isSending
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.send, size: 20),
                  style: IconButton.styleFrom(
                    backgroundColor: const Color(0xFFEF5350),
                    foregroundColor: Colors.white,
                  ),
                ),
              ],
            ),

            // Reply area
            if (_reply != null) ...[
              const SizedBox(height: 8),
              Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: const Color(0xFFF5F5F5),
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Text(
                  _reply!,
                  style: const TextStyle(fontSize: 12, height: 1.4),
                  maxLines: 6,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ],

            // Open full app link
            const SizedBox(height: 6),
            Center(
              child: TextButton.icon(
                onPressed: widget.onOpenFullApp,
                icon: const Icon(Icons.open_in_new, size: 14),
                label: const Text(
                  'Open Full App',
                  style: TextStyle(fontSize: 11),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
