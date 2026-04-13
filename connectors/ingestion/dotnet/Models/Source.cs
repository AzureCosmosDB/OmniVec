using System.Text.Json;
using System.Text.Json.Serialization;

namespace OmniVec.ChangeFeed.Models;

public class Source
{
    [JsonPropertyName("id")]
    public string Id { get; set; } = "";

    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("type")]
    public string Type { get; set; } = "";

    [JsonPropertyName("config")]
    public Dictionary<string, JsonElement> Config { get; set; } = new();

    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; } = true;

    // Convenience accessors for CosmosDB source config
    public string? Endpoint => TryGetString("endpoint");
    public string? Database => TryGetString("database");
    public string? Container => TryGetString("container");

    // SQL source config accessors (MS SQL + PostgreSQL)
    public string? Table => TryGetString("table");
    public string? SchemaName => TryGetString("schema_name") ?? TryGetString("schema");
    public string? PrimaryKey => TryGetString("primary_key") ?? "id";

    // Blob source config accessors
    public string? BlobAccountUrl => TryGetString("account_url");
    public string? BlobConnectionString => TryGetString("connection_string");
    public string? BlobContainer => TryGetString("container");
    public string? BlobPrefix => TryGetString("prefix") ?? "";
    public string? BlobFileType => TryGetString("file_type") ?? "pdf";

    /// <summary>
    /// Build connection string from config. Supports both explicit connection_string
    /// and individual host/port/database/user/password fields.
    /// </summary>
    public string? ConnectionString
    {
        get
        {
            var explicit_cs = TryGetString("connection_string");
            if (!string.IsNullOrEmpty(explicit_cs)) return explicit_cs;

            var host = TryGetString("host") ?? TryGetString("server") ?? "";
            var port = TryGetString("port") ?? (Type?.ToLowerInvariant() == "mssql" ? "1433" : "5432");
            var database = TryGetString("database") ?? "";
            var user = TryGetString("user") ?? "";
            var password = TryGetString("password") ?? "";

            if (string.IsNullOrEmpty(host)) return null;

            if (Type?.ToLowerInvariant() == "mssql")
            {
                if (!string.IsNullOrEmpty(user))
                    return $"Server={host},{port};Database={database};User Id={user};Password={password};Encrypt=True;TrustServerCertificate=False;";
                return $"Server={host},{port};Database={database};Encrypt=True;TrustServerCertificate=False;Authentication=Active Directory Default;";
            }

            // PostgreSQL
            var sslMode = TryGetString("ssl_mode") ?? "require";
            return $"Host={host};Port={port};Database={database};Username={user};Password={password};SSL Mode={sslMode}";
        }
    }

    /// <summary>Extract content from a row dictionary (for SQL sources).</summary>
    public static string ExtractContentFromRow(Dictionary<string, object?> row, List<string>? contentFields = null)
    {
        var fields = contentFields ?? new List<string> { "content" };
        var parts = new List<string>();
        foreach (var field in fields)
        {
            if (row.TryGetValue(field, out var val) && val is string s && !string.IsNullOrEmpty(s))
                parts.Add(s);
        }
        return string.Join("\n\n", parts);
    }

    public static bool RowHasContent(Dictionary<string, object?> row, List<string>? contentFields = null)
    {
        var fields = contentFields ?? new List<string> { "content" };
        foreach (var field in fields)
        {
            if (row.TryGetValue(field, out var val) && val is string s && !string.IsNullOrEmpty(s))
                return true;
        }
        return false;
    }

    /// <summary>
    /// Extract concatenated content from a JObject using specified content fields.
    /// </summary>
    public static string ExtractContent(Newtonsoft.Json.Linq.JObject doc, List<string>? contentFields = null)
    {
        var fields = contentFields ?? new List<string> { "content" };
        var parts = new List<string>();
        foreach (var field in fields)
        {
            var token = doc[field];
            if (token is null || token.Type == Newtonsoft.Json.Linq.JTokenType.Null)
                continue;
            var val = token.Type == Newtonsoft.Json.Linq.JTokenType.String
                ? (string?)token
                : token.ToString();
            if (!string.IsNullOrWhiteSpace(val))
                parts.Add(val);
        }
        return string.Join("\n\n", parts);
    }

    /// <summary>
    /// Check if a JObject has any of the specified content fields with non-empty values.
    /// </summary>
    public static bool HasContent(Newtonsoft.Json.Linq.JObject doc, List<string>? contentFields = null)
    {
        var fields = contentFields ?? new List<string> { "content" };
        foreach (var field in fields)
        {
            var token = doc[field];
            if (token is null || token.Type == Newtonsoft.Json.Linq.JTokenType.Null)
                continue;
            var val = token.ToString();
            if (!string.IsNullOrWhiteSpace(val))
                return true;
        }
        return false;
    }

    private string? TryGetString(string key)
    {
        if (Config.TryGetValue(key, out var v) && v.ValueKind == JsonValueKind.String)
            return v.GetString();
        return null;
    }
}

public class SourcesResponse
{
    [JsonPropertyName("sources")]
    public List<Source> Sources { get; set; } = new();
}
