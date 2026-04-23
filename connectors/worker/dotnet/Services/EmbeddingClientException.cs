namespace OmniVec.Worker.Services;

/// <summary>
/// Thrown by DocGrokClient when the embed endpoint returns a non-retryable
/// 4xx response (e.g., 400 "maximum context length exceeded", 413 payload
/// too large). The worker handles this by bisecting the batch and
/// dead-lettering the offending message rather than abandoning the whole
/// batch into an infinite redelivery loop.
///
/// 429 (Too Many Requests) is NOT mapped to this exception — it is retried
/// transparently by the client.
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
