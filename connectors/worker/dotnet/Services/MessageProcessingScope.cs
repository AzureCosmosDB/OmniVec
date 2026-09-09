using System.Collections.Concurrent;
using Azure.Messaging.ServiceBus;

namespace OmniVec.Worker.Services;

/// <summary>Owns received locks, including messages waiting for a processing slot.</summary>
internal sealed class MessageProcessingScope : IAsyncDisposable
{
    private readonly ServiceBusReceiver _receiver;
    private readonly ConcurrentDictionary<ServiceBusReceivedMessage, DateTimeOffset> _pending = new();
    private readonly CancellationTokenSource _budget;
    private readonly CancellationTokenSource _renewal;
    private readonly Task _renewTask;
    public ServiceBusReceiver Receiver { get; }
    public CancellationToken Token => _budget.Token;

    public MessageProcessingScope(ServiceBusReceiver receiver, IReadOnlyList<ServiceBusReceivedMessage> messages,
        TimeSpan lifetime, CancellationToken ct, TimeSpan? tick = null)
    {
        _receiver = receiver;
        _budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _budget.CancelAfter(lifetime);
        _renewal = CancellationTokenSource.CreateLinkedTokenSource(_budget.Token);
        foreach (var message in messages) _pending[message] = message.LockedUntil;
        Receiver = new SettlingReceiver(this);
        _renewTask = RenewAsync(tick ?? TimeSpan.FromSeconds(5));
    }

    private async Task RenewAsync(TimeSpan tick)
    {
        try
        {
            while (!_renewal.IsCancellationRequested)
            {
                WorkerHeartbeat.Beat();
                var due = _pending.Where(pair => pair.Value - DateTimeOffset.UtcNow <= TimeSpan.FromSeconds(20)).ToArray();
                await Parallel.ForEachAsync(due, new ParallelOptions
                {
                    MaxDegreeOfParallelism = 8,
                    CancellationToken = _renewal.Token,
                }, async (pair, token) =>
                {
                    var (message, until) = pair;
                    try
                    {
                        await _receiver.RenewMessageLockAsync(message, token);
                        _pending.TryUpdate(message, message.LockedUntil, until);
                    }
                    catch (ServiceBusException ex) when (ex.Reason == ServiceBusFailureReason.MessageLockLost)
                    {
                        // Settlement may race renewal; losing an already settled lock is harmless.
                        if (_pending.ContainsKey(message)) _budget.Cancel();
                    }
                });
                await Task.Delay(tick, _renewal.Token);
            }
        }
        catch (OperationCanceledException) when (_renewal.IsCancellationRequested) { }
        catch
        {
            // Do not continue expensive work when its delivery ownership is uncertain.
            _budget.Cancel();
        }
    }

    public async ValueTask DisposeAsync()
    {
        _renewal.Cancel();
        await _renewTask;
        using var settlement = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        foreach (var message in _pending.Keys)
        {
            try { await _receiver.AbandonMessageAsync(message, cancellationToken: settlement.Token); }
            catch { /* A lost lock is redelivered by Service Bus. */ }
        }
        _renewal.Dispose();
        _budget.Dispose();
    }

    private sealed class SettlingReceiver(MessageProcessingScope owner) : ServiceBusReceiver
    {
        public override async Task CompleteMessageAsync(ServiceBusReceivedMessage message, CancellationToken ct = default)
        {
            await owner._receiver.CompleteMessageAsync(message, ct);
            owner._pending.TryRemove(message, out _);
        }

        public override async Task AbandonMessageAsync(ServiceBusReceivedMessage message,
            IDictionary<string, object>? propertiesToModify = null, CancellationToken cancellationToken = default)
        {
            await owner._receiver.AbandonMessageAsync(message, propertiesToModify, cancellationToken);
            owner._pending.TryRemove(message, out _);
        }

        public override async Task DeadLetterMessageAsync(ServiceBusReceivedMessage message,
            string deadLetterReason, string? deadLetterErrorDescription = null, CancellationToken cancellationToken = default)
        {
            await owner._receiver.DeadLetterMessageAsync(message, deadLetterReason, deadLetterErrorDescription, cancellationToken);
            owner._pending.TryRemove(message, out _);
        }
    }
}
