using System.Text.Json.Serialization;

namespace OmniVec.Worker.Models;

public sealed class TextChunkConfig
{
    [JsonPropertyName("chunk_size")]
    public int Size { get; set; } = 1000;
    [JsonPropertyName("chunk_overlap")]
    public int Overlap { get; set; } = 200;
    [JsonPropertyName("chunk_unit")]
    public string Unit { get; set; } = "chars";
    [JsonPropertyName("store_text")]
    public bool StoreText { get; set; }
    [JsonPropertyName("text_field")]
    public string TextField { get; set; } = "text";
    [JsonPropertyName("doc_id_pattern")]
    public string DocIdPattern { get; set; } = "{source}-chunk-{chunk}";
    [JsonExtensionData]
    public Dictionary<string, System.Text.Json.JsonElement>? Extra { get; set; }
}
