using System;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace Chili.Services;

/// <summary>
/// Talks to the CHILI FastAPI backend the same way the Flutter app does:
/// Bearer-token auth, JSON bodies, and an SSE stream for chat. Accepts the local
/// dev self-signed certificate for localhost only.
/// </summary>
public sealed class ChiliApiClient
{
    private readonly HttpClient _http;
    private readonly AppSettings _settings;

    public ChiliApiClient(AppSettings settings)
    {
        _settings = settings;
        var handler = new HttpClientHandler
        {
            // Trust the local dev cert for localhost only; never blanket-trust remote hosts.
            ServerCertificateCustomValidationCallback = (msg, _, _, _) =>
            {
                var host = msg.RequestUri?.Host ?? "";
                return host is "localhost" or "127.0.0.1" or "::1";
            }
        };
        _http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(120) };
    }

    private HttpRequestMessage Build(HttpMethod method, string path, HttpContent? body = null)
    {
        var req = new HttpRequestMessage(method, _settings.BaseUrl.TrimEnd('/') + path);
        if (!string.IsNullOrEmpty(_settings.DeviceToken))
            req.Headers.Add("Authorization", "Bearer " + _settings.DeviceToken);
        if (body != null) req.Content = body;
        return req;
    }

    /// <summary>Liveness check against /healthz.</summary>
    public async Task<bool> HealthAsync()
    {
        try
        {
            using var r = await _http.SendAsync(Build(HttpMethod.Get, "/healthz"));
            return r.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    /// <summary>GET a JSON endpoint and return the (cloned) root element, or null.</summary>
    public async Task<JsonElement?> GetJsonAsync(string path, CancellationToken ct = default)
    {
        try
        {
            using var resp = await _http.SendAsync(Build(HttpMethod.Get, path), ct);
            if (!resp.IsSuccessStatusCode) return null;
            var s = await resp.Content.ReadAsStringAsync(ct);
            using var doc = JsonDocument.Parse(s);
            return doc.RootElement.Clone(); // survives doc disposal
        }
        catch { return null; }
    }

    /// <summary>POST a JSON body and return the (cloned) root element, or null.</summary>
    public async Task<JsonElement?> PostJsonAsync(string path, object body, CancellationToken ct = default)
    {
        try
        {
            var content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");
            using var resp = await _http.SendAsync(Build(HttpMethod.Post, path, content), ct);
            if (!resp.IsSuccessStatusCode) return null;
            var s = await resp.Content.ReadAsStringAsync(ct);
            using var doc = JsonDocument.Parse(s);
            return doc.RootElement.Clone();
        }
        catch { return null; }
    }

    /// <summary>Stream a chat reply token-by-token from /api/mobile/chat/stream.
    /// Returns the conversation id from the final event (for threading).</summary>
    public async Task<int?> StreamChatAsync(string message, int? conversationId,
        Action<string> onToken, CancellationToken ct = default)
    {
        var payload = JsonSerializer.Serialize(new { message, conversation_id = conversationId });
        var req = Build(HttpMethod.Post, "/api/mobile/chat/stream",
            new StringContent(payload, Encoding.UTF8, "application/json"));

        using var resp = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);
        resp.EnsureSuccessStatusCode();

        await using var stream = await resp.Content.ReadAsStreamAsync(ct);
        using var reader = new StreamReader(stream);

        int? convoId = conversationId;
        string? line;
        while ((line = await reader.ReadLineAsync(ct)) != null)
        {
            if (!line.StartsWith("data:", StringComparison.Ordinal)) continue;
            var json = line.Substring(5).Trim();
            if (json.Length == 0) continue;
            try
            {
                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;
                if (root.TryGetProperty("token", out var tok))
                {
                    var t = tok.GetString();
                    if (!string.IsNullOrEmpty(t)) onToken(t!);
                }
                if (root.TryGetProperty("done", out var d) &&
                    d.ValueKind is JsonValueKind.True or JsonValueKind.False && d.GetBoolean())
                {
                    if (root.TryGetProperty("conversation_id", out var c) &&
                        c.ValueKind == JsonValueKind.Number)
                        convoId = c.GetInt32();
                    break;
                }
            }
            catch { /* skip malformed event */ }
        }
        return convoId;
    }
}
