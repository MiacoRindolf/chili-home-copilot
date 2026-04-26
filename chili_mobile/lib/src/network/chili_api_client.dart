import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';
import 'package:http_parser/http_parser.dart' show MediaType;
import 'package:path/path.dart' as p;

import '../brain/device_auth_store.dart';
import '../config/app_config.dart';
import 'network_error_message.dart' show userMessageForHttpStatus;

/// Structured response from the chat endpoint.
class ChatResponse {
  ChatResponse({required this.reply, this.clientAction, this.wasCancelled = false});

  final String reply;
  final Map<String, dynamic>? clientAction;

  /// True when the user cancelled the stream mid-generation.
  final bool wasCancelled;
}

/// Lightweight token that allows callers to cancel an in-flight streaming
/// request.  Call [cancel] to stop consuming the SSE stream; the partial
/// reply collected so far will be returned as the final response.
class CancelToken {
  bool _cancelled = false;
  bool get isCancelled => _cancelled;
  void cancel() => _cancelled = true;
}

/// API client for the CHILI backend.
///
/// Uses an [IOClient] that trusts all certificates so self-signed
/// local dev certs (localhost.pem) work without extra setup.
class ChiliApiClient {
  /// Current server root; applied immediately when changed in Settings (see [AppConfig.setBaseUrl]).
  static String get baseUrl => AppConfig.instance.apiBaseUrl;

  String? _token;

  set token(String? value) => _token = value;

  static final http.Client _client = _buildClient();

  static http.Client _buildClient() {
    final context = SecurityContext.defaultContext;
    // Use a block for the cert callback so cascades (..) are not parsed as
    // `=> true..connectionTimeout` (cascade on [bool]).
    final httpClient = HttpClient(context: context)
      ..badCertificateCallback = (cert, host, port) {
        return true;
      }
      ..connectionTimeout = const Duration(seconds: 30);
    return IOClient(httpClient);
  }

  Map<String, String> _headers({bool json = true}) {
    final h = <String, String>{};
    if (json) h['Content-Type'] = 'application/json';
    if (_token != null && _token!.isNotEmpty) {
      h['Authorization'] = 'Bearer $_token';
    }
    return h;
  }

  static const Map<String, String> _pairingJsonHeaders = {
    'Content-Type': 'application/json',
  };

  /// POST /api/pair/request — same as the Chat web UI; unauthenticated; no Bearer.
  Future<Map<String, dynamic>> pairRequest({required String email}) async {
    final res = await _client.post(
      Uri.parse('$baseUrl/api/pair/request'),
      headers: _pairingJsonHeaders,
      body: jsonEncode({'email': email.trim()}),
    );
    return _coercePairingResponse(res);
  }

  /// POST /api/pair/verify — same as the Chat web UI; returns JSON [token] for native.
  Future<Map<String, dynamic>> pairVerify({
    required String code,
    String label = 'CHILI Desktop Companion',
  }) async {
    final res = await _client.post(
      Uri.parse('$baseUrl/api/pair/verify'),
      headers: _pairingJsonHeaders,
      body: jsonEncode({
        'code': code.trim(),
        'label': label.trim().isEmpty ? 'Unknown Device' : label.trim(),
      }),
    );
    return _coercePairingResponse(res);
  }

  /// Pairing routes return JSON for both success and most errors; 502/504 often return HTML from a proxy.
  static Map<String, dynamic> _coercePairingResponse(http.Response res) {
    if (res.statusCode >= 200 && res.statusCode < 300) {
      try {
        final m = jsonDecode(res.body) as Map<String, dynamic>?;
        if (m != null) return m;
      } catch (_) {}
      return {'ok': false, 'error': 'Invalid response from server.'};
    }
    try {
      final m = jsonDecode(res.body) as Map<String, dynamic>?;
      if (m != null) {
        if (m['ok'] == false) {
          return m;
        }
        final err = m['error'];
        if (err is String && err.isNotEmpty) {
          return {'ok': false, 'error': err};
        }
        final detail = m['detail'];
        if (detail is String && detail.isNotEmpty) {
          return {'ok': false, 'error': detail};
        }
        if (detail is List && detail.isNotEmpty) {
          return {'ok': false, 'error': detail.first.toString()};
        }
      }
    } catch (_) {}
    return {'ok': false, 'error': userMessageForHttpStatus(res.statusCode)};
  }

  // ── Chat ──

  Future<ChatResponse> sendMessage(String message) async {
    final uri = Uri.parse('$baseUrl/api/mobile/chat');
    final response = await _client.post(
      uri,
      headers: _headers(),
      body: jsonEncode({'message': message}),
    );
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(response.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (response.statusCode != 200) {
      final err = decoded?['error'] ?? decoded?['detail'] ?? response.body;
      throw Exception(err is String ? err : 'HTTP ${response.statusCode}');
    }
    final reply = (decoded?['reply'] as String?) ?? 'CHILI did not send a reply.';
    final clientAction = decoded?['client_action'] as Map<String, dynamic>?;
    return ChatResponse(reply: reply, clientAction: clientAction);
  }

  /// Stream chat from SSE; [onToken] is called with each token as it arrives.
  /// Returns the final [ChatResponse] when the stream completes or is
  /// cancelled via [cancelToken].
  Future<ChatResponse> sendMessageStream(
    String message, {
    void Function(String token)? onToken,
    CancelToken? cancelToken,
  }) async {
    final uri = Uri.parse('$baseUrl/api/mobile/chat/stream');
    final request = http.Request('POST', uri);
    request.headers.addAll(_headers());
    request.body = jsonEncode({'message': message});

    final streamedResponse = await _client.send(request);
    if (streamedResponse.statusCode != 200) {
      final body = await streamedResponse.stream.bytesToString();
      Map<String, dynamic>? decoded;
      try {
        decoded = jsonDecode(body) as Map<String, dynamic>?;
      } catch (_) {}
      final err = decoded?['error'] ?? decoded?['detail'] ?? body;
      throw Exception(err is String ? err : 'HTTP ${streamedResponse.statusCode}');
    }

    return _consumeSseStream(streamedResponse, onToken: onToken, cancelToken: cancelToken);
  }

  /// Stream chat with image attachments via multipart form data.
  ///
  /// Uses the web endpoint `/api/chat/stream` which accepts `images` as file
  /// parts and `message` as a form field. Falls back to text-only streaming
  /// when [imagePaths] is empty.
  Future<ChatResponse> sendMessageStreamWithImages(
    String message, {
    required List<String> imagePaths,
    void Function(String token)? onToken,
    CancelToken? cancelToken,
  }) async {
    if (imagePaths.isEmpty) {
      return sendMessageStream(message, onToken: onToken, cancelToken: cancelToken);
    }

    final uri = Uri.parse('$baseUrl/api/chat/stream');
    final request = http.MultipartRequest('POST', uri);
    if (_token != null && _token!.isNotEmpty) {
      request.headers['Authorization'] = 'Bearer $_token';
    }
    request.fields['message'] = message;

    for (final path in imagePaths) {
      final file = File(path);
      if (!await file.exists()) continue;
      final ext = p.extension(path).toLowerCase();
      final mime = switch (ext) {
        '.png' => 'image/png',
        '.gif' => 'image/gif',
        '.webp' => 'image/webp',
        _ => 'image/jpeg',
      };
      request.files.add(await http.MultipartFile.fromPath(
        'images',
        path,
        filename: p.basename(path),
        contentType: MediaType.parse(mime),
      ));
    }

    final streamedResponse = await _client.send(request);
    if (streamedResponse.statusCode != 200) {
      final body = await streamedResponse.stream.bytesToString();
      Map<String, dynamic>? decoded;
      try {
        decoded = jsonDecode(body) as Map<String, dynamic>?;
      } catch (_) {}
      final err = decoded?['error'] ?? decoded?['detail'] ?? body;
      throw Exception(err is String ? err : 'HTTP ${streamedResponse.statusCode}');
    }

    return _consumeSseStream(streamedResponse, onToken: onToken, cancelToken: cancelToken);
  }

  /// Shared SSE consumer used by both text and image streaming methods.
  ///
  /// Uses [StreamSubscription] so the stream can be cancelled mid-flight
  /// via [cancelToken]. Whatever has been collected is returned as the reply.
  Future<ChatResponse> _consumeSseStream(
    http.StreamedResponse streamedResponse, {
    void Function(String token)? onToken,
    CancelToken? cancelToken,
  }) {
    final completer = Completer<ChatResponse>();
    final buffer = StringBuffer();
    Map<String, dynamic>? clientAction;
    bool cancelled = false;

    final lines = streamedResponse.stream
        .transform(utf8.decoder)
        .transform(const LineSplitter());

    late StreamSubscription<String> sub;
    sub = lines.listen(
      (line) {
        if (cancelToken != null && cancelToken.isCancelled && !cancelled) {
          cancelled = true;
          sub.cancel();
          final reply = buffer.toString().trim();
          completer.complete(ChatResponse(
            reply: reply.isEmpty ? 'CHILI did not send a reply.' : reply,
            clientAction: clientAction,
            wasCancelled: true,
          ));
          return;
        }

        if (!line.startsWith('data: ')) return;
        final payload = line.substring(6).trim();
        if (payload == '[DONE]' || payload.isEmpty) return;
        try {
          final data = jsonDecode(payload) as Map<String, dynamic>?;
          if (data == null) return;
          final token = (data['token'] as String?) ?? '';
          if (token.isNotEmpty) {
            buffer.write(token);
            onToken?.call(token);
          }
          if (data['done'] == true) {
            clientAction = data['client_action'] as Map<String, dynamic>?;
          }
        } catch (_) {}
      },
      onDone: () {
        if (!completer.isCompleted) {
          final reply = buffer.toString().trim();
          completer.complete(ChatResponse(
            reply: reply.isEmpty ? 'CHILI did not send a reply.' : reply,
            clientAction: clientAction,
          ));
        }
      },
      onError: (Object error) {
        if (!completer.isCompleted) {
          completer.completeError(error);
        }
      },
      cancelOnError: true,
    );

    return completer.future;
  }

  // ── Dashboard helpers ──

  Future<List<dynamic>> fetchChores() async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/chores'),
      headers: _headers(json: false),
    );
    if (res.statusCode != 200) return [];
    return (jsonDecode(res.body)['chores'] ?? []) as List;
  }

  Future<List<dynamic>> fetchBirthdays() async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/birthdays'),
      headers: _headers(json: false),
    );
    if (res.statusCode != 200) return [];
    return (jsonDecode(res.body)['birthdays'] ?? []) as List;
  }

  Future<List<dynamic>> fetchActivity({int limit = 15}) async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/activity?limit=$limit'),
      headers: _headers(json: false),
    );
    if (res.statusCode != 200) return [];
    return (jsonDecode(res.body)['events'] ?? []) as List;
  }

  Future<Map<String, dynamic>?> fetchIntercomStatus() async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/intercom/status'),
      headers: _headers(json: false),
    );
    if (res.statusCode != 200) return null;
    return jsonDecode(res.body) as Map<String, dynamic>;
  }

  Future<void> setIntercomStatus(String status) async {
    await _client.post(
      Uri.parse('$baseUrl/api/intercom/status'),
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'status=$status',
    );
  }

  // ── Voice ──

  /// Build the URL for the streaming TTS endpoint.  [audioplayers] UrlSource
  /// will connect directly and start playing as chunks arrive.
  String getTtsStreamUrl(String text) {
    return '$baseUrl/api/voice/tts/stream?text=${Uri.encodeComponent(text.trim())}';
  }

  /// Fetch TTS audio via the streaming endpoint.  Bytes arrive incrementally
  /// from Edge TTS, reducing time-to-complete vs the buffered POST endpoint.
  Future<List<int>?> fetchTtsStreaming(String text) async {
    if (text.trim().isEmpty) return null;
    final uri = Uri.parse(
      '$baseUrl/api/voice/tts/stream?text=${Uri.encodeComponent(text.trim())}',
    );
    final request = http.Request('GET', uri);
    if (_token != null && _token!.isNotEmpty) {
      request.headers['Authorization'] = 'Bearer $_token';
    }

    try {
      final streamed = await _client.send(request).timeout(
            const Duration(seconds: 60),
          );
      if (streamed.statusCode != 200) return null;

      final bytes = await streamed.stream.toBytes().timeout(
            const Duration(seconds: 60),
          );
      if (bytes.length < 100) return null;
      return bytes;
    } catch (e) {
      return null;
    }
  }

  /// Request TTS audio for [text] (buffered). Returns raw MP3 bytes, or null.
  Future<List<int>?> fetchTts(String text) async {
    if (text.trim().isEmpty) return null;
    final uri = Uri.parse('$baseUrl/api/voice/tts');
    final response = await _client
        .post(
          uri,
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            if (_token != null && _token!.isNotEmpty)
              'Authorization': 'Bearer $_token',
          },
          body: 'text=${Uri.encodeComponent(text)}',
        )
        .timeout(const Duration(seconds: 30));
    if (response.statusCode != 200) return null;
    final bytes = response.bodyBytes;
    final contentType = response.headers['content-type'] ?? '';
    if (contentType.contains('json')) return null;
    if (bytes.length < 100) return null;
    return bytes;
  }

  /// Transcribe raw WAV bytes via backend Whisper. Returns text or null.
  Future<String?> transcribeAudioBytes(Uint8List wavBytes) async {
    final uri = Uri.parse('$baseUrl/api/voice/transcribe');
    final request = http.MultipartRequest('POST', uri);
    if (_token != null && _token!.isNotEmpty) {
      request.headers['Authorization'] = 'Bearer $_token';
    }
    request.files.add(http.MultipartFile.fromBytes(
      'audio',
      wavBytes,
      filename: 'command.wav',
    ));
    request.fields['mime_type'] = 'audio/wav';

    final streamedResponse = await _client.send(request);
    final response = await http.Response.fromStream(streamedResponse);
    if (response.statusCode != 200) return null;

    final decoded = jsonDecode(response.body) as Map<String, dynamic>;
    final ok = decoded['ok'] as bool? ?? false;
    final text = (decoded['text'] as String?)?.trim();
    if (!ok || text == null || text.isEmpty) return null;
    return text;
  }

  /// Transcribe audio only; returns transcribed text or null on failure.
  Future<String?> transcribe(File audioFile) async {
    final uri = Uri.parse('$baseUrl/api/voice/transcribe');
    final request = http.MultipartRequest('POST', uri);
    if (_token != null && _token!.isNotEmpty) {
      request.headers['Authorization'] = 'Bearer $_token';
    }
    request.files.add(await http.MultipartFile.fromPath('audio', audioFile.path));
    request.fields['mime_type'] = 'audio/wav';

    final streamedResponse = await _client.send(request);
    final response = await http.Response.fromStream(streamedResponse);
    if (response.statusCode != 200) return null;

    final decoded = jsonDecode(response.body) as Map<String, dynamic>;
    final ok = decoded['ok'] as bool? ?? false;
    final text = (decoded['text'] as String?)?.trim();
    if (!ok || text == null || text.isEmpty) return null;
    return text;
  }

  /// Send a recorded audio file: transcribe, then send text to chat and return reply.
  Future<ChatResponse> sendVoice(File audioFile) async {
    final text = await transcribe(audioFile);
    if (text == null || text.isEmpty) {
      return ChatResponse(reply: 'No speech detected. Try again.');
    }
    final resp = await sendMessage(text);
    return ChatResponse(
      reply: 'You said: $text\n\n${resp.reply}',
      clientAction: resp.clientAction,
    );
  }

  // ── Dispatch / Brain (Bearer via [initFromStore]) ──

  Future<void> loadTokenFromStore() async {
    _token = await DeviceAuthStore.getToken();
  }

  Future<void> initFromStore() async {
    await loadTokenFromStore();
  }

  Future<Map<String, dynamic>> getDispatchStatus() async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/brain/dispatch/status'),
      headers: _headers(json: false),
    );
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(res.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (res.statusCode != 200) {
      final err = decoded?['error'] ?? decoded?['detail'] ?? res.body;
      throw Exception(err is String ? err : 'HTTP ${res.statusCode}');
    }
    return decoded ?? {};
  }

  // ── Phase F — Context Brain endpoints ───────────────────────────────
  Future<Map<String, dynamic>> getContextBrainStatus() async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/brain/context/status'),
      headers: _headers(json: false),
    );
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(res.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (res.statusCode != 200) {
      final err = decoded?['error'] ?? decoded?['detail'] ?? res.body;
      throw Exception(err is String ? err : 'HTTP ${res.statusCode}');
    }
    return decoded ?? {};
  }

  Future<List<Map<String, dynamic>>> getContextBrainAssemblies({int limit = 50}) async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/brain/context/assemblies?limit=$limit'),
      headers: _headers(json: false),
    );
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(res.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (res.statusCode != 200) return [];
    final items = (decoded?['items'] as List?) ?? [];
    return items.cast<Map<String, dynamic>>();
  }

  Future<List<Map<String, dynamic>>> getContextBrainSources() async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/brain/context/sources'),
      headers: _headers(json: false),
    );
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(res.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (res.statusCode != 200) return [];
    final items = (decoded?['items'] as List?) ?? [];
    return items.cast<Map<String, dynamic>>();
  }

  Future<List<Map<String, dynamic>>> getDispatchRuns({
    int limit = 20,
    int? taskId,
  }) async {
    final q = <String, String>{'limit': '$limit'};
    if (taskId != null) {
      q['task_id'] = '$taskId';
    }
    final uri =
        Uri.parse('$baseUrl/api/brain/dispatch/runs').replace(queryParameters: q);
    final res = await _client.get(uri, headers: _headers(json: false));
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(res.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (res.statusCode != 200) {
      final err = decoded?['error'] ?? decoded?['detail'] ?? res.body;
      throw Exception(err is String ? err : 'HTTP ${res.statusCode}');
    }
    final raw = (decoded?['runs'] ?? []) as List<dynamic>;
    return raw.map((e) => Map<String, dynamic>.from(e as Map)).toList();
  }

  Future<Map<String, dynamic>> queueDispatchTask({
    required String title,
    required String description,
    required int projectId,
    List<String>? intendedFiles,
    int? forceTier,
    String? sourceInput,
  }) async {
    final body = <String, dynamic>{
      'title': title,
      'description': description,
      'project_id': projectId,
      'intended_files': intendedFiles ?? [],
      if (forceTier != null) 'force_tier': forceTier,
      if (sourceInput != null && sourceInput.trim().isNotEmpty)
        'source_input': sourceInput.trim(),
    };
    final res = await _client.post(
      Uri.parse('$baseUrl/api/brain/dispatch/queue'),
      headers: _headers(),
      body: jsonEncode(body),
    );
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(res.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (res.statusCode != 200) {
      final err = decoded?['error'] ?? decoded?['detail'] ?? res.body;
      throw Exception(err is String ? err : 'HTTP ${res.statusCode}');
    }
    return decoded ?? {};
  }

  Future<Map<String, dynamic>> toggleKillSwitch({
    required bool active,
    String? reason,
  }) async {
    final res = await _client.post(
      Uri.parse('$baseUrl/api/brain/dispatch/kill-switch'),
      headers: _headers(),
      body: jsonEncode({'active': active, 'reason': reason}),
    );
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(res.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (res.statusCode != 200) {
      final err = decoded?['error'] ?? decoded?['detail'] ?? res.body;
      throw Exception(err is String ? err : 'HTTP ${res.statusCode}');
    }
    return decoded ?? {};
  }

  Future<List<Map<String, dynamic>>> listProjects() async {
    final res = await _client.get(
      Uri.parse('$baseUrl/api/brain/dispatch/projects'),
      headers: _headers(json: false),
    );
    Map<String, dynamic>? decoded;
    try {
      decoded = jsonDecode(res.body) as Map<String, dynamic>?;
    } catch (_) {}
    if (res.statusCode != 200) {
      final err = decoded?['error'] ?? decoded?['detail'] ?? res.body;
      throw Exception(err is String ? err : 'HTTP ${res.statusCode}');
    }
    final raw = (decoded?['projects'] ?? []) as List<dynamic>;
    return raw.map((e) => Map<String, dynamic>.from(e as Map)).toList();
  }
}
