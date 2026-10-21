using System.Reflection;
using System.Text.Json;
using Azure;
using Azure.Messaging.ServiceBus;
using Azure.Messaging.ServiceBus.Administration;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Options;
using OmniVec.ChangeFeed.Configuration;
using OmniVec.ChangeFeed.Services;
using OmniVec.Worker.Services;
using Lane = OmniVec.Worker.Lanes.LaneMetadata;
using Protocol = OmniVec.Worker.Lanes.PipelineLaneProtocol;
using ProducerLane = OmniVec.ChangeFeed.Lanes.LaneMetadata;
using ProducerRegistry = OmniVec.ChangeFeed.Lanes.PipelineLaneRegistry;
using Message = OmniVec.ChangeFeed.Models.EmbeddingMessage;

internal static class PipelineLaneTests
{
    private static void Check(bool value, string message = "Assertion failed")
    { if (!value) throw new Exception(message); }
    private static async Task Fails(Func<Task> operation)
    {
        try { await operation(); } catch { return; }
        throw new Exception("Expected failure");
    }
    private static Lane Policy(string id) => new(1, id, 10, 1, "normal", "shared");
    private static RuleProperties Rule(string name, RuleFilter filter) => ServiceBusModelFactory.RuleProperties(name, filter, null);
    private static ProducerLane ProducerPolicy(string id) => new(1, id, 10, 1, "normal", "shared");
    private static void Set(object target, string field, object value)
        => target.GetType().GetField(field, BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(target, value);

    public static async Task<int> RunAsync()
    {
        var tests = new List<(string Name, Func<Task> Run)>
        {
            ("Exact UTF8 hash, shared wire metadata, subject-only rule and fail-closed ownership", async () =>
            {
                foreach (var (id, expected) in new[] {
                    ("pipeline-A", "p-eee5dc2982fc13ded6ee1c3a73e3c5f9"),
                    ("π/東京", "p-f75c96e1b8c42663d11080110f4e485a") })
                {
                    var workerName = Protocol.SubscriptionName(id);
                    Check(workerName == OmniVec.ChangeFeed.Lanes.PipelineLaneProtocol.SubscriptionName(id));
                    Check(workerName == expected && workerName.Length == 34 && workerName == workerName.ToLowerInvariant());
                    Check(JsonSerializer.Serialize(Policy(id)) == JsonSerializer.Serialize(ProducerPolicy(id)));
                }
                Protocol.ValidateMetadata(Protocol.SubscriptionName("A"), Policy("A"));
                Protocol.ValidateRules("A", [Rule("pipeline", new CorrelationRuleFilter { Subject = "A" })]);
                await Fails(() => { Protocol.ValidateTopicName("embeddings"); return Task.CompletedTask; });
                await Fails(() => { Protocol.ValidateTopicName(""); return Task.CompletedTask; });
                await Fails(() => { Protocol.ValidateMetadata(Protocol.SubscriptionName("A"), Policy("B")); return Task.CompletedTask; });
                foreach (var rules in new RuleProperties[][] {
                    [], [Rule("$Default", new TrueRuleFilter())],
                    [Rule("pipeline", new CorrelationRuleFilter { Subject = "B" })],
                    [Rule("pipeline", new CorrelationRuleFilter { Subject = "A", SessionId = "session" })],
                    [Rule("pipeline", new CorrelationRuleFilter { Subject = "A" }), Rule("$Default", new TrueRuleFilter())] })
                    await Fails(() => { Protocol.ValidateRules("A", rules); return Task.CompletedTask; });
            }),
            ("Atomic create, explicit conflict verification, bounded validation cache", async () =>
            {
                var admin = new LaneAdmin();
                var registry = new ProducerRegistry(admin, "embeddings-lanes-v1");
                var results = await Task.WhenAll(Enumerable.Range(0, 8).Select(_ => registry.EnsureAsync(ProducerPolicy("A"), default)));
                Check(results.All(name => name == Protocol.SubscriptionName("A")) && admin.Creates == 1);
                Check(admin.RuleReads == 1 && admin.TopicReads == 1, "Provisioning cache must avoid admin reads per message");
                Check(admin.AtomicRules == 1);
                var conflict = new LaneAdmin { Conflict = true };
                await new ProducerRegistry(conflict, "embeddings-lanes-v1").EnsureAsync(ProducerPolicy("A"), default);
                Check(conflict.Creates == 1);
                await Fails(() => registry.EnsureAsync(ProducerPolicy("A") with { ResourceWeight = 20 }, default));
                var foreign = new LaneAdmin { Marker = "foreign" };
                await Fails(() => new ProducerRegistry(foreign, "embeddings-lanes-v1").EnsureAsync(ProducerPolicy("A"), default));
                Check(foreign.Creates == 0);
                var broad = new LaneAdmin();
                broad.Add("A");
                broad.Rules[Protocol.SubscriptionName("A")].Add(Rule("$Default", new TrueRuleFilter()));
                await Fails(() => new ProducerRegistry(broad, "embeddings-lanes-v1").DiscoverAsync(default));
                var denied = new LaneAdmin { DenyCreate = true };
                await Fails(() => new ProducerRegistry(denied, "embeddings-lanes-v1").EnsureAsync(ProducerPolicy("A"), default));
                Check(denied.Creates == 1, "Non-conflict failures cannot be swallowed");
            }),
            ("Local backpressure continues healthy groups and surfaces checkpoint failure; legacy preserves routing", async () =>
            {
                var sender = new LocalSender();
                var admin = new LaneAdmin();
                admin.Add("A"); admin.Add("B");
                admin.Counts[Protocol.SubscriptionName("A")] = 5000;
                await using var publisher = new ServiceBusPublisher(Options.Create(new ChangeFeedOptions()), NullLogger<ServiceBusPublisher>.Instance);
                Set(publisher, "_sender", sender); Set(publisher, "_adminClient", admin);
                Set(publisher, "_lanes", new ProducerRegistry(admin, "embeddings-lanes-v1"));
                Set(publisher, "_laneMode", true); Set(publisher, "_topicName", "embeddings-lanes-v1");
                var messages = new List<Message> {
                    new() { PipelineId = "A", MaxConcurrencyPerWorker = 1, MessageId = "a" },
                    new() { PipelineId = "B", MaxConcurrencyPerWorker = 1, MessageId = "b" } };
                await Fails(() => publisher.PublishBatchAsync(messages, default));
                Check(sender.Sent.Count == 1 && sender.Sent[0].Subject == "B" && sender.Sent[0].MessageId == "b");
                Check(await publisher.HasSourceCapacityAsync(["A", "B"], default));
                Check(!await publisher.HasSourceCapacityAsync(["A"], default));
                await Fails(() => publisher.HasCapacityAsync(default));
                await using var legacy = new ServiceBusPublisher(Options.Create(new ChangeFeedOptions()), NullLogger<ServiceBusPublisher>.Instance);
                var legacySender = new LocalSender();
                Set(legacy, "_sender", legacySender);
                await legacy.PublishBatchAsync(messages, default);
                Check(legacySender.Sent.Count == 2 && legacySender.Sent[0].Subject == "A" && legacySender.Sent[1].Subject == "B");
            }),
            ("Admission precedes receive, cancellation never receives; flooded A allows B to complete", async () =>
            {
                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                var scheduler = new PipelineFairScheduler(1);
                var held = await scheduler.AcquireAsync("A", 10, 1, "normal", "shared", 1, deadline.Token);
                var receives = 0;
                using var cancel = new CancellationTokenSource();
                var waiting = LaneDispatch.RunOnceAsync(scheduler, Policy("B"), _ => { receives++; return Task.CompletedTask; }, cancel.Token);
                Check(!waiting.IsCompleted && receives == 0, "No broker lock while awaiting admission");
                cancel.Cancel();
                await Fails(() => waiting);
                Check(receives == 0);
                var aReceived = 1;
                var bCompleted = false;
                // A is flooded. Only one A receive may be queued; B must actually process and complete.
                var flood = Task.Run(async () =>
                {
                    for (var i = 0; i < 100; i++)
                        await LaneDispatch.RunOnceAsync(scheduler, Policy("A"), _ => { aReceived++; return Task.CompletedTask; }, deadline.Token);
                });
                var healthy = LaneDispatch.RunOnceAsync(scheduler, Policy("B"), _ =>
                {
                    Check(aReceived < 101, "Healthy B received only after all A: unfair");
                    bCompleted = true;
                    return Task.CompletedTask;
                }, deadline.Token);
                await held.DisposeAsync();
                await healthy.WaitAsync(deadline.Token);
                Check(bCompleted);
                await flood;
                var bounded = new PipelineFairScheduler(2);
                var active = 0; var peak = 0;
                var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                var tasks = Enumerable.Range(0, 8).Select(i => LaneDispatch.RunOnceAsync(bounded, Policy(i.ToString()), async token =>
                {
                    var current = Interlocked.Increment(ref active);
                    peak = Math.Max(peak, current);
                    await release.Task.WaitAsync(token);
                    Interlocked.Decrement(ref active);
                }, deadline.Token)).ToArray();
                Check(active == 2 && peak == 2);
                release.SetResult();
                await Task.WhenAll(tasks);
                Check(active == 0 && peak == 2);
            }),
        };
        var failed = 0;
        foreach (var (name, run) in tests)
        {
            try { await run(); Console.WriteLine($"PASS LANE {name}"); }
            catch (Exception ex) { failed++; Console.WriteLine($"FAIL LANE {name}: {ex}"); }
        }
        Console.WriteLine($"LANES: {tests.Count - failed} passed, {failed} failed (in-memory doubles; NOT live broker proof)");
        return failed;
    }

    private sealed class LaneAdmin : ServiceBusAdministrationClient
    {
        public string Marker = Protocol.TopicMarker;
        public int TopicReads, RuleReads, Creates, AtomicRules;
        public bool Conflict, DenyCreate;
        public Dictionary<string, SubscriptionProperties> Subscriptions = new();
        public Dictionary<string, List<RuleProperties>> Rules = new();
        public Dictionary<string, long> Counts = new();
        public void Add(string id)
        {
            var name = Protocol.SubscriptionName(id);
            Subscriptions[name] = ServiceBusModelFactory.SubscriptionProperties("embeddings-lanes-v1", name,
                lockDuration: TimeSpan.FromMinutes(5), defaultMessageTimeToLive: TimeSpan.FromDays(1),
                autoDeleteOnIdle: TimeSpan.MaxValue, maxDeliveryCount: 10, status: EntityStatus.Active,
                userMetadata: JsonSerializer.Serialize(ProducerPolicy(id)));
            Rules[name] = [Rule("pipeline", new CorrelationRuleFilter { Subject = id })];
        }
        public override Task<Response<TopicProperties>> GetTopicAsync(string topicName, CancellationToken ct = default)
        {
            TopicReads++;
            var topic = ServiceBusModelFactory.TopicProperties(topicName, maxSizeInMegabytes: 5120,
                defaultMessageTimeToLive: TimeSpan.FromDays(1), autoDeleteOnIdle: TimeSpan.MaxValue,
                duplicateDetectionHistoryTimeWindow: TimeSpan.FromMinutes(10), status: EntityStatus.Active);
            topic.UserMetadata = Marker;
            return Task.FromResult(Response.FromValue(topic, null!));
        }
        public override AsyncPageable<SubscriptionProperties> GetSubscriptionsAsync(string topicName, CancellationToken ct = default)
            => AsyncPageable<SubscriptionProperties>.FromPages([Page<SubscriptionProperties>.FromValues(Subscriptions.Values.ToArray(), null, null!)]);
        public override Task<Response<SubscriptionProperties>> GetSubscriptionAsync(string topicName, string subscriptionName, CancellationToken ct = default)
            => Task.FromResult(Response.FromValue(Subscriptions[subscriptionName], null!));
        public override AsyncPageable<RuleProperties> GetRulesAsync(string topicName, string subscriptionName, CancellationToken ct = default)
        {
            RuleReads++;
            return AsyncPageable<RuleProperties>.FromPages([Page<RuleProperties>.FromValues(Rules[subscriptionName], null, null!)]);
        }
        public override Task<Response<SubscriptionProperties>> CreateSubscriptionAsync(CreateSubscriptionOptions options, CreateRuleOptions rule, CancellationToken ct = default)
        {
            Creates++;
            if (DenyCreate) throw new RequestFailedException(403, "Denied");
            Check(rule.Filter is CorrelationRuleFilter filter && filter.Subject != null);
            AtomicRules++;
            var id = ((CorrelationRuleFilter)rule.Filter).Subject;
            Add(id);
            Subscriptions[options.SubscriptionName].UserMetadata = options.UserMetadata;
            if (Conflict) throw new ServiceBusException("Already exists", ServiceBusFailureReason.MessagingEntityAlreadyExists);
            return Task.FromResult(Response.FromValue(Subscriptions[options.SubscriptionName], null!));
        }
        public override Task<Response<SubscriptionRuntimeProperties>> GetSubscriptionRuntimePropertiesAsync(string topicName, string subscriptionName, CancellationToken ct = default)
            => Task.FromResult(Response.FromValue(ServiceBusModelFactory.SubscriptionRuntimeProperties(
                topicName, subscriptionName, activeMessageCount: Counts.GetValueOrDefault(subscriptionName)), null!));
    }
}
