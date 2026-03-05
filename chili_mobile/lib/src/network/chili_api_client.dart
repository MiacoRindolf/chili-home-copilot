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
    if (response.statusCode != 200) {
      throw Exception('Chat request failed with ${response.statusCode}');
    }
    final decoded = jsonDecode(response.body) as Map<String, dynamic>;
    return (decoded['reply'] as String?) ?? 'CHILI did not send a reply.';
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

  /// Send a recorded audio file to the backend for STT + chat processing.
  /// Falls back to the chat endpoint with a note if no voice endpoint exists.
  Future<String> sendVoice(File audioFile) async {
    final uri = Uri.parse('$baseUrl/api/voice/transcribe');
    final request = http.MultipartRequest('POST', uri);
    if (_token != null && _token!.isNotEmpty) {
      request.headers['Authorization'] = 'Bearer $_token';
    }
    request.files.add(await http.MultipartFile.fromPath('audio', audioFile.path));

    // Use the underlying IOClient's inner HttpClient for multipart.
    final innerClient = HttpClient()
      ..badCertificateCallback = (cert, host, port) => true;
    try {
      final ioRequest = await innerClient.openUrl(request.method, uri);
      request.headers.forEach((k, v) => ioRequest.headers.set(k, v));
      final bodyBytes = await request.finalize().toBytes();
      ioRequest.headers.contentType = ContentType.parse(request.headers['content-type'] ?? 'multipart/form-data');
      ioRequest.contentLength = bodyBytes.length;
      ioRequest.add(bodyBytes);

      final ioResponse = await ioRequest.close();
      final responseBody = await ioResponse.transform(const SystemEncoding().decoder).join();

      if (ioResponse.statusCode == 200) {
        final decoded = jsonDecode(responseBody) as Map<String, dynamic>;
        return (decoded['reply'] ?? decoded['text'] ?? 'No transcription.') as String;
      } else if (ioResponse.statusCode == 404) {
        return 'Voice endpoint not available. Send text messages instead.';
      } else {
        return 'Voice request failed (${ioResponse.statusCode}).';
      }
    } finally {
      innerClient.close();
    }
  }
}
