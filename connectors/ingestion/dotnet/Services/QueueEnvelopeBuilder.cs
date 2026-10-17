using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Azure.Messaging.ServiceBus;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

internal static class QueueEnvelopeBuilder
{
    public const int MaxEnvelopeBytes = 224 * 1024;
    public const int MaxDocuments = 1000;
    public const int MaxSupportedEnvelopeBytes = 960 * 1024;
    public const int MaxSupportedSendBatchBytes = 1024 * 1024;

    public static void Validate(EmbeddingMessage message)
    {
        if (message.QueueDocumentsPerMessage is < 1 or > MaxDocuments
            || message.QueuePublishBatchSize is < 1 or > MaxDocuments)
            throw new ArgumentOutOfRangeException(nameof(message), "Queue document limits must be from 1 to 1000");
        if (message.QueueMaxEnvelopeBytes is < 1024 or > MaxSupportedEnvelopeBytes
            || message.QueueSendBatchMaxBytes is < 1024 or > MaxSupportedSendBatchBytes
            || message.QueueSendBatchMaxBytes < message.QueueMaxEnvelopeBytes + 16 * 1024)
            throw new ArgumentOutOfRangeException(nameof(message), "Invalid queue byte limits; send batches must reserve 16 KiB above the envelope cap");
    }

    public static IEnumerable<ServiceBusMessage> Build(IEnumerable<EmbeddingMessage> messages)
    {
        var pending = new List<EmbeddingMessage>();
        var bodies = new List<string>();
        string? route = null;
        var bytes = 0;
        var identities = new HashSet<string>(StringComparer.Ordinal);
        foreach (var message in messages)
        {
            Validate(message);
            var nextRoute = JsonSerializer.Serialize(new
            {
                message.PipelineId, message.PipelineGeneration, message.SourceId,
                message.DestinationId, message.DestinationType, message.DestinationConfig,
                message.DocgrokPipeline, message.MessageType, message.ContentType,
                message.ResourceWeight, message.MaxConcurrencyPerWorker,
                message.ResourcePriority, message.WorkloadClass, message.EmbedBatchSize,
                message.ModelMaxBatchSize, message.ModelContextWindowTokens, message.ModelMaxBatchTokens,
                message.QueueDocumentsPerMessage,
                message.QueueMaxEnvelopeBytes, message.QueueSendBatchMaxBytes, message.QueuePublishBatchSize,
            });
            var body = JsonSerializer.Serialize(message);
            var size = Encoding.UTF8.GetByteCount(body);
            var overhead = 256 + Encoding.UTF8.GetByteCount(JsonSerializer.Serialize(message.PipelineId));
            if (pending.Count > 0 && (route != nextRoute
                || pending.Count >= Math.Min(message.QueueDocumentsPerMessage, message.QueuePublishBatchSize)
                || identities.Contains(message.MessageId)
                || bytes + size + pending.Count + overhead > message.QueueMaxEnvelopeBytes))
            {
                yield return Create(pending, bodies);
                pending.Clear();
                bodies.Clear();
                bytes = 0;
                identities.Clear();
            }
            pending.Add(message);
            identities.Add(message.MessageId);
            bodies.Add(body);
            bytes += size;
            route = nextRoute;
        }
        if (pending.Count > 0) yield return Create(pending, bodies);
    }

    private static ServiceBusMessage Create(List<EmbeddingMessage> documents, List<string> bodies)
    {
        var head = documents[0];
        if (documents.Count == 1)
            return new ServiceBusMessage(bodies[0])
            { MessageId = head.MessageId, Subject = head.PipelineId, ContentType = "application/json" };
        var id = "qe-" + Convert.ToHexString(SHA256.HashData(
            Encoding.UTF8.GetBytes(string.Join("\n", documents.Select(document => document.MessageId)))))
            .ToLowerInvariant();
        var header = JsonSerializer.Serialize(new
        {
            queue_envelope_version = 1, message_id = id, pipeline_id = head.PipelineId,
        });
        var body = header[..^1] + ",\"documents\":[" + string.Join(",", bodies) + "]}";
        if (Encoding.UTF8.GetByteCount(body) > head.QueueMaxEnvelopeBytes)
            throw new InvalidOperationException("Queue envelope exceeds its serialized byte limit");
        return new ServiceBusMessage(body)
        { MessageId = id, Subject = head.PipelineId, ContentType = "application/json" };
    }
}
