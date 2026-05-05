using Azure.Identity;
using Microsoft.Azure.Cosmos;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Hosting;
using OmniVec.ChangeFeed.Services;

// Prevent thread starvation under high concurrency (CosmosDB patches + DocGrok calls)
// 500 threads for 100K RU/s provisioned throughput
ThreadPool.SetMinThreads(1000, 1000);

var builder = Host.CreateApplicationBuilder(args);

// Bind configuration from env vars: ChangeFeed__OmniVecCosmosEndpoint, etc.
builder.Services.Configure<ChangeFeedOptions>(
    builder.Configuration.GetSection("ChangeFeed"));

// CosmosClient for omnivec-cosmos (metadata) and the lease store. When
// LeaseCosmosEndpoint is configured (T-PWK-1) they are separate accounts.
builder.Services.AddSingleton(sp =>
{
    var opts = sp.GetRequiredService<IOptions<ChangeFeedOptions>>().Value;
    return new CosmosClient(opts.OmniVecCosmosEndpoint, new DefaultAzureCredential(),
        new CosmosClientOptions
        {
            ConnectionMode = ConnectionMode.Direct,
            MaxRetryAttemptsOnRateLimitedRequests = int.MaxValue,
            MaxRetryWaitTimeOnRateLimitedRequests = TimeSpan.FromSeconds(300),
        });
});

// Dedicated lease-store client (falls back to the metadata client when no
// separate account is configured).
builder.Services.AddKeyedSingleton<CosmosClient>("lease", (sp, _) =>
{
    var opts = sp.GetRequiredService<IOptions<ChangeFeedOptions>>().Value;
    var leaseEndpoint = string.IsNullOrWhiteSpace(opts.LeaseCosmosEndpoint)
        ? opts.OmniVecCosmosEndpoint
        : opts.LeaseCosmosEndpoint;
    return new CosmosClient(leaseEndpoint, new DefaultAzureCredential(),
        new CosmosClientOptions
        {
            ConnectionMode = ConnectionMode.Direct,
            MaxRetryAttemptsOnRateLimitedRequests = int.MaxValue,
            MaxRetryWaitTimeOnRateLimitedRequests = TimeSpan.FromSeconds(300),
        });
});

// HTTP client for OmniVec API
builder.Services.AddHttpClient<OmniVecApiClient>((sp, client) =>
{
    var opts = sp.GetRequiredService<IOptions<ChangeFeedOptions>>().Value;
    client.BaseAddress = new Uri(opts.OmniVecApiBaseUrl);
    client.Timeout = TimeSpan.FromSeconds(30);
});

// Services
builder.Services.AddSingleton<LeaseContainerManager>();
builder.Services.AddSingleton<BlobLeaseManager>();
builder.Services.AddSingleton<ContentHasher>();
builder.Services.AddSingleton<ServiceBusPublisher>();
builder.Services.AddSingleton<SourceWatcherManager>();
builder.Services.AddHostedService<SourceDiscoveryService>();

var host = builder.Build();

var logger = host.Services.GetRequiredService<ILogger<Program>>();
var opts = host.Services.GetRequiredService<IOptions<ChangeFeedOptions>>().Value;
logger.LogInformation("OmniVec Change Feed Processor starting");
logger.LogInformation("  Instance: {Instance}", opts.InstanceName);
logger.LogInformation("  API: {Api}", opts.OmniVecApiBaseUrl);
logger.LogInformation("  Cosmos: {Cosmos}", opts.OmniVecCosmosEndpoint);
if (!string.IsNullOrWhiteSpace(opts.LeaseCosmosEndpoint))
    logger.LogInformation("  Lease Cosmos: {Lease}/{Db}", opts.LeaseCosmosEndpoint,
        string.IsNullOrWhiteSpace(opts.LeaseCosmosDatabase) ? opts.OmniVecDatabase : opts.LeaseCosmosDatabase);
logger.LogInformation("  Poll: {Poll}s, Feed: {Feed}s, Batch: {Batch}",
    opts.SourcePollIntervalSeconds, opts.FeedPollIntervalSeconds, opts.MaxItemsPerBatch);

await host.RunAsync();
