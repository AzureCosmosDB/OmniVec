using System.Text.Json;
using Azure.Messaging.ServiceBus;

namespace OmniVec.Worker.Services;

/// <summary>Projects document deliveries locally; only the parent owns a broker lock.</summary>
internal sealed class QueueEnvelopeReceiver : ServiceBusReceiver
{
    public const int MaxEnvelopeBytes = 960 * 1024;
    public const string DeadLetterReasonProperty = "omnivec_queue_dead_letter_reason";
    public const string DeadLetterDescriptionProperty = "omnivec_queue_dead_letter_description";
    private readonly ServiceBusReceiver _receiver;
    private readonly ServiceBusReceivedMessage _parent;
    private readonly Func<ServiceBusMessage, CancellationToken, Task> _handoff;
    private readonly Dictionary<ServiceBusReceivedMessage, DocumentState> _documents = new();
    public IReadOnlyList<ServiceBusReceivedMessage> Documents => _documents.Keys.ToArray();

    private sealed class DocumentState { public int Disposition; }

    public QueueEnvelopeReceiver(ServiceBusReceiver receiver, ServiceBusReceivedMessage parent,
        Func<ServiceBusMessage, CancellationToken, Task> handoff)
    {
        _receiver = receiver;
        _parent = parent;
        _handoff = handoff;
        if (parent.Body.ToMemory().Length > MaxEnvelopeBytes)
            throw new JsonException("Queue envelope exceeds its serialized byte limit");
        using var json = JsonDocument.Parse(parent.Body);
        var root = json.RootElement;
        if (root.ValueKind != JsonValueKind.Object
            || !root.TryGetProperty("queue_envelope_version", out var version)
            || version.ValueKind != JsonValueKind.Number || !version.TryGetInt32(out var value) || value != 1
            || !root.TryGetProperty("pipeline_id", out var pipeline) || pipeline.ValueKind != JsonValueKind.String
            || string.IsNullOrWhiteSpace(pipeline.GetString())
            || !root.TryGetProperty("documents", out var documents) || documents.ValueKind != JsonValueKind.Array
            || documents.GetArrayLength() is < 1 or > 1000)
            throw new JsonException("Invalid queue envelope version, routing or document count");
        var identities = new HashSet<string>(StringComparer.Ordinal);
        foreach (var document in documents.EnumerateArray())
        {
            if (document.ValueKind != JsonValueKind.Object
                || document.TryGetProperty("queue_envelope_version", out _)
                || !document.TryGetProperty("message_id", out var id) || id.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(id.GetString()) || !identities.Add(id.GetString()!)
                || !document.TryGetProperty("pipeline_id", out var route) || route.ValueKind != JsonValueKind.String
                || route.GetString() != pipeline.GetString())
                throw new JsonException("Envelope documents require unique IDs and identical pipeline routing");
            var delivery = ServiceBusModelFactory.ServiceBusReceivedMessage(
                body: BinaryData.FromString(document.GetRawText()), messageId: id.GetString(),
                subject: parent.Subject, deliveryCount: parent.DeliveryCount,
                enqueuedTime: parent.EnqueuedTime, lockedUntil: parent.LockedUntil);
            _documents.Add(delivery, new DocumentState());
        }
    }

    public override Task CompleteMessageAsync(ServiceBusReceivedMessage message, CancellationToken ct = default)
    {
        ct.ThrowIfCancellationRequested();
        Interlocked.CompareExchange(ref _documents[message].Disposition, 1, 0);
        return Task.CompletedTask;
    }

    public override Task AbandonMessageAsync(ServiceBusReceivedMessage message,
        IDictionary<string, object>? propertiesToModify = null, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Interlocked.CompareExchange(ref _documents[message].Disposition, 2, 0);
        return Task.CompletedTask;
    }

    public override async Task DeadLetterMessageAsync(ServiceBusReceivedMessage message, string deadLetterReason,
        string? deadLetterErrorDescription = null, CancellationToken cancellationToken = default)
    {
        var state = _documents[message];
        if (Interlocked.CompareExchange(ref state.Disposition, 4, 0) != 0) return;
        try
        {
            var outgoing = new ServiceBusMessage(message.Body)
            { MessageId = message.MessageId, Subject = message.Subject, ContentType = "application/json" };
            outgoing.ApplicationProperties[DeadLetterReasonProperty] = deadLetterReason;
            outgoing.ApplicationProperties[DeadLetterDescriptionProperty] = deadLetterErrorDescription ?? "";
            // A confirmed durable handoff must precede releasing the original envelope.
            await _handoff(outgoing, cancellationToken);
            Interlocked.Exchange(ref state.Disposition, 3);
        }
        catch
        {
            Interlocked.Exchange(ref state.Disposition, 0);
            throw;
        }
    }

    public async Task SettleParentAsync(CancellationToken ct)
    {
        if (_documents.Values.All(state => Volatile.Read(ref state.Disposition) is 1 or 3))
            await _receiver.CompleteMessageAsync(_parent, ct);
        else
            await _receiver.AbandonMessageAsync(_parent, cancellationToken: ct);
    }
}
