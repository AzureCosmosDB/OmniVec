namespace OmniVec.Worker.Services;

/// <summary>
/// HTTP failure after bounded retries, or an immediate permanent response.
/// Only input-specific 400/413/415/422 responses may trigger bisect/dead-letter.
/// Exhausted 429/5xx and configuration/auth failures must not discard a batch.
/// </summary>
public class EmbeddingClientException : Exception
{
    public int StatusCode { get; }
    public string? ResponseBody { get; }

    public EmbeddingClientException(int statusCode, string? body, string message)
        : base(message)
    {
        StatusCode = statusCode;
        ResponseBody = body;
    }
}
