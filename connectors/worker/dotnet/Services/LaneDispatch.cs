using OmniVec.Worker.Lanes;

namespace OmniVec.Worker.Services;

/// <summary>Admission surrounds receive AND settlement, so no locks are acquired while waiting for capacity.</summary>
public static class LaneDispatch
{
    public static async Task RunOnceAsync(PipelineFairScheduler scheduler, LaneMetadata lane,
        Func<CancellationToken, Task> receiveAndProcess, CancellationToken ct)
    {
        await using var admission = await scheduler.AcquireAsync(lane.PipelineId,
            lane.ResourceWeight, lane.MaxConcurrencyPerWorker, lane.ResourcePriority,
            lane.WorkloadClass, 1, ct);
        ct.ThrowIfCancellationRequested();
        await receiveAndProcess(ct);
    }
}
