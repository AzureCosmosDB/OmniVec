using System.Net.Http.Headers;
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
        CancellationToken ct)
    {
        var token = await _credential.GetTokenAsync(new TokenRequestContext(GraphScopes), ct);
        var url =
            $"https://graph.microsoft.com/v1.0/sites/{Uri.EscapeDataString(siteId)}" +
            $"/drives/{Uri.EscapeDataString(driveId)}/items/{Uri.EscapeDataString(itemId)}/content";
        using var request = new HttpRequestMessage(HttpMethod.Get, url);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token.Token);
        using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
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
        return buffer.ToArray();
    }
}
