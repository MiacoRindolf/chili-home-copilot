import 'package:flutter/material.dart';

import '../desktop/desktop_actions.dart';
import '../network/chili_api_client.dart';

/// Shared helper for sending chat messages and handling desktop client actions.
class ChatSendController {
  ChatSendController(this._client);

  final ChiliApiClient _client;

  Future<ChatResponse> send(
    String message, {
    void Function(String token)? onToken,
  }) {
    return _client.sendMessageStream(
      message,
      onToken: onToken,
    );
  }

  Future<void> handleClientAction(
    BuildContext context,
    Map<String, dynamic>? clientAction,
  ) async {
    if (clientAction == null) return;
    final actionResult = await DesktopActions.execute(clientAction);
    if (actionResult != null && actionResult.isNotEmpty) {
      if (!context.mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(actionResult),
          duration: const Duration(seconds: 2),
        ),
      );
    }
  }
}

