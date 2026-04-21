using Azure.Identity;
using Azure.Messaging.ServiceBus;
using Microsoft.Extensions.Options;
using OmniVec.Worker.Configuration;
using OmniVec.Worker.Destinations;
using OmniVec.Worker.Services;

ThreadPool.SetMinThreads(500, 500);

var builder = Host.CreateApplicationBuilder(args);

builder.Services.Configure<WorkerOptions>(
    builder.Configuration.GetSection("Worker"));

// Service Bus client — only register when namespace is configured.
// When not configured, return null; EmbeddingWorkerService detects this, logs a
// warning, marks the pod Ready (so helm --wait doesn't hang), and idles. This
// allows deployments without blob source (no Service Bus provisioned) to run
// cleanly instead of CrashLoopBackOff'ing the worker pod.
builder.Services.AddSingleton(sp =>
{
    var opts = sp.GetRequiredService<IOptions<WorkerOptions>>().Value;
    if (string.IsNullOrWhiteSpace(opts.ServiceBusNamespace))
    {
        var log = sp.GetRequiredService<ILogger<Program>>();
        log.LogWarning("Service Bus namespace not configured — worker will start but stay idle (set Worker__ServiceBusNamespace to enable queue processing).");
        return (ServiceBusClient?)null;
    }
    return new ServiceBusClient(opts.ServiceBusNamespace, new DefaultAzureCredential());
});

// DocGrok HTTP client
builder.Services.AddHttpClient<DocGrokClient>((sp, client) =>
{
    var opts = sp.GetRequiredService<IOptions<WorkerOptions>>().Value;
    client.BaseAddress = new Uri(opts.DocGrokBaseUrl);
    client.Timeout = TimeSpan.FromSeconds(120);
});

// Metrics reporter HTTP client
builder.Services.AddHttpClient<MetricsReporter>((sp, client) =>
{
    var opts = sp.GetRequiredService<IOptions<WorkerOptions>>().Value;
    client.BaseAddress = new Uri(opts.OmniVecApiBaseUrl);
    client.Timeout = TimeSpan.FromSeconds(10);
});

// Destination writers
builder.Services.AddSingleton<IDestinationWriter, CosmosDbDestinationWriter>();
builder.Services.AddSingleton<IDestinationWriter, PostgresDestinationWriter>();
builder.Services.AddSingleton<IDestinationWriter, MsSqlDestinationWriter>();

// Health endpoint (must be a hosted service so it runs alongside the worker)
builder.Services.AddHostedService<HealthEndpointService>();

// Worker
builder.Services.AddHostedService<EmbeddingWorkerService>();

var host = builder.Build();

var logger = host.Services.GetRequiredService<ILogger<Program>>();
var opts = host.Services.GetRequiredService<IOptions<WorkerOptions>>().Value;
logger.LogInformation("OmniVec .NET Worker starting");
logger.LogInformation("  Service Bus: {Namespace}", opts.ServiceBusNamespace);
logger.LogInformation("  Topic: {Topic}, Subscription: {Sub}", opts.TopicName, opts.SubscriptionName);
logger.LogInformation("  DocGrok: {Url}", opts.DocGrokBaseUrl);
logger.LogInformation("  Concurrency: {Concurrency}, Batch: {Batch}", opts.MaxConcurrentCalls, opts.EmbedBatchSize);

await host.RunAsync();
