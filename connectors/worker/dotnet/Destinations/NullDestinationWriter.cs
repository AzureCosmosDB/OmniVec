using System.Collections.Concurrent;
using System.Globalization;
using System.Text.Json;

namespace OmniVec.Worker.Destinations;

public sealed class NullDestinationWriter : IDestinationWriter
{
    private sealed class RateGate
    {
        public SemaphoreSlim Lock { get; } = new(1, 1);
        public DateTimeOffset NextAvailable { get; set; } = DateTimeOffset.UtcNow;
    }

    private readonly ConcurrentDictionary<string, RateGate> _rateGates = new();

    public string DestinationType => "null";

    public async Task WriteBatchAsync(
        Dictionary<string, object> config,
        List<EmbeddingResult> results,
        CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        var rate = GetRate(config);
        if (rate <= 0 || results.Count == 0)
            return;

        var rateKey = GetString(config, "rate_key", "default");
        var gate = _rateGates.GetOrAdd(rateKey, _ => new RateGate());
        TimeSpan delay;
        await gate.Lock.WaitAsync(ct);
        try
        {
            var now = DateTimeOffset.UtcNow;
            if (gate.NextAvailable < now)
                gate.NextAvailable = now;
            delay = gate.NextAvailable - now;
            gate.NextAvailable += TimeSpan.FromSeconds(results.Count / rate);
        }
        finally
        {
            gate.Lock.Release();
        }

        if (delay > TimeSpan.Zero)
            await Task.Delay(delay, ct);
    }

    private static double GetRate(Dictionary<string, object> config)
    {
        if (!config.TryGetValue("max_docs_per_second", out var value) || value is null)
            return 0;
        var rate = value is JsonElement element
            ? element.GetDouble()
            : Convert.ToDouble(value, CultureInfo.InvariantCulture);
        if (!double.IsFinite(rate) || rate < 0)
            throw new ArgumentException("null destination max_docs_per_second must be a finite non-negative number");
        return rate;
    }

    private static string GetString(
        Dictionary<string, object> config,
        string key,
        string fallback)
    {
        if (!config.TryGetValue(key, out var value) || value is null)
            return fallback;
        var text = value is JsonElement element ? element.GetString() : value.ToString();
        return string.IsNullOrWhiteSpace(text) ? fallback : text;
    }
}
