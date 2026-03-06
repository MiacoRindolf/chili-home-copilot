import 'package:flutter/foundation.dart';

/// A single message in the shared chat history.
class SharedChatMessage {
  const SharedChatMessage({required this.role, required this.content});
  final String role; // 'user', 'assistant', 'system'
  final String content;
}

/// Shared chat history used by both the avatar quick chat and the full ChatScreen.
class SharedChatHistory extends ChangeNotifier {
  final List<SharedChatMessage> _messages = [];

  List<SharedChatMessage> get messages => List.unmodifiable(_messages);

  void addUser(String content) {
    _messages.add(SharedChatMessage(role: 'user', content: content));
    notifyListeners();
  }

  void addAssistant(String content) {
    _messages.add(SharedChatMessage(role: 'assistant', content: content));
    notifyListeners();
  }

  void addSystem(String content) {
    _messages.add(SharedChatMessage(role: 'system', content: content));
    notifyListeners();
  }

  void clear() {
    _messages.clear();
    notifyListeners();
  }

  void removeLast() {
    if (_messages.isNotEmpty) {
      _messages.removeLast();
      notifyListeners();
    }
  }

  void removeLastTwo() {
    if (_messages.length >= 2) {
      _messages.removeLast();
      _messages.removeLast();
      notifyListeners();
    }
  }
}
