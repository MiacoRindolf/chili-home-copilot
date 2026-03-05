import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:http/io_client.dart';

/// API client for the CHILI backend.
///
/// Uses an [IOClient] that trusts all certificates so self-signed
/// local dev certs (localhost.pem) work without extra setup.
class ChiliApiClient {
  static const String baseUrl = 'https://localhost:8000';

  String? _token;

  set token(String? value) => _token = value;

  static final http.Client _client = _buildClient();

  static http.Client _buildClient() {
    final context = SecurityContext.defaultContext;
    final httpClient = HttpClient(context: context)
      ..badCertificateCallback = (cert, host, port) => true;
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

  // ── Chat ──

  Future<String> sendMessage(String message) async {
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
    return (decoded?['reply'] as String?) ?? 'CHILI did not send a reply.';
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

  /// Request TTS audio for [text]. Returns raw MP3 bytes, or null on failure.
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
  Future<String> sendVoice(File audioFile) async {
    final text = await transcribe(audioFile);
    if (text == null || text.isEmpty) {
      return 'No speech detected. Try again.';
    }
    final reply = await sendMessage(text);
    return 'You said: $text\n\n$reply';
  }
}
