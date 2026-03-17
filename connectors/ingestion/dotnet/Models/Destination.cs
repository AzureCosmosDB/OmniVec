using System.Text.Json.Serialization;

namespace OmniVec.ChangeFeed.Models;

public class Destination
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("type")]
    public string Type { get; set; } = "";

    [JsonPropertyName("config")]
    public Dictionary<string, object> Config { get; set; } = new();

    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; } = true;
}

public class DestinationsResponse
{
    [JsonPropertyName("destinations")]
    public List<Destination> Destinations { get; set; } = new();
}
