# Generic Cosmos DB MCP server

This reusable Azure Functions app exposes read-only MCP tools over a
Streamable HTTP endpoint. The default mode uses a Function key for a shared
trust boundary. **Protected SharePoint mode** instead requires each caller's
Entra identity and checks their live SharePoint access before returning content.

- `list_allowed_containers` reports the deployment's configured allowlist.
- `list_documents` returns a bounded, explicit projection from an allowed
  container.
- `vector_search` embeds natural language and performs Cosmos DB vector search,
  returning only explicitly requested fields. It also accepts optional exact
  `source_id`, `pipeline_id`, and `source_ref` filters so one shared vector
  destination can be queried for a specific ingestion scope.

The Function App uses managed identity for both Cosmos DB and Azure OpenAI.
In default mode the MCP endpoint uses a Function key, which should be stored in a
Microsoft Foundry project connection rather than source code or agent prompts.
Container names and projected fields are validated before they are interpolated
into Cosmos SQL, filter names are fixed to the allowlist above, filter values
remain parameterized, and result counts are capped at 10.

## Shared-key deployment

This mode does **not** enforce individual SharePoint permissions. Use it only
when every client holding the key is authorized to read every exposed document.
The OmniVec portal/CLI cloud deployment builder currently creates this mode;
its synthetic SharePoint demo is not a permission-enforcement test.

Application settings:

```text
COSMOS_ENDPOINT
COSMOS_DATABASE
COSMOS_ALLOWED_CONTAINERS
COSMOS_DEFAULT_CONTAINER
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_DEPLOYMENT
AZURE_OPENAI_API_VERSION
```

Deploy the directory as an Azure Functions Python package, grant its managed
identity Cosmos DB Built-in Data Reader on the allowed containers and Cognitive
Services OpenAI User on the embedding account, then connect Foundry to:

```text
https://<function-app>.azurewebsites.net/api/mcp
```

Configure the project connection with `CustomKeys` authentication and the
Function key in the `x-functions-key` header.

## Protected SharePoint deployment

For the end-to-end trust boundaries, authorization diagrams, Cosmos provenance,
revocation behavior, and security sign-off requirements, see the
[SharePoint permission-enforcement review](../../docs/security/sharepoint-acl-enforcement.md).

Use a **separate Function App and dedicated SharePoint-only Cosmos containers**.
Only trusted, upgraded OmniVec SharePoint workers may write to those containers.
Provenance is writer-controlled authorization metadata, not a signature: an
arbitrary writer could forge it. Do not expose these containers through another
shared-key MCP deployment, generic search endpoint, or direct client Cosmos
access. OmniVec administrative search is a separate privileged interface, not
this per-user boundary.

In addition to the shared Cosmos/OpenAI settings, configure:

```text
COSMOS_SHAREPOINT_PROTECTED_CONTAINERS=sharepoint-vectors
MCP_ENTRA_TENANT_ID=<resource-tenant-guid>
MCP_ENTRA_CLIENT_ID=<mcp-api-app-client-guid>
MCP_ENTRA_CLIENT_SECRET=@Microsoft.KeyVault(SecretUri=https://<vault>.vault.azure.net/secrets/<obo-secret>/)
MCP_PUBLIC_URL=https://<function-app>.azurewebsites.net/api/mcp
COSMOS_SHAREPOINT_ALLOWED_FIELDS=id,title,content,source_ref
MCP_ALLOWED_CLIENT_IDS=<approved-client-app-guid>[,<another-client-app-guid>]
```

`MCP_ALLOWED_CLIENT_IDS` is optional but recommended: when set, only tokens whose
`azp` claim is a listed client application are accepted. Generic (non-protected)
queries never return rows that carry `_omnivec_provenance`, so SharePoint chunks
are served only through the permission-aware path.

`COSMOS_SHAREPOINT_PROTECTED_CONTAINERS` is a comma-separated subset of
`COSMOS_ALLOWED_CONTAINERS`. Omitting it leaves the original shared-key mode
enabled; a nonempty invalid configuration fails closed. Deploy code and settings
together before admitting traffic. Do not run the shared-key cloud builder
against this Function App: its settings, connection creation and readiness
checks do not configure delegated authentication.

The approved-field setting is optional; its default is
`id,title,body,content,source_ref`. Use `fields=id,title,content,source_ref` for
the standard OmniVec stored-text field, or explicitly approve your custom text
field. Never approve vector or internal metadata fields. A client cannot add a
field by changing its tool arguments.

### Entra and downstream Graph setup

1. Register a **single-tenant MCP API app** in the SharePoint resource tenant.
   Set `api.requestedAccessTokenVersion` to `2`, expose
   `api://<mcp-api-app-client-guid>/access_as_user`, and grant the API app
   **delegated** Microsoft Graph `Files.Read.All` with the required consent for
   user-accessible shared libraries. Narrower delegated permissions can be used
   only if they cover all item metadata and download operations in your library.
   Do not substitute Graph application permissions or the ingestion app's token.
2. Give that API app a client credential for OBO. Keep its secret in Key Vault,
   grant the Function identity access to that secret, and use an App Service
   Key Vault reference as above. Confirm the reference resolves; rotate it using
   the normal app-credential process. This credential authenticates the API
   during **user assertion exchange**, never an app-only file-read fallback.
3. Register a separate OAuth client, grant it the MCP API's delegated
   `access_as_user` permission, and configure consent. Direct clients request
   that scope and send their MCP-audience token in `Authorization: Bearer ...`.
   Graph-audience, app-only, expired, wrong-issuer and wrong-tenant tokens are
   rejected. Pre-register clients; this app does not provide dynamic client
   registration.

The Function host binding for `/api/mcp` is anonymous in protected mode because
the application itself validates Entra bearer tokens **before all MCP messages**,
including discovery. A Function key does not bypass this validation.
The native Functions MCP extension entrypoints cannot carry this validated
caller context and therefore refuse protected tool calls. Use `/api/mcp`.
All containers in a protected deployment require an authenticated caller;
only the configured protected containers get the SharePoint checks.

The `WWW-Authenticate` challenge points clients to
`/api/.well-known/oauth-protected-resource` (relative to the endpoint's host).
Conditional-access/consent challenges return HTTP 401; clients must obtain a
fresh user token or complete consent, not retry with a service identity.
Invalid configuration/signing-key availability returns HTTP 503. A Graph/OBO
outage during a tool call is an explicit MCP tool error, not an empty search
result.

### Retrieval contract and limits

The watcher verifies the resource tenant; the worker writes
`_omnivec_provenance` and `_omnivec_sync` on every chunk and revision manifest.
See the [SharePoint protocol and migration guide](../../docs/sharepoint-source.md).
Existing rows without this metadata are hidden until reindexed.

Both `list_documents` and `vector_search` first select at most **50 private
candidates**, without projecting their text. For each distinct file/version
they require a completed current Cosmos revision, exchange the caller's token
via OBO, check Graph metadata and the exact indexed eTag, make a fresh delegated
`/content` request, and recheck the metadata. Metadata visibility alone is not
enough. Only then are chunks read, checked against their candidate and current
manifest, and projected into tool results.

The content check does not read the file body or follow, retain, or log Graph's
preauthenticated download URL. Decisions are reused only within one tool
request, not between users or requests. Graph work has a 20-second budget and
at most three attempts per request. The result limit remains 10; denials can
produce fewer matches, without scanning indefinitely for replacements.
Missing provenance, deleted/inaccessible files, incomplete/replaced revisions,
tenant mismatches and changed eTags never release cached text or citations.
After a source edit, results can disappear until reingestion finishes.

This first version supports **same-tenant, ordinary downloadable SharePoint
files** in the public Microsoft cloud. Cross-tenant ingestion remains supported,
but an MCP caller cannot retrieve rows belonging to another resource tenant.
This is not a Purview/IRM decryption, no-download-policy, or sensitivity-label
enforcement engine; do not use it for content requiring those additional
controls. Authorization is a point-in-time check, not a transactional lock on
SharePoint permissions. Previously disclosed text and downstream conversation
history cannot be retroactively revoked. Never share conversations or cache
tool results across users.

## Foundry per-user OAuth client

For protected retrieval choose **OAuth identity passthrough / custom OAuth**
in the Foundry MCP connection, not `CustomKeys`, project managed identity, or
agent identity. Use the same HTTPS `/api/mcp` URL and configure:

| Connection field | Value |
| --- | --- |
| Client ID / secret | The separate OAuth client's credentials, stored in the connection |
| Authorization URL | `https://login.microsoftonline.com/<tenant-guid>/oauth2/v2.0/authorize` |
| Token and refresh URL | `https://login.microsoftonline.com/<tenant-guid>/oauth2/v2.0/token` |
| Scopes | `api://<mcp-api-app-client-guid>/access_as_user offline_access` |

Add the **redirect URL returned by Foundry** to that OAuth client's web redirect
URIs. The incoming scope is for this MCP API, not Microsoft Graph; OBO obtains
the downstream Graph token. The Foundry project, signed-in user, MCP API and
SharePoint resource must be in the same tenant for this supported configuration.
Consumers need the Foundry Agent Consumer role (or another suitable project
role); users creating agent versions need the corresponding authoring rights.
Foundry does not currently support cross-tenant OAuth token exchange.

Use `scripts/foundry_cosmos_mcp_agent.py` with either a shared-key or per-user
OAuth connection. Install its dependencies in a separate
virtual environment from the Function App because the Foundry SDK and deployed
Function App intentionally have independent dependency sets:

```powershell
python -m pip install -r scripts\requirements-foundry-cosmos-mcp.txt
python scripts\foundry_cosmos_mcp_agent.py `
  --project-endpoint "<foundry-project-endpoint>" --tenant-id "<tenant-guid>" `
  --connection "<per-user-oauth-connection>" `
  --server-url "https://<function-app>.azurewebsites.net/api/mcp" `
  --model "<chat-deployment>" --container "sharepoint-vectors" `
  --fields "id,title,content,source_ref" --agent-name "sharepoint-policy-agent" `
  --question "What is the meal allowance?"
```

Authenticate `azd` as the **interacting user** in that tenant, not as the
ingestion/deployment service. If the output status is `requires_consent`, open
each returned `consent_links` URL as that same user. Treat those links as
user-specific sign-in material; do not share or log them. Then resume using the
printed IDs and the original question:

```powershell
python scripts\foundry_cosmos_mcp_agent.py `
  --project-endpoint "<foundry-project-endpoint>" --tenant-id "<tenant-guid>" `
  --resume-response-id "<response-id>" --agent-name "sharepoint-policy-agent" `
  --agent-version "<printed-version>" --question "What is the meal allowance?"
```

Resume reuses the saved agent version and response rather than creating another
agent or conversation. Consent may be requested again; an incomplete/empty
answer without consent is a failure, not a successful demonstration. Inspect
actual tool results and source references before claiming end-to-end success.
Requests disable automatic inference retries.

For rollout, verify two real test users with different library access, removal
of access between fresh requests, stale/deleted files, and conditional-access
challenges. Local doubles cannot validate your tenant consent, Graph policies,
Foundry connection, or Cosmos consistency settings.

References: [Foundry MCP authentication](https://learn.microsoft.com/azure/foundry/agents/how-to/mcp-authentication),
[Entra OBO](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow),
[Graph file download](https://learn.microsoft.com/graph/api/driveitem-get-content?view=graph-rest-1.0).
