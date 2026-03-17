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
    public string ExtractContentFromRow(Dictionary<string, object?> row)
    {
        var parts = new List<string>();
        foreach (var field in ContentFields)
        {
            if (row.TryGetValue(field, out var val) && val is string s && !string.IsNullOrEmpty(s))
                parts.Add(s);
        }
        return string.Join("\n\n", parts);
    }

    public bool RowHasContent(Dictionary<string, object?> row)
    {
        foreach (var field in ContentFields)
        {
            if (row.TryGetValue(field, out var val) && val is string s && !string.IsNullOrEmpty(s))
                return true;
        }
        return false;
    }

    /// <summary>
    /// Returns the content field(s) as a list. Supports both string and array configs.
    /// </summary>
    public List<string> ContentFields
    {
        get
        {
            if (!Config.TryGetValue("content_field", out var v))
                return new List<string> { "content" };

            if (v.ValueKind == JsonValueKind.Array)
            {
                var fields = new List<string>();
                foreach (var item in v.EnumerateArray())
                {
                    var s = item.GetString();
                    if (!string.IsNullOrEmpty(s)) fields.Add(s);
                }
                return fields.Count > 0 ? fields : new List<string> { "content" };
            }

            if (v.ValueKind == JsonValueKind.String)
            {
                var s = v.GetString();
                return new List<string> { string.IsNullOrEmpty(s) ? "content" : s };
            }

            return new List<string> { "content" };
        }
    }

    /// <summary>Single content field (backward compat). Returns first field.</summary>
    public string ContentField => ContentFields[0];

    /// <summary>
    /// Extract concatenated content from a JObject using all configured content fields.
    /// </summary>
    public string ExtractContent(Newtonsoft.Json.Linq.JObject doc)
    {
        var parts = new List<string>();
        foreach (var field in ContentFields)
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
    /// Check if a JObject has any of the configured content fields with non-empty values.
    /// </summary>
    public bool HasContent(Newtonsoft.Json.Linq.JObject doc)
    {
        foreach (var field in ContentFields)
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
