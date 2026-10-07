using OmniVec.Mock;
using Newtonsoft.Json.Linq;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;

var builder = WebApplication.CreateBuilder(args);
builder.Logging.AddFilter("Microsoft.AspNetCore", LogLevel.Warning);
builder.WebHost.ConfigureKestrel(options => options.Limits.MaxRequestBodySize = Wire.MaxBody);
builder.Services.AddSingleton<Metadata>();
builder.Services.AddSingleton<IMetadata>(services => services.GetRequiredService<Metadata>());
builder.Services.AddSingleton<Receiver>();
var app = builder.Build();
_ = app.Services.GetRequiredService<Receiver>();
using (var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(30)))
    await app.Services.GetRequiredService<Metadata>().Ready(deadline.Token);
app.MapGet("/health", () => Results.Json(new { status = "healthy", component = "mock-destination", implementation = "dotnet" }));
app.MapGet("/ready", async (Metadata metadata, CancellationToken token) =>
{
    using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
    deadline.CancelAfter(TimeSpan.FromSeconds(4));
    try { await metadata.Ready(deadline.Token); return Results.Ok(new { status = "ready" }); }
    catch (Exception error) { app.Logger.LogError(error, "Mock destination readiness failed"); return Results.StatusCode(503); }
});
app.MapPost("/api/mock-sinks/{id}/accept", async (string id, string run_id, string batch_id,
    HttpRequest request, Receiver receiver, CancellationToken token) =>
{
    var timer = Stopwatch.StartNew();
    if (!receiver.Authorized(request.Headers.Authorization.ToString())) return Results.Unauthorized();
    if (run_id.Length > 128 || batch_id.Length > 128)
        return Results.BadRequest(new { detail = "Mock receipt identifiers are too long" });
    if (request.ContentType?.Split(';')[0] != Wire.Media) return Results.StatusCode(415);
    using var body = new MemoryStream();
    await request.Body.CopyToAsync(body, token);
    var payload = body.ToArray();
    if (payload.Length > Wire.MaxBody) return Results.StatusCode(413);
    try
    {
        var receipt = await receiver.Accept(id, run_id, batch_id, payload, token);
        receipt["request_seconds"] = timer.Elapsed.TotalSeconds;
        return Results.Text(receipt.ToString(Newtonsoft.Json.Formatting.None), "application/json");
    }
    catch (SinkError error)
    {
        if (error.RetryAfter is not null) request.HttpContext.Response.Headers.RetryAfter = error.RetryAfter;
        return Results.Json(new { detail = error.Message }, statusCode: error.Status);
    }
    catch (ArgumentException error) { return Results.BadRequest(new { detail = error.Message }); }
    catch (Exception error) { app.Logger.LogError(error, "Mock sink request failed"); return Results.StatusCode(503); }
});
await app.RunAsync();

public sealed class SinkError(int status, string message, string? retryAfter = null) : Exception(message)
{
    public int Status { get; } = status;
    public string? RetryAfter { get; } = retryAfter;
}

public sealed class Receiver
{
    private sealed class State
    {
        public double Tokens;
        public double Updated;
        public Dictionary<(string Run, string Batch), string> Receipts = new();
    }
    private readonly IMetadata metadata;
    private readonly SemaphoreSlim gate = new(1);
    private readonly Dictionary<string, (double Expires, JObject? Config)> configs = new();
    private readonly Dictionary<string, State> states = new();
    private readonly double refresh = Wire.RefreshSeconds();
    private readonly int shard;
    private readonly byte[] authorization;

    public Receiver(IMetadata metadata)
    {
        this.metadata = metadata;
        shard = int.Parse(Environment.GetEnvironmentVariable("OMNIVEC_MOCK_RECEIVER_SHARD") ?? "0");
        if (shard is < 0 or > 31) throw new InvalidOperationException("Invalid mock receiver shard");
        string token = Environment.GetEnvironmentVariable("OMNIVEC_ADMIN_TOKEN") ?? "";
        if (string.IsNullOrWhiteSpace(token)) throw new InvalidOperationException("Mock destination requires OMNIVEC_ADMIN_TOKEN");
        authorization = Encoding.UTF8.GetBytes("Bearer " + token);
    }

    public bool Authorized(string value) => CryptographicOperations.FixedTimeEquals(
        authorization, Encoding.UTF8.GetBytes(value));

    public async Task<JObject> Accept(string id, string run, string batch, byte[] payload, CancellationToken token)
    {
        var timer = Stopwatch.StartNew();
        JObject? destination;
        await gate.WaitAsync(token);
        try
        {
            double now = Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;
            if (!configs.TryGetValue(id, out var entry) || entry.Expires <= now)
            {
                foreach (var key in configs.Where(item => item.Value.Expires <= now).Select(item => item.Key).ToList())
                    configs.Remove(key);
                if (configs.Count >= 100) throw new SinkError(503, "Metadata cache capacity reached");
                destination = await metadata.Read(id, "destination", token);
                configs[id] = (now + refresh * (0.8 + Random.Shared.NextDouble() * 0.2), destination);
            }
            else destination = entry.Config;
        }
        finally { gate.Release(); }
        double metadataSeconds = timer.Elapsed.TotalSeconds;
        if (destination is null || (string?)destination["type"] != "mock" || (bool?)destination["enabled"] == false)
            throw new SinkError(404, "Enabled mock destination not found");
        JObject cfg = (JObject)destination["config"]!;
        if (((int?)cfg["receiver_shard"] ?? 0) != shard) throw new SinkError(409, "Incorrect receiver shard");
        int dimensions = (int)cfg["embedding_dimensions"]!;
        double configuredRate = (double)cfg["accepted_documents_per_second"]!;
        int configuredBurst = (int)cfg["burst_documents"]!;
        if (dimensions is < 1 or > 65536 || !double.IsFinite(configuredRate)
            || configuredRate is < 0 or > 10_000_000 || configuredBurst is < 1 or > 1_000_000)
            throw new SinkError(503, "Invalid mock destination configuration");
        timer.Restart();
        int count = Wire.Validate(payload, dimensions);
        string digest = Wire.Digest(payload);
        double validationSeconds = timer.Elapsed.TotalSeconds;
        await gate.WaitAsync(token);
        try
        {
            double now = Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;
            foreach (var key in states.Where(item => now - item.Value.Updated > 3600).Select(item => item.Key).ToList())
                states.Remove(key);
            if (!states.TryGetValue(id, out var state))
            {
                if (states.Count >= 100) throw new SinkError(503, "Active destination capacity reached");
                state = new State { Tokens = (double)cfg["burst_documents"]!, Updated = now };
                states[id] = state;
            }
            var keyPair = (run, batch);
            bool dedup = state.Receipts.TryGetValue(keyPair, out var prior);
            if (dedup && prior != digest) throw new SinkError(409, "Batch replay payload differs");
            if (!dedup)
            {
                double rate = (double)cfg["accepted_documents_per_second"]!;
                double burst = (double)cfg["burst_documents"]!;
                state.Tokens = Math.Min(burst, state.Tokens + (now - state.Updated) * rate);
                state.Updated = now;
                if (rate > 0 && count > burst) throw new SinkError(422, "Batch exceeds burst capacity");
                if (rate > 0 && count > state.Tokens)
                    throw new SinkError(429, "Mock sink acceptance rate exceeded",
                        Math.Max(1, Math.Ceiling((count - state.Tokens) / rate)).ToString(System.Globalization.CultureInfo.InvariantCulture));
                if (state.Receipts.Count >= 200_000) throw new SinkError(503, "Receipt cache capacity reached");
                if (rate > 0) state.Tokens -= count;
                state.Receipts[keyPair] = digest;
            }
            return new JObject { ["accepted"] = count, ["payload_bytes"] = payload.Length,
                ["sha256"] = digest, ["dedup"] = dedup, ["validation_seconds"] = validationSeconds,
                ["metadata_seconds"] = metadataSeconds };
        }
        finally { gate.Release(); }
    }
}
