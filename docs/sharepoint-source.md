# SharePoint Online source

OmniVec can ingest files from a SharePoint Online document library by polling
Microsoft Graph delta changes. The watcher publishes stable site, drive, and
item identifiers to Service Bus; the worker downloads each file with workload
identity and sends the bytes to DocGrok for extraction, chunking, and embedding.

## Permissions

Grant the OmniVec workload identity a Microsoft Graph application permission:

- `Sites.Selected` (recommended), followed by a read grant on each allowed site.
- `Files.Read.All` for tenant-wide document-library access.

Admin consent is required. No SharePoint credential or download URL is stored in
source configuration or placed on Service Bus.

## Source configuration

```json
{
  "name": "Corporate policies",
  "type": "sharepoint",
  "config": {
    "site_id": "contoso.sharepoint.com,site-guid,web-guid",
    "drive_id": "drive-guid",
    "folder_path": "Policies",
    "file_types": ["pdf", "docx", "txt", "md", "html"],
    "poll_interval_seconds": 60,
    "max_file_size_bytes": 52428800,
    "auth_type": "managed-identity"
  }
}
```

Use Microsoft Graph Explorer or the Graph API to resolve the site and drive IDs:

```text
GET /v1.0/sites/{hostname}:/sites/{site-path}
GET /v1.0/sites/{site-id}/drives
```

Enable the dedicated watcher in Helm:

```yaml
sharepointWatcher:
  enabled: true
```

The default maximum file size is 50 MiB, matching the DocGrok router request
limit. Initial startup enumerates existing files; subsequent polls use the Graph
delta link to detect additions, updates, and deletions.
