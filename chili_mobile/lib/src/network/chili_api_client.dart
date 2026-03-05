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
}
