using System.Net.Http.Headers;
using System.Net;
using System.Text.Json;
using Azure.Core;
using Azure.Identity;

namespace OmniVec.Worker.Services;

/// <summary>Downloads SharePoint drive items through Microsoft Graph using workload identity.</summary>
public sealed class SharePointContentClient
{
    private static readonly string[] GraphScopes = ["https://graph.microsoft.com/.default"];
    private readonly HttpClient _http;
    private readonly DefaultAzureCredential _credential = new();

    public SharePointContentClient(HttpClient http)
    {
        _http = http;
    }

    public async Task<byte[]> DownloadAsync(
        string siteId,
        string driveId,
        string itemId,
        long maxBytes,
        CancellationToken ct,
        string? expectedETag = null)
    {
        if (maxBytes <= 0 || maxBytes > 50L * 1024 * 1024)
            throw new ArgumentOutOfRangeException(nameof(maxBytes), "SharePoint files must be limited to at most 50 MiB");
        var token = await _credential.GetTokenAsync(new TokenRequestContext(GraphScopes), ct);
        var url =
            $"https://graph.microsoft.com/v1.0/sites/{Uri.EscapeDataString(siteId)}" +
            $"/drives/{Uri.EscapeDataString(driveId)}/items/{Uri.EscapeDataString(itemId)}";
        if (expectedETag is not null) await VerifyVersionAsync(url, token.Token, expectedETag, ct);
        using var request = new HttpRequestMessage(HttpMethod.Get, url + "/content");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token.Token);
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
        if (response.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.Gone)
            throw new SharePointVersionChangedException("Item was removed before download");
        response.EnsureSuccessStatusCode();

        if (response.Content.Headers.ContentLength is long length && length > maxBytes)
            throw new InvalidOperationException(
                $"SharePoint file is {length} bytes, exceeding the configured limit of {maxBytes} bytes");

        await using var stream = await response.Content.ReadAsStreamAsync(ct);
        using var buffer = new MemoryStream();
        var block = new byte[81920];
        while (true)
        {
            var read = await stream.ReadAsync(block, ct);
            if (read == 0) break;
            if (buffer.Length + read > maxBytes)
                throw new InvalidOperationException(
                    $"SharePoint file exceeds the configured limit of {maxBytes} bytes");
            await buffer.WriteAsync(block.AsMemory(0, read), ct);
        }
        // Graph's content endpoint redirects to storage; do not rely on a storage
        // ETag matching Graph's eTag, or on If-Match being forwarded on redirect.
        if (expectedETag is not null) await VerifyVersionAsync(url, token.Token, expectedETag, ct);
        return buffer.ToArray();
    }

    private async Task VerifyVersionAsync(string url, string token, string expected, CancellationToken ct)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, url + "?$select=id,eTag");
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        using var response = await _http.SendAsync(request, ct);
        if (response.StatusCode is HttpStatusCode.NotFound or HttpStatusCode.Gone)
            throw new SharePointVersionChangedException("Item no longer exists");
        response.EnsureSuccessStatusCode();
        using var json = JsonDocument.Parse(await response.Content.ReadAsStringAsync(ct));
        if (!json.RootElement.TryGetProperty("eTag", out var etag) || etag.GetString() != expected)
            throw new SharePointVersionChangedException("Item eTag no longer matches the queued version");
    }
}

public sealed class SharePointVersionChangedException(string message) : Exception(message);
