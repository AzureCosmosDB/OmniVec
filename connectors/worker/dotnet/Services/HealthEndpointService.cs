using System.Net;
using System.Text;

namespace OmniVec.Worker.Services;

/// <summary>
/// Minimal HTTP health endpoint for Kubernetes probes.
/// Exposes:
///   GET /healthz  — liveness (200 if the process is running; 503 if receive loop has been
///                   dead for longer than UnhealthyAfterSeconds after startup).
///   GET /ready    — readiness (200 once the worker has completed at least one successful
///                   receive cycle, meaning SB auth + network are working).
///
/// Deliberately uses HttpListener so we don't need to pull in ASP.NET Core.
/// </summary>
public class HealthEndpointService : BackgroundService
{
    private readonly ILogger<HealthEndpointService> _logger;
    private readonly int _port;

    public HealthEndpointService(ILogger<HealthEndpointService> logger)
    {
        _logger = logger;
        _port = int.TryParse(Environment.GetEnvironmentVariable("HEALTH_PORT"), out var p) ? p : 8080;
    }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        // Everything inside runs on a BackgroundService. In .NET 8 an uncaught
        // exception here causes HostOptions.BackgroundServiceExceptionBehavior
        // (default: StopHost) to terminate the entire process — including the
        // EmbeddingWorkerService — with exit 0. A broken health listener must
        // never kill the worker. We isolate failures here so the worker keeps
        // running; kubelet's startup probe will surface the bad state.
        try
        {
            await RunListenerAsync(ct);
        }
        catch (OperationCanceledException) { /* shutdown */ }
        catch (Exception ex)
        {
            _logger.LogError(ex,
                "Health endpoint crashed — probes will fail but worker stays up. " +
                "Set HEALTH_PORT or investigate HttpListener support in this runtime.");
            // Idle until shutdown so this BackgroundService doesn't fault the host.
            try { await Task.Delay(Timeout.Infinite, ct); } catch { /* shutdown */ }
        }
    }

    private async Task RunListenerAsync(CancellationToken ct)
    {
        var listener = new HttpListener();
        listener.Prefixes.Add($"http://+:{_port}/");
        try
        {
            listener.Start();
        }
        catch (Exception ex)
        {
            // Common on dev machines without netsh urlacl, and in some Linux
            // container runtimes where the '+' prefix needs elevated caps.
            // Fall back to localhost-only (also wrapped so the whole host
            // does not go down if even that fails).
            _logger.LogWarning(ex, "HttpListener could not bind to http://+:{Port}/, falling back to http://127.0.0.1:{Port}/", _port, _port);
            listener = new HttpListener();
            listener.Prefixes.Add($"http://127.0.0.1:{_port}/");
            listener.Start();
        }

        _logger.LogInformation("Health endpoint listening on port {Port} (/healthz, /ready)", _port);

        while (!ct.IsCancellationRequested)
        {
            HttpListenerContext context;
            try
            {
                context = await listener.GetContextAsync().WaitAsync(ct);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                _logger.LogDebug(ex, "Health listener accept error");
                continue;
            }

            _ = Task.Run(() => HandleAsync(context, ct), ct);
        }

        try { listener.Stop(); } catch { /* ignore */ }
    }

    private static async Task HandleAsync(HttpListenerContext ctx, CancellationToken ct)
    {
        string path = ctx.Request.Url?.AbsolutePath?.ToLowerInvariant() ?? "/";
        var (status, payload) = path switch
        {
            "/healthz" => EvaluateLiveness(),
            "/ready"   => EvaluateReadiness(),
            _          => (404, "not found"),
        };

        ctx.Response.StatusCode = status;
        ctx.Response.ContentType = "text/plain";
        var bytes = Encoding.UTF8.GetBytes(payload);
        ctx.Response.ContentLength64 = bytes.Length;
        try
        {
            await ctx.Response.OutputStream.WriteAsync(bytes, 0, bytes.Length, ct);
        }
        catch { /* client gone */ }
        finally
        {
            try { ctx.Response.OutputStream.Close(); } catch { }
        }
    }

    private static (int, string) EvaluateLiveness()
    {
        // Liveness: alive as long as the receive loop has heart-beaten within
        // WorkerHeartbeat.UnhealthyAfterSeconds (or we're still in grace period).
        if (WorkerHeartbeat.IsHealthy(out var ageSec))
            return (200, $"ok age_s={ageSec}");
        return (503, $"stale last_beat_age_s={ageSec}");
    }

    private static (int, string) EvaluateReadiness()
    {
        if (WorkerHeartbeat.HasReceivedFirstMessage)
            return (200, "ready");
        return (503, "not_ready — no successful receive yet");
    }
}

/// <summary>Shared heartbeat state between the receive loop and health endpoint.</summary>
public static class WorkerHeartbeat
{
    private static long _lastBeatTicks = DateTime.UtcNow.Ticks;
    private static int _hasReceivedFirstMessage; // 0/1
    private static readonly DateTime _startedAt = DateTime.UtcNow;

    /// <summary>Grace period after startup during which liveness always passes.</summary>
    public static int GraceSeconds { get; set; } = 60;

    /// <summary>If no beat within this window (after grace), liveness fails.</summary>
    public static int UnhealthyAfterSeconds { get; set; } = 120;

    public static void Beat()
    {
        Interlocked.Exchange(ref _lastBeatTicks, DateTime.UtcNow.Ticks);
    }

    public static void MarkReceivedFirstMessage()
    {
        Interlocked.CompareExchange(ref _hasReceivedFirstMessage, 1, 0);
        Beat();
    }

    public static bool HasReceivedFirstMessage =>
        Interlocked.CompareExchange(ref _hasReceivedFirstMessage, 0, 0) == 1;

    public static bool IsHealthy(out long ageSeconds)
    {
        var lastBeat = new DateTime(Interlocked.Read(ref _lastBeatTicks), DateTimeKind.Utc);
        ageSeconds = (long)(DateTime.UtcNow - lastBeat).TotalSeconds;

        // Always healthy during grace period after process start.
        if ((DateTime.UtcNow - _startedAt).TotalSeconds < GraceSeconds) return true;

        return ageSeconds < UnhealthyAfterSeconds;
    }
}
