using System.Diagnostics;
using System.Diagnostics.Metrics;

namespace OmniVec.Worker.Services;

public sealed class MetricsReporter
{
    public const string MeterName = "omnivec";
    private static readonly Meter Meter = new(MeterName, "1.0.0");
    private static readonly Counter<long> DocumentsEmbedded =
        Meter.CreateCounter<long>("omnivec.documents.embedded", "documents");
    private static readonly Counter<long> DocumentsFailed =
        Meter.CreateCounter<long>("omnivec.documents.failed", "documents");
    private static readonly Counter<long> TokensUsed =
        Meter.CreateCounter<long>("omnivec.tokens.used", "tokens");
    private static readonly Counter<long> Retries =
        Meter.CreateCounter<long>("omnivec.model.retries", "retries");
    private static readonly Counter<long> Throttles =
        Meter.CreateCounter<long>("omnivec.model.throttles", "throttles");
    private static readonly Counter<long> PipelineRequests =
        Meter.CreateCounter<long>("omnivec.pipeline.requests", "requests");
    private static readonly Counter<double> ProcessingTime =
        Meter.CreateCounter<double>("omnivec.pipeline.processing_time", "ms");
    private static readonly Counter<long> InputBytes =
        Meter.CreateCounter<long>("omnivec.pipeline.input_bytes", "bytes");
    private static readonly Histogram<double> EmbeddingLatency =
        Meter.CreateHistogram<double>("omnivec.embedding.latency", "ms");
    private static readonly Histogram<double> QueueWait =
        Meter.CreateHistogram<double>("omnivec.queue.wait", "ms");
    private static readonly Histogram<double> ThrottleDelay =
        Meter.CreateHistogram<double>("omnivec.model.throttle_delay", "ms");

    public MetricsReporter(HttpClient _, ILogger<MetricsReporter> __)
    {
    }

    public Task ReportInlineMetricsAsync(
        string pipelineId, int processed, int failed, long processingTimeMs,
        string batchKey, CancellationToken ct = default, string sourceId = "",
        string destinationId = "", string modelId = "", long tokensUsed = 0,
        long inputBytes = 0, double queueWaitMs = 0, int retryCount = 0,
        int throttleCount = 0, double throttleDelayMs = 0,
        string errorCategory = "", string lastDocument = "",
        int resourceWeight = 10, int maxConcurrencyPerWorker = 2,
        string resourcePriority = "normal", string workloadClass = "shared")
    {
        var tags = new TagList
        {
            { "pipeline_id", pipelineId },
            { "source_id", sourceId },
            { "model_id", modelId },
            { "destination_id", destinationId },
            { "resource_weight", resourceWeight },
            { "max_concurrency_per_worker", maxConcurrencyPerWorker },
            { "resource_priority", resourcePriority },
            { "workload_class", workloadClass },
        };
        if (!string.IsNullOrEmpty(errorCategory))
            tags.Add("error_category", errorCategory);

        if (processed > 0) DocumentsEmbedded.Add(processed, tags);
        if (failed > 0) DocumentsFailed.Add(failed, tags);
        if (tokensUsed > 0) TokensUsed.Add(tokensUsed, tags);
        if (retryCount > 0) Retries.Add(retryCount, tags);
        if (throttleCount > 0) Throttles.Add(throttleCount, tags);
        PipelineRequests.Add(1, tags);
        if (processingTimeMs > 0)
        {
            ProcessingTime.Add(processingTimeMs, tags);
            EmbeddingLatency.Record(processingTimeMs, tags);
        }
        if (inputBytes > 0) InputBytes.Add(inputBytes, tags);
        if (queueWaitMs > 0) QueueWait.Record(queueWaitMs, tags);
        if (throttleDelayMs > 0) ThrottleDelay.Record(throttleDelayMs, tags);
        return Task.CompletedTask;
    }
}
