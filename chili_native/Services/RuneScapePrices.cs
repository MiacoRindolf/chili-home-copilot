using System;
using System.Globalization;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;

namespace Chili.Services;

/// <summary>A Grand Exchange price for a RuneScape item.</summary>
public sealed record ItemPrice(string Name, long Id, long Price, long Volume, long TimestampMs);

/// <summary>Brief wiki info: an intro extract and an item thumbnail URL.</summary>
public sealed record ItemInfo(string Extract, string? ThumbUrl);

/// <summary>
/// RuneScape price + info engine — C# port of the Flutter <c>runescape_prices.dart</c>.
/// All endpoints are free + keyless:
///   • item search  → RS Wiki MediaWiki opensearch (fuzzy → exact item name)
///   • GE price     → WeirdGloop exchange/history/rs/latest
///   • brief info   → RS Wiki query (intro extract + page thumbnail)
/// </summary>
public sealed class RuneScapePrices
{
    private const string Ua = "CHILI-Home-Copilot/1.0 (rindolf.miaco@gmail.com)";
    private readonly HttpClient _http;

    public RuneScapePrices(HttpClient? http = null)
    {
        _http = http ?? new HttpClient { Timeout = TimeSpan.FromSeconds(12) };
        if (!_http.DefaultRequestHeaders.UserAgent.TryParseAdd(Ua))
            _http.DefaultRequestHeaders.TryAddWithoutValidation("User-Agent", Ua);
    }

    /// <summary>Resolve a fuzzy query to the wiki's exact item name (WeirdGloop is
    /// case/spelling sensitive). Returns null if nothing matched.</summary>
    public async Task<string?> SearchExactName(string query)
    {
        var url = "https://runescape.wiki/api.php?action=opensearch&format=json&limit=5&redirects=resolve&search="
                  + Uri.EscapeDataString(query);
        var json = await _http.GetStringAsync(url).ConfigureAwait(false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement; // [term, [names...], [descs...], [urls...]]
        if (root.ValueKind != JsonValueKind.Array || root.GetArrayLength() < 2) return null;
        var names = root[1];
        if (names.ValueKind != JsonValueKind.Array || names.GetArrayLength() == 0) return null;
        return names[0].GetString();
    }

    /// <summary>Latest GE price for an exact item name, or null if unpriced.</summary>
    public async Task<ItemPrice?> PriceByName(string name)
    {
        var url = "https://api.weirdgloop.org/exchange/history/rs/latest?name="
                  + Uri.EscapeDataString(name);
        var json = await _http.GetStringAsync(url).ConfigureAwait(false);
        return ParseWeirdGloopLatest(json, name);
    }

    /// <summary>Search then price in one call (fuzzy query → GE price).</summary>
    public async Task<ItemPrice?> Lookup(string query)
    {
        var exact = await SearchExactName(query).ConfigureAwait(false);
        if (string.IsNullOrWhiteSpace(exact)) return null;
        return await PriceByName(exact!).ConfigureAwait(false);
    }

    /// <summary>Download raw bytes (e.g. an item thumbnail) with the shared UA.</summary>
    public Task<byte[]> FetchBytes(string url) => _http.GetByteArrayAsync(url);

    /// <summary>Best-effort intro blurb + thumbnail for an item.</summary>
    public async Task<ItemInfo> WikiInfo(string name)
    {
        var url = "https://runescape.wiki/api.php?action=query&prop=extracts%7Cpageimages"
                  + "&exintro&explaintext&piprop=thumbnail&pithumbsize=96&redirects=1&format=json&titles="
                  + Uri.EscapeDataString(name);
        var json = await _http.GetStringAsync(url).ConfigureAwait(false);
        return ParseWikiInfo(json);
    }

    // ---- pure parsers (unit-testable) -------------------------------------

    /// <summary>Parse a WeirdGloop <c>/latest</c> body. The response is an object
    /// keyed by item name → {id, price, volume, timestamp}.</summary>
    public static ItemPrice? ParseWeirdGloopLatest(string json, string fallbackName)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (root.ValueKind != JsonValueKind.Object) return null;
        foreach (var prop in root.EnumerateObject())
        {
            var v = prop.Value;
            if (v.ValueKind != JsonValueKind.Object) continue;
            // an error payload looks like {"success":false,...}
            if (prop.NameEquals("success") || prop.NameEquals("error")) continue;
            var name = prop.Name.Length > 0 ? prop.Name : fallbackName;
            return new ItemPrice(
                name,
                ReadLong(v, "id"),
                ReadLong(v, "price"),
                ReadLong(v, "volume"),
                ReadTimestampMs(v, "timestamp"));
        }
        return null;
    }

    /// <summary>Parse the wiki query response → extract + thumbnail.</summary>
    public static ItemInfo ParseWikiInfo(string json)
    {
        using var doc = JsonDocument.Parse(json);
        if (!doc.RootElement.TryGetProperty("query", out var q) ||
            !q.TryGetProperty("pages", out var pages) ||
            pages.ValueKind != JsonValueKind.Object)
            return new ItemInfo("", null);

        foreach (var page in pages.EnumerateObject())
        {
            var p = page.Value;
            var extract = p.TryGetProperty("extract", out var e) ? e.GetString() ?? "" : "";
            string? thumb = null;
            if (p.TryGetProperty("thumbnail", out var t) &&
                t.TryGetProperty("source", out var s))
                thumb = s.GetString();
            return new ItemInfo(extract, thumb);
        }
        return new ItemInfo("", null);
    }

    private static long ReadLong(JsonElement obj, string key)
    {
        if (!obj.TryGetProperty(key, out var el)) return 0;
        switch (el.ValueKind)
        {
            case JsonValueKind.Number:
                return el.TryGetInt64(out var n) ? n : (long)el.GetDouble();
            case JsonValueKind.String:
                return long.TryParse(el.GetString(), NumberStyles.Any,
                    CultureInfo.InvariantCulture, out var s) ? s : 0;
            default:
                return 0;
        }
    }

    private static long ReadTimestampMs(JsonElement obj, string key)
    {
        if (!obj.TryGetProperty(key, out var el)) return 0;
        if (el.ValueKind == JsonValueKind.Number && el.TryGetInt64(out var ms)) return ms;
        if (el.ValueKind == JsonValueKind.String)
        {
            var str = el.GetString();
            if (long.TryParse(str, out var asMs)) return asMs;
            if (DateTimeOffset.TryParse(str, CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal, out var dto))
                return dto.ToUnixTimeMilliseconds();
        }
        return 0;
    }

    // ---- formatting helpers ----------------------------------------------

    /// <summary>Full grouped number, e.g. 85139 → "85,139".</summary>
    public static string FormatGpFull(long n) => n.ToString("N0", CultureInfo.InvariantCulture);

    /// <summary>Abbreviated price, e.g. 85139 → "85.1k", 1234567 → "1.2m".</summary>
    public static string FormatGp(long n)
    {
        double a = Math.Abs(n);
        string sign = n < 0 ? "-" : "";
        if (a >= 1_000_000_000) return $"{sign}{a / 1_000_000_000:0.#}b";
        if (a >= 1_000_000) return $"{sign}{a / 1_000_000:0.#}m";
        if (a >= 1_000) return $"{sign}{a / 1_000:0.#}k";
        return n.ToString(CultureInfo.InvariantCulture);
    }

    /// <summary>Trim a wiki extract to a short blurb at a word boundary.</summary>
    public static string BriefBlurb(string extract, int maxChars = 160)
    {
        if (string.IsNullOrWhiteSpace(extract)) return "";
        var s = extract.Trim();
        if (s.Length <= maxChars) return s;
        var cut = s.LastIndexOf(' ', Math.Min(maxChars, s.Length - 1));
        if (cut < maxChars / 2) cut = maxChars;
        return s.Substring(0, cut).TrimEnd() + "…";
    }
}
