namespace OmniVec.Worker.Destinations;

public readonly record struct GarnetEndpoint(string Host, int Port, bool Tls)
{
    public static GarnetEndpoint Parse(string endpoint, bool tls = true)
    {
        var value = endpoint.Trim();
        if (!Uri.TryCreate(value.Contains("://", StringComparison.Ordinal) ? value : $"redis://{value}",
                UriKind.Absolute, out var uri)
            || uri.Scheme is not ("redis" or "rediss")
            || string.IsNullOrWhiteSpace(uri.Host)
            || !string.IsNullOrEmpty(uri.UserInfo)
            || uri.AbsolutePath is not ("" or "/")
            || !string.IsNullOrEmpty(uri.Query)
            || !string.IsNullOrEmpty(uri.Fragment))
            throw new ArgumentException("Garnet endpoint must be a host[:port] or redis[s] URL without credentials or path");
        var port = uri.Port < 0 ? (tls ? 6380 : 6379) : uri.Port;
        if (port < 1 || port > 65535)
            throw new ArgumentException("Garnet endpoint port must be between 1 and 65535");
        return new GarnetEndpoint(uri.Host.Trim('[', ']').TrimEnd('.').ToLowerInvariant(), port, tls);
    }
}
