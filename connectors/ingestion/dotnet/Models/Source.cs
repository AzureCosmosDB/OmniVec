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

    // Databricks Delta Lake source config accessors (consumed via Change Data Feed)
    public string? DatabricksWorkspaceUrl => TryGetString("workspace_url");
    public string? DatabricksHttpPath => TryGetString("http_path");
    public string? DatabricksCatalog => TryGetString("catalog");
    // NB: shares the "schema" key with SQL sources via SchemaName above.
    // NB: shares the "table" key with SQL sources via Table above.
    public string DatabricksContentColumn => TryGetString("content_column") ?? "content";
    public string DatabricksIdColumn => TryGetString("id_column") ?? "id";
    // "pat" or "managed-identity" (default)
    public string DatabricksAuthType => TryGetString("auth_type") ?? "managed-identity";
    // Key Vault secret URI when auth_type=pat
    public string? DatabricksPatSecretRef => TryGetString("pat_secret_ref");

    // Blob source config accessors
    public string? BlobAccountUrl => TryGetString("account_url");
    public string? BlobConnectionString => TryGetString("connection_string");
    // For pure blob sources, "container" names the blob container.
    // For cosmosdb attachment-mode sources, "container" names the *cosmos*
    // container, so we honor "attachment_blob_container" first as a default
    // for relative attachment URLs (and as the SSRF guard fallback container).
    public string? BlobContainer =>
        TryGetString("attachment_blob_container") ?? TryGetString("container");
    public string? BlobPrefix => TryGetString("prefix") ?? "";
    public string? BlobFileType => TryGetString("file_type") ?? "pdf";

    // CosmosDB attachment-mode config accessors. When AttachmentsField is set,
    // SourceWatcher iterates the named array on each document, applies filters,
    // and emits one blob_ref EmbeddingMessage per matching attachment instead of
    // extracting inline content_fields.
    public string? AttachmentsField => TryGetString("attachments_field");
    public string AttachmentUrlField => TryGetString("attachment_url_field") ?? "url";
    public string AttachmentNameField => TryGetString("attachment_name_field") ?? "name";
    public string AttachmentContentTypeField => TryGetString("attachment_content_type_field") ?? "contentType";
    public string? AttachmentNameRegex => TryGetString("attachment_name_regex");
    public List<string> AttachmentFileTypes => TryGetStringList("attachment_file_types");
    public List<string> AttachmentContentTypes => TryGetStringList("attachment_content_types");
    // Optional explicit allowlist of blob storage account hosts permitted as
    // attachment sources. Used as a tighter SSRF guard than BlobAccountUrl
    // when a source legitimately spans multiple storage accounts. Each entry
    // may be a full URL ("https://acct.blob.core.windows.net") or just a
    // hostname ("acct.blob.core.windows.net").
    public List<string> AttachmentBlobAccountAllowlist => TryGetStringList("attachment_blob_account_allowlist");

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

    private List<string> TryGetStringList(string key)
    {
        var result = new List<string>();
        if (!Config.TryGetValue(key, out var v)) return result;
        if (v.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in v.EnumerateArray())
                if (item.ValueKind == JsonValueKind.String)
                    result.Add(item.GetString()!);
        }
        else if (v.ValueKind == JsonValueKind.String)
        {
            // CSV fallback
            foreach (var part in (v.GetString() ?? "").Split(','))
                if (!string.IsNullOrWhiteSpace(part))
                    result.Add(part.Trim());
        }
        return result;
    }

    /// <summary>
    /// Reference to a single attachment selected from a CosmosDB document's
    /// attachments array, after applying user-supplied filters.
    /// </summary>
    public sealed record AttachmentRef(
        string Name,
        string Url,
        string? ContentType,
        string? AccountUrl,
        string? Container,
        string BlobName);

    /// <summary>
    /// Iterate <paramref name="doc"/>'s attachments array (named by <see cref="AttachmentsField"/>)
    /// and return the attachments that pass all configured filters
    /// (<see cref="AttachmentNameRegex"/>, <see cref="AttachmentFileTypes"/>,
    /// <see cref="AttachmentContentTypes"/>). Returns an empty list when
    /// attachment mode is not configured or no attachments match.
    /// </summary>
    public List<AttachmentRef> ExtractAttachments(Newtonsoft.Json.Linq.JObject doc)
    {
        var matches = new List<AttachmentRef>();
        var fieldName = AttachmentsField;
        if (string.IsNullOrEmpty(fieldName)) return matches;

        var arr = doc[fieldName] as Newtonsoft.Json.Linq.JArray;
        if (arr is null) return matches;

        System.Text.RegularExpressions.Regex? nameRx = null;
        if (!string.IsNullOrEmpty(AttachmentNameRegex))
        {
            try
            {
                nameRx = new System.Text.RegularExpressions.Regex(
                    AttachmentNameRegex,
                    System.Text.RegularExpressions.RegexOptions.IgnoreCase
                    | System.Text.RegularExpressions.RegexOptions.CultureInvariant,
                    TimeSpan.FromSeconds(1));
            }
            catch
            {
                // Treat invalid regex as match-nothing rather than match-all to surface config errors.
                return matches;
            }
        }

        var fileTypeAllow = new HashSet<string>(
            AttachmentFileTypes.Select(t => t.TrimStart('.').ToLowerInvariant()),
            StringComparer.OrdinalIgnoreCase);
        var contentTypeAllow = new HashSet<string>(
            AttachmentContentTypes.Select(t => t.ToLowerInvariant()),
            StringComparer.OrdinalIgnoreCase);

        var nameField = AttachmentNameField;
        var urlField = AttachmentUrlField;
        var ctField = AttachmentContentTypeField;

        foreach (var token in arr)
        {
            if (token is not Newtonsoft.Json.Linq.JObject obj) continue;

            var name = obj[nameField]?.ToString() ?? "";
            var url = obj[urlField]?.ToString() ?? "";
            var contentType = obj[ctField]?.ToString();
            if (string.IsNullOrWhiteSpace(url)) continue;

            if (nameRx is not null && !nameRx.IsMatch(name)) continue;

            if (fileTypeAllow.Count > 0)
            {
                var ext = ExtractExtension(name) ?? ExtractExtension(url);
                if (ext is null || !fileTypeAllow.Contains(ext)) continue;
            }

            if (contentTypeAllow.Count > 0)
            {
                if (string.IsNullOrEmpty(contentType)) continue;
                if (!contentTypeAllow.Contains(contentType.ToLowerInvariant())) continue;
            }

            var (acct, ctnr, blob) = ResolveBlobLocation(url);
            if (string.IsNullOrEmpty(blob)) continue;

            matches.Add(new AttachmentRef(
                Name: string.IsNullOrEmpty(name) ? blob : name,
                Url: url,
                ContentType: contentType,
                AccountUrl: acct,
                Container: ctnr,
                BlobName: blob));
        }
        return matches;
    }

    private static string? ExtractExtension(string s)
    {
        if (string.IsNullOrEmpty(s)) return null;
        // Strip query string / fragment, then take last dot segment.
        var clean = s;
        var q = clean.IndexOfAny(new[] { '?', '#' });
        if (q >= 0) clean = clean.Substring(0, q);
        var dot = clean.LastIndexOf('.');
        var slash = clean.LastIndexOfAny(new[] { '/', '\\' });
        if (dot < 0 || dot < slash) return null;
        var ext = clean.Substring(dot + 1).ToLowerInvariant();
        return string.IsNullOrEmpty(ext) ? null : ext;
    }

    /// <summary>
    /// Resolve an attachment URL to an (accountUrl, container, blobName) triple.
    /// Accepts either a full https URL on a *.blob.core.windows.net host
    /// (validated against the source's configured <see cref="BlobAccountUrl"/>
    /// or <see cref="AttachmentBlobAccountAllowlist"/> as an SSRF guard), or
    /// a relative blob name that is joined with the source's configured
    /// account_url + container. Returns (null,null,"") for invalid /
    /// disallowed URLs.
    ///
    /// Hardening (T-CON-2 / T-BLB-1):
    ///   * Absolute URLs require either BlobAccountUrl or
    ///     AttachmentBlobAccountAllowlist to be set; otherwise any
    ///     *.blob.core.windows.net host could be reached.
    ///   * Blob name segments are validated to reject '..' traversal,
    ///     leading/trailing whitespace, control characters, and empty
    ///     segments after URL-decoding.
    /// </summary>
    public (string? AccountUrl, string? Container, string BlobName) ResolveBlobLocation(string url)
    {
        if (string.IsNullOrWhiteSpace(url)) return (null, null, "");

        if (Uri.TryCreate(url, UriKind.Absolute, out var uri))
        {
            if (uri.Scheme != "https") return (null, null, "");
            if (!uri.Host.EndsWith(".blob.core.windows.net", StringComparison.OrdinalIgnoreCase))
                return (null, null, "");

            // SSRF guard: source MUST pin either account_url or an allowlist.
            // Without one, any *.blob.core.windows.net host (incl. attacker-
            // controlled accounts in the same tenant) would be reachable.
            var pinnedHost = ExtractHost(BlobAccountUrl);
            var allowedHosts = AttachmentBlobAccountAllowlist
                .Select(ExtractHost)
                .Where(h => !string.IsNullOrEmpty(h))
                .Select(h => h!)
                .ToList();
            if (string.IsNullOrEmpty(pinnedHost) && allowedHosts.Count == 0)
                return (null, null, "");

            var hostMatches = false;
            if (!string.IsNullOrEmpty(pinnedHost)
                && string.Equals(pinnedHost, uri.Host, StringComparison.OrdinalIgnoreCase))
                hostMatches = true;
            if (!hostMatches && allowedHosts.Any(h =>
                    string.Equals(h, uri.Host, StringComparison.OrdinalIgnoreCase)))
                hostMatches = true;
            if (!hostMatches) return (null, null, "");

            var path = uri.AbsolutePath.TrimStart('/');
            var slashIdx = path.IndexOf('/');
            if (slashIdx < 0) return (null, null, "");
            var ctnr = path.Substring(0, slashIdx);
            var rawBlob = path.Substring(slashIdx + 1);
            if (string.IsNullOrEmpty(rawBlob)) return (null, null, "");
            var decoded = Uri.UnescapeDataString(rawBlob);
            if (!IsSafeBlobName(decoded) || !IsSafeContainerName(ctnr))
                return (null, null, "");
            return ($"https://{uri.Host}", ctnr, decoded);
        }

        // Relative — fall back to source-configured account/container.
        // Defense-in-depth: do NOT strip a leading slash. Reject absolute-style
        // relative URLs so attackers can't smuggle a different key shape.
        if (string.IsNullOrEmpty(BlobAccountUrl) || string.IsNullOrEmpty(BlobContainer))
            return (null, null, "");
        var relDecoded = Uri.UnescapeDataString(url);
        if (!IsSafeBlobName(relDecoded)) return (null, null, "");
        return (BlobAccountUrl, BlobContainer, relDecoded);
    }

    private static string? ExtractHost(string? urlOrHost)
    {
        if (string.IsNullOrWhiteSpace(urlOrHost)) return null;
        if (Uri.TryCreate(urlOrHost, UriKind.Absolute, out var u)) return u.Host;
        return urlOrHost.Trim().TrimEnd('/');
    }

    /// <summary>
    /// Validate a (URL-decoded) blob name. Rejects path traversal, control
    /// characters, leading slashes, and absolute paths — anything that could
    /// let an attacker pivot away from the configured container or smuggle a
    /// crafted key into downstream storage operations.
    /// </summary>
    internal static bool IsSafeBlobName(string name)
    {
        if (string.IsNullOrWhiteSpace(name)) return false;
        if (name.Length > 1024) return false; // Azure Blob max key length
        if (name.StartsWith('/') || name.StartsWith('\\')) return false;
        foreach (var ch in name)
        {
            if (ch == '\0' || (ch < 0x20 && ch != '\t')) return false;
        }
        foreach (var seg in name.Split(new[] { '/', '\\' }, StringSplitOptions.None))
        {
            if (seg.Length == 0) return false; // empty segment ("//" or trailing "/")
            if (seg == "." || seg == "..") return false;
            if (seg.Trim() != seg) return false; // leading/trailing whitespace per segment
        }
        return true;
    }

    internal static bool IsSafeContainerName(string ctnr)
    {
        if (string.IsNullOrWhiteSpace(ctnr)) return false;
        // Azure container names: 3-63 chars, lowercase letters, digits, hyphens.
        // We only need a permissive sanity check — the storage SDK will do strict
        // validation. The point here is to refuse traversal-style values.
        foreach (var ch in ctnr)
        {
            if (ch == '/' || ch == '\\' || ch == '.' || ch < 0x20) return false;
        }
        return ctnr.Length is >= 1 and <= 63;
    }
}

public class SourcesResponse
{
    [JsonPropertyName("sources")]
    public List<Source> Sources { get; set; } = new();
}
