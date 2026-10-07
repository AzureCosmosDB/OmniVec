using Azure.Identity;
using Microsoft.Azure.Cosmos;
using Newtonsoft.Json.Linq;
using Newtonsoft.Json;
using System.Net;
using System.Text;

namespace OmniVec.Mock;

public interface IMetadata
{
    Task Ready(CancellationToken token);
    Task<JObject?> Read(string id, string kind, CancellationToken token);
    Task<List<JObject>> List(string kind, CancellationToken token);
    Task<JObject> Create(JObject document, CancellationToken token);
    Task<JObject> Replace(JObject document, CancellationToken token);
}

public sealed class Metadata : IMetadata, IDisposable
{
    private readonly CosmosClient client;
    private readonly Container container;

    public Metadata()
    {
        string endpoint = Environment.GetEnvironmentVariable("COSMOS_ENDPOINT")
            ?? throw new InvalidOperationException("COSMOS_ENDPOINT is required");
        client = new CosmosClient(endpoint, new DefaultAzureCredential(),
            new CosmosClientOptions { ConnectionMode = ConnectionMode.Gateway, Serializer = new MetadataSerializer() });
        // Match the OmniVec metadata store, not model-specific database settings.
        container = client.GetContainer("omnivec", "metadata");
    }

    public sealed class MetadataSerializer : CosmosSerializer
    {
        public override T FromStream<T>(Stream stream)
        {
            using (stream)
            using (var reader = new StreamReader(stream))
            using (var json = new JsonTextReader(reader) { DateParseHandling = DateParseHandling.None })
                return JsonSerializer.CreateDefault().Deserialize<T>(json)
                    ?? throw new JsonSerializationException("Empty metadata response");
        }

        public override Stream ToStream<T>(T input)
        {
            var stream = new MemoryStream();
            using (var writer = new StreamWriter(stream, new UTF8Encoding(false), 1024, leaveOpen: true))
            using (var json = new JsonTextWriter(writer))
            {
                JsonSerializer.CreateDefault().Serialize(json, input);
                json.Flush();
            }
            stream.Position = 0;
            return stream;
        }
    }

    public async Task Ready(CancellationToken token) => await container.ReadContainerAsync(cancellationToken: token);

    public async Task<JObject?> Read(string id, string kind, CancellationToken token)
    {
        try
        {
            return (await container.ReadItemAsync<JObject>(id, new PartitionKey(kind),
                cancellationToken: token)).Resource;
        }
        catch (CosmosException error) when (error.StatusCode == HttpStatusCode.NotFound) { return null; }
    }

    public async Task<List<JObject>> List(string kind, CancellationToken token)
    {
        using var iterator = container.GetItemQueryIterator<JObject>(
            new QueryDefinition("SELECT * FROM c WHERE c.doc_type = @kind").WithParameter("@kind", kind),
            requestOptions: new QueryRequestOptions { PartitionKey = new PartitionKey(kind) });
        var result = new List<JObject>();
        while (iterator.HasMoreResults)
            result.AddRange(await iterator.ReadNextAsync(token));
        return result;
    }

    public async Task<JObject> Create(JObject document, CancellationToken token) =>
        (await container.CreateItemAsync(document, new PartitionKey((string)document["doc_type"]!),
            cancellationToken: token)).Resource;

    public async Task<JObject> Replace(JObject document, CancellationToken token) =>
        (await container.ReplaceItemAsync(document, (string)document["id"]!,
            new PartitionKey((string)document["doc_type"]!),
            new ItemRequestOptions { IfMatchEtag = (string?)document["_etag"]
                ?? throw new InvalidOperationException("Missing run ETag") }, token)).Resource;

    public void Dispose() => client.Dispose();
}
