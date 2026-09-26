namespace OmniVec.Worker.Services;

/// <summary>
/// Per-worker weighted fair admission with pipeline concurrency caps.
/// Service Bus remains the durable queue; this scheduler only orders groups
/// already received by the worker.
/// </summary>
public sealed class PipelineFairScheduler
{
    private readonly object _sync = new();
    private readonly int _globalLimit;
    private readonly Dictionary<string, PipelineState> _pipelines = new();
    private int _active;

    public PipelineFairScheduler(int globalLimit)
    {
        _globalLimit = Math.Max(1, globalLimit);
    }

    public Task<IAsyncDisposable> AcquireAsync(
        string pipelineId, int weight, int maxConcurrency, string priority,
        string workloadClass, int cost, CancellationToken ct)
    {
        var request = new AdmissionRequest(
            pipelineId,
            EffectiveWeight(weight, priority, workloadClass),
            Math.Clamp(maxConcurrency, 1, 32),
            Math.Max(1, cost));
        lock (_sync)
        {
            var state = StateFor(request);
            state.Pending.Enqueue(request);
            request.Cancellation = ct.Register(() => Cancel(request, ct));
            DispatchLocked();
        }
        return request.Completion.Task;
    }

    private PipelineState StateFor(AdmissionRequest request)
    {
        if (!_pipelines.TryGetValue(request.PipelineId, out var state))
        {
            state = new PipelineState();
            _pipelines[request.PipelineId] = state;
        }
        state.Weight = request.Weight;
        state.MaxConcurrency = request.MaxConcurrency;
        return state;
    }

    private void Cancel(AdmissionRequest request, CancellationToken ct)
    {
        lock (_sync)
        {
            request.Completion.TrySetCanceled(ct);
            DispatchLocked();
        }
    }

    private void Release(string pipelineId)
    {
        lock (_sync)
        {
            if (_pipelines.TryGetValue(pipelineId, out var state))
                state.Active = Math.Max(0, state.Active - 1);
            _active = Math.Max(0, _active - 1);
            DispatchLocked();
        }
    }

    private void DispatchLocked()
    {
        while (_active < _globalLimit)
        {
            PipelineState? selected = null;
            string? selectedId = null;
            var selectedFinish = double.MaxValue;
            foreach (var (pipelineId, state) in _pipelines)
            {
                while (state.Pending.TryPeek(out var stale) && stale.Completion.Task.IsCompleted)
                {
                    state.Pending.Dequeue();
                    stale.Cancellation.Dispose();
                }
                if (state.Pending.Count == 0 || state.Active >= state.MaxConcurrency)
                    continue;
                var pending = state.Pending.Peek();
                var finish = state.VirtualRuntime
                    + pending.Cost / (double)Math.Max(1, state.Weight);
                if (selected is null || finish < selectedFinish)
                {
                    selected = state;
                    selectedId = pipelineId;
                    selectedFinish = finish;
                }
            }
            if (selected is null || selectedId is null) return;

            var request = selected.Pending.Dequeue();
            request.Cancellation.Dispose();
            selected.Active++;
            _active++;
            selected.VirtualRuntime += request.Cost / (double)Math.Max(1, selected.Weight);
            request.Completion.TrySetResult(new AdmissionLease(this, selectedId));
        }
    }

    internal static int EffectiveWeight(
        int weight, string priority, string workloadClass = "shared")
    {
        var factor = priority?.ToLowerInvariant() switch
        {
            "low" => 1,
            "high" => 4,
            "critical" => 8,
            _ => 2,
        };
        var classFactor = workloadClass?.ToLowerInvariant() switch
        {
            "burst" => 2,
            "dedicated" => 4,
            _ => 1,
        };
        return Math.Clamp(weight, 1, 100) * factor * classFactor;
    }

    private sealed class PipelineState
    {
        public Queue<AdmissionRequest> Pending { get; } = new();
        public int Active { get; set; }
        public int MaxConcurrency { get; set; } = 2;
        public int Weight { get; set; } = 20;
        public double VirtualRuntime { get; set; }
    }

    private sealed class AdmissionRequest(
        string pipelineId, int weight, int maxConcurrency, int cost)
    {
        public string PipelineId { get; } = pipelineId;
        public int Weight { get; } = weight;
        public int MaxConcurrency { get; } = maxConcurrency;
        public int Cost { get; } = cost;
        public TaskCompletionSource<IAsyncDisposable> Completion { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        public CancellationTokenRegistration Cancellation { get; set; }
    }

    private sealed class AdmissionLease(
        PipelineFairScheduler owner, string pipelineId) : IAsyncDisposable
    {
        private int _released;

        public ValueTask DisposeAsync()
        {
            if (Interlocked.Exchange(ref _released, 1) == 0)
                owner.Release(pipelineId);
            return ValueTask.CompletedTask;
        }
    }
}
