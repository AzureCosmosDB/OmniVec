using System.Text.Json;
using Microsoft.Data.SqlClient;
using Npgsql;
using OmniVec.ChangeFeed.Models;

namespace OmniVec.ChangeFeed.Services;

internal static class InlineSourceOwnership
{
    internal static HashSet<string> FindBlockedSourceIds(List<Source> sources, List<Pipeline> pipelines)
    {
        var owners = pipelines.Where(p => p.Status == "active" && p.ProcessingMode == "inline")
            .SelectMany(p => p.Sources.Select(s => (PipelineId: p.Id, SourceId: s.SourceId)))
            .Distinct().ToList();
        var blocked = new HashSet<string>();
        var targets = new Dictionary<string, List<Source>>();
        foreach (var source in sources.Where(s => s.Enabled))
        {
            string key;
            try { key = TargetKey(source); }
            catch (Exception error) when (error is ArgumentException or FormatException)
            {
                if (owners.Any(owner => owner.SourceId == source.Id)) blocked.Add(source.Id);
                continue;
            }
            if (!targets.TryGetValue(key, out var group)) targets[key] = group = new();
            group.Add(source);
        }
        foreach (var group in targets.Values)
        {
            var ids = group.Select(s => s.Id).ToHashSet();
            if (owners.Count(owner => ids.Contains(owner.SourceId)) > 1)
                blocked.UnionWith(ids);
        }
        return blocked;
    }

    private static string Required(string? value)
        => string.IsNullOrWhiteSpace(value) ? throw new ArgumentException("Missing inline target field") : value;

    internal static string TargetKey(Source source)
    {
        var type = source.Type.ToLowerInvariant();
        if (type == "cosmosdb")
        {
            var endpoint = new Uri(Required(source.Endpoint), UriKind.Absolute);
            return JsonSerializer.Serialize(new[] { type,
                endpoint.GetLeftPart(UriPartial.Authority).ToLowerInvariant() + endpoint.AbsolutePath.TrimEnd('/'),
                Required(source.Database), Required(source.Container) });
        }
        if (type == "postgresql")
        {
            var connection = new NpgsqlConnectionStringBuilder(Required(source.ConnectionString));
            return JsonSerializer.Serialize(new[] { type,
                Required(connection.Host).Trim().TrimEnd('.').ToLowerInvariant(),
                connection.Port.ToString(System.Globalization.CultureInfo.InvariantCulture),
                Required(connection.Database), source.SchemaName ?? "public", Required(source.Table) });
        }
        if (type == "mssql")
        {
            var connection = new SqlConnectionStringBuilder(Required(source.ConnectionString));
            var host = Required(connection.DataSource).Trim().ToLowerInvariant();
            if (host.StartsWith("tcp:")) host = host[4..];
            var port = 1433;
            var separator = host.LastIndexOf(',');
            if (separator >= 0)
            {
                if (!int.TryParse(host[(separator + 1)..], out port))
                    throw new ArgumentException("Invalid inline target port");
                host = host[..separator];
            }
            return JsonSerializer.Serialize(new[] { type, host.Trim().TrimEnd('.'),
                port.ToString(System.Globalization.CultureInfo.InvariantCulture),
                Required(connection.InitialCatalog), source.SchemaName ?? "dbo", Required(source.Table) });
        }
        return JsonSerializer.Serialize(new[] { "source", source.Id });
    }
}
